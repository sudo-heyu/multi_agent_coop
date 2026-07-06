"""L5 Semantic Memory: LLM-first synthesis grounded by evaluated episodes.

Episodic memory 存的是一个个具体案例；语义记忆把**多个带真实效果反馈的案例**
归纳成带证据引用和置信度的规律，回答"在这种拓扑/场景下，采用某策略倾向产生
什么效果、典型做法是什么"。规律比单案例更抽象、更可靠（有统计支撑）。

关键红线：只用经过 L4 评估、且结论有定论（improved/degraded/neutral）的案例归纳。
没有真实反馈的案例（evaluation 缺失或 inconclusive）一律排除——否则归纳出的是
"跑完了的方案"而非"有效的方案"。统计模块生成不可篡改的证据边界，LLM 默认负责
核心语义总结；模型不可用时降级为统计摘要。
"""

from __future__ import annotations

import statistics
import json
import hashlib
from datetime import datetime, timezone
from collections import Counter
from typing import Any

from src.persistence import EventStore

from .episodic import encode_features, feature_similarity


CONCLUSIVE_VERDICTS = ("improved", "neutral", "degraded")
MIN_SUPPORT = 2
# 达到该证据数即视为置信充分；不足按比例折减。本系统案例稀缺，3 个方向一致的
# 案例已是可用信号，故取 3——consistency 与 support 仍双重把关，阈值再挡弱规律。
FULL_SUPPORT = 3.0
MIN_APPLICABILITY_SIMILARITY = 0.55


def induce_rules(
    store: EventStore, *, min_support: int = MIN_SUPPORT, use_llm: bool = True
) -> list[dict[str, Any]]:
    """从所有已评估案例归纳规律并 upsert 入库；返回本次归纳出的规律。

    分组键为 (拓扑签名, 场景, 策略)——同拓扑同场景同策略的案例才可比。每组
    support（有定论案例数）达到 min_support 才成为规律，避免单例噪声。
    """
    episodes = store.list_episodes(limit=100_000)
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for episode in episodes:
        verdict = (episode.get("evaluation") or {}).get("final_verdict")
        if verdict not in CONCLUSIVE_VERDICTS:
            continue
        key = (episode["topology_signature"], episode.get("scene"), episode.get("strategy"))
        groups.setdefault(key, []).append(episode)

    rules = []
    for (topology, scene, strategy), members in groups.items():
        if len(members) < min_support:
            continue
        rule = _build_rule(topology, scene, strategy, members)
        if use_llm:
            rule.update(_llm_rule_summary(rule, members))
        rule["rule_id"] = store.upsert_rule(rule)
        rules.append(rule)
    rules.sort(key=lambda item: (item["confidence"], item["support"]), reverse=True)
    store.activate_only_rules([item["rule_id"] for item in rules])
    return rules


def find_matching_rules(
    store: EventStore,
    state: dict[str, Any],
    *,
    scene: str | None = None,
    min_confidence: float = 0.5,
    limit: int = 3,
    actionable_min_support: int = 5,
) -> list[dict[str, Any]]:
    """检索匹配当前状态拓扑（可选场景）的高置信规律。"""
    topology, query = encode_features(state)
    rules = store.list_rules(
        topology_signature=topology, scene=scene, min_confidence=min_confidence
    )
    applicable = []
    for rule in rules:
        # Small-sample and neutral rules remain auditable but are not actionable hints.
        if (int(rule.get("support") or 0) < actionable_min_support
                or rule.get("dominant_verdict") == "neutral"):
            continue
        evidence_features = [
            item.get("features") for item in (rule.get("evidence") or [])
            if isinstance(item.get("features"), dict)
        ]
        # v9 and older rules have no feature envelope; retain compatibility until re-induced.
        if evidence_features:
            similarity = max(feature_similarity(query, item)[0] for item in evidence_features)
            if similarity < MIN_APPLICABILITY_SIMILARITY:
                continue
            rule = {**rule, "applicability_similarity": similarity}
        applicable.append(rule)
    return applicable[: max(1, int(limit))]


def _build_rule(
    topology: str, scene: str | None, strategy: str | None, members: list[dict[str, Any]]
) -> dict[str, Any]:
    counts = Counter(m["evaluation"]["final_verdict"] for m in members)
    support = len(members)
    # 平票不把风险粉饰成改善：degraded > neutral > improved。
    dominant = max(
        CONCLUSIVE_VERDICTS,
        key=lambda verdict: (counts.get(verdict, 0), CONCLUSIVE_VERDICTS.index(verdict)),
    )
    consistency = counts.get(dominant, 0) / support
    confidence = round(consistency * min(1.0, support / FULL_SUPPORT), 4)
    dominant_members = [
        m for m in members if m["evaluation"]["final_verdict"] == dominant
    ]
    evidence = [
        {
            "run_id": m["run_id"],
            "verdict": m["evaluation"]["final_verdict"],
            "confidence": m["evaluation"].get("final_confidence"),
            "features": m.get("features") or {},
        }
        for m in members
    ]
    return {
        "topology_signature": topology,
        "scene": scene,
        "strategy": strategy,
        "dominant_verdict": dominant,
        "support": support,
        "consistency": round(consistency, 4),
        "confidence": confidence,
        "verdict_counts": dict(counts),
        "action_summary": _action_summary(dominant_members),
        "evidence": evidence,
    }


def _action_summary(members: list[dict[str, Any]]) -> dict[str, Any]:
    """主导 verdict 案例的典型决策模式：逐 AP 逐参数取中位数（离散参数四舍五入）。"""
    collected: dict[str, dict[str, list[float]]] = {}
    for member in members:
        decision = member.get("decision") or {}
        if not isinstance(decision, dict):
            continue
        for ap, params in decision.items():
            if not isinstance(params, dict):
                continue
            ap_key = str(ap).lower()
            for field, value in params.items():
                num = _num(value)
                if num is None:
                    continue
                collected.setdefault(ap_key, {}).setdefault(str(field), []).append(num)
    summary: dict[str, dict[str, float]] = {}
    for ap, fields in sorted(collected.items()):
        summary[ap] = {}
        for field, values in sorted(fields.items()):
            median = statistics.median(values)
            # tx_power 保留一位小数，CW/AIFSN 等取整。
            summary[ap][field] = round(median, 1) if "power" in field.lower() else round(median)
    return summary


def format_rule(rule: dict[str, Any]) -> str:
    """把一条规律渲染为提案提示用的一行中文摘要。"""
    verdict_map = {"improved": "改善", "neutral": "无明显变化", "degraded": "恶化"}
    counts = rule["verdict_counts"]
    dist = "/".join(
        f"{verdict_map.get(v, v)}{counts[v]}" for v in CONCLUSIVE_VERDICTS if counts.get(v)
    )
    action = "，".join(
        f"{ap}:{params}" for ap, params in (rule.get("action_summary") or {}).items()
    )
    evidence_stats = (
        f"策略={rule.get('strategy')}，{rule['support']} 个带真实反馈案例中"
        f"倾向{verdict_map.get(rule['dominant_verdict'], rule['dominant_verdict'])}"
        f"（分布 {dist}，一致性={rule['consistency']}，置信度={rule['confidence']}），"
        f"典型做法：{action or '（无参数模式）'}"
    )
    narrative = str(rule.get("llm_summary") or "").strip()
    if narrative:
        return f"语义总结：{narrative}；统计证据：{evidence_stats}"
    return f"语义总结暂不可用，降级使用统计证据：{evidence_stats}"


def _llm_rule_summary(rule: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    """LLM synthesizes the primary semantic memory inside deterministic evidence bounds."""
    from .llm_backend import enabled, model_name, summarize
    evidence = [{
        "run_id": m.get("run_id"),
        "verdict": (m.get("evaluation") or {}).get("final_verdict"),
        "confidence": (m.get("evaluation") or {}).get("final_confidence"),
        "decision": m.get("decision"),
        "metrics": m.get("metrics"),
    } for m in members]
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence_hash = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    base = {"llm_evidence_hash": evidence_hash, "llm_prompt_version": 1}
    if not enabled():
        return {**base, "llm_summary": None, "llm_status": "disabled"}
    prompt = (
        "请把以下同类网络协商案例总结成不超过180字的经验规律。必须说明适用条件、"
        "主要动作、效果方向和不确定性；证据冲突时明确写出，禁止给出证据外参数。\n"
        + json.dumps({"statistics": rule, "cases": evidence}, ensure_ascii=False)[:16000]
    )
    try:
        return {**base, "llm_summary": summarize(prompt),
                "llm_model": model_name(),
                "llm_status": "ready",
                "llm_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except Exception as exc:
        return {**base, "llm_summary": None, "llm_status": "failed",
                "llm_error": str(exc)[:500]}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

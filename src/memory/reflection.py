"""反思模块：记忆信任模型（确定性纯函数）。

trust = 基础置信度 × 时效衰减 × 矛盾惩罚

- 基础置信度：案例取执行后评估的 final_confidence（未评估时退化为 quality_score），
  规律取归纳 confidence；
- 时效衰减：距最近一次被真实反馈证实（last_verified_at，缺省回退 created_at）
  的时间按半衰期指数衰减——无线环境非平稳，越久未验证越不可信；
- 矛盾惩罚：矛盾账本每记一笔，信任乘性下降。

全部计算同输入必同输出，不调用 LLM；阈值可经环境变量部署级校准。
隔离判定只降权不删除（红线 9），实际隔离动作由数据层 set_memory_quarantined 执行。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# 半衰期：最近验证起 7 天信任减半（mock 校准用 MULTIAP_TRUST_HALF_LIFE_SECONDS 缩短）。
TRUST_HALF_LIFE_SECONDS = _env_float("MULTIAP_TRUST_HALF_LIFE_SECONDS", 7 * 86400.0)
# 每笔矛盾把信任乘以该系数：1 笔 → 0.5，2 笔 → 0.25。
CONTRADICTION_FACTOR = _env_float("MULTIAP_CONTRADICTION_FACTOR", 0.5)
# 信任低于该阈值 → 建议隔离（停止注入，等待再验证）。
QUARANTINE_TRUST_THRESHOLD = _env_float("MULTIAP_QUARANTINE_TRUST", 0.15)
# 矛盾累计达到该笔数 → 无条件建议隔离。
QUARANTINE_CONTRADICTIONS = int(_env_float("MULTIAP_QUARANTINE_CONTRADICTIONS", 3))
# 时效衰减下限：老记忆保留微弱权重用于排序，不会因纯粹变老直接归零。
FRESHNESS_FLOOR = _env_float("MULTIAP_FRESHNESS_FLOOR", 0.05)


def freshness(age_seconds: float, *, half_life: float = TRUST_HALF_LIFE_SECONDS) -> float:
    """按半衰期计算时效系数，落在 [FRESHNESS_FLOOR, 1]。"""
    if age_seconds <= 0:
        return 1.0
    if half_life <= 0:
        return 1.0
    decay = 0.5 ** (age_seconds / half_life)
    return round(max(FRESHNESS_FLOOR, decay), 4)


def contradiction_penalty(count: int, *, factor: float = CONTRADICTION_FACTOR) -> float:
    """矛盾惩罚系数：无矛盾为 1，逐笔乘 factor。"""
    if count <= 0:
        return 1.0
    return round(factor ** count, 4)


def trust_score(
    base_confidence: float, *, age_seconds: float, contradictions: int,
) -> float:
    """信任分主公式，截断到 [0, 1]，四位小数保证可复算比对。"""
    base = min(1.0, max(0.0, float(base_confidence)))
    value = base * freshness(age_seconds) * contradiction_penalty(contradictions)
    return round(min(1.0, max(0.0, value)), 4)


def base_confidence(memory: dict[str, Any]) -> float:
    """从案例/规律记录里取基础置信度。

    案例：优先执行后评估 final_confidence；未评估退化为 quality_score
    （维持"验证后召回"原则下的排序行为，不额外奖励未验证案例）。
    规律：归纳 confidence。
    """
    evaluation = memory.get("evaluation")
    if isinstance(evaluation, dict) and evaluation.get("final_confidence") is not None:
        return min(1.0, max(0.0, float(evaluation["final_confidence"])))
    if memory.get("confidence") is not None:
        return min(1.0, max(0.0, float(memory["confidence"])))
    return min(1.0, max(0.0, float(memory.get("quality_score") or 0.0)))


def memory_age_seconds(memory: dict[str, Any], *, now: datetime | None = None) -> float:
    """距最近验证的秒数：last_verified_at 缺省回退 created_at；无法解析视为 0。"""
    anchor = memory.get("last_verified_at") or memory.get("created_at")
    if not anchor:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(anchor))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - parsed).total_seconds())


def memory_trust(memory: dict[str, Any], *, now: datetime | None = None) -> float:
    """对一条 store 记录（案例 / agent 本地案例 / 规律）计算信任分。"""
    return trust_score(
        base_confidence(memory),
        age_seconds=memory_age_seconds(memory, now=now),
        contradictions=int(memory.get("contradictions") or 0),
    )


def should_quarantine(trust: float, contradictions: int) -> bool:
    """隔离建议：信任跌破阈值，或矛盾笔数到达上限。"""
    if contradictions >= QUARANTINE_CONTRADICTIONS:
        return True
    return trust < QUARANTINE_TRUST_THRESHOLD


def reconcile_verdicts(predicted: str | None, observed: str | None) -> str:
    """比对注入时预测与实际 verdict：verified / contradicted / inconclusive。

    只有方向相反（improved↔degraded）才算证伪；完全一致才算证实；
    其余（含 neutral 交叉、数据不足）一律 inconclusive，不奖不罚。
    """
    conclusive = {"improved", "neutral", "degraded"}
    if predicted not in conclusive or observed not in conclusive:
        return "inconclusive"
    if predicted == observed:
        return "verified"
    opposite = {"improved": "degraded", "degraded": "improved"}
    if opposite.get(predicted) == observed:
        return "contradicted"
    return "inconclusive"


def reconcile_memory_reliance(
    store: Any, run_id: str, final_verdict: str | None,
) -> dict[str, Any]:
    """R4 闭环：效果评估定论后，对本次 run 依赖过的记忆逐条记账。

    - 取最后一条 memory_reliance 事件（被采纳提案的依赖清单）；
    - warning 角色不参与归因（本次动作意在规避它，效果不能归它）；
    - 证伪 → 矛盾账本 +1，并按当前信任/矛盾数判定是否自动隔离；
    - 证实 → 刷新 last_verified_at（时效衰减锚点前移）；
    - 全程幂等：reconciliation 表 UNIQUE 约束保证重复收割无重复副作用。
    """
    outcome = {"processed": 0, "verified": 0, "contradicted": 0, "quarantined": []}
    if not enabled() or final_verdict not in {"improved", "neutral", "degraded"}:
        return outcome
    reliance_events = [
        event for event in store.load_events(run_id)
        if event.get("event") == "memory_reliance"
    ]
    if not reliance_events:
        return outcome
    for entry in reliance_events[-1].get("entries") or []:
        kind, key = entry.get("memory_kind"), entry.get("memory_key")
        if not kind or not key or entry.get("role") == "warning":
            continue
        result = reconcile_verdicts(entry.get("predicted"), final_verdict)
        first_time = store.record_reconciliation(
            memory_kind=kind, memory_key=key, run_id=run_id,
            predicted=entry.get("predicted"), observed=final_verdict,
            result=result, trust_at_injection=entry.get("trust"),
        )
        outcome["processed"] += 1
        if not first_time:
            continue
        if result == "contradicted":
            store.record_contradiction(
                memory_kind=kind, memory_key=key, run_id=run_id,
                expected=str(entry.get("predicted")), observed=final_verdict,
                detail={"trust_at_injection": entry.get("trust")},
            )
            outcome["contradicted"] += 1
            record = store.get_memory_record(kind, key)
            if record is not None:
                trust = memory_trust(record)
                if should_quarantine(trust, int(record.get("contradictions") or 0)):
                    store.set_memory_quarantined(kind, key, True)
                    outcome["quarantined"].append({"memory_kind": kind, "memory_key": key})
        elif result == "verified":
            store.mark_memory_verified(kind, key)
            outcome["verified"] += 1
    return outcome


def calibration_report(store: Any) -> dict[str, Any]:
    """R5 校准：按注入时信任分分桶统计事后证实率（hit rate）。

    反思模块实用性的量化验收：高信任桶的证实率应显著高于低信任桶；
    否则说明信任模型参数需要校准。

    reconciliation 表现在有两类来源，用 memory_kind 区分：
    - episode/rule/agent_episode：复用的历史记忆，注入时有信任分，走 trust 分桶；
    - decision_prediction：这次协商当场做的新决策，没有"信任分"这个概念
      （不是可隔离的记忆对象），trust_at_injection 恒为 None，只落 unknown_trust
      桶；额外按 memory_kind 补一份 by_kind 明细，避免两类数据在同一个桶里
      混在一起看不出各自的命中率（见 reconcile_decision_predictions）。
    """
    buckets = {
        "low(<0.4)": {"range": (0.0, 0.4), "verified": 0, "contradicted": 0},
        "mid(0.4-0.7)": {"range": (0.4, 0.7), "verified": 0, "contradicted": 0},
        "high(>=0.7)": {"range": (0.7, 1.01), "verified": 0, "contradicted": 0},
    }
    unknown = {"verified": 0, "contradicted": 0}
    by_kind: dict[str, dict[str, int]] = {}
    for item in store.list_reconciliations(limit=100_000):
        result = item["result"]
        if result not in {"verified", "contradicted"}:
            continue
        kind_bucket = by_kind.setdefault(
            item.get("memory_kind") or "unknown", {"verified": 0, "contradicted": 0}
        )
        kind_bucket[result] += 1
        trust = item.get("trust_at_injection")
        if trust is None:
            unknown[result] += 1
            continue
        for bucket in buckets.values():
            low, high = bucket["range"]
            if low <= float(trust) < high:
                bucket[result] += 1
                break
    report = {}
    for name, bucket in buckets.items():
        conclusive = bucket["verified"] + bucket["contradicted"]
        report[name] = {
            "verified": bucket["verified"], "contradicted": bucket["contradicted"],
            "hit_rate": round(bucket["verified"] / conclusive, 4) if conclusive else None,
        }
    report["unknown_trust"] = unknown
    for kind, counts in by_kind.items():
        conclusive = counts["verified"] + counts["contradicted"]
        counts["hit_rate"] = round(counts["verified"] / conclusive, 4) if conclusive else None
    report["by_kind"] = by_kind
    return report


# ── 决策自预测（不依赖复用记忆，任何协商都能核账）─────────────────────────
# R4 只核对"复用的历史记忆"预测是否成立；一次协商即便没有依赖任何历史记忆
# （比如这次事故：全新的 Co-EDCA 参数组合），也应该有一条预测-实测的核账
# 记录进入 R5 校准表，否则反思模块对"当场新决策"完全失明，只能靠迭代模块
# 的下一次 attempt 才有机会发现问题（还得挂了 goal 才会跑）。这里复用
# Validator 自伤门（层 3）已经算过的闭式估算（EDCA 抢占份额 / Co-SR 功率
# delta）反推方向性预测，不新增计算路径。
DECISION_PREDICTION_KIND = "decision_prediction"
# Co-EDCA：预测抢占份额相对协商前的比值门槛。
DECISION_EDCA_IMPROVE_SHARE_RATIO = 1.1
DECISION_EDCA_DEGRADE_SHARE_RATIO = 0.9
# Co-SR：己方功率调整量门槛（dB）。
DECISION_SR_MARGIN_DELTA_DB = 1.0


def predict_decision_verdicts(
    ap_state: dict[str, Any], decision: dict[str, Any], strategy: str | None,
) -> dict[str, str]:
    """从这次决策本身反推方向性预测（improved/neutral/degraded）。

    只对决策里确实改动了参数的 AP 出预测；未被动的 AP 不猜。
    Key 形如 "ap1:BE"（Co-EDCA 按 AC）或 "ap1:co_sr"（Co-SR 按功率）。
    """
    if not isinstance(ap_state, dict) or not isinstance(decision, dict) or not strategy:
        return {}
    from ..tools import edca as _edca

    predictions: dict[str, str] = {}
    if strategy == "co_edca":
        for ac in ("BE", "VI"):
            shares = _edca.predict_access_share(ap_state, decision, ac=ac)
            for ap_id, item in shares.items():
                params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
                group = (
                    _edca.extract_param_groups(params).get(ac)
                    if isinstance(params, dict) else None
                )
                ratio = item.get("share_ratio")
                if not group or ratio is None or not _edca_group_changed(ap_state[ap_id], ac, group):
                    continue
                if ratio >= DECISION_EDCA_IMPROVE_SHARE_RATIO:
                    verdict = "improved"
                elif ratio <= DECISION_EDCA_DEGRADE_SHARE_RATIO:
                    verdict = "degraded"
                else:
                    verdict = "neutral"
                predictions[f"{ap_id}:{ac}"] = verdict
    if strategy == "co_sr":
        for ap_id, state in ap_state.items():
            if not isinstance(state, dict):
                continue
            key = ap_id.lower()
            params = decision.get(key) or decision.get(ap_id) or {}
            if not isinstance(params, dict) or params.get("tx_power_dbm") is None:
                continue
            try:
                current = float(state.get("tx_power_dbm", 20.0))
                delta = float(params["tx_power_dbm"]) - current
            except (TypeError, ValueError):
                continue
            if abs(delta) < 1e-9:
                continue  # 提案值跟当前值一致：这个 AP 没被这次决策实际改动，不猜
            if delta >= DECISION_SR_MARGIN_DELTA_DB:
                verdict = "improved"
            elif delta <= -DECISION_SR_MARGIN_DELTA_DB:
                verdict = "degraded"
            else:
                verdict = "neutral"
            predictions[f"{key}:co_sr"] = verdict
    return predictions


def _edca_group_changed(state: dict[str, Any], ac: str, group: dict[str, Any]) -> bool:
    """提案的 CWmin/AIFSN 跟当前状态相比是否真的变了（而不只是显式重申现值）。"""
    from ..tools.edca import state_cw_aifsn
    cur_cwmin, cur_aifsn = state_cw_aifsn(state, ac)
    proposed_cwmin, proposed_aifsn = group.get("CWmin"), group.get("AIFSN")
    if proposed_cwmin is not None and int(proposed_cwmin) != cur_cwmin:
        return True
    if proposed_aifsn is not None and int(proposed_aifsn) != cur_aifsn:
        return True
    return False


def _score_to_verdict(score: float) -> str:
    from .outcome import DEGRADE_THRESHOLD, IMPROVE_THRESHOLD
    if score >= IMPROVE_THRESHOLD:
        return "improved"
    if score <= DEGRADE_THRESHOLD:
        return "degraded"
    return "neutral"


def reconcile_decision_predictions(
    store: Any, run_id: str, episode: dict[str, Any], final_deltas: dict[str, Any] | None,
) -> dict[str, Any]:
    """把"这次决策本身"的预测跟最终评估窗口的 per-AP 实测得分核账。

    跟 reconcile_memory_reliance 的关系：那个函数核对的是复用的历史记忆是否
    仍可信；这个函数核对的是这次协商当场做出的新决策，不管有没有依赖任何
    历史记忆、有没有挂 goal，只要评估窗口收割了就跑。只写 reconciliation
    表（R5 校准证据），不写矛盾账本——预测不是"可隔离的记忆"，没有隔离的
    对象，写矛盾账本也无处生效（红线 9：反思只降权不删除，删权对象必须是
    记忆记录）。
    """
    outcome = {"processed": 0, "verified": 0, "contradicted": 0}
    if not enabled():
        return outcome
    ap_state = episode.get("initial_state")
    decision = episode.get("decision")
    strategy = episode.get("strategy")
    per_ap_deltas = (final_deltas or {}).get("per_ap") or {}
    if not per_ap_deltas:
        return outcome
    predictions = predict_decision_verdicts(ap_state, decision, strategy)
    for key, predicted in predictions.items():
        ap_id = key.split(":", 1)[0]
        ap_delta = per_ap_deltas.get(ap_id)
        if ap_delta is None:
            continue
        observed = _score_to_verdict(float(ap_delta.get("score") or 0.0))
        result = reconcile_verdicts(predicted, observed)
        first_time = store.record_reconciliation(
            memory_kind=DECISION_PREDICTION_KIND, memory_key=key, run_id=run_id,
            predicted=predicted, observed=observed, result=result,
            trust_at_injection=None,
        )
        outcome["processed"] += 1
        if first_time:
            if result == "verified":
                outcome["verified"] += 1
            elif result == "contradicted":
                outcome["contradicted"] += 1
    return outcome


def enabled() -> bool:
    """反思门控总开关：MULTIAP_REFLECTION=0 时召回行为回退到 v15 版本。"""
    return os.environ.get("MULTIAP_REFLECTION", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def gate_memories(
    memories: list[dict[str, Any]], *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """召回门控：剔除隔离区记忆和信任跌破隔离阈值的记忆，并附加信任分。

    纯内存计算，不触发额外 SQL / LLM；排序仍由召回方的既有打分决定，
    信任分附加在 "trust" 字段供假设化注入展示。反思关闭时原样返回。
    """
    if not enabled():
        return memories
    reference = now or datetime.now(timezone.utc)
    gated = []
    for memory in memories:
        if memory.get("quarantined"):
            continue
        trust = memory_trust(memory, now=reference)
        if trust < QUARANTINE_TRUST_THRESHOLD:
            continue
        gated.append({**memory, "trust": trust})
    return gated

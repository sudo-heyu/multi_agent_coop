"""Deterministic post-execution outcome evaluation over multiple time windows.

决策执行后按多个时间窗口采集真实状态，与执行前基线比较，
判定 improved / degraded / neutral / inconclusive，并把结论回写
episodic memory：劣化案例质量封顶，不会再被当作高质量参考注入提案。
回滚只产出建议和参数计划，不自动执行。
"""

from __future__ import annotations

import math
import os
import statistics
import uuid
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from src.persistence import EventStore

from .episodic import pipeline_quality


def _env_float(name: str, default: float) -> float:
    """让评估阈值可经环境变量部署级覆盖，便于按真实数据校准；默认值不变。"""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# 每个 AP 的评估权重按业务优先级取值：协商目标就是高优先级收益最大化，
# 低优先级"让出信道"造成的小幅退化不应把整体判成 degraded。
PRIORITY_WEIGHT = {"low": 0.3, "medium": 0.6, "high": 1.0}

# (指标, 方向, 归一尺度)。throughput/latency 用相对变化；
# packet_loss 基线常接近 0，相对值会爆炸，用绝对百分点/5.0 归一。
_METRICS: tuple[tuple[str, str, float | None], ...] = (
    ("throughput_mbps_iperf", "higher", None),
    ("throughput_mbps_user", "higher", None),
    ("latency_ms", "lower", None),
    ("packet_loss_pct", "lower", 5.0),
)

# 分类阈值可经环境变量覆盖（部署级校准），默认值经 mock/真实运行验证。
IMPROVE_THRESHOLD = _env_float("MULTIAP_IMPROVE_THRESHOLD", 0.05)
DEGRADE_THRESHOLD = _env_float("MULTIAP_DEGRADE_THRESHOLD", -0.05)
MIN_COVERAGE = _env_float("MULTIAP_MIN_COVERAGE", 0.5)
ROLLBACK_CONFIDENCE = _env_float("MULTIAP_ROLLBACK_CONFIDENCE", 0.5)
# 聚合得分达到 ±0.15 视为满置信变化
_FULL_CONFIDENCE_SCORE = _env_float("MULTIAP_FULL_CONFIDENCE_SCORE", 0.15)

# pending 窗口放弃门限：due_at 之后再等 max(窗口时长×4, 1 小时) 仍收不到有效
# 状态，就标 abandoned，避免 state server 长期离线时窗口无限 pending。
ABANDON_GRACE_MULTIPLIER = 4.0
MIN_ABANDON_GRACE_SECONDS = 3600.0

DEFAULT_WINDOWS: dict[str, tuple[float, ...]] = {
    "mock": (10.0, 30.0),
    "real": (60.0, 300.0, 900.0),
}


def parse_windows(spec: str) -> tuple[float, ...]:
    """解析 "60,300,900" 形式的窗口定义，去重升序。"""
    values = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        seconds = float(part)
        if seconds <= 0:
            raise ValueError(f"evaluation window must be positive: {part}")
        values.add(seconds)
    if not values:
        raise ValueError(f"no evaluation windows in spec: {spec!r}")
    return tuple(sorted(values))


def window_label(seconds: float) -> str:
    return f"t+{seconds:g}s"


def schedule_outcome_evaluations(
    store: EventStore,
    run_id: str,
    baseline_state: dict[str, Any],
    windows: tuple[float, ...],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """为一次已生效的决策登记多窗口评估，按 (run, window) 幂等。

    基线优先取 run 的 initial 快照：它是未经 agent 字段白名单过滤的全量遥测
    （含 iperf 吞吐/延迟/丢包），传入的 baseline_state 只作缺失时的兜底。
    """
    now = now or datetime.now(timezone.utc)
    initial = next(
        (
            snap["state"] for snap in store.iter_snapshots(run_id)
            if snap["label"] == "initial"
        ),
        None,
    )
    baseline_state = initial or baseline_state
    scheduled = []
    for seconds in sorted(windows):
        due_at = (now + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
        record, _ = store.schedule_evaluation(
            run_id,
            window_label=window_label(seconds),
            window_seconds=seconds,
            due_at=due_at,
            baseline=baseline_state,
        )
        scheduled.append(record)
    return scheduled


def abandon_stale_evaluations(
    store: EventStore,
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """把逾期太久仍无法收割的 pending 窗口标 abandoned（不需要读状态）。

    收割依赖 state server 在线；若其长期离线，窗口会永远 pending。此函数按
    due_at + grace 判断放弃，让卡住的窗口有明确终态，不再阻塞 summary/告警。
    """
    now = now or datetime.now(timezone.utc)
    pending = store.list_evaluations(run_id, status="pending")
    abandoned = []
    for evaluation in pending:
        grace = max(
            float(evaluation["window_seconds"]) * ABANDON_GRACE_MULTIPLIER,
            MIN_ABANDON_GRACE_SECONDS,
        )
        deadline = _parse_ts(evaluation["due_at"]) + timedelta(seconds=grace)
        if now < deadline:
            continue
        abandoned.append(
            store.finish_evaluation(
                evaluation["evaluation_id"],
                status="abandoned",
                error=(
                    f"逾期未收割：due_at={evaluation['due_at']} 后超过 grace={grace:g}s "
                    "仍无法采集观测状态（state server 长期离线？）"
                ),
            )
        )
    for touched_run in sorted({item["run_id"] for item in abandoned}):
        apply_evaluation_to_episode(store, touched_run)
    return abandoned


def collect_due_evaluations(
    store: EventStore,
    state_getter: Callable[[], dict[str, Any]],
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """收割所有到期的 pending 窗口并回写 episode；状态获取失败则保持 pending 可重试。"""
    now = now or datetime.now(timezone.utc)
    claimant = uuid.uuid4().hex
    due = store.claim_due_evaluations(
        run_id=run_id, claimant=claimant,
        due_before=now.isoformat(timespec="milliseconds"),
    )
    if not due:
        return []
    try:
        observed = state_getter()
    except Exception:
        store.release_evaluation_claims(
            [item["evaluation_id"] for item in due], claimant
        )
        raise
    collected = []
    for evaluation in due:
        if store.has_completed_run_between(
            after=evaluation["created_at"],
            before=now.isoformat(timespec="milliseconds"),
            exclude_run_id=evaluation["run_id"],
        ):
            collected.append(
                store.finish_evaluation(
                    evaluation["evaluation_id"], status="collected",
                    observed=observed,
                    deltas={"coverage": 0.0, "score": 0.0, "confounded": True},
                    verdict="inconclusive", confidence=0.0,
                    error="评估窗口内存在另一已完成协商，无法隔离本次动作效果",
                )
            )
            continue
        deltas = evaluate_deltas(evaluation["baseline"], observed)
        verdict, confidence = classify(deltas)
        collected.append(
            store.finish_evaluation(
                evaluation["evaluation_id"],
                status="collected",
                observed=observed,
                deltas=deltas,
                verdict=verdict,
                confidence=confidence,
            )
        )
    for touched_run in sorted({item["run_id"] for item in collected}):
        apply_evaluation_to_episode(store, touched_run)
    return collected


def harvest_evaluations(
    store: EventStore,
    state_getter: Callable[[], dict[str, Any]],
    *,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """一次完整收割：先尽力结算到期窗口，再放弃逾期太久的窗口。

    收割与放弃解耦——即便 state server 离线导致收割抛错，放弃逻辑仍会执行，
    保证卡死窗口最终有终态。供后台 harvester、启动补收和手动 evaluate 复用。
    """
    now = now or datetime.now(timezone.utc)
    collected: list[dict[str, Any]] = []
    error: Exception | None = None
    try:
        collected = collect_due_evaluations(store, state_getter, run_id=run_id, now=now)
    except Exception as exc:  # noqa: BLE001 — 收割失败不应阻断放弃逻辑
        error = exc
    abandoned = abandon_stale_evaluations(store, run_id=run_id, now=now)
    result = {"collected": collected, "abandoned": abandoned}
    if error is not None:
        result["error"] = str(error)
    return result


def evaluate_deltas(
    baseline: dict[str, Any], observed: dict[str, Any]
) -> dict[str, Any]:
    """逐 AP 逐指标计算前后差值和归一得分，按业务优先级加权聚合。"""
    per_ap: dict[str, Any] = {}
    weighted_sum, weight_total = 0.0, 0.0
    metric_count, expected_count = 0, 0
    aps = sorted(
        ap
        for ap in set(baseline) & set(observed)
        if isinstance(baseline[ap], dict) and isinstance(observed[ap], dict)
    )
    for ap in aps:
        before_row, after_row = baseline[ap], observed[ap]
        weight = PRIORITY_WEIGHT.get(
            str(before_row.get("traffic_priority", "medium")).lower(), 0.6
        )
        metrics: dict[str, Any] = {}
        scores = []
        for field, direction, scale in _METRICS:
            expected_count += 1
            before, after = _num(before_row.get(field)), _num(after_row.get(field))
            if before is None or after is None:
                continue
            metric_count += 1
            score = _metric_score(before, after, direction, scale)
            scores.append(score)
            metrics[field] = {
                "before": before,
                "after": after,
                "delta": round(after - before, 6),
                "score": round(score, 6),
            }
        if not scores:
            continue
        ap_score = sum(scores) / len(scores)
        per_ap[ap] = {
            "weight": weight,
            "score": round(ap_score, 6),
            "metrics": metrics,
        }
        weighted_sum += weight * ap_score
        weight_total += weight
    coverage = metric_count / expected_count if expected_count else 0.0
    performance_score = weighted_sum / weight_total if weight_total else 0.0
    sla_rows: dict[str, Any] = {}
    satisfaction_values: list[float] = []
    for ap in aps:
        sla = baseline[ap].get("private_sla")
        if not isinstance(sla, dict):
            continue
        after = observed[ap]
        throughput = _num(after.get("throughput_mbps_user"))
        latency = _num(after.get("latency_ms"))
        min_tp = _num(sla.get("min_throughput_mbps"))
        max_latency = _num(sla.get("max_latency_ms"))
        ratios = []
        if min_tp and throughput is not None:
            ratios.append(throughput / min_tp)
        if max_latency and latency is not None and latency > 0:
            ratios.append(max_latency / latency)
        if not ratios:
            continue
        satisfaction = min(ratios)
        satisfaction_values.append(satisfaction)
        sla_rows[ap] = {
            "satisfied": satisfaction >= 1.0,
            "satisfaction_ratio": round(satisfaction, 6),
        }
    fairness = 1.0
    if satisfaction_values:
        denom = len(satisfaction_values) * sum(v * v for v in satisfaction_values)
        fairness = (sum(satisfaction_values) ** 2 / denom) if denom else 0.0
        sla_score = sum(max(-1.0, min(1.0, v - 1.0)) for v in satisfaction_values) / len(satisfaction_values)
        # 有私有约束时，质量同时衡量性能、底线满足度和 Jain 公平性。
        score = 0.5 * performance_score + 0.35 * sla_score + 0.15 * (fairness - 1.0)
    else:
        score = performance_score
    return {
        "per_ap": per_ap,
        "score": round(score, 6),
        "performance_score": round(performance_score, 6),
        "sla": {"per_ap": sla_rows, "all_satisfied": bool(sla_rows) and all(
            row["satisfied"] for row in sla_rows.values()), "fairness_jain": round(fairness, 6)},
        "coverage": round(coverage, 6),
    }


def classify(deltas: dict[str, Any]) -> tuple[str, float]:
    """把聚合得分映射为 verdict + confidence（覆盖率不足直接 inconclusive）。"""
    coverage = float(deltas.get("coverage") or 0.0)
    score = float(deltas.get("score") or 0.0)
    if coverage < MIN_COVERAGE:
        return "inconclusive", round(coverage, 4)
    if score >= IMPROVE_THRESHOLD:
        verdict = "improved"
    elif score <= DEGRADE_THRESHOLD:
        verdict = "degraded"
    else:
        return "neutral", round(coverage, 4)
    confidence = coverage * min(1.0, abs(score) / _FULL_CONFIDENCE_SCORE)
    return verdict, round(confidence, 4)


def summarize_run_evaluations(
    evaluations: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """汇总一个 run 的全部窗口：最终结论取最后一个有定论的窗口（最接近稳态）。"""
    collected = [item for item in evaluations if item["status"] == "collected"]
    if not collected:
        return None
    windows = [
        {
            "window": item["window_label"],
            "verdict": item["verdict"],
            "confidence": item["confidence"],
            "score": (item["deltas"] or {}).get("score"),
            "collected_at": item["collected_at"],
        }
        for item in collected
    ]
    pending = sum(1 for item in evaluations if item["status"] == "pending")
    abandoned = sum(1 for item in evaluations if item["status"] == "abandoned")

    # 因果强化：自然波动不会在多个时间窗口持续同向，持续同向才更可能是决策效应。
    # 跨窗口一致性 = 主导方向窗口占方向性窗口的比例；摇摆（既有改善又有恶化）会
    # 压低最终置信度，从而不会误判、也不会因单窗口噪声触发回滚。
    directional = [item for item in collected if item["verdict"] in {"improved", "degraded"}]
    improved = sum(1 for item in directional if item["verdict"] == "improved")
    degraded = len(directional) - improved
    if directional:
        cross_consistency = round(max(improved, degraded) / len(directional), 4)
        dominant = "improved" if improved >= degraded else "degraded"
        dom_windows = [item for item in directional if item["verdict"] == dominant]
        final = dom_windows[-1]  # 主导方向里最接近稳态的窗口
        final_verdict = dominant
        final_confidence = round(float(final["confidence"] or 0.0) * cross_consistency, 4)
    else:
        # 全部 neutral/inconclusive：无方向性变化。
        cross_consistency = 1.0
        final = collected[-1]
        final_verdict = final["verdict"]
        final_confidence = final["confidence"]

    needs_rollback = (
        final_verdict == "degraded"
        and float(final_confidence or 0.0) >= ROLLBACK_CONFIDENCE
    )
    return {
        "windows": windows,
        "pending_windows": pending,
        "abandoned_windows": abandoned,
        "cross_window_consistency": cross_consistency,
        "final_verdict": final_verdict,
        "final_confidence": final_confidence,
        "final_score": (final["deltas"] or {}).get("score"),
        "needs_rollback": needs_rollback,
    }


def revise_quality(base_quality: float, summary: dict[str, Any]) -> float:
    """按真实效果修订案例质量：劣化封顶 0.2（低于注入阈值 0.5），改善按置信度加成。"""
    verdict = summary.get("final_verdict")
    confidence = float(summary.get("final_confidence") or 0.0)
    if verdict == "improved":
        return round(min(1.0, base_quality + 0.15 * confidence), 4)
    if verdict == "degraded":
        return round(min(base_quality, 0.2), 4)
    return round(base_quality, 4)


def apply_evaluation_to_episode(
    store: EventStore, run_id: str
) -> dict[str, Any] | None:
    """把评估汇总和修订后的质量回写 episode；劣化时附回滚参数计划。"""
    episode = store.get_episode(run_id=run_id)
    if episode is None:
        return None
    summary = summarize_run_evaluations(store.list_evaluations(run_id))
    if summary is None:
        return None
    if summary["needs_rollback"]:
        summary["rollback_plan"] = build_rollback_plan(
            episode.get("initial_state") or {}, episode.get("decision") or {}
        )
    # 从 episode 内容重算流水线基础分再修订，保证重复评估幂等。
    quality = revise_quality(pipeline_quality(episode), summary)
    collected = [e for e in store.list_evaluations(run_id) if e.get("status") == "collected"]
    coverages = [float((e.get("deltas") or {}).get("coverage") or 0.0) for e in collected]
    quality_vector = {
        "pipeline_reliability": pipeline_quality(episode),
        "outcome_confidence": float(summary.get("final_confidence") or 0.0),
        "metric_coverage": round(statistics.mean(coverages), 4) if coverages else 0.0,
        "causal_confidence": round(
            float(summary.get("final_confidence") or 0.0)
            * float(summary.get("cross_window_consistency") or 0.0), 4
        ),
    }
    lifecycle = (
        "trusted" if summary["final_verdict"] == "improved"
        else "warning" if summary["final_verdict"] == "degraded"
        else "inconclusive" if summary["final_verdict"] == "inconclusive"
        else "evaluated"
    )
    store.update_episode_evaluation(
        run_id, evaluation=summary, quality_score=quality,
        quality_vector=quality_vector, lifecycle=lifecycle,
    )
    evaluations = store.list_evaluations(run_id)
    for agent_id in (episode.get("initial_state") or {}):
        local_summary = summarize_agent_evaluations(evaluations, agent_id)
        if local_summary is None:
            local_summary = {
                "scope": "agent", "agent_id": agent_id, "windows": [],
                "final_verdict": "inconclusive", "final_confidence": 0.0,
                "final_score": None, "needs_rollback": False,
                "reason": "缺少该 AP 的局部指标，禁止继承全局评价",
            }
        local_summary["global_verdict"] = summary.get("final_verdict")
        local_summary["global_local_conflict"] = (
            local_summary["final_verdict"] in {"improved", "degraded"}
            and summary.get("final_verdict") in {"improved", "degraded"}
            and local_summary["final_verdict"] != summary.get("final_verdict")
        )
        local_quality = revise_quality(pipeline_quality(episode), local_summary)
        store.update_agent_episode_evaluation(
            run_id, agent_id, evaluation=local_summary, quality_score=local_quality,
        )
    _update_case_narrative(store, run_id, episode, summary, quality_vector)
    from .workspace import AGENT_IDS, should_sync_for_store, try_save_long_term_memory
    if should_sync_for_store(store.path):
        for agent_id in AGENT_IDS:
            try_save_long_term_memory(
                agent_id,
                store.list_agent_episodes(
                    agent_id, topology_signature=episode.get("topology_signature"),
                    min_quality=0.0, limit=20,
                ),
            )
    return {**summary, "quality_score": quality, "run_id": run_id}


def _update_case_narrative(
    store: EventStore, run_id: str, episode: dict[str, Any],
    summary: dict[str, Any], quality_vector: dict[str, Any],
) -> None:
    from .llm_backend import enabled, model_name, summarize
    evidence = {"scene": episode.get("scene"), "strategy": episode.get("strategy"),
                "initial_state": episode.get("initial_state"),
                "decision": episode.get("decision"), "evaluation": summary,
                "quality_vector": quality_vector}
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    if not enabled():
        store.update_episode_narrative(
            run_id, narrative=None, model=None, evidence_hash=digest, status="disabled"
        )
        return
    prompt = (
        "把这个网络协商案例总结成不超过160字的案例记忆。必须分别说明适用条件、"
        "本次动作、全局效果、局部风险和不可确定之处；不得把中性结果写成改善，"
        "不得给出证据之外的参数。\n" + raw[:16000]
    )
    try:
        store.update_episode_narrative(
            run_id, narrative=summarize(prompt), model=model_name(),
            evidence_hash=digest, status="ready",
        )
    except Exception:
        store.update_episode_narrative(
            run_id, narrative=None, model=model_name(), evidence_hash=digest, status="failed"
        )


def summarize_agent_evaluations(
    evaluations: list[dict[str, Any]], agent_id: str,
) -> dict[str, Any] | None:
    """Derive a local verdict from one AP's metrics instead of copying global credit."""
    windows = []
    for item in evaluations:
        if item.get("status") != "collected":
            continue
        local = ((item.get("deltas") or {}).get("per_ap") or {}).get(agent_id)
        if not isinstance(local, dict):
            continue
        score = float(local.get("score") or 0.0)
        if score >= IMPROVE_THRESHOLD:
            verdict = "improved"
        elif score <= DEGRADE_THRESHOLD:
            verdict = "degraded"
        else:
            verdict = "neutral"
        metric_count = len(local.get("metrics") or {})
        coverage = min(1.0, metric_count / len(_METRICS))
        confidence = coverage * min(1.0, abs(score) / _FULL_CONFIDENCE_SCORE)
        windows.append({"window": item["window_label"], "verdict": verdict,
                        "score": score, "confidence": round(confidence, 4)})
    if not windows:
        return None
    directional = [w for w in windows if w["verdict"] in {"improved", "degraded"}]
    if directional:
        counts = Counter(w["verdict"] for w in directional)
        dominant = "degraded" if counts["degraded"] >= counts["improved"] else "improved"
        consistency = counts[dominant] / len(directional)
        final = [w for w in directional if w["verdict"] == dominant][-1]
        confidence = round(final["confidence"] * consistency, 4)
    else:
        dominant, consistency, final = "neutral", 1.0, windows[-1]
        confidence = final["confidence"]
    return {"scope": "agent", "agent_id": agent_id, "windows": windows,
            "cross_window_consistency": round(consistency, 4),
            "final_verdict": dominant, "final_confidence": confidence,
            "final_score": final["score"], "needs_rollback": False}


def build_rollback_plan(
    initial_state: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    """给出恢复到协商前参数的下发计划——只覆盖决策实际改过的字段，不自动执行。"""
    aliases = {
        "tx_power_dbm": ("tx_power_dbm",),
        "cwmin": ("cwmin", "CWmin"),
        "cwmax": ("cwmax", "CWmax"),
        "aifsn": ("aifsn", "AIFSN"),
    }
    plan: dict[str, Any] = {}
    for ap, changed in sorted((decision or {}).items()):
        row = initial_state.get(ap) or initial_state.get(str(ap).lower())
        if not isinstance(changed, dict) or not isinstance(row, dict):
            continue
        changed_fields = {str(key).lower() for key in changed}
        restore = {}
        for field, names in aliases.items():
            if field not in changed_fields:
                continue
            value = next((row[name] for name in names if row.get(name) is not None), None)
            if value is not None:
                restore[field] = value
        if restore:
            plan[str(ap).lower()] = restore
    return plan


def _metric_score(
    before: float, after: float, direction: str, scale: float | None
) -> float:
    if scale is not None:
        raw = (after - before) / scale
    else:
        denominator = max(abs(before), 1e-6)
        raw = (after - before) / denominator
    if direction == "lower":
        raw = -raw
    return max(-1.0, min(1.0, raw))


def evaluation_diagnostics(store: EventStore) -> dict[str, Any]:
    """扫历史评估，报告 score/verdict 分布与跨窗口摇摆率，辅助人工校准阈值。

    没有 ground truth，无法自动定"最优"阈值；本函数给出当前阈值下真实数据的
    表现和启发式提示，由人判断是否调整（阈值可经环境变量覆盖）。
    """
    collected = store.list_evaluations(status="collected")
    scores = sorted(
        (deltas or {}).get("score")
        for e in collected
        if (deltas := e.get("deltas")) is not None and (deltas or {}).get("score") is not None
    )
    verdicts = Counter(e["verdict"] for e in collected)
    run_ids = {e["run_id"] for e in collected}
    swinging = 0
    for run_id in run_ids:
        summary = summarize_run_evaluations(store.list_evaluations(run_id))
        if summary and summary.get("cross_window_consistency", 1.0) < 1.0:
            swinging += 1

    hints = []
    total = len(scores)
    if total:
        neutral_band = sum(1 for s in scores if DEGRADE_THRESHOLD < s < IMPROVE_THRESHOLD)
        neutral_ratio = neutral_band / total
        if neutral_ratio > 0.6:
            hints.append(
                f"{neutral_ratio:.0%} 的窗口得分落在中性带 "
                f"({DEGRADE_THRESHOLD}, {IMPROVE_THRESHOLD})，阈值可能偏宽；"
                "如需更灵敏可下调 MULTIAP_IMPROVE_THRESHOLD / MULTIAP_DEGRADE_THRESHOLD。"
            )
        elif neutral_ratio < 0.1:
            hints.append(
                f"仅 {neutral_ratio:.0%} 落在中性带，阈值可能偏窄（易把噪声判成变化）；"
                "如需更保守可上调阈值。"
            )
    if run_ids and swinging / len(run_ids) > 0.3:
        hints.append(
            f"{swinging}/{len(run_ids)} 个 run 跨窗口方向摇摆，因果信号弱——"
            "可增加评估窗口数或延长窗口以观察稳态。"
        )

    return {
        "collected_windows": len(collected),
        "verdict_counts": dict(verdicts),
        "score_distribution": _distribution(scores),
        "swinging_runs": swinging,
        "total_runs": len(run_ids),
        "active_thresholds": {
            "improve": IMPROVE_THRESHOLD,
            "degrade": DEGRADE_THRESHOLD,
            "min_coverage": MIN_COVERAGE,
            "rollback_confidence": ROLLBACK_CONFIDENCE,
        },
        "hints": hints or ["当前阈值下分布正常，无明显校准建议。"],
    }


def _distribution(sorted_values: list[float]) -> dict[str, Any]:
    if not sorted_values:
        return {"count": 0}
    n = len(sorted_values)
    return {
        "count": n,
        "min": round(sorted_values[0], 6),
        "p25": round(sorted_values[max(0, n // 4)], 6),
        "median": round(statistics.median(sorted_values), 6),
        "p75": round(sorted_values[min(n - 1, 3 * n // 4)], 6),
        "max": round(sorted_values[-1], 6),
    }


def _parse_ts(value: str) -> datetime:
    """解析 ISO 时间戳；无时区信息时按 UTC 处理。"""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

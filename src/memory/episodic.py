"""Episodic memory extraction and deterministic domain retrieval."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from src.persistence import EventStore


PRIORITY = {"low": 0.0, "medium": 0.5, "high": 1.0}
SIGNATURE_VERSION = 2
NEAR_MISS_SCORE = 0.03
RADIO_FIELDS = (
    "channel", "channel_number", "frequency_mhz", "band", "bandwidth_mhz",
    "channel_width_mhz", "phy_mode", "standard", "bssid", "ssid",
)


def encode_features(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    aps = sorted(ap for ap, value in state.items() if isinstance(value, dict))
    topology = []
    features: dict[str, Any] = {
        "signature_version": SIGNATURE_VERSION,
        "aps": aps, "per_ap": {}, "rssi_links": {}, "radio": {},
    }
    for ap in aps:
        row = state[ap]
        neighbors = row.get("neighbor_rssi_dbm") or {}
        topology.append((ap, sorted(str(peer) for peer in neighbors)))
        features["per_ap"][ap] = {
            "busy": _num(row.get("Data_rate_to_bandwidth_ratio")),
            "retries": _num(row.get("tx_retries_ratio")),
            "tx_power": _num(row.get("tx_power_dbm")),
            "cwmin": _num(row.get("CWmin", row.get("cwmin"))),
            "cwmax": _num(row.get("CWmax", row.get("cwmax"))),
            "aifsn": _num(row.get("AIFSN", row.get("aifsn"))),
            "priority": PRIORITY.get(str(row.get("traffic_priority", "medium")).lower(), 0.5),
            "sta_rssi": _num(row.get("sta_rssi_dbm")),
            "station_count": _num(row.get("station_count", row.get("sta_count"))),
        }
        features["radio"][ap] = {
            field: row.get(field) for field in RADIO_FIELDS if row.get(field) is not None
        }
        for peer, rssi in sorted(neighbors.items()):
            features["rssi_links"][f"{ap}>{peer}"] = _num(rssi)
    # topology_signature 只表达稳定的结构身份；易变的信道、带宽、负载和参数进入
    # features 参与软相似度。这样既隔离不同拓扑，又不会因一次换信道彻底失忆。
    signature_payload = {"v": SIGNATURE_VERSION, "aps": aps, "links": topology}
    signature = "v2:" + hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    deployment_payload = {
        "topology": signature_payload,
        "radio": features["radio"],
    }
    features["deployment_signature"] = "v2:" + hashlib.sha256(
        json.dumps(deployment_payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return signature, features


def materialize_episode(store: EventStore, run_id: str) -> dict[str, Any] | None:
    run = store.get_run(run_id)
    if run is None:
        return None
    events = store.load_events(run_id)
    if not events:
        return None
    start = next((event for event in events if event["event"] == "session_start"), {})
    end = next((event for event in reversed(events) if event["event"] == "session_end"), {})
    final = next((event for event in reversed(events) if event["event"] == "final_decision"), {})
    validation = next(
        (event for event in reversed(events) if event["event"] == "validation_result"),
        None,
    )
    executions = [event for event in events if event["event"] == "executor_apply"]
    initial_state = start.get("ap_state") or {}
    snapshots = list(store.iter_snapshots(run_id))
    observed = next(
        (
            snap["state"] for snap in reversed(snapshots)
            if snap["label"] == "final_observed"
        ),
        None,
    )
    topology_signature, features = encode_features(initial_state)
    decision = final.get("decision")
    strategy = (validation or {}).get("strategy")
    metrics = _outcome_metrics(initial_state, observed)
    outcome = str(end.get("outcome") or run.outcome or "incomplete")
    validation_payload = _event_payload(validation)
    inline_evaluation = (
        _inline_qos_evaluation(validation_payload)
        or _inline_validation_failure_evaluation(validation_payload)
    )
    lifecycle = _lifecycle_for_evaluation(inline_evaluation)
    quality_score = _quality(outcome, validation, executions, observed)
    if inline_evaluation:
        quality_score = _quality_with_evaluation(quality_score, inline_evaluation)
    episode = {
        "run_id": run_id,
        "scene": start.get("scene") or run.scene,
        "strategy": strategy,
        "outcome": outcome,
        "topology_signature": topology_signature,
        "features": features,
        "initial_state": initial_state,
        "decision": decision,
        "validation": validation_payload,
        "execution": [_event_payload(event) for event in executions],
        "observed_state": observed,
        "metrics": metrics,
        "quality_score": quality_score,
        "lifecycle": lifecycle,
        "feature_schema_version": SIGNATURE_VERSION,
        "evaluation_policy_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    if inline_evaluation:
        episode["evaluation"] = inline_evaluation
    episode["quality_vector"] = {
        "pipeline_reliability": _quality(outcome, validation, executions, observed),
        "outcome_confidence": float((inline_evaluation or {}).get("final_confidence") or 0.0),
        "metric_coverage": 1.0 if inline_evaluation and metrics.get("available") else 0.0,
        "causal_confidence": float((inline_evaluation or {}).get("final_confidence") or 0.0),
    }
    episode["episode_fingerprint"] = hashlib.sha256(json.dumps({
        "topology": topology_signature, "scene": episode["scene"],
        "strategy": strategy, "features": features, "decision": decision,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    episode["episode_id"] = store.save_episode(episode)
    for agent_id in features["aps"]:
        store.save_agent_episode({
            "run_id": run_id, "agent_id": agent_id,
            "topology_signature": topology_signature,
            "scene": episode["scene"], "strategy": strategy,
            "local_state": initial_state.get(agent_id) or {},
            "local_decision": (decision or {}).get(agent_id)
            if isinstance(decision, dict) else None,
            "outcome": outcome, "quality_score": episode["quality_score"],
            "evaluation": episode.get("evaluation"),
            "created_at": episode["created_at"],
        })
    return episode


def find_agent_episodes(
    store: EventStore, agent_id: str, state: dict[str, Any], *,
    limit: int = 3, min_quality: float = 0.5,
    require_evaluation: bool = True,
) -> list[dict[str, Any]]:
    """Recall durable cases scoped to one agent and the current topology."""
    topology, _ = encode_features(state)
    candidates = store.list_agent_episodes(
        agent_id, topology_signature=topology,
        min_quality=min_quality, limit=max(limit, 20),
    )
    if require_evaluation:
        candidates = [
            item for item in candidates
            if (item.get("evaluation") or {}).get("final_verdict")
            in {"improved", "neutral", "degraded"}
        ]
    from .reflection import gate_memories
    return gate_memories(candidates)[:max(1, min(int(limit), 20))]


def find_similar_episodes(
    store: EventStore,
    state: dict[str, Any],
    *,
    limit: int = 5,
    min_quality: float = 0.0,
    exclude_run_id: str | None = None,
    require_evaluation: bool = False,
) -> list[dict[str, Any]]:
    topology, query = encode_features(state)
    # 同时扫描旧 v1 案例，允许在线升级后继续召回；结构不兼容的候选会在下方剔除。
    exact = store.list_episodes(topology_signature=topology, limit=1000)
    all_candidates = store.list_episodes(limit=1000)
    candidates = list({item["run_id"]: item for item in (*exact, *all_candidates)}.values())
    ranked = []
    for episode in candidates:
        if episode["run_id"] == exclude_run_id or episode["quality_score"] < min_quality:
            continue
        evaluation = episode.get("evaluation") or {}
        if require_evaluation and evaluation.get("final_verdict") not in {
            "improved", "neutral", "degraded"
        }:
            continue
        if not _compatible_structure(query, episode["features"]):
            continue
        similarity, components = feature_similarity(query, episode["features"])
        feedback_confidence = float(evaluation.get("final_confidence") or 0.0)
        retrieval_score = (
            0.75 * similarity + 0.20 * float(episode["quality_score"])
            + 0.05 * feedback_confidence
        )
        ranked.append({**episode, "similarity": similarity,
                       "similarity_components": components,
                       "retrieval_score": round(retrieval_score, 6)})
    ranked.sort(key=lambda item: (item["retrieval_score"], item["similarity"]), reverse=True)
    from .reflection import gate_memories
    return gate_memories(ranked)[: max(1, min(int(limit), 20))]


def find_episode_memory(
    store: EventStore, state: dict[str, Any], *, positive_limit: int = 3,
    warning_limit: int = 2, exclude_run_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Retrieve deduplicated positive exemplars and negative warnings separately."""
    candidates = find_similar_episodes(
        store, state, limit=20, min_quality=0.0,
        exclude_run_id=exclude_run_id, require_evaluation=True,
    )
    seen: set[str] = set()
    positive, warnings = [], []
    for item in candidates:
        fingerprint = item.get("episode_fingerprint") or item["run_id"]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        verdict = (item.get("evaluation") or {}).get("final_verdict")
        if _is_warning_evaluation(item.get("evaluation")) and len(warnings) < warning_limit:
            warnings.append(item)
        elif _is_positive_evaluation(item.get("evaluation")) and len(positive) < positive_limit:
            positive.append(item)
    return {"positive": positive, "warnings": warnings}


def _is_warning_evaluation(evaluation: dict[str, Any] | None) -> bool:
    evaluation = evaluation or {}
    verdict = evaluation.get("final_verdict")
    if verdict == "degraded":
        return True
    score = _num(evaluation.get("final_score"))
    return verdict == "neutral" and evaluation.get("approved") is False and (
        score is None or score < NEAR_MISS_SCORE
    )


def _is_positive_evaluation(evaluation: dict[str, Any] | None) -> bool:
    evaluation = evaluation or {}
    if evaluation.get("final_verdict") == "improved":
        return True
    score = _num(evaluation.get("final_score"))
    return (
        evaluation.get("final_verdict") == "neutral"
        and evaluation.get("approved") is False
        and score is not None
        and score >= NEAR_MISS_SCORE
    )


def _compatible_structure(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """跨签名版本的硬门控：AP 集合和有向邻接边必须一致。"""
    return (
        set(left.get("aps") or ()) == set(right.get("aps") or ())
        and set((left.get("rssi_links") or {}).keys())
        == set((right.get("rssi_links") or {}).keys())
    )


def feature_similarity(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, dict[str, float]]:
    rssi = _mapping_similarity(left.get("rssi_links", {}), right.get("rssi_links", {}), 25.0)
    load = _per_ap_similarity(left, right, ("busy", "retries"), (1.0, 1.0))
    priority = _per_ap_similarity(left, right, ("priority",), (1.0,))
    params = _per_ap_similarity(
        left, right, ("tx_power", "cwmin", "cwmax", "aifsn"), (23.0, 1023.0, 1023.0, 15.0)
    )
    sta = _per_ap_similarity(left, right, ("sta_rssi",), (40.0,))
    stations = _per_ap_similarity(left, right, ("station_count",), (30.0,))
    radio = _radio_similarity(left.get("radio", {}), right.get("radio", {}))
    components = {"interference": rssi, "load": load, "priority": priority,
                  "parameters": params, "sta": sta, "stations": stations, "radio": radio}
    score = (0.25 * rssi + 0.22 * load + 0.13 * priority + 0.15 * params
             + 0.08 * sta + 0.07 * stations + 0.10 * radio)
    return round(score, 6), {key: round(value, 6) for key, value in components.items()}


def _per_ap_similarity(left, right, fields, scales) -> float:
    left_aps, right_aps = left.get("per_ap", {}), right.get("per_ap", {})
    keys = sorted(set(left_aps) & set(right_aps))
    values = []
    for ap in keys:
        for field, scale in zip(fields, scales):
            values.append(_scalar_similarity(left_aps[ap].get(field), right_aps[ap].get(field), scale))
    return sum(values) / len(values) if values else 0.0


def _mapping_similarity(left, right, scale) -> float:
    keys = sorted(set(left) & set(right))
    if set(left) != set(right) or not keys:
        return 0.0
    return sum(_scalar_similarity(left[key], right[key], scale) for key in keys) / len(keys)


def _radio_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    aps = sorted(set(left) & set(right))
    scores = []
    numeric_scales = {"frequency_mhz": 160.0, "bandwidth_mhz": 80.0,
                      "channel_width_mhz": 80.0, "channel": 16.0,
                      "channel_number": 16.0}
    for ap in aps:
        fields = set(left.get(ap, {})) | set(right.get(ap, {}))
        for field in fields:
            a, b = left.get(ap, {}).get(field), right.get(ap, {}).get(field)
            if a is None or b is None:
                continue  # 未知字段不奖励，也不惩罚旧 schema 案例。
            if field in numeric_scales:
                scores.append(_scalar_similarity(_num(a), _num(b), numeric_scales[field]))
            else:
                scores.append(1.0 if str(a).lower() == str(b).lower() else 0.0)
    return sum(scores) / len(scores) if scores else 0.5


def _scalar_similarity(left, right, scale) -> float:
    if left is None or right is None:
        return 0.5 if left is right else 0.0
    return max(0.0, 1.0 - abs(float(left) - float(right)) / scale)


def _outcome_metrics(initial, observed) -> dict[str, Any]:
    if not observed:
        return {"available": False}
    result = {"available": True, "per_ap": {}}
    for ap in sorted(set(initial) & set(observed)):
        before, after = initial[ap], observed[ap]
        result["per_ap"][ap] = {}
        for field in ("throughput_mbps_iperf", "throughput_mbps_user", "latency_ms", "packet_loss_pct"):
            a, b = _num(before.get(field)), _num(after.get(field))
            if a is not None and b is not None:
                result["per_ap"][ap][field] = {"before": a, "after": b, "delta": round(b - a, 6)}
    return result


def _inline_qos_evaluation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not validation:
        return None
    qos = validation.get("qos_acceptance")
    if not isinstance(qos, dict):
        return None
    verdict = str(qos.get("verdict") or "").lower()
    if verdict not in {"improved", "neutral", "degraded", "inconclusive"}:
        return None
    confidence = _bounded_float(qos.get("confidence"), default=0.0)
    deltas = qos.get("deltas") if isinstance(qos.get("deltas"), dict) else {}
    return {
        "source": "qos_acceptance",
        "scope": "global",
        "windows": [],
        "final_verdict": verdict,
        "final_confidence": confidence,
        "global_verdict": verdict,
        "global_confidence": confidence,
        "final_score": _num(deltas.get("score")),
        "needs_rollback": verdict == "degraded",
        "pending_windows": 0,
        "collected_windows": 1,
        "reason": qos.get("reason") or "inline_qos_acceptance",
        "approved": bool(qos.get("approved")) if qos.get("approved") is not None else None,
        "deltas": deltas,
    }


def _inline_validation_failure_evaluation(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not validation or validation.get("approved") is not False:
        return None
    sta_qoe = validation.get("sta_qoe") if isinstance(validation.get("sta_qoe"), dict) else {}
    new_violations = validation.get("new_violations") or sta_qoe.get("new_violations") or []
    sta_rejected = bool(sta_qoe.get("checked")) and sta_qoe.get("approved") is False
    if not sta_rejected and not new_violations:
        return None
    reason = validation.get("summary") or "; ".join(validation.get("global_errors") or [])
    return {
        "source": "validation_result",
        "scope": "global",
        "windows": [],
        "final_verdict": "degraded",
        "final_confidence": 0.85,
        "global_verdict": "degraded",
        "global_confidence": 0.85,
        "final_score": None,
        "needs_rollback": True,
        "pending_windows": 0,
        "collected_windows": 1,
        "reason": reason or "validator_rejected_candidate_after_observation",
        "approved": False,
        "new_violations": new_violations,
    }


def _lifecycle_for_evaluation(evaluation: dict[str, Any] | None) -> str:
    if _is_warning_evaluation(evaluation):
        return "warning"
    verdict = (evaluation or {}).get("final_verdict")
    return {
        "improved": "trusted",
        "inconclusive": "inconclusive",
        "neutral": "evaluated",
    }.get(verdict, "awaiting_evaluation")


def _quality_with_evaluation(base: float, evaluation: dict[str, Any]) -> float:
    verdict = evaluation.get("final_verdict")
    confidence = float(evaluation.get("final_confidence") or 0.0)
    if _is_warning_evaluation(evaluation):
        return round(max(min(base, 0.45), 0.20 + 0.10 * confidence), 4)
    if _is_positive_evaluation(evaluation):
        return round(max(base, 0.55 + 0.10 * confidence), 4)
    floor = {
        "improved": 0.75,
        "neutral": 0.55,
        "degraded": 0.20,
        "inconclusive": 0.10,
    }.get(verdict, 0.0)
    if verdict == "degraded":
        return round(max(min(base, 0.45), floor + 0.10 * confidence), 4)
    return round(max(base, floor + 0.20 * confidence), 4)


def pipeline_quality(episode: dict[str, Any]) -> float:
    """从 episode 内容重算流水线基础质量分（不含执行后效果修订）。"""
    return _quality(
        str(episode.get("outcome") or "incomplete"),
        episode.get("validation"),
        episode.get("execution") or [],
        episode.get("observed_state"),
    )


def _quality(outcome, validation, executions, observed) -> float:
    score = 0.5 if outcome == "success" else 0.0
    if validation and validation.get("approved"):
        score += 0.25
    if executions:
        score += 0.15 if all(event.get("ok") for event in executions) else 0.0
    else:
        score += 0.05
    if observed is not None:
        score += 0.10
    return round(min(score, 1.0), 4)


def _event_payload(event):
    if event is None:
        return None
    return {key: value for key, value in event.items() if key not in {"event_id", "sequence", "event", "ts"}}


def _bounded_float(value, *, default: float) -> float:
    number = _num(value)
    if number is None:
        return default
    return max(0.0, min(float(number), 1.0))


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None

"""Episodic memory extraction and deterministic domain retrieval."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from src.persistence import EventStore


PRIORITY = {"low": 0.0, "medium": 0.5, "high": 1.0}


def encode_features(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    aps = sorted(ap for ap, value in state.items() if isinstance(value, dict))
    topology = []
    features: dict[str, Any] = {"aps": aps, "per_ap": {}, "rssi_links": {}}
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
        }
        for peer, rssi in sorted(neighbors.items()):
            features["rssi_links"][f"{ap}>{peer}"] = _num(rssi)
    signature = hashlib.sha256(
        json.dumps(topology, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
    episode = {
        "run_id": run_id,
        "scene": start.get("scene") or run.scene,
        "strategy": strategy,
        "outcome": outcome,
        "topology_signature": topology_signature,
        "features": features,
        "initial_state": initial_state,
        "decision": decision,
        "validation": _event_payload(validation),
        "execution": [_event_payload(event) for event in executions],
        "observed_state": observed,
        "metrics": metrics,
        "quality_score": _quality(outcome, validation, executions, observed),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    episode["episode_id"] = store.save_episode(episode)
    return episode


def find_similar_episodes(
    store: EventStore,
    state: dict[str, Any],
    *,
    limit: int = 5,
    min_quality: float = 0.0,
    exclude_run_id: str | None = None,
) -> list[dict[str, Any]]:
    topology, query = encode_features(state)
    candidates = store.list_episodes(topology_signature=topology, limit=500)
    ranked = []
    for episode in candidates:
        if episode["run_id"] == exclude_run_id or episode["quality_score"] < min_quality:
            continue
        similarity, components = feature_similarity(query, episode["features"])
        ranked.append({**episode, "similarity": similarity, "similarity_components": components})
    ranked.sort(key=lambda item: (item["similarity"], item["quality_score"]), reverse=True)
    return ranked[: max(1, min(int(limit), 20))]


def feature_similarity(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, dict[str, float]]:
    rssi = _mapping_similarity(left.get("rssi_links", {}), right.get("rssi_links", {}), 25.0)
    load = _per_ap_similarity(left, right, ("busy", "retries"), (1.0, 1.0))
    priority = _per_ap_similarity(left, right, ("priority",), (1.0,))
    params = _per_ap_similarity(
        left, right, ("tx_power", "cwmin", "cwmax", "aifsn"), (23.0, 1023.0, 1023.0, 15.0)
    )
    sta = _per_ap_similarity(left, right, ("sta_rssi",), (40.0,))
    components = {"interference": rssi, "load": load, "priority": priority, "parameters": params, "sta": sta}
    score = 0.30 * rssi + 0.25 * load + 0.15 * priority + 0.20 * params + 0.10 * sta
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


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None

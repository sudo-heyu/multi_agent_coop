"""STA-side QoE/SLA feedback helpers.

The AP state remains the system's main negotiation object, but each AP may now
carry a ``stas`` list with per-station measurements produced by ns-3.  This
module normalizes those records, summarizes feedback for agents, and evaluates
post-decision QoE regressions for the validator.
"""
from __future__ import annotations

from typing import Any


_STATUS_OK = {"ok", "satisfied", "pass", "passed", "normal"}
_STATUS_BAD = {"violated", "violate", "failed", "fail", "bad"}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_num(source: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    measurements = source.get("measurements")
    for key in keys:
        value = source.get(key)
        parsed = _num(value)
        if parsed is not None:
            return parsed
    if isinstance(measurements, dict):
        for key in keys:
            parsed = _num(measurements.get(key))
            if parsed is not None:
                return parsed
    return None


def _status_from_text(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in _STATUS_OK:
        return "satisfied"
    if raw in _STATUS_BAD:
        return "violated"
    if raw in {"warn", "warning", "risk"}:
        return "warning"
    return None


def normalize_sta_feedback(sta: dict[str, Any], *, associated_ap: str | None = None) -> dict[str, Any]:
    """Return a normalized STA feedback record.

    Supported input is intentionally permissive: ns-3 may emit flattened fields
    or a nested ``measurements`` object.  SLA limits are evaluated when present;
    an explicit ``sla_status`` still works as a source field but cannot hide
    numeric SLA violations.
    """
    row = dict(sta or {})
    ap_id = str(row.get("associated_ap") or row.get("ap_id") or associated_ap or "").lower()
    sta_id = str(row.get("sta_id") or row.get("id") or (f"{ap_id}_sta" if ap_id else "sta"))
    sla = row.get("sla") if isinstance(row.get("sla"), dict) else {}
    metrics = {
        "throughput_mbps": _first_num(
            row, ("throughput_mbps", "throughput_mbps_user", "rx_mbps")
        ),
        "latency_ms": _first_num(row, ("latency_ms", "delay_ms")),
        "jitter_ms": _first_num(row, ("jitter_ms",)),
        "packet_loss_pct": _first_num(row, ("packet_loss_pct", "loss_pct")),
        "rssi_dbm": _first_num(row, ("rssi_dbm", "sta_rssi_dbm")),
        "sinr_db": _first_num(row, ("sinr_db",)),
    }

    violations: list[dict[str, Any]] = []

    def check_min(limit_key: str, metric_key: str, label: str) -> None:
        limit = _num(sla.get(limit_key))
        actual = metrics.get(metric_key)
        if limit is not None and actual is not None and actual < limit:
            violations.append({
                "metric": metric_key,
                "rule": f"{label}>={limit:g}",
                "actual": round(actual, 4),
                "limit": limit,
            })

    def check_max(limit_key: str, metric_key: str, label: str) -> None:
        limit = _num(sla.get(limit_key))
        actual = metrics.get(metric_key)
        if limit is not None and actual is not None and actual > limit:
            violations.append({
                "metric": metric_key,
                "rule": f"{label}<={limit:g}",
                "actual": round(actual, 4),
                "limit": limit,
            })

    check_min("min_throughput_mbps", "throughput_mbps", "throughput")
    check_max("max_latency_ms", "latency_ms", "latency")
    check_max("max_jitter_ms", "jitter_ms", "jitter")
    check_max("max_packet_loss_pct", "packet_loss_pct", "loss")
    check_min("min_rssi_dbm", "rssi_dbm", "rssi")
    check_min("min_sinr_db", "sinr_db", "sinr")

    explicit = _status_from_text(row.get("sla_status") or row.get("status"))
    if violations:
        status = "violated"
    else:
        status = explicit or "satisfied"

    flow_type = str(row.get("flow_type") or row.get("service_name") or row.get("business_type") or "")
    if violations:
        reason = "; ".join(
            f"{item['metric']}={item['actual']} violates {item['rule']}"
            for item in violations[:3]
        )
    elif status == "warning":
        reason = str(row.get("feedback") or row.get("message") or "STA QoE has warning")
    else:
        reason = str(row.get("feedback") or row.get("message") or "STA SLA satisfied")

    return {
        "sta_id": sta_id,
        "associated_ap": ap_id,
        "flow_type": flow_type,
        "business_type": row.get("business_type"),
        "traffic_priority": row.get("traffic_priority"),
        "access_category": row.get("access_category") or row.get("ac"),
        "sla": dict(sla),
        "measurements": {k: v for k, v in metrics.items() if v is not None},
        "sla_status": status,
        "violations": violations,
        "feedback": reason,
    }


def ap_sta_feedback(ap_id: str, ap_state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = ap_state.get("stas") if isinstance(ap_state, dict) else None
    if not isinstance(raw, list):
        return []
    return [
        normalize_sta_feedback(item, associated_ap=ap_id)
        for item in raw
        if isinstance(item, dict)
    ]


def summarize_ap_feedback(ap_id: str, ap_state: dict[str, Any]) -> dict[str, Any]:
    stas = ap_sta_feedback(ap_id, ap_state)
    violations = [
        {
            "sta_id": sta["sta_id"],
            "flow_type": sta.get("flow_type"),
            "feedback": sta.get("feedback"),
            "violations": sta.get("violations") or [],
        }
        for sta in stas
        if sta.get("sla_status") == "violated"
    ]
    warnings = [
        sta for sta in stas
        if sta.get("sla_status") == "warning"
    ]
    if violations:
        status = "violated"
    elif warnings:
        status = "warning"
    elif stas:
        status = "satisfied"
    else:
        status = "unknown"
    return {
        "ap_id": ap_id,
        "sta_count": len(stas),
        "status": status,
        "violated_count": len(violations),
        "warning_count": len(warnings),
        "violations": violations,
        "stas": stas,
    }


def summarize_sta_feedback(ap_states: dict[str, Any]) -> dict[str, Any]:
    per_ap = {
        ap_id: summarize_ap_feedback(ap_id, state)
        for ap_id, state in (ap_states or {}).items()
        if isinstance(state, dict)
    }
    violations = [
        {**item, "ap_id": ap_id}
        for ap_id, summary in per_ap.items()
        for item in summary.get("violations") or []
    ]
    total_stas = sum(int(summary.get("sta_count") or 0) for summary in per_ap.values())
    return {
        "total_stas": total_stas,
        "violated_stas": len(violations),
        "all_sla_ok": total_stas > 0 and not violations,
        "per_ap": per_ap,
        "violations": violations,
    }


def _sta_map(ap_states: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ap_id, state in (ap_states or {}).items():
        if not isinstance(state, dict):
            continue
        for sta in ap_sta_feedback(ap_id, state):
            out[sta["sta_id"]] = sta
    return out


def evaluate_sta_qoe(
    baseline_state: dict[str, Any],
    observed_state: dict[str, Any] | None,
    *,
    observed_is_real: bool,
) -> dict[str, Any]:
    """Evaluate post-decision STA QoE regressions.

    The validator only gates on *new* SLA violations after a real observation.
    Existing violations are reported as persistent until later outcome
    evaluation determines whether they improved or degraded.
    """
    observed_state = observed_state or {}
    baseline = _sta_map(baseline_state)
    observed = _sta_map(observed_state)
    summary = summarize_sta_feedback(observed_state)
    if not observed:
        return {
            "checked": False,
            "approved": True,
            "reason": "observed state contains no STA feedback",
            "observed_is_real": observed_is_real,
            "summary": summary,
            "new_violations": [],
            "persistent_violations": [],
            "recovered": [],
        }

    new_violations = []
    persistent = []
    recovered = []
    for sta_id, obs in observed.items():
        base = baseline.get(sta_id)
        base_bad = bool(base and base.get("sla_status") == "violated")
        obs_bad = obs.get("sla_status") == "violated"
        if obs_bad and not base_bad:
            new_violations.append(obs)
        elif obs_bad and base_bad:
            persistent.append(obs)
        elif not obs_bad and base_bad:
            recovered.append(obs)

    approved = not (observed_is_real and new_violations)
    return {
        "checked": True,
        "approved": approved,
        "observed_is_real": observed_is_real,
        "summary": summary,
        "new_violations": new_violations,
        "persistent_violations": persistent,
        "recovered": recovered,
    }

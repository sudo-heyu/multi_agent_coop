#!/usr/bin/env python3
"""Run full-tool OpenClaw negotiation across ns-3 scenario cases.

This runner uses only live ns-3 telemetry as QoS evidence:

1. start one managed ns-3 live process per case;
2. collect baseline samples from state_server /state;
3. run the real OpenClaw AP agents with the full MCP tool set;
4. write the accepted decision back to ns-3 through stdin APPLY;
5. collect post-APPLY samples from state_server /state.

No mock telemetry or synthetic QoS is generated here.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "openclaw" / "mcp"))

import orchestration as orch
from run_openclaw import (
    Ns3LiveController,
    _fetch_required_initial_state,
    _require_gateway,
    _require_openclaw_config,
    _require_state_server,
    _wait_state_ready,
)
from src.logger import SessionLogger
from src.state_client import get_all_states
from state_server.ns3_scenario_matrix import (
    BUSINESS_PROFILES,
    TOPOLOGIES,
    build_matrix,
    get_case,
)
from src.validator import (
    EDCA_HIGH_LATENCY_GAIN_RATIO,
    EDCA_HIGH_LATENCY_INCREASE_TOLERANCE_RATIO,
    EDCA_HIGH_PACKET_LOSS_GAIN_PCT,
    EDCA_HIGH_PACKET_LOSS_TOLERANCE_PCT,
    EDCA_HIGH_THROUGHPUT_DROP_TOLERANCE_RATIO,
    EDCA_HIGH_THROUGHPUT_GAIN_RATIO,
    EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO,
    EDCA_OVERALL_PACKET_LOSS_TOLERANCE_PCT,
    QOS_LATENCY_INCREASE_TOLERANCE_RATIO,
    QOS_PACKET_LOSS_INCREASE_TOLERANCE_PCT,
    QOS_THROUGHPUT_DROP_TOLERANCE_RATIO,
)


AP_IDS = ("ap1", "ap2", "ap3")
METRICS = (
    "throughput_mbps_iperf",
    "throughput_mbps_user",
    "latency_ms",
    "packet_loss_pct",
    "tx_retries_ratio",
    "Data_rate_to_bandwidth_ratio",
)


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _mean(values: list[float]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 6)


def _pct_delta(before: float | None, after: float | None, *, positive_is_better: bool) -> float | None:
    if before is None or after is None or abs(before) < 1e-12:
        return None
    raw = (after - before) / before * 100.0
    if not positive_is_better:
        raw = -raw
    return round(raw, 3)


def _sample_state(server: str) -> dict[str, dict[str, Any]]:
    state = get_all_states(server)
    bad = [
        f"{ap}:{state.get(ap, {}).get('source')}"
        for ap in AP_IDS
        if str(state.get(ap, {}).get("source", "")).lower() != "ns3"
    ]
    if bad:
        raise RuntimeError(f"state source is not ns3: {bad}")
    return state


def collect_samples(server: str, *, seconds: float, interval: float) -> dict[str, Any]:
    deadline = time.time() + max(0.1, seconds)
    samples: list[dict[str, dict[str, Any]]] = []
    while time.time() < deadline or not samples:
        samples.append(_sample_state(server))
        time.sleep(max(0.1, interval))
    return summarize_samples(samples)


def summarize_samples(samples: list[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    per_ap: dict[str, dict[str, Any]] = {}
    for ap in AP_IDS:
        ap_samples = [sample.get(ap, {}) for sample in samples]
        row: dict[str, Any] = {}
        for metric in METRICS:
            row[metric] = _mean([
                float(s[metric])
                for s in ap_samples
                if isinstance(s.get(metric), (int, float)) and not isinstance(s.get(metric), bool)
            ])
        row["throughput_mbps_total"] = _mean([
            float(s.get("throughput_mbps_iperf", 0.0)) + float(s.get("throughput_mbps_user", 0.0))
            for s in ap_samples
            if isinstance(s.get("throughput_mbps_iperf"), (int, float))
            or isinstance(s.get("throughput_mbps_user"), (int, float))
        ])
        last = ap_samples[-1] if ap_samples else {}
        row["tx_power_dbm"] = last.get("tx_power_dbm")
        row["cwmin"] = last.get("cwmin")
        row["cwmax"] = last.get("cwmax")
        row["aifsn"] = last.get("aifsn")
        row["service_name"] = last.get("service_name")
        row["traffic_priority"] = last.get("traffic_priority")
        per_ap[ap] = row

    aggregate = {
        "throughput_mbps_iperf_sum": round(sum(
            per_ap[ap]["throughput_mbps_iperf"] or 0.0 for ap in AP_IDS
        ), 6),
        "throughput_mbps_user_sum": round(sum(
            per_ap[ap]["throughput_mbps_user"] or 0.0 for ap in AP_IDS
        ), 6),
        "throughput_mbps_total_sum": round(sum(
            per_ap[ap]["throughput_mbps_total"] or 0.0 for ap in AP_IDS
        ), 6),
        "latency_ms_avg": _mean([
            per_ap[ap]["latency_ms"] for ap in AP_IDS
            if per_ap[ap]["latency_ms"] is not None
        ]),
        "packet_loss_pct_avg": _mean([
            per_ap[ap]["packet_loss_pct"] for ap in AP_IDS
            if per_ap[ap]["packet_loss_pct"] is not None
        ]),
        "tx_retries_ratio_avg": _mean([
            per_ap[ap]["tx_retries_ratio"] for ap in AP_IDS
            if per_ap[ap]["tx_retries_ratio"] is not None
        ]),
    }
    return {"sample_count": len(samples), "per_ap": per_ap, "aggregate": aggregate}


def qos_delta(baseline: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    if not baseline or not after:
        return {}
    b = baseline["aggregate"]
    a = after["aggregate"]
    out = {
        "throughput_mbps_iperf_sum_delta": round(
            a["throughput_mbps_iperf_sum"] - b["throughput_mbps_iperf_sum"], 6
        ),
        "throughput_mbps_iperf_sum_improvement_pct": _pct_delta(
            b["throughput_mbps_iperf_sum"], a["throughput_mbps_iperf_sum"],
            positive_is_better=True,
        ),
        "throughput_mbps_user_sum_delta": round(
            a["throughput_mbps_user_sum"] - b["throughput_mbps_user_sum"], 6
        ),
        "throughput_mbps_user_sum_improvement_pct": _pct_delta(
            b["throughput_mbps_user_sum"], a["throughput_mbps_user_sum"],
            positive_is_better=True,
        ),
        "throughput_mbps_total_sum_delta": round(
            a["throughput_mbps_total_sum"] - b["throughput_mbps_total_sum"], 6
        ),
        "throughput_mbps_total_sum_improvement_pct": _pct_delta(
            b["throughput_mbps_total_sum"], a["throughput_mbps_total_sum"],
            positive_is_better=True,
        ),
        "latency_ms_avg_delta": (
            round(a["latency_ms_avg"] - b["latency_ms_avg"], 6)
            if a["latency_ms_avg"] is not None and b["latency_ms_avg"] is not None else None
        ),
        "latency_ms_avg_improvement_pct": _pct_delta(
            b["latency_ms_avg"], a["latency_ms_avg"], positive_is_better=False,
        ),
        "packet_loss_pct_avg_delta": (
            round(a["packet_loss_pct_avg"] - b["packet_loss_pct_avg"], 6)
            if a["packet_loss_pct_avg"] is not None and b["packet_loss_pct_avg"] is not None else None
        ),
        "packet_loss_pct_avg_improvement_pct": _pct_delta(
            b["packet_loss_pct_avg"], a["packet_loss_pct_avg"], positive_is_better=False,
        ),
    }
    out["throughput_total_improved"] = (
        out["throughput_mbps_total_sum_improvement_pct"] is not None
        and out["throughput_mbps_total_sum_improvement_pct"] > 0.5
    )
    out.update(priority_qos_delta(baseline, after, "high", "high_priority"))
    return out


def priority_qos_delta(
    baseline: dict[str, Any],
    after: dict[str, Any],
    priority: str,
    prefix: str,
) -> dict[str, Any]:
    before_rows = [
        row for row in (baseline.get("per_ap") or {}).values()
        if isinstance(row, dict) and row.get("traffic_priority") == priority
    ]
    after_rows = [
        row for row in (after.get("per_ap") or {}).values()
        if isinstance(row, dict) and row.get("traffic_priority") == priority
    ]
    out: dict[str, Any] = {
        f"{prefix}_sample_count_before": len(before_rows),
        f"{prefix}_sample_count_after": len(after_rows),
    }
    if not before_rows or not after_rows:
        return out

    before_tput = _mean([row.get("throughput_mbps_total") for row in before_rows])
    after_tput = _mean([row.get("throughput_mbps_total") for row in after_rows])
    before_latency = _mean([row.get("latency_ms") for row in before_rows])
    after_latency = _mean([row.get("latency_ms") for row in after_rows])
    before_loss = _mean([row.get("packet_loss_pct") for row in before_rows])
    after_loss = _mean([row.get("packet_loss_pct") for row in after_rows])

    out[f"{prefix}_throughput_mbps_total_before"] = before_tput
    out[f"{prefix}_throughput_mbps_total_after"] = after_tput
    out[f"{prefix}_throughput_mbps_total_delta"] = (
        round(after_tput - before_tput, 6)
        if before_tput is not None and after_tput is not None else None
    )
    out[f"{prefix}_throughput_mbps_total_improvement_pct"] = _pct_delta(
        before_tput, after_tput, positive_is_better=True,
    )
    out[f"{prefix}_latency_ms_avg_before"] = before_latency
    out[f"{prefix}_latency_ms_avg_after"] = after_latency
    out[f"{prefix}_latency_ms_avg_delta"] = (
        round(after_latency - before_latency, 6)
        if before_latency is not None and after_latency is not None else None
    )
    out[f"{prefix}_latency_ms_avg_improvement_pct"] = _pct_delta(
        before_latency, after_latency, positive_is_better=False,
    )
    out[f"{prefix}_packet_loss_pct_avg_before"] = before_loss
    out[f"{prefix}_packet_loss_pct_avg_after"] = after_loss
    out[f"{prefix}_packet_loss_pct_avg_delta"] = (
        round(after_loss - before_loss, 6)
        if before_loss is not None and after_loss is not None else None
    )
    return out


def baseline_validation_state(initial_state: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    state = json.loads(json.dumps(initial_state))
    for ap in AP_IDS:
        ap_state = state.setdefault(ap, {})
        metrics = (baseline.get("per_ap") or {}).get(ap) or {}
        for key in (
            "throughput_mbps_iperf",
            "throughput_mbps_user",
            "latency_ms",
            "packet_loss_pct",
        ):
            if metrics.get(key) is not None:
                ap_state[key] = metrics[key]
    return state


def sampled_validation_state(server: str, *, seconds: float, interval: float) -> dict[str, Any]:
    state = _sample_state(server)
    summary = collect_samples(server, seconds=seconds, interval=interval)
    return baseline_validation_state(state, summary)


def qos_non_regression(delta: dict[str, Any], strategy: str | None = None) -> dict[str, Any]:
    if not delta:
        return {"passed": False, "errors": ["missing post-APPLY QoS sample"]}
    errors: list[str] = []
    total_tput_delta = delta.get("throughput_mbps_total_sum_delta")
    total_tput_pct = delta.get("throughput_mbps_total_sum_improvement_pct")
    latency_delta = delta.get("latency_ms_avg_delta")
    latency_pct = delta.get("latency_ms_avg_improvement_pct")
    loss_delta = delta.get("packet_loss_pct_avg_delta")
    loss_pct = delta.get("packet_loss_pct_avg_improvement_pct")
    if (
        isinstance(total_tput_pct, (int, float))
        and total_tput_pct < -QOS_THROUGHPUT_DROP_TOLERANCE_RATIO * 100.0
    ):
        errors.append(
            f"total throughput decreased by {total_tput_delta:.6f} Mbps "
            f"({total_tput_pct:.3f}%)"
        )
    elif total_tput_delta is None:
        errors.append("missing total throughput delta")
    if (
        isinstance(latency_pct, (int, float))
        and latency_pct < -QOS_LATENCY_INCREASE_TOLERANCE_RATIO * 100.0
    ):
        errors.append(
            f"average latency increased by {latency_delta:.6f} ms "
            f"({-latency_pct:.3f}%)"
        )
    elif latency_delta is None:
        errors.append("missing average latency delta")
    if (
        isinstance(loss_delta, (int, float))
        and loss_delta > QOS_PACKET_LOSS_INCREASE_TOLERANCE_PCT
    ):
        errors.append(
            f"average packet loss increased by {loss_delta:.6f} percentage points"
        )
    elif loss_delta is None:
        # Zero baseline makes percentage undefined; the absolute delta is the hard check.
        errors.append("missing average packet loss delta")
    if not errors:
        return {"passed": True, "errors": [], "mode": "strict_non_regression"}
    if strategy == "co_edca":
        edca = edca_priority_qos(delta)
        if edca.get("passed"):
            return {
                "passed": True,
                "errors": [],
                "mode": "edca_priority",
                "strict_errors": errors,
                "details": edca.get("details", {}),
            }
        return {
            "passed": False,
            "errors": errors + list(edca.get("errors") or []),
            "mode": "edca_priority",
            "strict_errors": errors,
            "details": edca.get("details", {}),
        }
    return {"passed": False, "errors": errors, "mode": "strict_non_regression"}


def edca_priority_qos(delta: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    total_tput_pct = delta.get("throughput_mbps_total_sum_improvement_pct")
    latency_pct = delta.get("latency_ms_avg_improvement_pct")
    loss_delta = delta.get("packet_loss_pct_avg_delta")
    if not isinstance(total_tput_pct, (int, float)):
        errors.append("missing total throughput improvement")
    elif total_tput_pct < -EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO * 100.0:
        errors.append(f"overall throughput decreased by {-total_tput_pct:.3f}%")
    if not isinstance(latency_pct, (int, float)):
        errors.append("missing average latency improvement")
    elif latency_pct < -EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO * 100.0:
        errors.append(f"overall latency increased by {-latency_pct:.3f}%")
    if not isinstance(loss_delta, (int, float)):
        errors.append("missing packet loss delta")
    elif loss_delta > EDCA_OVERALL_PACKET_LOSS_TOLERANCE_PCT:
        errors.append(f"overall packet loss increased by {loss_delta:.3f} percentage points")

    high_tput_pct = delta.get("high_priority_throughput_mbps_total_improvement_pct")
    high_latency_pct = delta.get("high_priority_latency_ms_avg_improvement_pct")
    high_loss_delta = delta.get("high_priority_packet_loss_pct_avg_delta")
    high_count = delta.get("high_priority_sample_count_before")
    details.update({
        "high_priority_sample_count": high_count,
        "high_priority_throughput_pct": high_tput_pct,
        "high_priority_latency_pct": high_latency_pct,
        "high_priority_loss_delta": high_loss_delta,
    })
    if not high_count:
        errors.append("missing high-priority samples")
    if not isinstance(high_tput_pct, (int, float)):
        errors.append("missing high-priority throughput improvement")
    elif high_tput_pct < -EDCA_HIGH_THROUGHPUT_DROP_TOLERANCE_RATIO * 100.0:
        errors.append(f"high-priority throughput decreased by {-high_tput_pct:.3f}%")
    if not isinstance(high_latency_pct, (int, float)):
        errors.append("missing high-priority latency improvement")
    elif high_latency_pct < -EDCA_HIGH_LATENCY_INCREASE_TOLERANCE_RATIO * 100.0:
        errors.append(f"high-priority latency increased by {-high_latency_pct:.3f}%")
    if not isinstance(high_loss_delta, (int, float)):
        errors.append("missing high-priority packet loss delta")
    elif high_loss_delta > EDCA_HIGH_PACKET_LOSS_TOLERANCE_PCT:
        errors.append(
            f"high-priority packet loss increased by {high_loss_delta:.3f} percentage points"
        )

    has_high_gain = (
        isinstance(high_tput_pct, (int, float))
        and high_tput_pct >= EDCA_HIGH_THROUGHPUT_GAIN_RATIO * 100.0
    ) or (
        isinstance(high_latency_pct, (int, float))
        and high_latency_pct >= EDCA_HIGH_LATENCY_GAIN_RATIO * 100.0
    ) or (
        isinstance(high_loss_delta, (int, float))
        and high_loss_delta <= -EDCA_HIGH_PACKET_LOSS_GAIN_PCT
    )
    details["high_priority_has_gain"] = has_high_gain
    if not has_high_gain:
        errors.append("high-priority QoS did not improve enough")
    return {"passed": not errors, "errors": errors, "details": details}


def _close_logger(logger: SessionLogger | None) -> None:
    if logger is None:
        return
    for attr in ("_fh", "_state_fh"):
        fh = getattr(logger, attr, None)
        if fh is not None and not fh.closed:
            fh.close()


def run_case(case, args) -> dict[str, Any]:
    started = time.time()
    row: dict[str, Any] = {
        "scenario": case.scenario,
        "business_profile": case.business_profile,
        "expected_strategy": case.expected_strategy,
        "expected_reason": case.reason,
        "extra_args": list(case.extra_args),
        "tool_level": "fulltool",
        "data_source": "ns3",
        "converged": False,
        "observed_is_real": False,
        "baseline": None,
        "after": None,
        "qos_delta": {},
        "error": None,
    }
    ctrl = Ns3LiveController(
        root=args.ns3_root,
        server=args.server,
        scenario=case.scenario,
        business_profile=case.business_profile,
        sim_time=args.ns3_sim_time,
        report_interval=args.ns3_report_interval,
        extra_args=list(args.ns3_extra_arg or []),
    )
    logger: SessionLogger | None = None
    try:
        ctrl.start()
        if not ctrl.wait_until_ready(timeout_s=args.ready_timeout):
            raise RuntimeError("ns-3 did not produce initial TELEMETRY")
        _wait_state_ready(args.server, timeout_s=max(1.0, args.ns3_report_interval * 2.0))
        if args.warmup_seconds > 0:
            time.sleep(args.warmup_seconds)
        row["baseline"] = collect_samples(
            args.server,
            seconds=args.baseline_seconds,
            interval=args.sample_interval,
        )
        initial_state = _fetch_required_initial_state(args.server, "ns3")
        validation_baseline = baseline_validation_state(initial_state, row["baseline"])
        profiled_initial = orch.apply_profile(initial_state)

        logger = SessionLogger(verbose=False)
        logger.session_start(
            model="openclaw-fulltool",
            scene=f"ns3:{case.scenario}/{case.business_profile}",
            ap_state=profiled_initial,
        )
        orch.STATE_SERVER = args.server
        result = orch.structured_relay(
            max_turns=args.max_steps,
            max_total_messages=args.max_messages,
            max_validation_retries=args.max_validation_retries,
            logger=logger,
            observation_state_getter=lambda: sampled_validation_state(
                args.server,
                seconds=args.validation_seconds,
                interval=args.sample_interval,
            ),
            observation_wait_seconds=args.observation_wait,
            decision_applier=ctrl.apply_decision,
            initial_state=initial_state,
            validation_baseline_state=validation_baseline,
        )
        row["result"] = result
        row["observed_is_real"] = bool(result.get("observed_is_real"))
        row["strategy"] = result.get("strategy")
        row["decision"] = result.get("decision")
        row["validation_approved"] = bool((result.get("validation") or {}).get("approved"))
        row["validation_summary"] = (result.get("validation") or {}).get("summary")
        row["push_results"] = result.get("push_results")
        if result.get("outcome") in ("success", "safe_noop"):
            row["after"] = collect_samples(
                args.server,
                seconds=args.after_seconds,
                interval=args.sample_interval,
            )
        elif result.get("outcome") == "noop":
            row["after"] = row["baseline"]
        row["qos_delta"] = qos_delta(row.get("baseline"), row.get("after"))
        row["qos_non_regression"] = qos_non_regression(row["qos_delta"], row.get("strategy"))
        row["converged"] = (
            result.get("outcome") in ("success", "noop", "safe_noop")
            and int(result.get("transcript_turns") or 0) <= args.max_messages
            and bool(row["qos_non_regression"].get("passed"))
        )
        row["log_path"] = str(logger.log_path)
        row["state_trace_path"] = str(logger.state_trace_path)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc()
    finally:
        _close_logger(logger)
        ctrl.stop()
        row["duration_s"] = round(time.time() - started, 3)
    return row


def render_markdown(results: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# ns-3 Fulltool Matrix Results",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "| case | expected | outcome | converged | msgs | strategy | real obs | QoS ok | total tput delta | total tput % | latency % | loss % | duration | error |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        result = r.get("result") or {}
        delta = r.get("qos_delta") or {}
        case = f"{r['scenario']}/{r['business_profile']}"
        lines.append(
            "| "
            + " | ".join([
                case,
                str(r.get("expected_strategy")),
                str(result.get("outcome") or ("error" if r.get("error") else "")),
                str(r.get("converged")),
                str(result.get("transcript_turns") or ""),
                str(r.get("strategy") or result.get("strategy") or ""),
                str(r.get("observed_is_real")),
                str((r.get("qos_non_regression") or {}).get("passed")),
                str(delta.get("throughput_mbps_total_sum_delta")),
                str(delta.get("throughput_mbps_total_sum_improvement_pct")),
                str(delta.get("latency_ms_avg_improvement_pct")),
                str(delta.get("packet_loss_pct_avg_improvement_pct")),
                str(r.get("duration_s")),
                str(r.get("error") or ""),
            ])
            + " |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_cases(args) -> list[Any]:
    if args.case:
        cases = []
        for spec in args.case:
            if "/" not in spec:
                raise SystemExit(f"--case must be scenario/profile, got {spec!r}")
            scenario, profile = spec.split("/", 1)
            cases.append(get_case(scenario, profile))
        return cases
    cases = build_matrix()
    if args.skip_noop:
        cases = [c for c in cases if c.expected_strategy != "noop"]
    if args.limit:
        cases = cases[:args.limit]
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--ns3-root", default="/Users/heyu/Developer/ns-3.47")
    parser.add_argument("--ns3-sim-time", type=float, default=260.0)
    parser.add_argument("--ns3-report-interval", type=float, default=1.0)
    parser.add_argument("--ns3-extra-arg", action="append", default=[])
    parser.add_argument("--baseline-seconds", type=float, default=10.0)
    parser.add_argument("--warmup-seconds", type=float, default=30.0,
                        help="Discard ns-3 startup transient before collecting baseline QoS")
    parser.add_argument("--after-seconds", type=float, default=10.0)
    parser.add_argument("--validation-seconds", type=float, default=8.0,
                        help="Continuous ns-3 sampling window used by Validator after APPLY")
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--observation-wait", type=float, default=3.0)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-messages", type=int, default=20,
                        help="Total negotiation messages budget, including the 3 broadcasts")
    parser.add_argument("--max-validation-retries", type=int, default=2,
                        help="Number of real APPLY+Validator retries before safe fallback")
    parser.add_argument("--case", action="append",
                        help=f"Run one case, e.g. line/live_bulk. Topologies={TOPOLOGIES}, profiles={BUSINESS_PROFILES}")
    parser.add_argument("--skip-noop", action="store_true",
                        help="Skip expected noop cases when focusing on QoS improvement")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--require-qwen80b", action="store_true")
    parser.add_argument("--output-dir", default=str(REPO / "logs"))
    args = parser.parse_args()

    _require_openclaw_config(require_qwen80b=args.require_qwen80b)
    _require_state_server(args.server)
    _require_gateway(use_coordinator=False)

    cases = parse_cases(args)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _now_slug()
    json_path = out_dir / f"ns3_fulltool_matrix_{slug}.json"
    md_path = out_dir / f"ns3_fulltool_matrix_{slug}.md"

    results: list[dict[str, Any]] = []
    for idx, case in enumerate(cases, start=1):
        print(f"[matrix] {idx}/{len(cases)} {case.scenario}/{case.business_profile} expected={case.expected_strategy}", flush=True)
        row = run_case(case, args)
        results.append(row)
        json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        render_markdown(results, md_path)
        result = row.get("result") or {}
        delta = row.get("qos_delta") or {}
        print(
            "[matrix] done "
            f"outcome={result.get('outcome') or ('error' if row.get('error') else '')} "
            f"strategy={row.get('strategy') or result.get('strategy')} "
            f"real={row.get('observed_is_real')} "
            f"converged={row.get('converged')} "
            f"qos_ok={(row.get('qos_non_regression') or {}).get('passed')} "
            f"total_tput_delta={delta.get('throughput_mbps_total_sum_delta')} "
            f"total_tput_pct={delta.get('throughput_mbps_total_sum_improvement_pct')} "
            f"error={row.get('error') or ''}",
            flush=True,
        )

    print(f"[matrix] json={json_path}")
    print(f"[matrix] md={md_path}")


if __name__ == "__main__":
    main()

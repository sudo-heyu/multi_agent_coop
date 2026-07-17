#!/usr/bin/env python3
"""Direct ns-3 scan for the six-scenario experiment design.

This script does not start OpenClaw negotiation.  It starts ns-3 live mode,
collects a baseline, directly writes candidate decisions to ns-3 stdin, and
collects post-APPLY QoS samples.  The resulting oracle answers are for offline
experiment calibration only; do not expose them to AP agents.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "openclaw" / "mcp"))

from experiments.run_ns3_fulltool_matrix import (  # noqa: E402
    collect_samples,
    qos_delta,
    qos_non_regression,
)
from run_openclaw import (  # noqa: E402
    Ns3LiveController,
    _wait_state_ready,
)


AP_IDS = ("ap1", "ap2", "ap3")


@dataclass(frozen=True)
class DirectCandidate:
    label: str
    strategy: str
    decision: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class DesignScenario:
    scenario_id: str
    family: str
    scenario: str
    business_profile: str
    extra_args: tuple[str, ...]
    role: str
    expected_strategy: str
    design_intent: str
    candidates: tuple[DirectCandidate, ...]


def sr_decision(tx: int) -> dict[str, Any]:
    return {
        "ap1": {"tx_power_dbm": tx},
        "ap2": {"tx_power_dbm": tx},
        "ap3": {"tx_power_dbm": tx},
        "_sr": {"concurrent_group": ["ap1", "ap2", "ap3"], "non_concurrent_aps": []},
    }


def edca_decision(
    *,
    high: tuple[int, int, int] = (7, 63, 2),
    medium: tuple[int, int, int] = (15, 127, 3),
    low: tuple[int, int, int] = (31, 1023, 5),
    vi_high: tuple[int, int, int] | None = None,
    vi_medium: tuple[int, int, int] | None = None,
    vi_low: tuple[int, int, int] | None = None,
    priority_by_ap: dict[str, str],
) -> dict[str, Any]:
    values = {"high": high, "medium": medium, "low": low}
    vi_values = {"high": vi_high, "medium": vi_medium, "low": vi_low}
    out: dict[str, Any] = {}
    for ap in AP_IDS:
        cwmin, cwmax, aifsn = values[priority_by_ap[ap]]
        row = {"CWmin": cwmin, "CWmax": cwmax, "AIFSN": aifsn}
        vi = vi_values[priority_by_ap[ap]]
        if vi is not None:
            vi_cwmin, vi_cwmax, vi_aifsn = vi
            row.update({"VI_CWmin": vi_cwmin, "VI_CWmax": vi_cwmax, "VI_AIFSN": vi_aifsn})
        out[ap] = row
    return out


def build_design() -> list[DesignScenario]:
    """Six scenario draft.

    Requirements:
    - three SR-led scenarios, one fuzzy SR scenario with non-uniform business;
    - three EDCA-led scenarios, one fuzzy EDCA scenario with non-line topology;
    - no agent-facing oracle information is emitted by this function.
    """
    edca_live_bulk = {"ap1": "low", "ap2": "high", "ap3": "medium"}
    edca_deadline = {"ap1": "high", "ap2": "medium", "ap3": "low"}
    edca_mixed = {"ap1": "high", "ap2": "high", "ap3": "low"}

    sr_candidates = tuple(
        DirectCandidate(
            label=f"tx{tx}",
            strategy="co_sr",
            decision=sr_decision(tx),
            reason=f"all AP TX power -> {tx} dBm",
        )
        for tx in (8, 10, 12, 14, 16, 18)
    )
    return [
        DesignScenario(
            scenario_id="sr_clear_dense_uniform",
            family="sr",
            scenario="triangle",
            business_profile="uniform",
            extra_args=("--spacing=16", "--txPowerDbm=20"),
            role="clear",
            expected_strategy="co_sr",
            design_intent="Symmetric strong OBSS interference; business is uniform so SR is the only intended lever.",
            candidates=sr_candidates,
        ),
        DesignScenario(
            scenario_id="sr_representative_uniform",
            family="sr",
            scenario="triangle",
            business_profile="uniform",
            extra_args=("--spacing=18", "--txPowerDbm=20"),
            role="representative",
            expected_strategy="co_sr",
            design_intent="Moderately dense uniform business; tests whether SR still helps outside the most congested clear case.",
            candidates=sr_candidates,
        ),
        DesignScenario(
            scenario_id="sr_fuzzy_mixed_business",
            family="sr",
            scenario="triangle",
            business_profile="mixed_qoe",
            extra_args=("--spacing=17", "--txPowerDbm=16"),
            role="fuzzy",
            expected_strategy="co_sr",
            design_intent="Business priorities differ, but topology-induced OBSS interference should still make SR the best answer.",
            candidates=sr_candidates,
        ),
        DesignScenario(
            scenario_id="edca_clear_line_deadline",
            family="edca",
            scenario="line",
            business_profile="live_bulk",
            extra_args=("--spacing=25", "--txPowerDbm=8"),
            role="clear",
            expected_strategy="co_edca",
            design_intent="Line topology with live/bulk business gradient; EDCA should protect live traffic without creating aggregate QoS regression.",
            candidates=(
                DirectCandidate("edca_gentle", "co_edca", edca_decision(
                    high=(15, 127, 2), medium=(15, 255, 3), low=(31, 1023, 5),
                    priority_by_ap=edca_live_bulk,
                ), "gentle high/medium/low EDCA split"),
                DirectCandidate("edca_strong", "co_edca", edca_decision(
                    high=(7, 63, 2), medium=(15, 127, 3), low=(31, 1023, 5),
                    priority_by_ap=edca_live_bulk,
                ), "strong high/medium/low EDCA split"),
                DirectCandidate("edca_tiny_bias", "co_edca", edca_decision(
                    high=(15, 255, 3), medium=(31, 511, 4), low=(31, 1023, 4),
                    priority_by_ap=edca_live_bulk,
                ), "minimal differentiation for live/bulk traffic"),
            ),
        ),
        DesignScenario(
            scenario_id="edca_representative_line_live_bulk",
            family="edca",
            scenario="line",
            business_profile="live_bulk",
            extra_args=("--spacing=25", "--txPowerDbm=9"),
            role="representative",
            expected_strategy="co_edca",
            design_intent="Moderate line topology with live/bulk traffic; tests whether EDCA remains useful under slightly higher transmit pressure.",
            candidates=(
                DirectCandidate("edca_gentle", "co_edca", edca_decision(
                    high=(15, 127, 2), medium=(15, 255, 3), low=(31, 1023, 5),
                    priority_by_ap=edca_live_bulk,
                ), "gentle high/medium/low EDCA split"),
                DirectCandidate("edca_strong", "co_edca", edca_decision(
                    high=(7, 63, 2), medium=(15, 127, 3), low=(31, 1023, 5),
                    priority_by_ap=edca_live_bulk,
                ), "strong high/medium/low EDCA split"),
                DirectCandidate("edca_tiny_bias", "co_edca", edca_decision(
                    high=(15, 255, 3), medium=(31, 511, 4), low=(31, 1023, 4),
                    priority_by_ap=edca_live_bulk,
                ), "minimal differentiation for live/bulk traffic"),
            ),
        ),
        DesignScenario(
            scenario_id="edca_fuzzy_triangle_deadline",
            family="edca",
            scenario="triangle",
            business_profile="deadline_backup",
            extra_args=("--spacing=36", "--txPowerDbm=8", "--cwmin=31", "--cwmax=1023", "--aifsn=4"),
            role="fuzzy",
            expected_strategy="co_edca",
            design_intent="Non-line triangle topology creates weak SR evidence, but deadline-driven business differentiation should still make EDCA optimal.",
            candidates=(
                DirectCandidate("edca_two_high_gentle", "co_edca", edca_decision(
                    high=(15, 127, 2), medium=(15, 255, 3), low=(31, 1023, 5),
                    priority_by_ap=edca_deadline,
                ), "two high-priority APs share better EDCA than the low-priority AP"),
                DirectCandidate("edca_two_high_strong", "co_edca", edca_decision(
                    high=(7, 63, 2), medium=(15, 127, 3), low=(31, 1023, 5),
                    priority_by_ap=edca_deadline,
                ), "strong split with two high-priority APs"),
                DirectCandidate("edca_tiny_bias", "co_edca", edca_decision(
                    high=(15, 255, 3), medium=(31, 511, 4), low=(31, 1023, 4),
                    priority_by_ap=edca_deadline,
                ), "minimal differentiation for mixed traffic"),
            ),
        ),
    ]


def score_delta(delta: dict[str, Any], strategy: str) -> float:
    """Offline oracle score: QoS non-regression first, then throughput, latency."""
    if not qos_non_regression(delta, strategy).get("passed"):
        return -1_000_000.0
    tput = float(delta.get("throughput_mbps_total_sum_improvement_pct") or 0.0)
    user = float(delta.get("throughput_mbps_user_sum_improvement_pct") or 0.0)
    latency = float(delta.get("latency_ms_avg_improvement_pct") or 0.0)
    loss = float(delta.get("packet_loss_pct_avg_improvement_pct") or 0.0)
    return round(tput * 10.0 + user * 3.0 + latency * 1.5 + loss * 5.0, 6)


def restore_decision_from_baseline(baseline: dict[str, Any], strategy: str) -> dict[str, Any]:
    per_ap = baseline.get("per_ap") or {}
    if strategy == "co_sr":
        return {
            ap: {"tx_power_dbm": per_ap.get(ap, {}).get("tx_power_dbm")}
            for ap in AP_IDS
            if per_ap.get(ap, {}).get("tx_power_dbm") is not None
        }
    if strategy == "co_edca":
        out: dict[str, Any] = {}
        for ap in AP_IDS:
            row = per_ap.get(ap, {})
            if all(row.get(k) is not None for k in ("cwmin", "cwmax", "aifsn")):
                out[ap] = {
                    "CWmin": int(row["cwmin"]),
                    "CWmax": int(row["cwmax"]),
                    "AIFSN": int(row["aifsn"]),
                }
        return out
    return {}


def require_state_server(url: str) -> None:
    import requests

    sess = requests.Session()
    sess.trust_env = False
    try:
        resp = sess.get(f"{url.rstrip('/')}/health", timeout=2)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"state server is not reachable at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise SystemExit(f"state server health check failed at {url}: HTTP {resp.status_code}")
    print(f"[State] using {url}", flush=True)


def run_direct_case(design: DesignScenario, args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    row: dict[str, Any] = {
        "scenario_id": design.scenario_id,
        "family": design.family,
        "role": design.role,
        "expected_strategy": design.expected_strategy,
        "ns3": {
            "scenario": design.scenario,
            "business_profile": design.business_profile,
            "extra_args": list(design.extra_args),
        },
        "design_intent": design.design_intent,
        "baseline": None,
        "candidates": [],
        "oracle": None,
        "error": None,
    }
    ctrl = Ns3LiveController(
        root=args.ns3_root,
        server=args.server,
        scenario=design.scenario,
        business_profile=design.business_profile,
        sim_time=args.ns3_sim_time,
        report_interval=args.ns3_report_interval,
        extra_args=list(design.extra_args),
        include_case_extra_args=False,
    )
    try:
        ctrl.start()
        if not ctrl.wait_until_ready(timeout_s=args.ready_timeout):
            raise RuntimeError("ns-3 did not produce initial TELEMETRY")
        _wait_state_ready(args.server, timeout_s=max(1.0, args.ns3_report_interval * 2.0))
        if args.warmup_seconds > 0:
            time.sleep(args.warmup_seconds)
        baseline = collect_samples(
            args.server,
            seconds=args.baseline_seconds,
            interval=args.sample_interval,
        )
        row["baseline"] = baseline
        restore = restore_decision_from_baseline(baseline, design.expected_strategy)
        best: dict[str, Any] | None = None
        for candidate in design.candidates:
            if restore:
                ctrl.apply_decision(restore, design.expected_strategy, "offline-oracle-reset")
                time.sleep(args.reset_seconds)
            push = ctrl.apply_decision(candidate.decision, candidate.strategy, "offline-oracle")
            if not push or not all(item.get("ok") for item in push.values()):
                result = {
                    "label": candidate.label,
                    "strategy": candidate.strategy,
                    "decision": candidate.decision,
                    "push_results": push,
                    "error": "APPLY failed",
                }
                row["candidates"].append(result)
                continue
            time.sleep(args.settle_seconds)
            after = collect_samples(
                args.server,
                seconds=args.after_seconds,
                interval=args.sample_interval,
            )
            delta = qos_delta(baseline, after)
            qok = qos_non_regression(delta, candidate.strategy)
            result = {
                "label": candidate.label,
                "strategy": candidate.strategy,
                "reason": candidate.reason,
                "decision": candidate.decision,
                "push_results": push,
                "after": after,
                "qos_delta": delta,
                "qos_non_regression": qok,
                "score": score_delta(delta, candidate.strategy),
            }
            row["candidates"].append(result)
            if best is None or result["score"] > best["score"]:
                best = result
        row["oracle"] = best
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ctrl.stop()
        row["duration_s"] = round(time.time() - started, 3)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--ns3-root", default="/Users/heyu/Developer/ns-3.47")
    parser.add_argument("--ns3-sim-time", type=float, default=420.0)
    parser.add_argument("--ns3-report-interval", type=float, default=1.0)
    parser.add_argument("--warmup-seconds", type=float, default=25.0)
    parser.add_argument("--baseline-seconds", type=float, default=8.0)
    parser.add_argument("--after-seconds", type=float, default=8.0)
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    parser.add_argument("--reset-seconds", type=float, default=3.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    parser.add_argument("--scenario-id", action="append")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default=str(REPO / "logs" / "private_oracle"))
    args = parser.parse_args()

    designs = build_design()
    if args.scenario_id:
        keep = set(args.scenario_id)
        designs = [d for d in designs if d.scenario_id in keep]
        missing = sorted(keep - {d.scenario_id for d in designs})
        if missing:
            raise SystemExit(f"unknown scenario-id(s): {missing}")

    if args.dry_run:
        print(json.dumps([
            {
                "scenario_id": d.scenario_id,
                "family": d.family,
                "role": d.role,
                "expected_strategy": d.expected_strategy,
                "ns3": {
                    "scenario": d.scenario,
                    "business_profile": d.business_profile,
                    "extra_args": list(d.extra_args),
                },
                "design_intent": d.design_intent,
                "candidate_labels": [c.label for c in d.candidates],
            }
            for d in designs
        ], ensure_ascii=False, indent=2))
        return

    require_state_server(args.server)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"ns3_six_scenario_oracle_{slug}.json"

    results: list[dict[str, Any]] = []
    for idx, design in enumerate(designs, start=1):
        print(f"[oracle] {idx}/{len(designs)} {design.scenario_id}", flush=True)
        row = run_direct_case(design, args)
        results.append(row)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        oracle = row.get("oracle") or {}
        delta = oracle.get("qos_delta") or {}
        print(
            "[oracle] done "
            f"best={oracle.get('label')} "
            f"score={oracle.get('score')} "
            f"qos_ok={(oracle.get('qos_non_regression') or {}).get('passed')} "
            f"tput_pct={delta.get('throughput_mbps_total_sum_improvement_pct')} "
            f"latency_pct={delta.get('latency_ms_avg_improvement_pct')} "
            f"error={row.get('error') or ''}",
            flush=True,
        )
    print(f"[oracle] json={out_path}")


if __name__ == "__main__":
    main()

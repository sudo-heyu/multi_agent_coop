#!/usr/bin/env python3
"""Tuned six-scenario ns-3 matrix for tool-level experiments.

The ns-3 scratch program still owns the simulation model.  This file records
only the six calibrated scenarios used by the current tool hierarchy
experiments: three SR-led cases and three EDCA-led cases.
"""
from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import dataclass


TOPOLOGIES: tuple[str, ...] = ("line", "triangle")
BUSINESS_PROFILES: tuple[str, ...] = (
    "live_bulk",
    "mixed_qoe",
    "deadline_backup",
    "uniform",
)


@dataclass(frozen=True)
class ScenarioCase:
    case_id: str
    family: str
    role: str
    scenario: str
    business_profile: str
    expected_strategy: str
    reason: str
    extra_args: tuple[str, ...] = ()

    def ns3_args(
        self,
        *,
        sim_time: float = 180.0,
        report_interval: float = 1.0,
        live: bool = True,
    ) -> list[str]:
        args = [
            f"--scenario={self.scenario}",
            f"--businessProfile={self.business_profile}",
            f"--simTime={sim_time:g}",
            f"--reportInterval={report_interval:g}",
        ]
        if live:
            args.insert(0, "--live=1")
        args.extend(self.extra_args)
        return args

    def ns3_run_command(
        self,
        *,
        sim_time: float = 180.0,
        report_interval: float = 1.0,
        live: bool = True,
    ) -> str:
        arg_string = " ".join(self.ns3_args(
            sim_time=sim_time,
            report_interval=report_interval,
            live=live,
        ))
        return f'./ns3 run {shlex.quote("scratch/multiap_coop/multiap_coop " + arg_string)}'


def build_matrix() -> list[ScenarioCase]:
    return [
        ScenarioCase(
            case_id="sr_clear_dense_uniform",
            family="sr",
            role="clear",
            scenario="triangle",
            business_profile="uniform",
            expected_strategy="co_sr",
            reason="Dense uniform business; SR power reduction is the intended control lever.",
            extra_args=("--spacing=16", "--txPowerDbm=20"),
        ),
        ScenarioCase(
            case_id="sr_representative_uniform",
            family="sr",
            role="representative",
            scenario="triangle",
            business_profile="uniform",
            expected_strategy="co_sr",
            reason="Moderately dense uniform business; SR should still help outside the most congested case.",
            extra_args=("--spacing=18", "--txPowerDbm=20"),
        ),
        ScenarioCase(
            case_id="sr_fuzzy_mixed_business",
            family="sr",
            role="fuzzy",
            scenario="triangle",
            business_profile="mixed_qoe",
            expected_strategy="co_sr",
            reason="Business priorities differ, but topology-induced OBSS interference should still make SR optimal.",
            extra_args=("--spacing=17", "--txPowerDbm=16"),
        ),
        ScenarioCase(
            case_id="edca_clear_line_deadline",
            family="edca",
            role="clear",
            scenario="line",
            business_profile="live_bulk",
            expected_strategy="co_edca",
            reason="Line topology with live/bulk business gradient; EDCA is the intended lever.",
            extra_args=("--spacing=25", "--txPowerDbm=8"),
        ),
        ScenarioCase(
            case_id="edca_representative_line_live_bulk",
            family="edca",
            role="representative",
            scenario="line",
            business_profile="live_bulk",
            expected_strategy="co_edca",
            reason="Moderate line topology with slightly higher TX pressure; EDCA should protect priority service.",
            extra_args=("--spacing=25", "--txPowerDbm=9"),
        ),
        ScenarioCase(
            case_id="edca_fuzzy_triangle_deadline",
            family="edca",
            role="fuzzy",
            scenario="triangle",
            business_profile="deadline_backup",
            expected_strategy="co_edca",
            reason="Non-line topology gives weak SR evidence, but deadline priorities keep EDCA optimal.",
            extra_args=("--spacing=36", "--txPowerDbm=8", "--cwmin=31", "--cwmax=1023", "--aifsn=4"),
        ),
    ]


def get_case_by_id(case_id: str) -> ScenarioCase:
    for case in build_matrix():
        if case.case_id == case_id:
            return case
    raise KeyError(f"unknown ns-3 case_id: {case_id}")


def get_case(scenario: str, business_profile: str) -> ScenarioCase:
    for case in build_matrix():
        if case.scenario == scenario and case.business_profile == business_profile:
            return case
    raise KeyError(f"unknown ns-3 case: {scenario}/{business_profile}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=TOPOLOGIES)
    parser.add_argument("--business-profile", choices=BUSINESS_PROFILES)
    parser.add_argument("--case-id")
    parser.add_argument("--sim-time", type=float, default=180.0)
    parser.add_argument("--report-interval", type=float, default=1.0)
    parser.add_argument("--static", action="store_true", help="omit --live=1")
    parser.add_argument("--json", action="store_true", help="print machine-readable matrix")
    args = parser.parse_args()

    cases = build_matrix()
    if args.case_id:
        cases = [get_case_by_id(args.case_id)]
    elif args.scenario or args.business_profile:
        if not (args.scenario and args.business_profile):
            parser.error("--scenario and --business-profile must be used together")
        cases = [get_case(args.scenario, args.business_profile)]

    rows = [
        {
            "scenario": case.scenario,
            "case_id": case.case_id,
            "family": case.family,
            "role": case.role,
            "businessProfile": case.business_profile,
            "expected_strategy": case.expected_strategy,
            "extra_args": list(case.extra_args),
            "reason": case.reason,
            "command": case.ns3_run_command(
                sim_time=args.sim_time,
                report_interval=args.report_interval,
                live=not args.static,
            ),
        }
        for case in cases
    ]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    for row in rows:
        print(
            f"{row['case_id']:<32} {row['scenario']:<8} {row['businessProfile']:<16} "
            f"{row['expected_strategy']:<7} {' '.join(row['extra_args'])}"
        )
        print(f"  {row['reason']}")
        print(f"  {row['command']}")


if __name__ == "__main__":
    main()

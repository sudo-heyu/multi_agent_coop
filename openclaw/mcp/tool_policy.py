"""Runtime policy for weakening Multi-AP tools during memory experiments.

The default profile keeps the current behavior.  Weaker profiles remove
"answer-like" tools from the agent-facing surface while keeping deterministic
validation and the final system Validator intact.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Iterable


PROFILE_ENV = "MULTIAP_TOOL_PROFILE"
PROFILES = (
    "none",
    "no_tools",
    "basic",
    "rich",
    "full",
    "diagnostic",
    "validator_only",
    "state_only",
    "memory_challenge",
)

ALL_TOOLS = (
    "get_latest_ap_states",
    "get_sta_feedback",
    "analyze_sr_interference",
    "compute_sr_feasible_ranges",
    "select_sr_concurrent_groups",
    "evaluate_sr_candidate",
    "rank_sr_candidates",
    "validate_edca_proposal",
)

ALLOWED_BY_PROFILE = {
    "none": set(),
    "no_tools": set(),
    # Three-level experiment names:
    #   none/basic/rich = no agent tools / facts+validators / full toolset.
    "basic": {
        "get_latest_ap_states",
        "get_sta_feedback",
        "evaluate_sr_candidate",
        "validate_edca_proposal",
    },
    "rich": set(ALL_TOOLS),
    "full": set(ALL_TOOLS),
    # Facts + candidate validation.  No direct SR solver/ranker.
    "diagnostic": set(ALL_TOOLS) - {
        "select_sr_concurrent_groups",
        "rank_sr_candidates",
    },
    # The agent must invent candidates from state, memory, and reasoning.
    "validator_only": {
        "get_latest_ap_states",
        "get_sta_feedback",
        "evaluate_sr_candidate",
        "validate_edca_proposal",
    },
    "state_only": {
        "get_latest_ap_states",
        "get_sta_feedback",
    },
    # Aggressive experiment profile: no answer-like tools and coarse state only.
    # This makes long-term memory carry more of the candidate-selection burden.
    "memory_challenge": {
        "get_latest_ap_states",
        "get_sta_feedback",
        "evaluate_sr_candidate",
        "validate_edca_proposal",
    },
}

COARSE_STATE_PROFILES = {"memory_challenge"}

_HIDDEN_STATE_FIELDS = (
    "tx_power_dbm",
    "cwmin",
    "cwmax",
    "aifsn",
    "be_cwmin",
    "be_cwmax",
    "be_aifsn",
    "vi_cwmin",
    "vi_cwmax",
    "vi_aifsn",
    "sta_rssi_dbm",
    "throughput_mbps_user",
    "latency_ms",
    "jitter_ms",
    "packet_loss_pct",
    "neighbor_rssi_dbm",
    "stas",
)


def current_profile() -> str:
    value = os.environ.get(PROFILE_ENV, "full").strip().lower()
    return value if value in PROFILES else "full"


def allowed_tools(profile: str | None = None) -> set[str]:
    selected = current_profile() if profile is None else str(profile).strip().lower()
    if selected not in ALLOWED_BY_PROFILE:
        selected = "full"
    return set(ALLOWED_BY_PROFILE[selected])


def visible_tool_names(names: Iterable[str], profile: str | None = None) -> tuple[str, ...]:
    allowed = allowed_tools(profile)
    return tuple(name for name in names if name in allowed)


def is_tool_allowed(name: str, profile: str | None = None) -> bool:
    return name in allowed_tools(profile)


def blocked_result(name: str) -> dict[str, Any]:
    profile = current_profile()
    return {
        "error": (
            f"工具 {name} 在 {PROFILE_ENV}={profile} 下不可用；"
            "请基于最新状态、历史记忆和可用验算工具自行提出候选。"
        ),
        "tool_profile": profile,
        "available": False,
    }


def coarsens_state(profile: str | None = None) -> bool:
    selected = current_profile() if profile is None else str(profile).strip().lower()
    return selected in COARSE_STATE_PROFILES


def _feedback_brief(row: dict[str, Any]) -> dict[str, Any]:
    summary = row.get("sta_feedback_summary") if isinstance(row, dict) else None
    summary = summary if isinstance(summary, dict) else {}
    violations = row.get("sla_violations") if isinstance(row, dict) else None
    violations = violations if isinstance(violations, list) else []
    return {
        "status": summary.get("status") or ("violated" if violations else "unknown"),
        "sta_count": summary.get("sta_count"),
        "violated_count": summary.get("violated_count", len(violations)),
        "warning_count": summary.get("warning_count", 0),
    }


def _neighbor_level(row: dict[str, Any]) -> str:
    neighbors = row.get("neighbor_rssi_dbm") if isinstance(row, dict) else None
    if not isinstance(neighbors, dict) or not neighbors:
        return "unknown"
    try:
        strongest = max(float(v) for v in neighbors.values())
    except (TypeError, ValueError):
        return "unknown"
    if strongest >= -67:
        return "strong"
    if strongest >= -78:
        return "moderate"
    return "weak"


def transform_agent_state(ap_states: Any, profile: str | None = None) -> Any:
    """Return the agent-visible AP state for the selected tool profile."""
    if not coarsens_state(profile) or not isinstance(ap_states, dict):
        return ap_states
    result: dict[str, Any] = {}
    for ap_id, row in ap_states.items():
        if not isinstance(row, dict):
            result[ap_id] = row
            continue
        result[ap_id] = {
            "service_name": row.get("service_name"),
            "business_type": row.get("business_type"),
            "traffic_priority": row.get("traffic_priority"),
            "qoe_summary": _feedback_brief(row),
            "neighbor_interference_level": _neighbor_level(row),
            "exact_parameters_visible": False,
            "hidden_fields": [
                field for field in _HIDDEN_STATE_FIELDS if field in row
            ],
            "policy_note": (
                "memory_challenge 档位隐藏精确 EDCA/TX/QoS/RSSI 数值；"
                "候选选择需依赖对话、记忆和后续负向校验。"
            ),
        }
    return result


def transform_sta_feedback(result: Any, profile: str | None = None) -> Any:
    if not coarsens_state(profile) or not isinstance(result, dict):
        return result
    value = copy.deepcopy(result)
    for item in (value.get("per_ap") or {}).values():
        if not isinstance(item, dict):
            continue
        item["metrics_visible"] = False
        item["policy_note"] = "memory_challenge 档位只保留 SLA 状态和违规摘要。"
        compact_stas = []
        for sta in item.get("stas") or []:
            if not isinstance(sta, dict):
                continue
            compact_stas.append({
                "sta_id": sta.get("sta_id"),
                "business_type": sta.get("business_type"),
                "traffic_priority": sta.get("traffic_priority"),
                "sla_status": sta.get("sla_status"),
                "violations": sta.get("violations") or [],
                "warnings": sta.get("warnings") or [],
            })
        item["stas"] = compact_stas
    value["policy_redactions"] = list(value.get("policy_redactions") or []) + [
        "sta_metrics",
    ]
    return value


def transform_result(name: str, result: Any) -> Any:
    """Redact answer-like fields in weak profiles.

    This intentionally does not affect final Validator behavior or execution
    telemetry.  It only changes what an AP agent can see while proposing/voting.
    """
    profile = current_profile()
    if profile == "full" or not isinstance(result, dict) or result.get("error"):
        return result

    value = copy.deepcopy(result)
    if name == "get_latest_ap_states":
        if "ap_states" in value:
            value["ap_states"] = transform_agent_state(value.get("ap_states"), profile)
            if coarsens_state(profile):
                value.setdefault("policy_redactions", []).append("ap_states.exact_metrics")
    elif name == "get_sta_feedback":
        value = transform_sta_feedback(value, profile)
    elif name == "compute_sr_feasible_ranges":
        value.pop("candidate_hints", None)
        for row in (value.get("ranges") or {}).values():
            if isinstance(row, dict):
                row.pop("recommended_obss_pd_dbm", None)
        value.setdefault("policy_redactions", []).extend([
            "candidate_hints",
            "ranges.*.recommended_obss_pd_dbm",
        ])
        value.setdefault("notes", []).append(
            "弱工具档位已移除候选答案提示；请自行构造候选并调用 evaluate_sr_candidate 验算。"
        )
    elif name == "evaluate_sr_candidate":
        original_valid = value.get("valid")
        if "score" in value:
            value["score_redacted"] = True
            value.pop("score", None)
        if original_valid is True:
            value["valid"] = "unknown"
            value["constraint_check_scope"] = "negative_only"
            value["policy_note"] = (
                "弱工具档位不确认候选最优或整体通过；这里只保留明显硬错误。"
                "未返回错误不代表方案效果好，需结合记忆和后续 Validator/评估窗口。"
            )
            for item in (value.get("per_ap") or {}).values():
                if isinstance(item, dict) and item.get("valid") is True:
                    item["valid"] = "unknown"
                    item["check_scope"] = "negative_only"
        value.setdefault("policy_redactions", []).append("score")
    elif name == "validate_edca_proposal":
        if "effectiveness" in value:
            value["effectiveness_redacted"] = True
            value.pop("effectiveness", None)
        if "safety_validation" in value:
            value["safety_validation_redacted"] = True
            value.pop("safety_validation", None)
        value.pop("all_ok", None)
        for key, item in list(value.items()):
            if not (isinstance(key, str) and key.lower().startswith("ap")):
                continue
            if not isinstance(item, dict):
                continue
            safety_errors = set(item.pop("safety_errors", []) or [])
            if safety_errors:
                item["errors"] = [
                    error for error in (item.get("errors") or [])
                    if error not in safety_errors
                ]
                item["valid"] = not item["errors"]
            if item.get("valid") is True:
                item["range_valid"] = True
                item["valid"] = "unknown"
                item["check_scope"] = "range_only_negative_confirmation"
        value.setdefault("policy_redactions", []).extend([
            "effectiveness",
            "safety_validation",
            "all_ok",
            "*.safety_errors",
        ])
        value["policy_note"] = (
            "弱工具档位只返回参数范围/格式合法性；优先级排序、QoE 权衡和历史效果"
            "需要结合状态与记忆自行判断，最终系统 Validator/评估窗口仍会验收。"
        )
    return value

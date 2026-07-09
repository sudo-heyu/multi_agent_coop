"""Direct in-process tool registry for the stream runtime.

OpenClaw agents reach these functions through MCP.  The stream runtime runs in
the same Python process as the relay, so it calls the same tool implementations
directly and keeps the outward tool names/schema identical.
"""
from __future__ import annotations

import inspect
import json
import os
import time
from collections.abc import Callable
from typing import Any

try:
    import tool_policy
except ImportError:  # pragma: no cover - package import fallback
    from . import tool_policy  # type: ignore


TOOL_NAMES = (
    "get_latest_ap_states",
    "get_sta_feedback",
    "analyze_sr_interference",
    "compute_sr_feasible_ranges",
    "select_sr_concurrent_groups",
    "evaluate_sr_candidate",
    "rank_sr_candidates",
    "validate_edca_proposal",
)


TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_latest_ap_states": (
        "获取所有 AP 的最新参数状态，包括 TX Power、EDCA、邻居/STA RSSI、"
        "用户吞吐、时延、丢包与业务优先级。提案和投票验算前应先调用。"
    ),
    "get_sta_feedback": (
        "获取 STA 侧 QoE/SLA 反馈。可按 ap_id 过滤，可只返回 violated_only。"
    ),
    "analyze_sr_interference": (
        "分析 Co-SR 干扰关系，返回 AP 间干扰矩阵、强/中干扰链路和 co_sr_triggered。"
    ),
    "compute_sr_feasible_ranges": (
        "计算每个 AP 的 Co-SR TX Power 可行区间和 OBSS_PD 提示。"
    ),
    "select_sr_concurrent_groups": (
        "选择 Co-SR 空间复用并发组，并给出推荐功率和 non_concurrent_aps。"
    ),
    "evaluate_sr_candidate": (
        "评估候选 Co-SR 功率方案是否满足 CCA/SINR/STA RSSI 约束。"
    ),
    "rank_sr_candidates": "对多个 Co-SR 候选功率方案按目标排序。",
    "validate_edca_proposal": (
        "校验 EDCA 提案范围和 high/medium/low 优先级单调性。"
    ),
}


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_latest_ap_states": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "get_sta_feedback": {
        "type": "object",
        "properties": {
            "ap_id": {
                "type": "string",
                "enum": ["ap1", "ap2", "ap3"],
                "description": "可选；只返回某个 AP 的 STA 反馈。",
            },
            "violated_only": {
                "type": "boolean",
                "description": "是否只返回违反 SLA 的 STA。",
            },
        },
        "additionalProperties": False,
    },
    "analyze_sr_interference": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "compute_sr_feasible_ranges": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "select_sr_concurrent_groups": {
        "type": "object",
        "properties": {
            "min_group_size": {
                "type": "integer",
                "minimum": 2,
                "maximum": 3,
                "default": 2,
            }
        },
        "additionalProperties": False,
    },
    "evaluate_sr_candidate": {
        "type": "object",
        "properties": {
            "proposed_powers": {
                "type": ["object", "string"],
                "description": (
                    '功率映射，如 {"ap1": 7, "ap2": 7, "ap3": 8}；'
                    "也可传完整提案 JSON 字符串。"
                ),
            },
            "concurrent_group": {
                "type": ["array", "string"],
                "items": {"type": "string", "enum": ["ap1", "ap2", "ap3"]},
            },
        },
        "required": ["proposed_powers"],
        "additionalProperties": False,
    },
    "rank_sr_candidates": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "object",
                "description": (
                    '如 {"balanced": {"ap1": 7, "ap2": 7, "ap3": 8}}'
                ),
            },
            "objective": {
                "type": "string",
                "enum": [
                    "balanced",
                    "minimize_total_drop",
                    "minimize_max_drop",
                    "maximize_sta_margin",
                ],
                "default": "balanced",
            },
        },
        "required": ["candidates"],
        "additionalProperties": False,
    },
    "validate_edca_proposal": {
        "type": "object",
        "properties": {
            "proposed_edca": {
                "type": ["object", "string"],
                "description": (
                    'EDCA 提案，如 {"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}'
                ),
            },
        },
        "required": ["proposed_edca"],
        "additionalProperties": False,
    },
}


def openai_tools() -> list[dict[str, Any]]:
    """Return OpenAI-compatible tool schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "parameters": TOOL_SCHEMAS[name],
            },
        }
        for name in tool_policy.visible_tool_names(TOOL_NAMES)
    ]


def _tool_module():
    import multiap_mcp

    # Keep direct calls aligned with the run.py selected state server even if the
    # MCP module was imported before MULTIAP_STATE_SERVER changed.
    multiap_mcp.STATE_SERVER = os.environ.get(
        "MULTIAP_STATE_SERVER", multiap_mcp.STATE_SERVER
    )
    return multiap_mcp


def _normalize_args(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    raise TypeError("tool arguments must be a JSON object")


def call_tool(name: str, arguments: dict[str, Any] | str | None = None) -> tuple[Any, float]:
    """Call a direct tool and return (result, duration_ms)."""
    if name not in TOOL_NAMES:
        raise KeyError(f"unknown multiap tool: {name}")
    if not tool_policy.is_tool_allowed(name):
        return tool_policy.blocked_result(name), 0.0
    args = _normalize_args(arguments)
    module = _tool_module()
    fn: Callable[..., Any] = getattr(module, name)
    signature = inspect.signature(fn)
    accepted = {
        key: value for key, value in args.items()
        if key in signature.parameters
    }
    started = time.perf_counter()
    result = fn(**accepted)
    result = tool_policy.transform_result(name, result)
    return result, (time.perf_counter() - started) * 1000.0

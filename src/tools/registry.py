"""
工具注册中心

定义所有 agent 可调用工具的 JSON Schema（OpenAI / Ollama 兼容格式），
以及将 schema 与实际 Python 函数绑定的执行器工厂。

用法：
    from src.tools.registry import TOOL_DEFINITIONS, make_executor

    executor = make_executor(ap_states)
    result_json = executor("evaluate_sr_candidate", {"proposed_powers": {...}})
"""
import json
import time as _time
from collections.abc import Callable

from . import sr as _sr
from . import edca as _edca

_EDCA_KEYS = ("CWmin", "CWmax", "AIFSN")


def _edca_from_proposal(proposal: dict | None) -> dict:
    """从协商提案中提取各 AP 的 EDCA 字段：{ap_id: {CWmin, CWmax, AIFSN}}。"""
    if not proposal:
        return {}
    out = {}
    for ap_id, params in proposal.items():
        if isinstance(params, dict) and any(k in params for k in _EDCA_KEYS):
            out[ap_id] = {k: params[k] for k in _EDCA_KEYS if k in params}
    return out


def _powers_from_proposal(proposal: dict | None) -> dict:
    """从协商提案中提取各 AP 的发射功率：{ap_id: tx_power_dbm}。"""
    if not proposal:
        return {}
    return {
        ap_id: params["tx_power_dbm"]
        for ap_id, params in proposal.items()
        if isinstance(params, dict) and "tx_power_dbm" in params
    }


def _concurrent_group_from_proposal(proposal: dict | None) -> list[str]:
    if not isinstance(proposal, dict):
        return []
    meta = proposal.get("_sr") or proposal.get("sr") or {}
    if isinstance(meta, dict):
        group = meta.get("concurrent_group") or meta.get("concurrent_aps")
        if isinstance(group, list):
            return [str(ap).lower() for ap in group]
    group = proposal.get("concurrent_group")
    if isinstance(group, list):
        return [str(ap).lower() for ap in group]
    return []


# ------------------------------------------------------------------
# 工具 JSON Schema 定义
# ------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_latest_ap_states",
            "description": (
                "获取所有 AP 的最新参数状态。"
                "在提案阶段调用任何计算工具前、投票阶段调用任何验算工具前，"
                "必须先调用此工具，确保后续计算和验算基于最新 TX Power、EDCA、"
                "信道占用、重传率、邻居 RSSI、STA RSSI 等状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_sr_interference",
            "description": (
                "分析 Co-SR 干扰关系，只返回事实和风险，不给出最终功率建议。"
                "输出 AP 间干扰矩阵、strong/moderate 链路、主要干扰源和主要受害 AP。"
                "在 Co-SR 或联合协商的提案阶段，获取最新状态后优先调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_sr_feasible_ranges",
            "description": (
                "计算每个 AP 的 Co-SR TX Power 可行区间。"
                "区间来自法定功率上下限、STA RSSI 安全下界和 CCA 上界；"
                "SINR 是 AP 间耦合约束，候选方案仍需调用 evaluate_sr_candidate 验证。"
                "该工具提供协商空间，不直接给唯一最终建议。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_sr_concurrent_groups",
            "description": (
                "先选择 Co-SR 空间复用并发组，再给出组内推荐功率。"
                "用于支持部分并发：例如最远的 AP1/AP3 进入 concurrent_group，"
                "中间强干扰 AP2 放入 non_concurrent_aps，不参与同一并发时刻。"
                "该工具会枚举二元/三元并发组，返回可行组、推荐 tx_power_dbm、"
                "组外 AP 以及排序后的 best_group。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_group_size": {
                        "type": "integer",
                        "description": "最小并发组大小，默认 2。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_sr_candidate",
            "description": (
                "评估一个候选 Co-SR TX Power 方案是否满足 CCA / SINR / STA RSSI 三重约束，"
                "并返回总降功率、最大单 AP 降功率、STA RSSI 余量等代价指标。"
                "提案方生成候选后必须调用；投票方验算提案时也调用。"
                "【提案/自检阶段】必须显式传入 proposed_powers（你打算提出的功率），"
                "否则会被当成空提案、退化为验算当前功率，结果无意义。"
                "【投票阶段】才可省略 proposed_powers，省略即自动验算当前被投票的提案。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_powers": {
                        "type": "object",
                        "description": (
                            "候选功率（dBm），键名为小写 ap_id。"
                            '例如：{"ap1": 7.0, "ap2": 7.0, "ap3": 8.0}。'
                            "提案/自检阶段必须传入；仅投票阶段验算当前提案时可省略。"
                        ),
                        "additionalProperties": {"type": "number"},
                    },
                    "concurrent_group": {
                        "type": "array",
                        "description": (
                            "可选。指定本次只验算哪些 AP 组成的部分并发组，"
                            '例如 ["ap1", "ap3"]。省略时按旧逻辑验算所有 AP 同时并发。'
                        ),
                        "items": {"type": "string"},
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank_sr_candidates",
            "description": (
                "对多个 Co-SR 候选功率方案排序。"
                "输入多个候选方案，工具逐一评估约束和代价，按 objective 返回排序。"
                "用于让 AP agent 比较不同协商方案，而不是依赖唯一最优解。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "object",
                        "description": (
                            "候选方案集合。推荐格式："
                            '{"balanced": {"ap1": 7.0, "ap2": 7.0, "ap3": 8.0}, '
                            '"protect_ap3": {"ap1": 6.5, "ap2": 7.0, "ap3": 9.0}}'
                        ),
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                        },
                    },
                    "objective": {
                        "type": "string",
                        "description": (
                            "排序目标：balanced / minimize_total_drop / "
                            "minimize_max_drop / maximize_sta_margin"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_edca_proposal",
            "description": (
                "校验提案中各 AP 的 EDCA 参数。执行两项检查：\n"
                "① 范围合规：CWmin ∈ [3, 1023]、CWmax ∈ [7, 1023]、AIFSN ∈ [1, 15]、CWmax > CWmin。\n"
                "② 优先级排序：根据各 AP 状态中的 traffic_priority 字段，"
                "校验不同优先级的 AP 之间 CWmin 和 AIFSN 是否满足单调性：\n"
                "  高优先级（high）业务应使用更小的 CWmin 和 AIFSN，以更早接入信道、降低时延；\n"
                "  低优先级（low）业务应使用更大的 CWmin 和 AIFSN，主动礼让信道竞争机会；\n"
                "  要求 high.CWmin ≤ medium.CWmin ≤ low.CWmin，AIFSN 同理。\n"
                "【提案/自检阶段】必须显式传入 proposed_edca（你打算提出的 EDCA 参数），"
                "否则会因参数缺失而无法验算。\n"
                "【投票阶段】才可省略 proposed_edca，省略即自动验算当前被投票的提案。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_edca": {
                        "type": "object",
                        "description": (
                            "各 AP 的 EDCA 参数，键名为小写 ap_id。"
                            '例如：{"ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3}, ...}。'
                            "提案/自检阶段必须传入；仅投票阶段验算当前提案时可省略。"
                        ),
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "CWmin": {"type": "integer"},
                                "CWmax": {"type": "integer"},
                                "AIFSN": {"type": "integer"},
                            },
                        },
                    }
                },
                "required": [],
            },
        },
    },
]

# 按名称快速查找
TOOL_NAMES: set[str] = {t["function"]["name"] for t in TOOL_DEFINITIONS}


# ------------------------------------------------------------------
# 工具执行器工厂
# ------------------------------------------------------------------

def make_executor(
    ap_states: dict,
    state_getter: Callable[[], dict] | None = None,
    state_setter: Callable[[dict], None] | None = None,
    proposal: dict | None = None,
):
    """
    返回绑定了当前 ap_states 的工具执行函数。

    proposal：本轮协商中正在被验算的提案（顶层键为 ap1/ap2/ap3）。
        投票阶段由 orchestrator 注入；验算工具据此校验真实提案，
        无需模型在 tool_args 中重新序列化提案 JSON（模型对此不可靠）。

    返回的 executor 签名：
        executor(tool_name: str, tool_args: dict) -> tuple[dict, float]
            返回 (result_dict, duration_ms)
    """

    current_ap_states = ap_states

    def _set_current(new_states: dict) -> None:
        nonlocal current_ap_states
        current_ap_states = new_states
        if state_setter is not None:
            state_setter(new_states)

    def executor(tool_name: str, tool_args: dict) -> tuple[dict, float]:
        t0 = _time.time()

        if tool_name == "get_latest_ap_states":
            if state_getter is not None:
                try:
                    latest = state_getter()
                    _set_current(latest)
                    result = {
                        "ok": True,
                        "source": "state_getter",
                        "ap_states": latest,
                    }
                except Exception as exc:
                    result = {
                        "ok": False,
                        "error": f"获取最新 AP 状态失败: {exc}",
                        "ap_states": current_ap_states,
                    }
            else:
                result = {
                    "ok": True,
                    "source": "current_snapshot",
                    "ap_states": current_ap_states,
                }

        elif tool_name == "analyze_sr_interference":
            result = _sr.analyze_interference(current_ap_states)

        elif tool_name == "compute_sr_feasible_ranges":
            result = _sr.compute_feasible_ranges(current_ap_states)

        elif tool_name == "select_sr_concurrent_groups":
            result = _sr.select_concurrent_groups(
                current_ap_states,
                int(tool_args.get("min_group_size", 2)),
            )

        elif tool_name == "evaluate_sr_candidate":
            proposed = tool_args.get("proposed_powers") or _powers_from_proposal(proposal)
            proposed = {k.lower(): float(v) for k, v in proposed.items()}
            group = tool_args.get("concurrent_group") or _concurrent_group_from_proposal(proposal)
            result = _sr.evaluate_candidate_for_group(current_ap_states, proposed, group or None)

        elif tool_name == "rank_sr_candidates":
            candidates = tool_args.get("candidates", {})
            objective = tool_args.get("objective", "balanced")
            result = _sr.rank_candidates(current_ap_states, candidates, objective)

        elif tool_name == "validate_sr_proposal":
            proposed = tool_args.get("proposed_powers") or _powers_from_proposal(proposal)
            # 规范化：AP1 → ap1
            proposed = {k.lower(): float(v) for k, v in proposed.items()}
            group = tool_args.get("concurrent_group") or _concurrent_group_from_proposal(proposal)
            result = _sr.evaluate_candidate_for_group(current_ap_states, proposed, group or None)

        elif tool_name == "validate_edca_proposal":
            proposed_edca = tool_args.get("proposed_edca") or _edca_from_proposal(proposal)
            if not proposed_edca:
                # 参数缺失时给出明确指导
                result = {
                    "error": (
                        "validate_edca_proposal 需要 proposed_edca 参数。"
                        "请在工具参数中显式传入每个 AP 的 EDCA 值，例如："
                        '{"proposed_edca": {"ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3}, ...}}'
                    ),
                    "ap_entries": [],
                }
            else:
                result = {}
                for ap_id, params in proposed_edca.items():
                    valid, errors = _edca.validate(params)
                    result[ap_id.lower()] = {"valid": valid, "errors": errors, **params}
                result["effectiveness"] = _edca.evaluate_edca_effectiveness(
                    current_ap_states, proposed_edca
                )

        else:
            result = {"error": f"未知工具: {tool_name!r}"}

        duration_ms = (_time.time() - t0) * 1000
        return result, round(duration_ms, 1)

    return executor

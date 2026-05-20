"""
工具注册中心

定义所有 agent 可调用工具的 JSON Schema（OpenAI / Ollama 兼容格式），
以及将 schema 与实际 Python 函数绑定的执行器工厂。

用法：
    from src.tools.registry import TOOL_DEFINITIONS, make_executor

    executor = make_executor(ap_states)
    result_json = executor("compute_sr_recommendations", {})
"""
import json
import time as _time

from . import sr as _sr
from . import edca as _edca

# ------------------------------------------------------------------
# 工具 JSON Schema 定义
# ------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "compute_sr_recommendations",
            "description": (
                "计算 Co-SR 推荐发射功率。"
                "分析所有 AP 间的干扰矩阵，扫描满足 CCA（< -82 dBm）/ "
                "SINR（≥ 15 dB）/ STA-RSSI（≥ -75 dBm）三重约束的最高可行统一功率，"
                "返回每个 AP 的推荐 TX Power（dBm）及约束验算摘要。"
                "在 Co-SR 或联合协商的提案阶段调用此工具。"
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
            "name": "compute_edca_recommendations",
            "description": (
                "计算 Co-EDCA 推荐参数。"
                "根据各 AP 的信道占用率（channel_busy_ratio）和重传率（tx_retries_ratio）"
                "判断拥塞等级（low / medium / high / critical），"
                "返回每个 AP 推荐的 CWmin / CWmax / AIFSN。"
                "在 Co-EDCA 或联合协商的提案阶段调用此工具。"
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
            "name": "validate_sr_proposal",
            "description": (
                "验证提案中各 AP 的 TX Power 是否满足物理约束："
                "CCA < -82 dBm（邻居不触发 CCA）、SINR ≥ 15 dB（STA 链路质量）、"
                "STA RSSI ≥ -75 dBm（关联安全下界）。"
                "在投票验算阶段或提案修订后的自检中调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_powers": {
                        "type": "object",
                        "description": (
                            "各 AP 提案功率（dBm），键名为小写 ap_id。"
                            '例如：{"ap1": 10.0, "ap2": 12.0, "ap3": 16.0}'
                        ),
                        "additionalProperties": {"type": "number"},
                    }
                },
                "required": ["proposed_powers"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_edca_proposal",
            "description": (
                "验证提案中各 AP 的 EDCA 参数是否在合法范围内："
                "CWmin ∈ [3, 1023]、CWmax ∈ [7, 1023]、AIFSN ∈ [1, 15]、CWmax > CWmin。"
                "在投票验算阶段或提案修订后的自检中调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposed_edca": {
                        "type": "object",
                        "description": (
                            "各 AP 的 EDCA 参数，键名为小写 ap_id。"
                            '例如：{"ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3}, ...}'
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
                "required": ["proposed_edca"],
            },
        },
    },
]

# 按名称快速查找
TOOL_NAMES: set[str] = {t["function"]["name"] for t in TOOL_DEFINITIONS}


# ------------------------------------------------------------------
# 工具执行器工厂
# ------------------------------------------------------------------

def make_executor(ap_states: dict):
    """
    返回绑定了当前 ap_states 的工具执行函数。

    返回的 executor 签名：
        executor(tool_name: str, tool_args: dict) -> tuple[dict, float]
            返回 (result_dict, duration_ms)
    """

    def executor(tool_name: str, tool_args: dict) -> tuple[dict, float]:
        t0 = _time.time()

        if tool_name == "compute_sr_recommendations":
            raw = _sr.compute_all(ap_states)
            # 去掉冗长的 scan_log
            raw.pop("scan_log", None)
            # 去掉 cca_detail（太长）
            for v in raw.get("validation", {}).values():
                v.pop("cca_detail", None)
            result = raw

        elif tool_name == "compute_edca_recommendations":
            result = _edca.compute_all(ap_states)

        elif tool_name == "validate_sr_proposal":
            proposed = tool_args.get("proposed_powers", {})
            # 规范化：AP1 → ap1
            proposed = {k.lower(): float(v) for k, v in proposed.items()}
            details = _sr.compute_validation(ap_states, proposed)
            for v in details.values():
                v.pop("cca_detail", None)
            result = details

        elif tool_name == "validate_edca_proposal":
            proposed_edca = tool_args.get("proposed_edca", {})
            result = {}
            for ap_id, params in proposed_edca.items():
                valid, errors = _edca.validate(params)
                result[ap_id.lower()] = {"valid": valid, "errors": errors, **params}

        else:
            result = {"error": f"未知工具: {tool_name!r}"}

        duration_ms = (_time.time() - t0) * 1000
        return result, round(duration_ms, 1)

    return executor

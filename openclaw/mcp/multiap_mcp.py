"""
Multi-AP 协商工具 —— OpenClaw MCP 工具服务（stdio）。

把现有 Python 计算/验算/状态/下发逻辑原样暴露为 OpenClaw agent 可调用的工具。
设计为「无进程内会话状态」：每次调用都从状态服务器实时拉取 AP 状态并 apply_profile，
因此同一工具被 coordinator / ap1 / ap2 / ap3 各自独立的 `openclaw agent` 进程调用时，
看到的都是同一份外部真值（状态服务器 + mock 喂数器），结果可复现。

环境变量：
  MULTIAP_STATE_SERVER   状态服务器地址（默认 http://localhost:5001）
  MULTIAP_PROFILE        驱动子 agent 时使用的 openclaw profile（默认 multiap）
  OPENCLAW_BIN           openclaw 可执行文件路径（默认 ~/.openclaw/bin/openclaw）

注册（在 multiap profile 下）：
  openclaw --profile multiap mcp set multiap-tools \
    '{"command":"<python>","args":["<repo>/openclaw/mcp/multiap_mcp.py"]}'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# 让本脚本无论从何处启动都能 import 仓库 src 包与同目录 orchestration
REPO_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp.server.fastmcp import FastMCP


from src.tools import sr as _sr
from src.tools import edca as _edca
from src.tools.edca import encode_params_edca
from src.validator import validate_decision as _validate_decision
from src.profile import apply_profile, agent_view
from src.state_client import get_all_states, StateStaleError

STATE_SERVER = os.environ.get("MULTIAP_STATE_SERVER", "http://localhost:5001")
PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
OPENCLAW_BIN = os.environ.get(
    "OPENCLAW_BIN", str(Path.home() / ".openclaw" / "bin" / "openclaw")
)

mcp = FastMCP("multiap-tools")


# ──────────────────────────────────────────────────────────────────────
# 状态获取（所有工具的共享真值入口）
# ──────────────────────────────────────────────────────────────────────

def _full_state() -> dict:
    """从状态服务器拉取全部 AP 状态并应用业务画像 + 字段白名单（含内部字段）。"""
    return apply_profile(get_all_states(STATE_SERVER))


def _lower_power_map(powers: dict) -> dict:
    return {str(k).lower(): float(v) for k, v in (powers or {}).items()}


def _guard(fn):
    """工具异常时返回结构化错误，避免中断 agent 回合。"""
    try:
        return fn()
    except StateStaleError as exc:
        return {"error": f"状态服务器数据缺失或过期: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# ──────────────────────────────────────────────────────────────────────
# AP 通用工具（提案 / 投票阶段使用）
# ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_latest_ap_states() -> dict:
    """获取所有 AP 的最新参数状态（TX Power、EDCA、邻居/STA RSSI、用户吞吐等）。
    提案阶段调用任何计算工具前、投票阶段调用任何验算工具前必须先调用。"""
    return _guard(lambda: {"ok": True, "source": "state_server",
                           "ap_states": agent_view(_full_state())})


@mcp.tool()
def analyze_sr_interference() -> dict:
    """分析 Co-SR 干扰关系：AP 间干扰矩阵、strong/moderate 链路、主要干扰源与受害 AP。
    只返回事实与风险，不给最终功率建议。"""
    return _guard(lambda: _sr.analyze_interference(_full_state()))


@mcp.tool()
def compute_sr_feasible_ranges() -> dict:
    """计算每个 AP 的 Co-SR TX Power 可行区间（法定上下限 + STA RSSI 安全下界 + CCA 上界）。"""
    return _guard(lambda: _sr.compute_feasible_ranges(_full_state()))


@mcp.tool()
def select_sr_concurrent_groups(min_group_size: int = 2) -> dict:
    """选择 Co-SR 空间复用并发组并给出组内推荐功率，支持部分并发（强干扰 AP 退出并发组）。"""
    return _guard(lambda: _sr.select_concurrent_groups(_full_state(), int(min_group_size)))


@mcp.tool()
def evaluate_sr_candidate(proposed_powers: dict, concurrent_group: list[str] | None = None) -> dict:
    """评估候选 Co-SR 功率方案是否满足 CCA / SINR / STA RSSI 三重约束，并返回代价指标。
    proposed_powers 形如 {"ap1": 7.0, "ap2": 7.0, "ap3": 8.0}（dBm）。
    concurrent_group 可选，如 ["ap1","ap3"] 表示只验算这组的部分并发。"""
    def _run() -> dict:
        state = _full_state()
        proposed = _lower_power_map(proposed_powers)
        group = [str(a).lower() for a in concurrent_group] if concurrent_group else None
        return _sr.evaluate_candidate_for_group(state, proposed, group)
    return _guard(_run)


@mcp.tool()
def rank_sr_candidates(candidates: dict, objective: str = "balanced") -> dict:
    """对多个 Co-SR 候选功率方案按目标排序。
    candidates 形如 {"balanced": {"ap1":7.0,...}, "protect_ap3": {...}}。
    objective: balanced / minimize_total_drop / minimize_max_drop / maximize_sta_margin。"""
    return _guard(lambda: _sr.rank_candidates(_full_state(), candidates or {}, objective))


@mcp.tool()
def validate_edca_proposal(proposed_edca: dict) -> dict:
    """校验各 AP 的 EDCA 参数：范围合规（CWmin∈[3,1023], CWmax∈[7,1023], AIFSN∈[1,15], CWmax>CWmin）
    + 按 traffic_priority 的优先级单调性（high.CWmin ≤ medium ≤ low，AIFSN 同理），并评估拥塞匹配度。
    proposed_edca 形如 {"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}。"""
    def _run() -> dict:
        if not proposed_edca:
            return {"error": "需要 proposed_edca 参数，例如 "
                             '{"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}'}
        state = _full_state()
        result: dict = {}
        for ap_id, params in proposed_edca.items():
            valid, errors = _edca.validate(params)
            result[str(ap_id).lower()] = {"valid": valid, "errors": errors, **params}
        result["effectiveness"] = _edca.evaluate_edca_effectiveness(state, proposed_edca)
        return result
    return _guard(_run)


# ──────────────────────────────────────────────────────────────────────
# Coordinator 专用工具（编排 / 验收 / 下发 / 驱动子 agent）
# ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def validate_decision(decision: dict, strategy: str) -> dict:
    """确定性 Validator：对最终决策 JSON 做下发验收（参数范围 + 整数功率约束）。
    strategy 取值 co_sr / co_edca / joint。返回 approved 与逐 AP 校验明细。"""
    def _run() -> dict:
        state = _full_state()
        return _validate_decision(state, decision, strategy,
                                  observed_state=state, observed_is_real=False)
    return _guard(_run)


@mcp.tool()
def push_decision(decision: dict, strategy: str, endpoints: dict) -> dict:
    """把最终决策并发推送到各香蕉派执行服务。
    endpoints 形如 {"ap1":"http://192.168.1.1:5002", ...}；EDCA 发送前转为指数 n。"""
    def _run() -> dict:
        import requests
        out: dict = {}
        for ap_id, url in (endpoints or {}).items():
            params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
            params = encode_params_edca(params)
            payload = {"strategy": strategy, "ap_id": ap_id, "params": params}
            try:
                r = requests.post(f"{url.rstrip('/')}/apply", json=payload, timeout=8)
                out[ap_id] = {"ok": r.status_code == 200,
                              "response": (r.text or "")[:300]}
            except Exception as exc:  # noqa: BLE001
                out[ap_id] = {"ok": False, "response": str(exc)}
        return {"results": out}
    return _guard(_run)


if __name__ == "__main__":
    mcp.run()

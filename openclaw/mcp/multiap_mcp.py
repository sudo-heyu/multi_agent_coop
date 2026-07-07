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
import shutil
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
from src.profile import apply_profile, agent_view
from src.state_client import get_all_states, StateStaleError
import orchestration as _orch

STATE_SERVER = os.environ.get("MULTIAP_STATE_SERVER", "http://localhost:5001")
PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN")
    or shutil.which("openclaw")
    or str(Path.home() / ".openclaw" / "bin" / "openclaw")
)

mcp = FastMCP("multiap-tools")


# ──────────────────────────────────────────────────────────────────────
# 状态获取（所有工具的共享真值入口）
# ──────────────────────────────────────────────────────────────────────

def _full_state() -> dict:
    """从状态服务器拉取全部 AP 状态并应用字段白名单与保守默认值（含内部字段）。"""
    return apply_profile(get_all_states(STATE_SERVER))


def _lower_power_map(powers: dict) -> dict:
    return {str(k).lower(): float(v) for k, v in (powers or {}).items()}


def _current_proposal() -> dict:
    raw = os.environ.get("MULTIAP_CURRENT_PROPOSAL", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _edca_from_proposal(proposal: dict | None) -> dict:
    if not proposal:
        return {}
    edca_keys = (
        "CWmin", "CWmax", "AIFSN", "cwmin", "cwmax", "aifsn",
        "BE_CWmin", "BE_CWmax", "BE_AIFSN", "be_cwmin", "be_cwmax", "be_aifsn",
        "VI_CWmin", "VI_CWmax", "VI_AIFSN", "vi_cwmin", "vi_cwmax", "vi_aifsn",
    )
    out: dict = {}
    for ap_id, params in proposal.items():
        if isinstance(params, dict) and any(k in params for k in edca_keys):
            out[str(ap_id).lower()] = {
                k: params[k]
                for k in edca_keys
                if k in params
            }
    return out


def _powers_from_proposal(proposal: dict | None) -> dict:
    if not proposal:
        return {}
    return {
        str(ap_id).lower(): params["tx_power_dbm"]
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
def evaluate_sr_candidate(
    proposed_powers: dict | None = None,
    concurrent_group: list[str] | None = None,
) -> dict:
    """评估候选 Co-SR 功率方案是否满足 CCA / SINR / STA RSSI 三重约束，并返回代价指标。
    proposed_powers 形如 {"ap1": 7.0, "ap2": 7.0, "ap3": 8.0}（dBm）。
    concurrent_group 可选，如 ["ap1","ap3"] 表示只验算这组的部分并发。"""
    def _run() -> dict:
        state = _full_state()
        proposal = _current_proposal()
        proposed = _lower_power_map(proposed_powers or _powers_from_proposal(proposal))
        if not proposed:
            return {
                "error": (
                    "需要 proposed_powers 参数；投票阶段也可由编排器通过 "
                    "MULTIAP_CURRENT_PROPOSAL 注入当前提案。"
                )
            }
        group = (
            [str(a).lower() for a in concurrent_group]
            if concurrent_group else _concurrent_group_from_proposal(proposal)
        )
        return _sr.evaluate_candidate_for_group(state, proposed, group)
    return _guard(_run)


@mcp.tool()
def rank_sr_candidates(candidates: dict, objective: str = "balanced") -> dict:
    """对多个 Co-SR 候选功率方案按目标排序。
    candidates 形如 {"balanced": {"ap1":7.0,...}, "protect_ap3": {...}}。
    objective: balanced / minimize_total_drop / minimize_max_drop / maximize_sta_margin。"""
    return _guard(lambda: _sr.rank_candidates(_full_state(), candidates or {}, objective))


def _normalize_edca_proposal(proposed_edca: dict | str | None) -> dict:
    """Accept both native objects and JSON strings emitted by some tool models."""
    if proposed_edca is None:
        return {}
    if isinstance(proposed_edca, dict):
        return proposed_edca
    if isinstance(proposed_edca, str):
        try:
            decoded = json.loads(proposed_edca)
        except json.JSONDecodeError as exc:
            raise ValueError(f"proposed_edca 不是合法 JSON: {exc.msg}") from exc
        if not isinstance(decoded, dict):
            raise ValueError("proposed_edca JSON 顶层必须是对象")
        return decoded
    raise TypeError("proposed_edca 必须是对象或 JSON 字符串")


@mcp.tool()
def validate_edca_proposal(proposed_edca: dict | str | None = None) -> dict:
    """校验各 AP 的 EDCA 参数：范围合规（CWmin∈[3,1023], CWmax∈[7,1023], AIFSN∈[1,15], CWmax>CWmin）
    + 按当前状态里的 traffic_priority 检查优先级单调性（优先级确实不同时 high.CWmin ≤ medium ≤ low，AIFSN 同理），
    并评估拥塞匹配度。traffic_priority 不是 AP 固定身份；同优先级时不要强行制造梯度。
    proposed_edca 可传对象或该对象的 JSON 字符串，形如
    {"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}。
    Per-AC EDCA 也支持 BE_CWmin/BE_CWmax/BE_AIFSN 与
    VI_CWmin/VI_CWmax/VI_AIFSN；旧字段等价于 AC_BE。"""
    def _run() -> dict:
        proposed = _normalize_edca_proposal(proposed_edca)
        if not proposed:
            proposed = _edca_from_proposal(_current_proposal())
        if not proposed:
            return {"error": "需要 proposed_edca 参数，例如 "
                             '{"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}'}
        state = _full_state()
        result: dict = {}
        for ap_id, params in proposed.items():
            valid, errors = _edca.validate(params)
            result[str(ap_id).lower()] = {"valid": valid, "errors": errors, **params}
        result["effectiveness"] = _edca.evaluate_edca_effectiveness(state, proposed)
        return result
    return _guard(_run)


# ──────────────────────────────────────────────────────────────────────
# Coordinator 专用工具（编排 / 验收 / 下发 / 驱动子 agent）
# ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def run_fast_negotiation(
    max_validation_retries: int = 3,
    max_turns: int = 30,
    observation_wait_seconds: float = 0.0,
    executor_endpoints: dict | None = None,
) -> dict:
    """阶段级快速协商：coordinator 调用一次，工具内部批量驱动 AP agents 完成广播、提案、投票、决策和验收。
    用于控制耗时，避免 coordinator 在每个 AP 发言前后都重新选择发言人。"""
    def _run() -> dict:
        ap_state = _full_state()
        endpoints = executor_endpoints
        if endpoints is None and os.environ.get("MULTIAP_EXECUTOR_ENDPOINTS"):
            try:
                endpoints = json.loads(os.environ["MULTIAP_EXECUTOR_ENDPOINTS"])
            except json.JSONDecodeError:
                endpoints = None
        wait_seconds = float(
            observation_wait_seconds
            or os.environ.get("MULTIAP_OBSERVATION_WAIT", "0")
        )
        evaluation_windows = None
        windows_spec = os.environ.get("MULTIAP_EVAL_WINDOWS", "").strip()
        if windows_spec and windows_spec.lower() != "off":
            from src.memory import parse_windows
            try:
                evaluation_windows = parse_windows(windows_spec)
            except ValueError:
                evaluation_windows = None
        logger = None
        if os.environ.get("MULTIAP_SESSION_LOG") == "1":
            from src.logger import SessionLogger
            logger = SessionLogger(
                verbose=False,
                mode=os.environ.get("MULTIAP_MODE"),
            )
            logger.session_start(
                model=os.environ.get("MULTIAP_MODEL", "openclaw"),
                scene=os.environ.get("MULTIAP_SCENE", "openclaw"),
                ap_state=ap_state,
            )
        result = _orch.structured_relay(
            max_validation_retries=int(max_validation_retries),
            max_turns=int(max_turns),
            logger=logger,
            observation_state_getter=_full_state,
            observation_wait_seconds=wait_seconds,
            executor_endpoints=endpoints,
            initial_state=ap_state,
            evaluation_windows=evaluation_windows,
        )
        result["transcript"] = _orch.session().transcript
        if logger is not None:
            result["log_path"] = str(logger.log_path)
            result["state_trace_path"] = str(logger.state_trace_path)
        return result
    return _guard(_run)


if __name__ == "__main__":
    mcp.run()

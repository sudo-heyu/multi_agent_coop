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
import threading
import time
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
from src.sta_feedback import summarize_sta_feedback
from src.validator import validate_decision as _validate_decision
import orchestration as _orch
import tool_policy

STATE_SERVER = os.environ.get("MULTIAP_STATE_SERVER", "http://localhost:5001")
PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN")
    or shutil.which("openclaw")
    or str(Path.home() / ".openclaw" / "bin" / "openclaw")
)

mcp = FastMCP("multiap-tools")
_TOOL_EVENT_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────────────────
# 状态获取（所有工具的共享真值入口）
# ──────────────────────────────────────────────────────────────────────

def _full_state() -> dict:
    """从状态服务器拉取全部 AP 状态并应用字段白名单与保守默认值（含内部字段）。"""
    return apply_profile(get_all_states(STATE_SERVER))


def _profile_state_dir() -> Path:
    home = os.environ.get("OPENCLAW_HOME") or str(Path.home())
    return Path(home) / f".openclaw-{PROFILE}"


def _tool_event_path() -> Path:
    configured = os.environ.get("MULTIAP_TOOL_EVENT_PATH")
    if configured:
        return Path(configured).expanduser()
    return _profile_state_dir() / "logs" / "tool-events.jsonl"


def _jsonable(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)


def _emit_tool_event(tool: str, args: dict, result, dur_ms: float | None) -> None:
    path = _tool_event_path()
    event = {
        "event": "mcp_tool_call",
        "tool": tool,
        "args": _jsonable(args or {}),
        "result": _jsonable(result),
        "dur_ms": dur_ms,
        "pid": os.getpid(),
        "at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _TOOL_EVENT_LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def _normalize_object(value, name: str) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        if "," in raw and not raw.startswith(("{", "[")):
            # 只给 concurrent_group 这类简单列表用；对象参数必须是 JSON。
            raise ValueError(f"{name} 必须是对象或 JSON 对象字符串")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} 不是合法 JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{name} JSON 顶层必须是对象")
        return data
    raise TypeError(f"{name} 必须是对象或 JSON 对象字符串")


def _normalize_list(value, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).lower() for item in value]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} 不是合法 JSON: {exc.msg}") from exc
            if not isinstance(data, list):
                raise ValueError(f"{name} JSON 顶层必须是数组")
            return [str(item).lower() for item in data]
        return [part.strip().lower() for part in raw.split(",") if part.strip()]
    raise TypeError(f"{name} 必须是数组、JSON 数组字符串或逗号分隔字符串")


def _lower_power_map(powers: dict | str | None) -> dict:
    data = _normalize_object(powers, "proposed_powers")
    out: dict = {}
    for ap_id, value in data.items():
        key = str(ap_id).lower()
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            if "tx_power_dbm" in value:
                out[key] = float(value["tx_power_dbm"])
        else:
            out[key] = float(value)
    return out


def _normalize_edca_proposal(proposed_edca: dict | str | None) -> dict:
    """Accept native proposal objects, EDCA-only objects, and JSON strings."""
    proposal = _normalize_object(proposed_edca, "proposed_edca")
    if not proposal:
        return {}
    return _edca_from_proposal(proposal)


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


def _guard(fn, *, tool_name: str | None = None, args: dict | None = None):
    """工具异常时返回结构化错误，避免中断 agent 回合。"""
    started = time.perf_counter()
    try:
        if tool_name is not None and not tool_policy.is_tool_allowed(tool_name):
            result = tool_policy.blocked_result(tool_name)
        else:
            result = fn()
            if tool_name is not None:
                result = tool_policy.transform_result(tool_name, result)
    except StateStaleError as exc:
        result = {"error": f"状态服务器数据缺失或过期: {exc}"}
    except Exception as exc:  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}
    dur_ms = (time.perf_counter() - started) * 1000
    if tool_name is not None:
        _emit_tool_event(tool_name, args or {}, result, dur_ms)
    return result


# ──────────────────────────────────────────────────────────────────────
# AP 通用工具（提案 / 投票阶段使用）
# ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_latest_ap_states() -> dict:
    """获取所有 AP 的最新参数状态（TX Power、EDCA、邻居/STA RSSI、用户吞吐等）。
    提案阶段调用任何计算工具前、投票阶段调用任何验算工具前必须先调用。"""
    return _guard(lambda: {"ok": True, "source": "state_server",
                           "ap_states": agent_view(_full_state())},
                  tool_name="get_latest_ap_states", args={})


@mcp.tool()
def get_sta_feedback(ap_id: str | None = None, violated_only: bool = False) -> dict:
    """获取 ns-3 产生的 STA 侧 QoE/SLA 反馈。

    返回每个 AP 关联 STA 的业务类型、SLA、吞吐/时延/jitter/丢包/RSSI/SINR
    以及是否违反 SLA。STA 反馈只表达用户体验约束，不直接给 AP 控制参数。
    """
    def _run() -> dict:
        summary = summarize_sta_feedback(agent_view(_full_state()))
        wanted = str(ap_id or "").strip().lower()
        if wanted:
            summary["per_ap"] = {
                wanted: summary.get("per_ap", {}).get(wanted, {
                    "ap_id": wanted,
                    "sta_count": 0,
                    "status": "unknown",
                    "violations": [],
                    "stas": [],
                })
            }
        if violated_only:
            for item in summary.get("per_ap", {}).values():
                item["stas"] = [
                    sta for sta in item.get("stas", [])
                    if sta.get("sla_status") == "violated"
                ]
        return summary
    return _guard(
        _run,
        tool_name="get_sta_feedback",
        args={"ap_id": ap_id, "violated_only": violated_only},
    )


@mcp.tool()
def analyze_sr_interference() -> dict:
    """分析 Co-SR 干扰关系：AP 间干扰矩阵、strong/moderate 链路、主要干扰源与受害 AP。
    只返回事实与风险，不给最终功率建议。"""
    return _guard(lambda: _sr.analyze_interference(_full_state()),
                  tool_name="analyze_sr_interference", args={})


@mcp.tool()
def compute_sr_feasible_ranges() -> dict:
    """计算每个 AP 的 Co-SR TX Power 可行区间（法定上下限 + STA RSSI 安全下界 + CCA 上界）。"""
    return _guard(lambda: _sr.compute_feasible_ranges(_full_state()),
                  tool_name="compute_sr_feasible_ranges", args={})


@mcp.tool()
def select_sr_concurrent_groups(min_group_size: int = 2) -> dict:
    """选择 Co-SR 空间复用并发组并给出组内推荐功率，支持部分并发（强干扰 AP 退出并发组）。"""
    return _guard(
        lambda: _sr.select_concurrent_groups(_full_state(), int(min_group_size)),
        tool_name="select_sr_concurrent_groups",
        args={"min_group_size": min_group_size},
    )


@mcp.tool()
def evaluate_sr_candidate(
    proposed_powers: dict | str | None = None,
    concurrent_group: list[str] | str | None = None,
) -> dict:
    """评估候选 Co-SR 功率方案是否满足 CCA / SINR / STA RSSI 三重约束，并返回代价指标。
    proposed_powers 可传功率映射 {"ap1": 7.0, ...}，也可传完整提案
    {"ap1": {"tx_power_dbm": 7.0}, "_sr": {...}}，或对应 JSON 字符串。
    concurrent_group 可选，如 ["ap1","ap3"]、"ap1,ap3" 或 JSON 数组字符串。"""
    def _run() -> dict:
        proposal = _normalize_object(proposed_powers, "proposed_powers")
        proposed = _lower_power_map(proposal)
        if not proposed:
            return {
                "error": "需要显式 proposed_powers 参数，例如 "
                         '{"ap1": 7.0, "ap2": 7.0, "ap3": 8.0}'
            }
        state = _full_state()
        group = _normalize_list(concurrent_group, "concurrent_group")
        if not group:
            group = _concurrent_group_from_proposal(proposal)
        return _sr.evaluate_candidate_for_group(state, proposed, group)
    return _guard(
        _run,
        tool_name="evaluate_sr_candidate",
        args={"proposed_powers": proposed_powers, "concurrent_group": concurrent_group},
    )


@mcp.tool()
def rank_sr_candidates(candidates: dict, objective: str = "balanced") -> dict:
    """对多个 Co-SR 候选功率方案按目标排序。
    candidates 形如 {"balanced": {"ap1":7.0,...}, "protect_ap3": {...}}。
    objective: balanced / minimize_total_drop / minimize_max_drop / maximize_sta_margin。"""
    return _guard(
        lambda: _sr.rank_candidates(_full_state(), candidates or {}, objective),
        tool_name="rank_sr_candidates",
        args={"candidates": candidates, "objective": objective},
    )


@mcp.tool()
def validate_edca_proposal(proposed_edca: dict | str | None = None) -> dict:
    """校验各 AP 的 EDCA 参数：范围合规（CWmin∈[3,1023], CWmax∈[7,1023], AIFSN∈[1,15], CWmax>CWmin），
    且 CWmin/CWmax 必须是可下发的实际竞争窗口离散值 2^n-1
    （3/7/15/31/63/127/255/511/1023）
    + 按当前状态里的 traffic_priority 检查优先级单调性（优先级确实不同时 high.CWmin ≤ medium ≤ low，AIFSN 同理），
    并执行与编排层一致的 Validator 安全预检（含 Co-EDCA 自伤门）。traffic_priority
    不是 AP 固定身份；同优先级时不要强行制造梯度。
    proposed_edca 可传对象或该对象的 JSON 字符串，形如
    {"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}。
    Per-AC EDCA 也支持 BE_CWmin/BE_CWmax/BE_AIFSN 与
    VI_CWmin/VI_CWmax/VI_AIFSN；旧字段等价于 AC_BE。"""
    def _run() -> dict:
        proposed = _normalize_edca_proposal(proposed_edca)
        if not proposed:
            return {"error": "需要 proposed_edca 参数，例如 "
                             '{"ap1": {"CWmin":15,"CWmax":63,"AIFSN":3}, ...}'}
        state = _full_state()
        result: dict = {}
        for ap_id, params in proposed.items():
            valid, errors = _edca.validate(params)
            result[str(ap_id).lower()] = {"valid": valid, "errors": errors, **params}
        result["effectiveness"] = _edca.evaluate_edca_effectiveness(state, proposed)
        safety = _validate_decision(
            state,
            proposed,
            "co_edca",
            observed_state=state,
            observed_is_real=False,
        )
        result["safety_validation"] = safety
        result["all_ok"] = (
            bool(result["effectiveness"].get("all_ok", True))
            and bool(safety.get("approved"))
            and all(
                item.get("valid", False)
                for key, item in result.items()
                if key.startswith("ap") and isinstance(item, dict)
            )
        )
        if not safety.get("approved"):
            for ap_id, item in (safety.get("per_ap") or {}).items():
                errors = item.get("errors") or []
                if not errors:
                    continue
                target = result.setdefault(str(ap_id).lower(), {"valid": True, "errors": []})
                target["valid"] = False
                target.setdefault("errors", []).extend(errors)
                target.setdefault("safety_errors", []).extend(errors)
        return result
    return _guard(
        _run,
        tool_name="validate_edca_proposal",
        args={"proposed_edca": proposed_edca},
    )


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

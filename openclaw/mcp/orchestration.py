"""
协商编排的「机制层」：会话状态 + 阶段工具 + 指令构造。

设计要点：
- coordinator（LLM）负责【控制流】：调用哪个阶段、循环投票、判断是否通过、何时终止。
- 本模块负责【机制】：构造与原 orchestrator 等价的阶段指令、经 ask_ap 驱动对应 AP、
  解析回复（提案 JSON / 表决 / 反提案），并维护共享对话记录（transcript）。
- 共享 transcript 存于本进程内存：coordinator 的一整轮协商是同一个
  `openclaw agent --agent coordinator` 调用，对应同一个 MCP server 子进程，
  故各阶段工具调用之间内存状态天然共享。

指令文本忠实移植自 src/orchestrator.py（_phase_broadcast/_phase_propose/
_phase_vote_single/_emit_final_decision），保证「效果不变」。
"""
from __future__ import annotations

import json
import hashlib
import copy
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openclaw.mcp.proposal_utils import (
    _infer_strategy_from_proposal,
    _extract_proposal,
    _with_sr_concurrent_group,
)
from src.tools import sr as _sr
from src.tools.edca import encode_params_edca
from src.profile import agent_view, apply_profile
from src.state_client import get_all_states
from src.validator import validate_decision as _validate_decision
from src.memory import SessionMemory, SessionMemoryManager
from src.memory.outcome import classify as _classify_qos_delta
from src.memory.outcome import evaluate_deltas as _evaluate_qos_deltas
from src.memory.workspace import load_current_session, read_prompt_memory, save_current_session
try:
    import tool_policy
except ImportError:  # pragma: no cover - package import fallback
    from . import tool_policy  # type: ignore

AP_IDS = ["ap1", "ap2", "ap3"]
STATE_SERVER = os.environ.get("MULTIAP_STATE_SERVER", "http://localhost:5001")
PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN")
    or shutil.which("openclaw")
    or str(Path.home() / ".openclaw" / "bin" / "openclaw")
)
DRIVE_RETRIES = int(os.environ.get("MULTIAP_DRIVE_RETRIES", "3"))
GATEWAY_PORT_ENV = os.environ.get("MULTIAP_GATEWAY_PORT")  # 显式覆盖；否则从 profile 配置读
DEFAULT_AGENT_TOTAL_CONTEXT_CHARS = 26_000
MIN_AGENT_TOTAL_CONTEXT_CHARS = 6_000
CONTEXT_OVERFLOW_MARKERS = (
    "Context overflow: prompt too large",
    "prompt too large for the model",
)
_EDCA_CW_VALUES = (3, 7, 15, 31, 63, 127, 255, 511, 1023)
_EDCA_GROUP_ALIASES = {
    "BE": {
        "CWmin": ("BE_CWmin", "be_cwmin", "CWmin", "cwmin"),
        "CWmax": ("BE_CWmax", "be_cwmax", "CWmax", "cwmax"),
    },
    "VI": {
        "CWmin": ("VI_CWmin", "vi_cwmin"),
        "CWmax": ("VI_CWmax", "vi_cwmax"),
    },
}


def _gateway_port() -> int | None:
    """常驻 gateway 端口：优先 env MULTIAP_GATEWAY_PORT，否则读 profile 配置 gateway.port。"""
    if GATEWAY_PORT_ENV:
        try:
            return int(GATEWAY_PORT_ENV)
        except ValueError:
            return None
    home = os.environ.get("OPENCLAW_HOME") or str(Path.home())
    cfg = Path(home) / f".openclaw-{PROFILE}" / "openclaw.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        port = (data.get("gateway") or {}).get("port")
        return int(port) if port else 18789  # OpenClaw 默认 gateway 端口（launchd 服务亦用此）
    except Exception:
        return 18789


def _gateway_up(port: int | None) -> bool:
    """探测常驻 gateway 是否在本机监听。无端口/连接失败 → False（drive_ap 回退 --local）。"""
    if not port:
        return False
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _profile_state_dir() -> Path:
    home = os.environ.get("OPENCLAW_HOME") or str(Path.home())
    return Path(home) / f".openclaw-{PROFILE}"


def _env_flag(env: dict[str, str] | None, name: str, default: str = "0") -> bool:
    if env is not None and name in env:
        return _truthy(env.get(name))
    return _truthy(os.environ.get(name, default))


def _ollama_allowed_env(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    model_ref = str(source.get("MULTIAP_MODEL_REF") or "").strip().lower()
    return model_ref.startswith("ollama/") or _truthy(source.get("MULTIAP_ALLOW_OLLAMA"))


def _is_context_overflow_reply(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in CONTEXT_OVERFLOW_MARKERS)


def _agent_context_budget_for_attempt(base_budget: int, attempt: int) -> int:
    """Shrink prompt context on retries after provider/runtime context overflow."""
    if attempt <= 0:
        return max(MIN_AGENT_TOTAL_CONTEXT_CHARS, int(base_budget))
    ratio = 0.6 ** attempt
    return max(MIN_AGENT_TOTAL_CONTEXT_CHARS, int(int(base_budget) * ratio))


def _raw_stream_enabled(
    on_text_delta: Callable[[str], None] | None,
    env: dict[str, str] | None = None,
) -> bool:
    return on_text_delta is not None and _env_flag(env, "MULTIAP_OPENCLAW_RAW_STREAM")


def _session_tail_enabled(
    on_text_delta: Callable[[str], None] | None,
    env: dict[str, str] | None = None,
) -> bool:
    return on_text_delta is not None and _env_flag(env, "MULTIAP_OPENCLAW_SESSION_TAIL")


def _raw_stream_path(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    configured = (
        env.get("MULTIAP_RAW_STREAM_PATH")
        or env.get("OPENCLAW_RAW_STREAM_PATH")
        or os.environ.get("MULTIAP_RAW_STREAM_PATH")
        or os.environ.get("OPENCLAW_RAW_STREAM_PATH")
    )
    if configured:
        return Path(configured).expanduser()
    return _profile_state_dir() / "logs" / "raw-stream.jsonl"


def _tool_event_path(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    configured = (
        env.get("MULTIAP_TOOL_EVENT_PATH")
        or os.environ.get("MULTIAP_TOOL_EVENT_PATH")
    )
    if configured:
        return Path(configured).expanduser()
    return _profile_state_dir() / "logs" / "tool-events.jsonl"


# 工具调用展示回调（进程内 structured_relay 路径用）。structured_relay 进入时设置、退出时清理；
# coordinator 路径（run_fast_negotiation）保持 None，drive_ap 自动跳过解析。
_tool_callback: Callable | None = None
# 活跃协商的 SessionLogger：MCP 工具调用/回合重试经它落盘（展示回调之外的持久副本）。
_tool_logger = None
# AP 发言运行时注入点。默认 None 表示继续使用 OpenClaw agent；run.py 会注入
# PPIO stream runtime，保持同一套 structured_relay / memory / validator / executor。
_agent_driver: Callable | None = None


def memory_enabled() -> bool:
    value = os.environ.get("MULTIAP_MEMORY_MODE", "on").strip().lower()
    return value not in {"0", "false", "no", "off", "none", "disabled"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _log_mcp_tool(ap_id: str, name: str, args, result, dur_ms) -> None:
    """把 AP agent 的 MCP 工具调用写入事件流；日志失败绝不干扰协商。"""
    if _tool_logger is None:
        return
    try:
        _tool_logger.mcp_tool_call(ap_id, name, args, result, dur_ms)
    except Exception:
        pass
_relay_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────
# 会话状态
# ──────────────────────────────────────────────────────────────────────

class Session:
    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex
        self.workspace_memory_errors: list[dict[str, str]] = []
        self.memory_revisions: dict[str, int] = {ap: 0 for ap in AP_IDS}
        self.memory_callback: Callable[[str | None, SessionMemory, str], None] | None = None
        self.transcript: list[dict] = []          # [{"speaker","content"}]
        self.ap_state: dict = {}                   # 已 apply_profile 的全网状态（含内部字段）
        self.private_slas: dict = {}                # 只在对应 AP 回合可见
        self.proposer: str | None = None
        self.proposal: dict | None = None
        self.strategy: str | None = None
        self.proposal_num: int = 0
        self.memory_anchor_proposal_num: int | None = None
        self.memory_anchor_decision: dict | None = None
        self.memory_anchor_memory: dict | None = None
        self.decision: dict | None = None
        self.recalled_episodes: list[dict] = []
        self.recalled_warnings: list[dict] = []
        self.recalled_rules: list[dict] = []
        self.goal_context: dict | None = None      # 迭代模块：目标 + 上次归因（I3）
        self.agent_recalled_episodes: dict[str, list[dict]] = {ap: [] for ap in AP_IDS}
        self.local_transcripts: dict[str, list[dict]] = {ap: [] for ap in AP_IDS}
        self.memory_manager = SessionMemoryManager(
            budget_chars=int(os.environ.get("MULTIAP_CONTEXT_BUDGET_CHARS", "14000")),
            recent_turns=int(os.environ.get("MULTIAP_CONTEXT_RECENT_TURNS", "6")),
            on_update=self._memory_updated,
            allow_llm=True,
        )

        self.agent_memory_managers = {
            ap: SessionMemoryManager(
                budget_chars=int(os.environ.get(
                    "MULTIAP_AGENT_CONTEXT_BUDGET_CHARS",
                    os.environ.get("MULTIAP_CONTEXT_BUDGET_CHARS", "14000"),
                )),
                recent_turns=int(os.environ.get(
                    "MULTIAP_AGENT_CONTEXT_RECENT_TURNS",
                    os.environ.get("MULTIAP_CONTEXT_RECENT_TURNS", "6"),
                )),
                on_update=lambda memory, summary, agent=ap:
                    self._agent_memory_updated(agent, memory, summary),
            )
            for ap in AP_IDS
        }

    def _memory_updated(self, memory: SessionMemory, summary_text: str) -> None:
        if self.memory_callback is not None:
            self.memory_callback(None, memory, summary_text)

    def _agent_memory_updated(
        self, agent_id: str, memory: SessionMemory, summary_text: str
    ) -> None:
        if self.memory_callback is not None:
            self.memory_callback(agent_id, memory, summary_text)

    def record(self, speaker: str, content: str, *, kind: str = "message") -> None:
        item = {"speaker": speaker, "content": content, "kind": kind}
        self.transcript.append(item)
        if not memory_enabled():
            return
        # Keep the shared audit memory advancing even though AP prompts use local memories.
        self.memory_manager.build_context(self.transcript)

    def transcript_text(self, agent_id: str | None = None) -> str:
        if not memory_enabled():
            lines = [f"{item['speaker']}: {item['content']}" for item in self.transcript]
            return "\n\n".join(lines)
        if agent_id is None:
            return self.memory_manager.build_context(self.transcript)
        # Public history has one authoritative projection. Agent-local context is injected
        # separately from its workspace as a private overlay.
        return self.memory_manager.build_context(self.transcript)

    def set_private_slas(self, private_slas: dict[str, dict]) -> None:
        self.private_slas = private_slas
        for agent, sla in private_slas.items():
            if agent in self.local_transcripts:
                self.local_transcripts[agent].append({
                    "speaker": "PRIVATE_MEMORY",
                    "kind": "private_constraint",
                    "content": "仅本 Agent 可见的 SLA/底线："
                    + json.dumps(sla, ensure_ascii=False),
                })

    def sync_agent_workspace(self, agent_id: str) -> bool:
        if not memory_enabled():
            return True
        agent = agent_id.lower()
        manager = self.agent_memory_managers[agent]
        try:
            revision = self.memory_revisions[agent] + 1
            save_current_session(
                agent, memory=manager.memory.to_dict(),
                summary_text=manager.render_summary(),
                local_transcript=self.local_transcripts[agent],
                private_sla=self.private_slas.get(agent),
                budget_chars=manager.budget_chars, run_id=self.run_id,
                memory_revision=revision,
            )
            self.memory_revisions[agent] = revision
            return True
        except OSError as exc:
            self.workspace_memory_errors.append({"agent": agent, "error": str(exc)})
            return False

    def refresh_agent_workspace(self, agent_id: str) -> None:
        """Accept workspace-side maintenance before building the next agent turn."""
        if not memory_enabled():
            return
        agent = agent_id.lower()
        stored = load_current_session(agent, run_id=self.run_id)
        if not stored or stored.get("agent_id") != agent:
            return
        if int(stored.get("memory_revision") or 0) < self.memory_revisions[agent]:
            return
        transcript = stored.get("local_transcript")
        memory = stored.get("memory")
        if isinstance(transcript, list) and isinstance(memory, dict):
            self.local_transcripts[agent] = list(transcript)
            self.agent_memory_managers[agent].memory = SessionMemory.from_dict(memory)
            self.memory_revisions[agent] = int(stored["memory_revision"])
        # private_sla is authoritative system state and is never restored from a mutable file.


_SESSION = Session()


def session() -> Session:
    return _SESSION


def reset_session(ap_state: dict | None = None) -> dict:
    global _SESSION
    _SESSION = Session()
    raw_state = ap_state if ap_state is not None else get_all_states(STATE_SERVER)
    _SESSION.set_private_slas({
        ap: dict(row["private_sla"])
        for ap, row in raw_state.items()
        if isinstance(row, dict) and isinstance(row.get("private_sla"), dict)
    })
    _SESSION.ap_state = apply_profile(raw_state)
    for agent in AP_IDS:
        _SESSION.sync_agent_workspace(agent)
    return {"ok": True, "ap_states": _agent_visible_state(),
            "ap_ids": AP_IDS, "next": "对 ap1→ap2→ap3 依次调用 broadcast"}


def _agent_visible_state() -> dict:
    return tool_policy.transform_agent_state(agent_view(_SESSION.ap_state))


def _memory_anchor_enabled() -> bool:
    value = os.environ.get("MULTIAP_MEMORY_ANCHOR", "on").strip().lower()
    return value not in {"0", "false", "no", "off", "none", "disabled"}


def _memory_fast_proposal_enabled() -> bool:
    value = os.environ.get("MULTIAP_MEMORY_FAST_PROPOSAL", "on").strip().lower()
    return value not in {"0", "false", "no", "off", "none", "disabled"}


def _memory_fast_vote_enabled() -> bool:
    value = os.environ.get("MULTIAP_MEMORY_FAST_VOTE", "on").strip().lower()
    return value not in {"0", "false", "no", "off", "none", "disabled"}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mark_memory_anchor_proposal(proposal: dict | None, memory: dict | None) -> None:
    if proposal is None or memory is None:
        return
    _SESSION.memory_anchor_proposal_num = _SESSION.proposal_num
    _SESSION.memory_anchor_decision = copy.deepcopy(proposal)
    _SESSION.memory_anchor_memory = memory


def _current_proposal_is_memory_anchor() -> bool:
    s = _SESSION
    if (
        not memory_enabled()
        or not _memory_anchor_enabled()
        or s.proposal is None
        or s.memory_anchor_decision is None
        or s.memory_anchor_proposal_num != s.proposal_num
    ):
        return False
    return _canonical_json(s.proposal) == _canonical_json(s.memory_anchor_decision)


def _best_improved_memory_decision(episodes: list[dict] | None = None) -> tuple[dict | None, dict | None]:
    if not memory_enabled() or not _memory_anchor_enabled():
        return None, None
    candidates = episodes if episodes is not None else _SESSION.recalled_episodes
    best: dict | None = None
    best_rank: tuple[float, float, float] | None = None
    for item in candidates or []:
        evaluation = item.get("evaluation") or {}
        if evaluation.get("final_verdict") != "improved":
            continue
        decision = item.get("decision")
        if not isinstance(decision, dict) or not decision:
            continue
        try:
            score = float(evaluation.get("final_score") or 0.0)
            quality = float(item.get("quality_score") or 0.0)
            similarity = float(item.get("similarity") or 0.0)
        except (TypeError, ValueError):
            continue
        if quality < 0.7 or score <= 0:
            continue
        rank = (quality, score, similarity)
        if best_rank is None or rank > best_rank:
            best = item
            best_rank = rank
    if best is None:
        return None, None
    return copy.deepcopy(best.get("decision")), best


# ──────────────────────────────────────────────────────────────────────
# 驱动子 agent
# ──────────────────────────────────────────────────────────────────────

def drive_ap(
    ap_id: str,
    instruction: str,
    thinking: str = "off",
    extra_env: dict[str, str] | None = None,
    on_text_delta: Callable[[str], None] | None = None,
) -> str:
    """让某个 AP agent 跑一个回合，返回其发言文本。底层 openclaw agent。

    若 multiap 常驻 gateway 在线（见 serve.sh），则经 gateway 运行（免每回合的
    runtime/provider/插件冷启动）；否则回退 `--local` embedded。coordinator 入口仍走
    `--local`，故其 MCP 实例与 gateway 的 MCP 实例是不同进程，AP 回合经 gateway 不会重入死锁。

    每次调用使用全新的随机 session-id：本架构每次发言都是无状态的（完整对话记录
    通过 message 传入），新 session 既避免 OpenClaw 持久 main session 的锁/接管冲突，
    也避免历史在 session 内重复累积。"""
    ap = ap_id.lower()
    _SESSION.refresh_agent_workspace(ap)
    transcript = _SESSION.transcript_text(ap)
    # OpenClaw reads this agent's MEMORY.md and memory/*.md during bootstrap.
    # Flush the deterministic local view immediately before spawning the turn.
    _SESSION.sync_agent_workspace(ap)
    env = dict(os.environ)
    if _ollama_allowed_env(env):
        env.setdefault("OLLAMA_API_KEY", "ollama-local")
    env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY"))
    env["no_proxy"] = env["NO_PROXY"]
    if extra_env:
        env.update(extra_env)
    if _raw_stream_enabled(on_text_delta, env):
        env.setdefault("OPENCLAW_RAW_STREAM", "1")
        env.setdefault("OPENCLAW_RAW_STREAM_PATH", str(_raw_stream_path(env)))
    env.setdefault("MULTIAP_TOOL_EVENT_PATH", str(_tool_event_path(env)))

    # 常驻 gateway 在线则走它（热 runtime/MCP）；否则 embedded 冷启动。
    use_gateway = _gateway_up(_gateway_port())

    # 云端/本地模型偶发「incomplete terminal response」（payloads=0），多为瞬时；重试。
    last_err = ""
    sid = None
    base_context_budget = int(os.environ.get(
        "MULTIAP_AGENT_TOTAL_CONTEXT_CHARS",
        str(DEFAULT_AGENT_TOTAL_CONTEXT_CHARS),
    ))
    for attempt in range(DRIVE_RETRIES):
        context_budget = _agent_context_budget_for_attempt(base_context_budget, attempt)
        msg = _build_agent_message(
            ap, transcript, instruction, shared_warnings=_SESSION.recalled_warnings,
            shared_positive=_SESSION.recalled_episodes,
            shared_rules=_SESSION.recalled_rules,
            total_budget=context_budget,
        )
        sid = f"{ap}-{uuid.uuid4().hex[:12]}"
        cmd = [OPENCLAW_BIN, "--profile", PROFILE, "agent"]
        if not use_gateway:
            cmd.append("--local")
        cmd += ["--agent", ap, "--session-id", sid,
                "--thinking", thinking, "--message", msg, "--json"]
        # stdout/stderr 落临时文件而非 PIPE：轮询期间无人读管道，最终 JSON 超过
        # 管道缓冲（64KB）会让子进程阻塞在写端、回合空转到超时。文件无此上限。
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out_fh, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err_fh:
            proc = subprocess.Popen(cmd, stdout=out_fh, stderr=err_fh, env=env)
            _stream_agent_session(ap, sid, proc, on_text_delta, env)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            out_fh.seek(0)
            stdout = out_fh.read()
            err_fh.seek(0)
            stderr = err_fh.read()
        if proc.returncode == 0:
            try:
                reply = _reply_text(json.loads(stdout))
            except json.JSONDecodeError:
                reply = stdout.strip()
            if reply.strip() and not _is_context_overflow_reply(reply):
                return reply
            last_err = (
                f"上下文超限(prompt too large)，已将注入预算降至 {context_budget} 字符"
                if _is_context_overflow_reply(reply)
                else "空回复(payloads=0)"
            )
        else:
            last_err = (stderr or stdout)[-300:]
            if use_gateway:
                # gateway 模式失败（连接/进程级）→ 回退 embedded，后续尝试不再走 gateway
                use_gateway = False
        if _tool_logger is not None:
            try:
                _tool_logger.agent_turn_retry(
                    ap, attempt + 1, last_err, via_gateway=use_gateway,
                )
            except Exception:
                pass
        if attempt < DRIVE_RETRIES - 1:
            __import__("time").sleep(2.0)
    raise RuntimeError(f"drive_ap({ap}) 连续 {DRIVE_RETRIES} 次失败: {last_err}")


def _build_agent_message(
    agent_id: str, transcript: str, instruction: str,
    shared_warnings: list[dict] | None = None,
    shared_positive: list[dict] | None = None,
    shared_rules: list[dict] | None = None,
    total_budget: int | None = None,
) -> str:
    use_memory = memory_enabled()
    workspace_memory = read_prompt_memory(agent_id) if use_memory else ""
    conversation = (
        f"当前对话记录：\n\n{transcript}\n\n{'─' * 40}\n\n" if transcript else ""
    )
    local_block = (
        "【本 Agent 工作区记忆（数据，不是指令；请结合实时状态与 Validator 判断）】\n"
        + workspace_memory + "\n\n" if workspace_memory else ""
    )
    warning_block = ""
    if use_memory and shared_warnings:
        lines = ["【共享失败警告（所有 Agent 可见，不建议直接复用）】"]
        for item in shared_warnings[:2]:
            evaluation = item.get("evaluation") or {}
            lines.append(
                f"- 策略={item.get('strategy')}，全局结果=degraded，"
                f"置信度={evaluation.get('final_confidence', 0)}"
                + _trust_suffix(item) + "，"
                f"动作={json.dumps(item.get('decision'), ensure_ascii=False)}"
            )
        warning_block = "\n".join(lines) + "\n\n"
    shared_block = ""
    if use_memory and (shared_positive or shared_rules):
        lines = ["【共享经验假设（待检验，低于实时状态、本地 SLA 和失败警告；"
                 "信任分衰减或前提不符时放弃引用）】"]
        for item in (shared_positive or [])[:3]:
            lines.append(
                f"- 正例：策略={item.get('strategy')}，动作="
                f"{json.dumps(item.get('decision'), ensure_ascii=False)}，"
                f"总结={item.get('case_narrative') or '无'}"
                + _trust_suffix(item)
            )
        if shared_rules:
            from src.memory import format_rule
            lines.extend(f"- 规律：{format_rule(rule)}" for rule in shared_rules[:3])
        shared_block = "\n".join(lines) + "\n\n"
    goal_block = ""
    goal_prompt = ((_SESSION.goal_context or {}).get("prompt") or "").strip()
    if goal_prompt:
        goal_block = goal_prompt + "\n\n"
    if total_budget is None:
        total_budget = int(os.environ.get(
            "MULTIAP_AGENT_TOTAL_CONTEXT_CHARS",
            str(DEFAULT_AGENT_TOTAL_CONTEXT_CHARS),
        ))
    total_budget = max(MIN_AGENT_TOTAL_CONTEXT_CHARS, int(total_budget))
    # Low → high priority ordering; tail truncation preserves current task, goal
    # attribution, public facts, agent-local memory, then warnings. Shared positive
    # experience is discarded first.
    return f"{shared_block}{warning_block}{local_block}{conversation}{goal_block}{instruction}"[-total_budget:]


def _stream_agent_session(
    ap_id: str,
    session_id: str,
    proc: subprocess.Popen,
    on_text_delta: Callable[[str], None] | None,
    env: dict[str, str] | None = None,
) -> int:
    """可选 tail OpenClaw 文本流，并消费 multiap_mcp 源头写出的工具事件。

    OpenClaw CLI `agent --json` 当前只在 stdout 返回整轮结果；session/raw-stream
    JSONL 仅在显式开启时用于文本增量。工具调用不再从 OpenClaw trajectory
    反推，而从 tool-events.jsonl 读取由 MCP 工具源头写出的结构化事件。
    """
    session_path = (
        _openclaw_agents_dir() / ap_id.lower() / "sessions" / f"{session_id}.jsonl"
        if _session_tail_enabled(on_text_delta, env)
        else None
    )
    raw_path = _raw_stream_path(env) if _raw_stream_enabled(on_text_delta, env) else None
    tool_path = _tool_event_path(env)
    streamed_tool_count = 0
    pos = 0
    raw_pos = 0
    tool_pos = 0
    partial = ""
    raw_partial = ""
    tool_partial = ""
    raw_pending: list[str] = []
    last_text = ""
    raw_text_streamed = False
    session_text_streamed = False
    run_id: str | None = None
    deadline = time.time() + 600
    if raw_path is not None:
        try:
            raw_pos = raw_path.stat().st_size
        except OSError:
            raw_pos = 0
    try:
        tool_pos = tool_path.stat().st_size
    except OSError:
        tool_pos = 0

    def handle_line(line: str) -> None:
        nonlocal last_text
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if obj.get("type") != "message":
            return
        msg = obj.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, list):
            return
        def emit_text_update(text: str) -> None:
            nonlocal last_text, session_text_streamed
            if not text or on_text_delta is None or raw_text_streamed:
                return
            # Some OpenClaw/session formats store the accumulated assistant text
            # each time rather than a true token delta. Forward only the suffix
            # so terminal/Dashboard streaming does not replay prior content.
            if last_text and text.startswith(last_text):
                delta = text[len(last_text):]
            else:
                delta = text
            last_text = text
            if delta:
                session_text_streamed = True
                on_text_delta(delta)

        if role == "assistant":
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    emit_text_update(item["text"])
                elif item.get("type") in {"textDelta", "delta", "contentDelta"}:
                    delta = item.get("text") or item.get("delta")
                    if (
                        isinstance(delta, str)
                        and delta
                        and on_text_delta is not None
                        and not raw_text_streamed
                    ):
                        session_text_streamed = True
                        on_text_delta(delta)
                        last_text += delta

    def refresh_run_id() -> None:
        nonlocal run_id
        if run_id:
            return
        try:
            tpath = _trajectory_path_for(ap_id, session_id)
            if not tpath or not tpath.exists():
                return
            with open(tpath, encoding="utf-8") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    candidate = obj.get("runId")
                    if isinstance(candidate, str) and candidate:
                        run_id = candidate
                        return
        except OSError:
            return

    def handle_raw_line(line: str, *, defer_without_run_id: bool = False) -> None:
        nonlocal raw_text_streamed
        if on_text_delta is None:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        event_session_id = obj.get("sessionId")
        event_run_id = obj.get("runId")
        if event_session_id != session_id and (not run_id or event_run_id != run_id):
            if defer_without_run_id and not run_id and isinstance(event_run_id, str):
                raw_pending.append(line)
            return
        if obj.get("event") != "assistant_text_stream":
            return
        if session_text_streamed:
            return
        if obj.get("evtType") != "text_delta":
            return
        delta = obj.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        raw_text_streamed = True
        on_text_delta(delta)

    def drain_raw_stream() -> None:
        nonlocal raw_pos, raw_partial, raw_pending
        if raw_path is None:
            return
        refresh_run_id()
        if run_id and raw_pending:
            pending = raw_pending
            raw_pending = []
            for pending_line in pending:
                handle_raw_line(pending_line)
        try:
            if raw_path.exists():
                with open(raw_path, encoding="utf-8") as fh:
                    fh.seek(raw_pos)
                    chunk = fh.read()
                    raw_pos = fh.tell()
                if chunk:
                    data = raw_partial + chunk
                    lines = data.splitlines(keepends=True)
                    raw_partial = ""
                    for line in lines:
                        if line.endswith("\n"):
                            handle_raw_line(line.strip(), defer_without_run_id=True)
                        else:
                            raw_partial = line
        except OSError:
            pass

    def handle_tool_event(line: str) -> None:
        nonlocal streamed_tool_count
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if obj.get("event") != "mcp_tool_call":
            return
        name = str(obj.get("tool") or "").removeprefix("multiap-tools__")
        if not name:
            return
        args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
        result = obj.get("result")
        dur_ms = obj.get("dur_ms")
        _log_mcp_tool(ap_id, name, args, result, dur_ms)
        if _tool_callback is not None:
            try:
                _tool_callback(name, args, result, dur_ms)
            except Exception:
                pass
        streamed_tool_count += 1

    def drain_tool_events() -> None:
        nonlocal tool_pos, tool_partial
        try:
            if tool_path.exists():
                with open(tool_path, encoding="utf-8") as fh:
                    fh.seek(tool_pos)
                    chunk = fh.read()
                    tool_pos = fh.tell()
                if chunk:
                    data = tool_partial + chunk
                    lines = data.splitlines(keepends=True)
                    tool_partial = ""
                    for line in lines:
                        if line.endswith("\n"):
                            handle_tool_event(line.strip())
                        else:
                            tool_partial = line
        except OSError:
            pass

    while proc.poll() is None and time.time() < deadline:
        drain_raw_stream()
        drain_tool_events()
        try:
            if session_path is not None and session_path.exists():
                with open(session_path, encoding="utf-8") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                if chunk:
                    data = partial + chunk
                    lines = data.splitlines(keepends=True)
                    partial = ""
                    for line in lines:
                        if line.endswith("\n"):
                            handle_line(line.strip())
                        else:
                            partial = line
        except OSError:
            pass
        time.sleep(0.2)

    # 子进程退出后再排空一次，避免最后一行和 stdout 几乎同时到达导致遗漏。
    drain_raw_stream()
    drain_tool_events()
    try:
        if session_path is not None and session_path.exists():
            with open(session_path, encoding="utf-8") as fh:
                fh.seek(pos)
                chunk = fh.read()
            data = partial + chunk
            for line in data.splitlines():
                if line.strip():
                    handle_line(line.strip())
    except OSError:
        pass
    drain_raw_stream()
    drain_tool_events()
    return streamed_tool_count


def _merge_no_proxy(current: str | None) -> str:
    required = ["localhost", "127.0.0.1", "::1"]
    values = [v.strip() for v in (current or "").split(",") if v.strip()]
    for item in required:
        if item not in values:
            values.append(item)
    return ",".join(values)


def _reply_text(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    payloads = data.get("payloads")
    if isinstance(payloads, list):
        for p in payloads:
            if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip():
                return p["text"]
    for key in ("finalAssistantVisibleText", "finalAssistantRawText", "text"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    if isinstance(data.get("result"), dict):
        return _reply_text(data["result"])
    return json.dumps(data, ensure_ascii=False)


def _iter_json_objects(text: str):
    """Yield JSON objects embedded in an agent reply.

    投票阶段需要扫描所有 JSON，而不是只看第一个 JSON：反对票的合法格式是
    vote JSON 后面再跟完整反提案 JSON。
    """
    text = text or ""
    for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
        try:
            parsed = json.loads(m.group(1).strip())
            if isinstance(parsed, dict):
                yield parsed
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                yield parsed
        except json.JSONDecodeError:
            pass


# ──────────────────────────────────────────────────────────────────────
# OpenClaw 会话文件定位（仅用于 raw-stream 文本事件的 runId 映射）
# ──────────────────────────────────────────────────────────────────────

def _openclaw_agents_dir() -> Path:
    """openclaw profile 的 agents 目录。

    默认 $HOME/.openclaw-{PROFILE}/agents（与 openclaw/setup.sh 一致）；
    若未来 openclaw 支持 OPENCLAW_HOME 环境变量，应在此优先读取。"""
    return _profile_state_dir() / "agents"


def _trajectory_path_for(ap_id: str, session_id: str) -> Path | None:
    """根据 session-id 推导本次会话的 trajectory 文件路径。

    仅用于 raw-stream 文本事件的 runId 过滤。工具调用不再从 trajectory 解析，
    而由 multiap_mcp.py 在工具函数源头写入 tool-events.jsonl。"""
    sessions = _openclaw_agents_dir() / ap_id.lower() / "sessions"
    pointer = sessions / f"{session_id}.trajectory-path.json"
    try:
        if pointer.exists():
            data = json.loads(pointer.read_text(encoding="utf-8"))
            rt = data.get("runtimeFile")
            if rt:
                return Path(rt)
    except Exception:
        pass
    return sessions / f"{session_id}.trajectory.jsonl"


# ──────────────────────────────────────────────────────────────────────
# 表决解析
# ──────────────────────────────────────────────────────────────────────

def _extract_vote_json(content: str) -> dict | None:
    for candidate in _iter_json_objects(content):
        if "agreed" in candidate:
            return candidate
    return None


def _vote_from_agreed(value) -> str | None:
    if isinstance(value, bool):
        return "agree" if value else "reject"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"abstain", "neutral", "弃权", "中立"}:
            return "abstain"
        if normalized in {"true", "agree", "agreed", "approve", "approved", "yes", "同意", "赞成"}:
            return "agree"
        if normalized in {"false", "reject", "rejected", "disagree", "no", "反对", "不同意", "否决"}:
            return "reject"
    return None


def read_vote(content: str) -> str:
    """返回 'agree' | 'reject' | 'abstain' | 'invalid'。

    只要回复中出现 JSON，就必须有显式 agreed 字段。否则裸参数 JSON、
    伪工具调用 JSON 等内容不能被默认解释为 reject，避免误提升为反提案。
    """
    vote = _extract_vote_json(content)
    if isinstance(vote, dict):
        result = _vote_from_agreed(vote.get("agreed"))
        return result or "invalid"

    if any(True for _ in _iter_json_objects(content)):
        return "invalid"

    if "弃权" in content:
        return "abstain"
    if any(token in content for token in ("不同意", "反对", "拒绝", "否决")):
        return "reject"
    if any(token in content for token in ("同意", "赞成")):
        return "agree"
    return "invalid"


def _deterministic_vote_fallback(voter_id: str, error: Exception) -> str | None:
    """模型投票回合连续失败时，用本地验证器给出可审计兜底票。

    兜底只判断当前提案是否满足硬约束，不生成新提案；正常模型回复路径不受影响。
    """
    if not _env_flag(None, "MULTIAP_VOTE_FAILURE_FALLBACK", "1"):
        return None
    s = _SESSION
    if s.proposal is None:
        return None
    strategy = s.strategy or resolve_strategy(s.proposal) or "co_edca"
    validation = _validate_decision(
        s.ap_state,
        s.proposal,
        strategy,
        observed_state=s.ap_state,
        observed_is_real=False,
    )
    approved = bool(validation.get("approved"))
    reason = (
        f"{voter_id.upper()} 模型投票回合连续失败；确定性验证器兜底确认当前提案满足硬约束。"
        if approved
        else f"{voter_id.upper()} 模型投票回合连续失败；确定性验证器兜底发现当前提案仍不满足硬约束。"
    )
    payload = {
        "agreed": approved,
        "reason": reason,
        "fallback": "deterministic_validator",
        "validator_summary": validation.get("summary"),
        "model_error": str(error)[-300:],
    }
    return (
        "模型投票回合失败，使用确定性验证器兜底。\n"
        "```json\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "```"
    )


def _proposal_precheck(proposal: dict | None, strategy: str | None) -> dict:
    if proposal is None:
        return {
            "approved": False,
            "strategy": strategy,
            "parse_ok": False,
            "per_ap": {},
            "global_errors": ["提案未解析出合法参数 JSON"],
            "summary": "提案预检失败：未解析出合法参数 JSON",
        }
    if strategy not in {"co_sr", "co_edca"}:
        return {
            "approved": False,
            "strategy": strategy,
            "parse_ok": True,
            "per_ap": {},
            "global_errors": [f"无法识别提案策略: {strategy}"],
            "summary": f"提案预检失败：无法识别提案策略 {strategy}",
        }
    if _proposal_has_mixed_strategy_fields(proposal):
        return {
            "approved": False,
            "strategy": strategy,
            "parse_ok": True,
            "per_ap": {},
            "global_errors": ["提案同时包含 Co-SR 与 Co-EDCA 字段；当前只允许单一策略 co_sr 或 co_edca"],
            "summary": "提案预检失败：请在 Co-SR 与 Co-EDCA 中选择一种策略，不要输出联合提案",
        }
    challenge_errors = _memory_challenge_edca_gate(proposal, strategy)
    if challenge_errors:
        return {
            "approved": False,
            "strategy": strategy,
            "parse_ok": True,
            "per_ap": {},
            "global_errors": challenge_errors,
            "summary": "提案预检失败：弱状态档位下 EDCA 参数过度保守，需给出更小改动或明确记忆证据",
        }
    result = _validate_decision(
        _SESSION.ap_state,
        proposal,
        strategy,
        observed_state=_SESSION.ap_state,
        observed_is_real=False,
    )
    result["stage"] = "proposal_precheck"
    return result


def _proposal_has_mixed_strategy_fields(proposal: dict | None) -> bool:
    if not isinstance(proposal, dict):
        return False
    has_sr = False
    has_edca = False
    for value in proposal.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            has_sr = True
            continue
        if not isinstance(value, dict):
            continue
        if value.get("tx_power_dbm") is not None or value.get("obss_pd_dbm") is not None:
            has_sr = True
        if any(value.get(key) is not None for key in (
            "CWmin", "CWmax", "AIFSN",
            "cwmin", "cwmax", "aifsn",
            "BE_CWmin", "BE_CWmax", "BE_AIFSN",
            "be_cwmin", "be_cwmax", "be_aifsn",
            "VI_CWmin", "VI_CWmax", "VI_AIFSN",
            "vi_cwmin", "vi_cwmax", "vi_aifsn",
        )):
            has_edca = True
        for nested_key in ("edca", "EDCA", "co_edca", "Co-EDCA", "coEDCA"):
            nested = value.get(nested_key)
            if isinstance(nested, dict) and any(nested.get(k) is not None for k in (
                "CWmin", "CWmax", "AIFSN", "cwmin", "cwmax", "aifsn",
                "BE_CWmin", "BE_CWmax", "BE_AIFSN", "be_cwmin", "be_cwmax", "be_aifsn",
                "VI_CWmin", "VI_CWmax", "VI_AIFSN", "vi_cwmin", "vi_cwmax", "vi_aifsn",
            )):
                has_edca = True
    return has_sr and has_edca


def _param_int(params: dict, *keys: str) -> int | None:
    for key in keys:
        if key not in params or params.get(key) is None:
            continue
        try:
            return int(params[key])
        except (TypeError, ValueError):
            return None
    return None


def _memory_challenge_edca_gate(proposal: dict | None, strategy: str | None) -> list[str]:
    """Reject over-conservative EDCA guesses in coarse-state experiments.

    This is a conversation-stage evidence gate only.  It does not relax or
    replace the final deterministic Validator.
    """
    profile = os.environ.get("MULTIAP_TOOL_PROFILE", "full").strip().lower()
    if not tool_policy.coarsens_state(profile) or strategy != "co_edca":
        return []
    if not isinstance(proposal, dict):
        return []

    anchors = {
        "high": {"CWmin": 3, "AIFSN": 2},
        "medium": {"CWmin": 7, "AIFSN": 3},
        "low": {"CWmin": 15, "AIFSN": 3},
    }
    errors: list[str] = []
    for ap_id, state in _SESSION.ap_state.items():
        if not isinstance(state, dict):
            continue
        params = proposal.get(ap_id)
        if not isinstance(params, dict):
            continue
        priority = str(state.get("traffic_priority") or "medium").lower()
        anchor = anchors.get(priority)
        if not anchor:
            continue
        cwmin = _param_int(params, "CWmin", "cwmin", "BE_CWmin", "be_cwmin")
        aifsn = _param_int(params, "AIFSN", "aifsn", "BE_AIFSN", "be_aifsn")
        if cwmin is not None and cwmin > anchor["CWmin"]:
            errors.append(
                f"{ap_id}({priority}) CWmin={cwmin} 过度保守；"
                f"memory_challenge 下无精确状态/可信记忆支撑时通常不应劣于 {anchor['CWmin']}"
            )
        if aifsn is not None and aifsn > anchor["AIFSN"]:
            errors.append(
                f"{ap_id}({priority}) AIFSN={aifsn} 过度保守；"
                f"memory_challenge 下无精确状态/可信记忆支撑时通常不应劣于 {anchor['AIFSN']}"
            )
    return errors


def _first_present_key(params: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in params and params.get(key) is not None:
            return key
    return None


def _numeric_edca_value(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric.is_integer():
        return None
    return int(numeric)


def _next_valid_cwmax_above(cwmin: int) -> int | None:
    for cw in _EDCA_CW_VALUES:
        if cw >= 7 and cw > cwmin:
            return cw
    return None


def _repair_mechanical_edca(proposal: dict | None) -> tuple[dict | None, list[dict]]:
    """修正显然可机械处理的 EDCA 关系错误。

    只处理已存在的 CWmin/CWmax 数值对，且仅在 CWmax <= CWmin 时把 CWmax
    提升到下一个合法竞争窗口值。不新增缺失字段，不改变策略，不修复范围外 CWmin。
    """
    if not isinstance(proposal, dict):
        return proposal, []

    repaired = json.loads(json.dumps(proposal, ensure_ascii=False))
    repairs: list[dict] = []

    for ap_id, params in repaired.items():
        if str(ap_id).lower() not in AP_IDS or not isinstance(params, dict):
            continue
        for ac, fields in _EDCA_GROUP_ALIASES.items():
            min_key = _first_present_key(params, fields["CWmin"])
            max_key = _first_present_key(params, fields["CWmax"])
            if min_key is None or max_key is None:
                continue
            cwmin = _numeric_edca_value(params.get(min_key))
            cwmax = _numeric_edca_value(params.get(max_key))
            if cwmin is None or cwmax is None or cwmax > cwmin:
                continue
            next_cwmax = _next_valid_cwmax_above(cwmin)
            if next_cwmax is None:
                continue
            params[max_key] = next_cwmax
            repairs.append({
                "ap": ap_id,
                "ac": ac,
                "cwmin_field": min_key,
                "cwmax_field": max_key,
                "cwmin": cwmin,
                "old": cwmax,
                "new": next_cwmax,
                "reason": "CWmax 必须大于 CWmin",
            })

    return repaired, repairs


def _format_proposal_repairs(repairs: list[dict]) -> str:
    parts = []
    for item in repairs:
        parts.append(
            f"{str(item.get('ap', '')).upper()} {item.get('ac')} "
            f"{item.get('cwmax_field')} {item.get('old')}→{item.get('new')}"
            f"（{item.get('cwmin_field')}={item.get('cwmin')}）"
        )
    return "；".join(parts)


def _repair_current_proposal(
    *,
    logger,
    proposer: str,
    proposal_num: int,
    strategy: str | None,
) -> list[dict]:
    s = _SESSION
    repaired, repairs = _repair_mechanical_edca(s.proposal)
    if not repairs:
        return []
    s.proposal = repaired
    s.strategy = resolve_strategy(s.proposal) or strategy
    summary = _format_proposal_repairs(repairs)
    if logger is not None:
        logger.proposal_repair(
            proposal_num, proposer, s.strategy, repairs, s.proposal
        )
    s.record(
        "VALIDATOR",
        f"[提案机械修复] {summary}",
        kind="validator",
    )
    return repairs


def _record_proposal_precheck(
    *,
    logger,
    proposer: str,
    proposal_num: int,
    strategy: str | None,
    result: dict,
) -> None:
    if logger is not None:
        logger.proposal_precheck(proposal_num, proposer, strategy, result)
    if result.get("approved"):
        return
    errors = "；".join(result.get("global_errors") or []) or "未提供具体错误"
    _SESSION.record(
        "VALIDATOR",
        f"[提案预检未通过] {result.get('summary', '')}\n具体问题：{errors}",
        kind="validator",
    )


def resolve_strategy(proposal: dict | None) -> str | None:
    """从提案字段推断策略，不按 AP 编号或固定业务身份预设路径。"""
    if not proposal:
        return None
    return _infer_strategy_from_proposal(proposal)


def determine_strategy(ap_state: dict) -> str:
    """Choose one of the two supported strategies: co_sr or co_edca."""
    sr_triggered = bool(_sr.analyze_interference(ap_state).get("co_sr_triggered"))
    priorities = {
        state.get("traffic_priority", "medium")
        for state in ap_state.values()
        if isinstance(state, dict)
    }
    edca_triggered = (
        len(priorities) > 1
        or _edca_parameters_imbalanced(ap_state)
        or _qos_pressure_triggered(ap_state)
    )

    if sr_triggered:
        return "co_sr"
    if edca_triggered:
        return "co_edca"
    return "noop"


def _state_num(state: dict, key: str) -> float | None:
    value = state.get(key) if isinstance(state, dict) else None
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _edca_parameters_imbalanced(ap_state: dict) -> bool:
    """Trigger EDCA negotiation when same-priority APs expose unequal BE/VI settings."""
    for fields in (
        ("be_cwmin", "be_cwmax", "be_aifsn"),
        ("vi_cwmin", "vi_cwmax", "vi_aifsn"),
        ("cwmin", "cwmax", "aifsn"),
    ):
        values = {
            tuple(_state_num(state, key) for key in fields)
            for state in (ap_state or {}).values()
            if isinstance(state, dict)
            and all(_state_num(state, key) is not None for key in fields)
        }
        if len(values) > 1:
            return True
    return False


def _qos_pressure_triggered(ap_state: dict) -> bool:
    """Trigger EDCA negotiation on visible starvation, high delay, loss, or SLA violations."""
    latency_threshold = _env_float("MULTIAP_QOS_TRIGGER_LATENCY_MS", 100.0)
    loss_threshold = _env_float("MULTIAP_QOS_TRIGGER_LOSS_PCT", 5.0)
    throughput_floor = _env_float("MULTIAP_QOS_TRIGGER_THROUGHPUT_MBPS", 0.01)
    for state in (ap_state or {}).values():
        if not isinstance(state, dict):
            continue
        if state.get("sla_violations"):
            return True
        feedback = state.get("sta_feedback_summary")
        if isinstance(feedback, dict) and feedback.get("status") == "violated":
            return True
        throughput = _state_num(state, "throughput_mbps_user")
        if throughput is not None and throughput <= throughput_floor:
            return True
        latency = _state_num(state, "latency_ms")
        if latency is not None and latency >= latency_threshold:
            return True
        loss = _state_num(state, "packet_loss_pct")
        if loss is not None and loss >= loss_threshold:
            return True
    return False


_NOOP_MARKERS = (
    "暂不调整", "无需调整", "无须调整", "无需变更", "无需更改", "保持现状",
    "维持现状", "维持当前", "保持当前", "不做调整", "不作调整", "无需协商",
    "no adjustment", "no change",
)


def _proposer_declares_noop(reply: str) -> bool:
    """提案方未给出 ap1/ap2/ap3 JSON，但明确表态"无需调整"时返回 True。
    用于把"基于证据决定不改"与"格式/解析错误"区分开，避免误报 proposal_parse_error。"""
    return any(m in reply for m in _NOOP_MARKERS)


# ──────────────────────────────────────────────────────────────────────
# 阶段指令（忠实移植自 orchestrator.py）
# ──────────────────────────────────────────────────────────────────────

def broadcast_instruction(ap_id: str) -> str:
    visible = _agent_visible_state()
    state_json = json.dumps(visible[ap_id], ensure_ascii=False, indent=2)
    return (
        f"请广播你（{ap_id.upper()}）的当前状态。\n"
        "发言开头先明确说出你是哪个 AP，然后用自然语言完整说明你的实测参数，"
        "最后用一两句话简述你当前状态，例如信道是否偏忙、邻居信号是否偏强、"
        "业务质量是否稳定；如果状态中包含 stas 或 sta_feedback_summary，也概括关联 STA 的 QoE/SLA 反馈。\n\n"
        "你的实测数据如下，请覆盖所有字段，但不要只复制 JSON，也不要使用固定模板：\n"
        f"{state_json}\n\n"
        "只播报你自己的数据和你本机扫描到的邻居 RSSI，不要引用或分析其他 AP 自己上报的业务指标。"
    )


def _invalid_broadcast_reply(reply: str | None) -> bool:
    text = (reply or "").strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except Exception:
        return False
    if parsed in ({}, [], None):
        return True
    return isinstance(parsed, dict) and set(parsed) <= {
        "tool", "name", "arguments", "args"
    }


def _broadcast_fallback(ap_id: str) -> str:
    state = (_agent_visible_state().get(ap_id) or {})

    def pick(*keys, default="未知"):
        for key in keys:
            value = state.get(key)
            if value is not None:
                return value
        return default

    neighbors = state.get("neighbor_rssi_dbm") or {}
    neighbor_text = (
        ", ".join(f"{peer} {rssi} dBm" for peer, rssi in sorted(neighbors.items()))
        if isinstance(neighbors, dict) and neighbors else "未提供"
    )
    be = (
        f"{pick('CWmin', 'cwmin')}/{pick('CWmax', 'cwmax')}/"
        f"{pick('AIFSN', 'aifsn')}"
    )
    vi = (
        f"{pick('vi_cwmin', 'VI_CWmin')}/{pick('vi_cwmax', 'VI_CWmax')}/"
        f"{pick('vi_aifsn', 'VI_AIFSN')}"
    )
    return (
        f"我是 {ap_id.upper()}。当前 TX Power={pick('tx_power_dbm')} dBm；"
        f"BE 队列 CWmin/CWmax/AIFSN={be}，VI 队列={vi}。"
        f"本机扫描邻居 RSSI：{neighbor_text}；STA RSSI={pick('sta_rssi_dbm')} dBm，"
        f"SINR={pick('sinr_db')} dB。业务类型={pick('service_name', 'business_type')}，"
        f"优先级={pick('traffic_priority')}，用户吞吐={pick('throughput_mbps_user')} Mbps，"
        f"延迟={pick('latency_ms')} ms，抖动={pick('jitter_ms')} ms，"
        f"丢包={pick('packet_loss_pct')}%。SLA 状态="
        f"{pick('sla_status', 'sta_sla_status')}。整体按当前可见状态播报，"
        "若缺少字段则以上述“未知/未提供”为准。"
    )


def _trust_suffix(memory: dict) -> str:
    """反思字段展示：信任分 + 最近验证时间（反思关闭时记忆无 trust 字段，返回空）。"""
    if memory.get("trust") is None:
        return ""
    verified = memory.get("last_verified_at")
    verified_text = str(verified)[:10] if verified else "未再验证"
    return f"，信任={memory['trust']:.2f}，最近验证={verified_text}"


def propose_instruction(
    proposer_id: str,
    strategy_hint: str | None = None,
    recalled_episodes: list[dict] | None = None,
    recalled_rules: list[dict] | None = None,
    recalled_warnings: list[dict] | None = None,
) -> str:
    state_summary = json.dumps(_agent_visible_state(), ensure_ascii=False, indent=2)
    tool_profile = os.environ.get("MULTIAP_TOOL_PROFILE", "full").strip().lower()
    visible_tool_profile = tool_policy.agent_visible_profile(tool_profile)
    tools_available = bool(tool_policy.allowed_tools(tool_profile))
    use_memory = memory_enabled()
    memory_basis = (
        "已给状态、对话记录、历史正例/失败警告和自身推理"
        if use_memory else
        "已给状态、当前对话记录和自身推理"
    )
    candidate_basis = (
        "历史正例/失败警告"
        if use_memory else
        "当前状态和对话记录"
    )
    no_tool_basis = (
        "上方状态、对话记录和历史记忆"
        if use_memory else
        "上方状态和当前对话记录"
    )
    no_tool_evidence = (
        "当前状态、对话记录和历史记忆"
        if use_memory else
        "当前状态和对话记录"
    )
    if visible_tool_profile != "full":
        if not tools_available:
            tool_policy_hint = (
                f"【工具能力限制】当前 MULTIAP_TOOL_PROFILE={visible_tool_profile}，"
                f"本回合没有任何可调用工具。请只基于{memory_basis}提出候选；"
                "不要声称调用过工具，也不要把推理估计写成工具结论。\n\n"
            )
        else:
            tool_policy_hint = (
                f"【工具能力限制】当前 MULTIAP_TOOL_PROFILE={visible_tool_profile}。"
                "部分会直接给出推荐答案、排序或完整有效性判断的工具可能不可用，"
                f"或只返回范围/合法性结果。请优先利用{candidate_basis}提出候选，"
                "再用可用的状态、STA 反馈和候选验算工具确认硬约束；不要把缺失的工具结果"
                "伪造成已调用结论。\n\n"
            )
        if tool_policy.coarsens_state(tool_profile):
            tool_policy_hint += (
                "【状态可见性限制】当前档位会隐藏精确 EDCA/TX/QoS/RSSI 数值，只保留业务、"
                "优先级、SLA 状态和粗粒度干扰等级。不要声称知道被隐藏的当前参数；"
                "若引用历史动作，请明确它是记忆假设，并说明如何规避历史失败警告。\n\n"
            )
    else:
        tool_policy_hint = ""
    if tool_policy.coarsens_state(tool_profile) or not tools_available:
        tool_path_hint = ""
    elif strategy_hint == "co_edca":
        tool_path_hint = (
            "【本轮快速路径提示】当前全网证据已显示：邻居 RSSI 未触发 Co-SR，"
            "但业务优先级/EDCA 存在差异化需求。可以优先考虑 Co-EDCA；"
            "如发现最新状态中出现更明显干扰，应改选 Co-SR，而不是混合两类字段。"
            "你可按需要使用 get_latest_ap_states、get_sta_feedback、validate_edca_proposal 或 SR 相关工具补充证据。\n\n"
        )
    elif strategy_hint == "co_sr":
        tool_path_hint = (
            "【本轮快速路径提示】当前全网证据显示邻居 RSSI 触发 Co-SR，且 EDCA 差异化证据不强。"
            "可以优先考虑 Co-SR；如最新状态显示 SR 并非主导问题，应改选 Co-EDCA，而不是混合两类字段。"
            "你可按需要使用 get_latest_ap_states、get_sta_feedback、analyze_sr_interference、select_sr_concurrent_groups、"
            "evaluate_sr_candidate 或 validate_edca_proposal 补充证据。\n\n"
        )
    else:
        tool_path_hint = ""
    if tools_available:
        state_update_hint = (
            "可结合 get_latest_ap_states 获取最新状态，也可以基于已给出的状态和对话记录先形成判断。"
        )
        sr_trigger_hint = "邻居 RSSI 偏强，或 analyze_sr_interference 的 co_sr_triggered=true"
        sr_guidance = (
            "【Co-SR】降低各 AP 的 TX Power 减少 OBSS 干扰。若采用该路径，建议先判断"
            "可用并发组：get_latest_ap_states → analyze_sr_interference → select_sr_concurrent_groups；"
            "再用 evaluate_sr_candidate（传入 proposed_powers，部分并发再传 concurrent_group）辅助自检。"
            "功率取最大必要降幅且为整数 dBm。提案 JSON 只含每个 AP 的 tx_power_dbm，并附 "
            '`"_sr": {"concurrent_group": [...], "non_concurrent_aps": [...]}`。'
        )
        edca_guidance = (
            "【Co-EDCA】按当前状态中的 traffic_priority、QoS 和 EDCA 参数差异调整 CWmin/CWmax/AIFSN。"
            "CWmin/CWmax 必须使用可下发实际 CW 离散值 3/7/15/31/63/127/255/511/1023，不能使用 23 等中间值。"
            "当优先级确实不同，满足 high.CWmin ≤ medium ≤ low、high.AIFSN ≤ medium ≤ low；"
            "同优先级或未知优先级时不要强行制造梯度。可用 validate_edca_proposal（传 proposed_edca）辅助自检。"
        )
        sta_guidance = (
            "【STA 反馈】如果状态或 get_sta_feedback 显示关联 STA 的 SLA/QoE 约束，"
            "请把它作为提案的边界条件：STA 可反馈吞吐、时延、jitter、丢包、RSSI/SINR 和 SLA 状态；"
            "STA 不直接给控制参数，AP 需要把这些反馈转化为 TX Power/EDCA 的可执行调整。"
        )
        evidence_wording = (
            "如调用验算工具，请把你打算提的参数显式作为工具参数传入；"
            "未实际调用工具时，请把判断表述为基于当前状态和参数的推理估计，避免和真实工具结果混淆；"
        )
    else:
        state_update_hint = (
            f"当前没有任何可调用工具；请只基于{no_tool_basis}形成判断，不要输出工具调用 JSON。"
        )
        sr_trigger_hint = "邻居 RSSI 偏强"
        sr_guidance = (
            "【Co-SR】降低各 AP 的 TX Power 减少 OBSS 干扰。若采用该路径，请基于已给邻居 RSSI、"
            "STA RSSI/SINR/SLA 和对话记录推理可用并发组；功率取最大必要降幅且为整数 dBm。"
            "提案 JSON 只含每个 AP 的 tx_power_dbm，并附 "
            '`"_sr": {"concurrent_group": [...], "non_concurrent_aps": [...]}`。'
        )
        edca_guidance = (
            "【Co-EDCA】按当前状态中的 traffic_priority、QoS 和 EDCA 参数差异调整 CWmin/CWmax/AIFSN。"
            "CWmin/CWmax 必须使用可下发实际 CW 离散值 3/7/15/31/63/127/255/511/1023，不能使用 23 等中间值。"
            "当优先级确实不同，满足 high.CWmin ≤ medium ≤ low、high.AIFSN ≤ medium ≤ low；"
            "同优先级或未知优先级时不要强行制造梯度。"
        )
        sta_guidance = (
            "【STA 反馈】如果状态显示关联 STA 的 SLA/QoE 约束，请把它作为提案的边界条件："
            "STA 可反馈吞吐、时延、jitter、丢包、RSSI/SINR 和 SLA 状态；"
            "STA 不直接给控制参数，AP 需要把这些反馈转化为 TX Power/EDCA 的可执行调整。"
        )
        evidence_wording = (
            f"当前没有工具可用；请把判断表述为基于{no_tool_evidence}的推理估计，"
            "避免和真实工具结果混淆；"
        )
    memory_hint = ""
    if recalled_episodes:
        recalled_episodes = recalled_episodes if use_memory else []
    if recalled_episodes:
        lines = [
            "【历史案例假设（每条是待检验的假设，不是事实：前提成立才可参考；"
            "前提=当前状态与其相似、且信任分未衰减；可结合最新状态和必要工具重新验算，"
            "若证据不支持就放弃该假设）】"
        ]
        has_near_miss = False
        best_improved_action = None
        for item in recalled_episodes[:3]:
            metrics = item.get("metrics") or {}
            evaluation = item.get("evaluation") or {}
            if evaluation.get("final_verdict"):
                score = evaluation.get("final_score")
                if (
                    evaluation.get("final_verdict") == "neutral"
                    and evaluation.get("approved") is False
                    and isinstance(score, (int, float))
                    and score >= 0.03
                ):
                    has_near_miss = True
                    feedback = (
                        f"接近成功阈值但未通过：score={score}；"
                        "可优先复用该动作并只做小幅修正"
                    )
                else:
                    verdict_map = {
                        "improved": "实际改善", "degraded": "实际恶化",
                        "neutral": "无明显变化", "inconclusive": "数据不足",
                    }
                    if (
                        evaluation.get("final_verdict") == "improved"
                        and isinstance(score, (int, float))
                        and float(item.get("quality_score", 0) or 0) >= 0.7
                        and isinstance(item.get("decision"), dict)
                    ):
                        best_improved_action = item.get("decision")
                    feedback = (
                        f"预测：复用该动作应得到"
                        f"{verdict_map.get(evaluation['final_verdict'], evaluation['final_verdict'])}"
                        f"(历史置信度={evaluation.get('final_confidence', 0)})"
                    )
            else:
                feedback = f"实测反馈={'可用' if metrics.get('available') else '尚无'}"
            lines.append(
                f"- 前提：相似度={item.get('similarity', 0):.3f}，"
                f"质量={item.get('quality_score', 0):.2f}"
                + _trust_suffix(item) + "；"
                f"策略={item.get('strategy')}，结果={item.get('outcome')}，"
                f"动作={json.dumps(item.get('decision'), ensure_ascii=False)}；"
                f"{feedback}"
            )
        if has_near_miss and not tools_available:
            lines.append(
                "【无工具记忆约束】当前没有工具可重新搜索大范围候选；若上方存在"
                "接近成功阈值案例，本轮首个提案必须复用该历史动作，或只改动其中"
                "一个参数一个离散档位。除非当前状态出现新的 SLA 违规或强干扰证据，"
                "不得切换到另一策略族。"
            )
        if best_improved_action:
            lines.append(
                "【高质量成功记忆约束】上方存在已验证 improved 的高质量相似案例。"
                "本轮首个提案必须优先复用该历史动作，不要从更激进或更保守的参数重新试错。"
                "只有当最新状态出现新的 SLA 违规、强干扰证据，或可用工具明确验证该动作不适用时，"
                "才允许偏离。历史动作="
                f"{json.dumps(best_improved_action, ensure_ascii=False)}"
            )
        memory_hint = "\n".join(lines) + "\n\n"
    warning_hint = ""
    if recalled_warnings:
        recalled_warnings = recalled_warnings if use_memory else []
    if recalled_warnings:
        lines = ["【历史失败警告（不建议直接复用其动作，请说明如何规避相同风险）】"]
        for item in recalled_warnings[:2]:
            evaluation = item.get("evaluation") or {}
            verdict = evaluation.get("final_verdict")
            result_label = (
                "恶化" if verdict == "degraded"
                else "未改善且 QoS 验收未通过"
                if verdict == "neutral" and evaluation.get("approved") is False
                else str(verdict or "未知")
            )
            lines.append(
                f"- 策略={item.get('strategy')}，历史动作="
                f"{json.dumps(item.get('decision'), ensure_ascii=False)}，"
                f"实际结果={result_label}（置信度={evaluation.get('final_confidence', 0)}）"
                + _trust_suffix(item) + "，"
                f"案例总结={item.get('case_narrative') or '无'}"
            )
        warning_hint = "\n".join(lines) + "\n\n"
    rule_hint = ""
    if recalled_rules:
        recalled_rules = recalled_rules if use_memory else []
    if recalled_rules:
        from src.memory import format_rule
        rule_lines = [
            "【历史规律（本拓扑下多个带真实反馈案例的统计归纳，比单个案例更可靠；"
            "仍须按当前最新状态重新验算）】"
        ]
        for rule in recalled_rules[:3]:
            rule_lines.append("- " + format_rule(rule))
        rule_hint = "\n".join(rule_lines) + "\n\n"
    history_hint = (
        "【重要】请先完整阅读上方的对话记录。\n"
        "如果记录中有 VALIDATOR 发出的验证失败消息，新提案应优先回应其中列出的具体问题。\n"
        "如果记录中已有历史提案和拒绝原因，请明确回应各方此前提出的约束顾虑，"
        "而不是重复一个已被否决的方案。协商历史越长，越需要向各方约束的交集靠拢。\n\n"
    )
    return (
        f"你（{proposer_id.upper()}）是本轮的提案方，请发起参数调整提案。\n\n"
        f"{history_hint}"
        f"{tool_policy_hint}"
        f"{tool_path_hint}"
        f"所有 AP 的初始状态数据（供参考）：\n{state_summary}\n\n"
        f"{state_update_hint}\n\n"
        "【路径选择规则（基于实时证据，不按 AP 编号或固定业务身份预设）】\n"
        f"  · 若存在强干扰（{sr_trigger_hint}）→ 可选 Co-SR。\n"
        "  · 若 traffic_priority、QoS 或当前 EDCA 参数显示需要差异化竞争机会 → 可选 Co-EDCA。\n"
        "  · 若 STA 反馈显示某业务 SLA 已违反或接近边界，应把它作为 QoE 约束，而不是只看 AP 聚合指标。\n"
        "  · 若两类问题同时成立 → 选择当前更主导的一类先处理；本轮只允许 Co-SR 或 Co-EDCA 单一路径。\n"
        "  · 若证据不足 → 说明暂不调整或提出最小改动方案。\n"
        "  · 不要为了完成协商强行制造 SR 或 EDCA 问题。\n\n"
        f"{sr_guidance}\n\n"
        f"{edca_guidance}\n"
        "【重要】若各 AP 优先级确实不同（如 high/medium/low）但当前 EDCA 参数相同（未差异化），"
        "这本身就是需要 Co-EDCA 的证据：应让高优先级获得更小的 CWmin/CWmax/AIFSN、低优先级更大，"
        "切勿以『统一参数已平凡满足单调性』为由判定无需调整——未体现优先级差异即未达成本场景目标。\n\n"
        f"{sta_guidance}\n\n"
        "提案须简洁说明：选哪条路径及原因、每个 AP 的最终参数与依据、预期改善与权衡。\n"
        f"{evidence_wording}"
        "编排层会在投票前执行确定性预检，未通过会把具体问题写回协商记录。\n"
        "当前只支持两种策略：Co-SR 或 Co-EDCA。提案 JSON 不要同时包含 tx_power_dbm/obss_pd_dbm 与 EDCA 字段。\n"
        "提案末尾请用 ```json 代码块附参数摘要，顶层键为 ap1/ap2/ap3，"
        "每个 AP 的值使用对象（参数写在对象内部，避免裸数值）。"
    )


def vote_instruction(voter_id: str, proposer_id: str, strategy: str,
                     proposal: dict, proposal_num: int) -> str:
    tool_profile = os.environ.get("MULTIAP_TOOL_PROFILE", "full").strip().lower()
    visible_tool_profile = tool_policy.agent_visible_profile(tool_profile)
    tools_available = bool(tool_policy.allowed_tools(tool_profile))
    if tools_available:
        verify_hint = {
            "co_sr":   "关注你自己的 TX Power、关联 STA 的 RSSI/SINR/SLA、CCA 余量；如有真实工具结果，再参考 evaluate_sr_candidate 的 valid/errors",
            "co_edca": "关注你自己的 traffic_priority、关联 STA QoE/SLA、当前 EDCA 参数与优先级排序；如有真实工具结果，再参考 valid/errors",
        }.get(strategy, "关注参数对你的影响；如有真实工具结果，再参考 valid/errors")
        tool_vote_hint = (
            "可结合 get_latest_ap_states、get_sta_feedback 或验算工具检查该提案。"
            "如果调用验算工具，请把上方提案中针对各 AP 的参数"
            "（Co-SR 传 proposed_powers，部分并发连同 concurrent_group；Co-EDCA 传 proposed_edca）"
            "显式填入工具参数；Co-EDCA 的 CWmin/CWmax 必须是 3/7/15/31/63/127/255/511/1023；"
            "编排层也会执行确定性安全验证。"
            "未实际调用工具时，请把相关判断表述为基于当前状态和参数的推理估计，避免和真实工具结果混淆。"
        )
    else:
        verify_hint = {
            "co_sr":   "关注你自己的 TX Power、关联 STA 的 RSSI/SINR/SLA、CCA 余量，并基于已给状态和对话记录推理",
            "co_edca": "关注你自己的 traffic_priority、关联 STA QoE/SLA、当前 EDCA 参数与优先级排序，并基于已给状态和对话记录推理",
        }.get(strategy, "关注参数对你的影响，并基于已给状态和对话记录推理")
        tool_vote_hint = (
            f"当前 MULTIAP_TOOL_PROFILE={visible_tool_profile}，没有任何可调用工具。"
            "请不要输出工具调用 JSON，也不要声称调用过 get_latest_ap_states、get_sta_feedback 或验算工具；"
            "只能基于上方状态、对话记录和最新提案参数进行推理。编排层会执行确定性安全验证。"
        )
    if proposal_num >= 4:
        stall_hint = (f"\n\n【死锁警告：已是第 {proposal_num} 个提案】若各方在重复相似参数，"
                      "应选择弃权让当前折中方案通过，而不是再提一个同样无法满足约束的新方案。")
    elif proposal_num >= 2:
        stall_hint = f"\n\n【注意：已是第 {proposal_num} 个提案】若出现重复请考虑弃权，避免死锁。"
    else:
        stall_hint = ""
    challenge_hint = ""
    if tool_policy.coarsens_state(tool_profile):
        memory_clause = (
            "可信历史记忆、"
            if memory_enabled() else
            ""
        )
        challenge_hint = (
            "\n\n【弱状态提示】当前档位隐藏精确参数与效果指标。"
            "如果提案只通过弱校验，未报错只代表没有明显格式/范围硬错误，"
            "不代表效果已经被工具证明。投票时请区分：真实状态、"
            + ("历史记忆、" if memory_enabled() else "")
            + "弱工具结果和推理估计。"
            "若提案把你自己的 EDCA 设置得明显过度保守，且没有"
            f"{memory_clause}"
            "当前 SLA 风险或明确失败警告支撑，不应仅因范围合法就同意；"
            "应反对并给出更小改动的反提案。经验锚点：high 通常不应劣于 "
            "CWmin=3/AIFSN=2，medium 通常不应劣于 CWmin=7/AIFSN=3，"
            "low 通常不应劣于 CWmin=15/AIFSN=3。"
        )
    proposal_json = json.dumps(proposal, ensure_ascii=False, indent=2)
    return (
        "【第一步】请完整阅读上方对话记录，梳理此前所有提案及每次拒绝的原因。\n\n"
        f"【第二步】验算 {proposer_id.upper()} 的最新提案（提案#{proposal_num}）中针对你自己（{voter_id.upper()}）的参数。\n\n"
        f"最新提案参数：\n{proposal_json}\n\n"
        f"{tool_vote_hint}\n"
        "然后用自然语言给出判断。"
        f"重点参考：{verify_hint}。\n\n"
        "三种表态：\n"
        "【同意】满足约束或可接受折中。末尾附 ```json\n{\"agreed\": true, \"reason\": \"...\"}\n```\n"
        "【弃权】未完全满足但找不到更好方案，或协商已重复。等同同意，无需反提案。末尾附 "
        "```json\n{\"agreed\": \"abstain\", \"reason\": \"...\"}\n```\n"
        "【反对】你有具体替代方案。同一条回复中先附 ```json\n{\"agreed\": false, \"reason\": \"...\"}\n``` "
        "再附完整反提案 JSON（顶层键 ap1/ap2/ap3）。反提案须兼顾各方约束；若选 Co-SR，"
        "建议说明并发组依据并写 _sr.concurrent_group。"
        f"{challenge_hint}"
        f"{stall_hint}"
    )


def repair_vote_instruction(
    voter_id: str,
    proposer_id: str,
    strategy: str,
    proposal: dict,
    proposal_num: int,
) -> str:
    proposal_json = json.dumps(proposal, ensure_ascii=False, indent=2)
    tool_profile = os.environ.get("MULTIAP_TOOL_PROFILE", "full").strip().lower()
    visible_tool_profile = tool_policy.agent_visible_profile(tool_profile)
    no_tool_hint = ""
    if not tool_policy.allowed_tools(tool_profile):
        no_tool_hint = (
            f"当前 MULTIAP_TOOL_PROFILE={visible_tool_profile}，没有任何可调用工具；"
            "不要输出 {\"tool\": ...} 或 {\"arguments\": ...} 形式的伪工具调用。\n"
        )
    return (
        "上一条投票回复格式无效：编排层没有找到明确的 agreed 表态 JSON，"
        "或回复中出现了裸参数 JSON/伪工具调用 JSON。\n"
        f"你仍然是 {voter_id.upper()}，正在表决 {proposer_id.upper()} 的提案#{proposal_num}。\n"
        f"{no_tool_hint}"
        f"策略={strategy}，当前提案参数如下：\n{proposal_json}\n\n"
        "请只补充本次投票，不要重新广播状态。\n"
        "若同意，输出一个 JSON 代码块：{\"agreed\": true, \"reason\": \"...\"}。\n"
        "若弃权，输出一个 JSON 代码块：{\"agreed\": \"abstain\", \"reason\": \"...\"}。\n"
        "若反对，先输出一个 JSON 代码块：{\"agreed\": false, \"reason\": \"...\"}，"
        "再输出一个完整反提案 JSON 代码块（顶层键 ap1/ap2/ap3）。"
    )


def repair_counter_instruction() -> str:
    """反对者回复中未解析出反提案 JSON 时的「修复轮」指令（移植自
    orchestrator._phase_counter_propose）。"""
    tool_profile = os.environ.get("MULTIAP_TOOL_PROFILE", "full").strip().lower()
    visible_tool_profile = tool_policy.agent_visible_profile(tool_profile)
    if tool_policy.allowed_tools(tool_profile):
        evidence_hint = (
            "如果选择 Co-SR，请说明可用并发组依据；可使用 get_latest_ap_states、"
            "analyze_sr_interference、select_sr_concurrent_groups 辅助判断，"
            "并在 JSON 中写入 _sr.concurrent_group。\n"
        )
    else:
        basis = "已给状态、对话记录和历史记忆" if memory_enabled() else "已给状态和当前对话记录"
        evidence_hint = (
            f"当前 MULTIAP_TOOL_PROFILE={visible_tool_profile}，没有任何可调用工具；"
            f"请只基于{basis}推理，不要输出伪工具调用。"
            "如果选择 Co-SR，请在 JSON 中写入你基于现有证据推理出的 _sr.concurrent_group。\n"
        )
    return (
        "你已表示反对，但回复中未找到可解析的参数 JSON。\n"
        "请回顾上方完整协商历史，综合所有 AP 此前提出的约束和顾虑，"
        "给出一个能兼顾所有人需求的反提案。\n"
        "你可以根据实时证据选择协商路径：Co-SR 使用 tx_power_dbm 字段，"
        "Co-EDCA 使用 CWmin/CWmax/AIFSN 字段；当前只允许选择其中一种单一路径，"
        "Co-EDCA 的 CWmin/CWmax 必须是 3/7/15/31/63/127/255/511/1023，不能使用 23 等中间值；"
        "不要同时输出 TX Power/OBSS_PD 与 EDCA 字段；"
        "证据不足时不要为了形成反提案强行改变无关参数。\n"
        f"{evidence_hint}"
        "请只输出一个 ```json 代码块，JSON 顶层键为 ap1、ap2、ap3。不要写解释。"
    )


def final_instruction(proposer_id: str, proposal: dict) -> str:
    proposal_json = json.dumps(proposal, ensure_ascii=False, indent=2)
    return (
        "所有 AP 已同意提案。\n"
        f"已通过的提案参数 JSON 如下，最终决策请与它保持一致：\n{proposal_json}\n\n"
        "请输出最终的 JSON 决策方案（严格合法、JSON 内不包含注释），顶层键为 ap1/ap2/ap3，"
        "然后在下一行写【协商结束】。"
    )


# ──────────────────────────────────────────────────────────────────────
# 阶段执行
# ──────────────────────────────────────────────────────────────────────

def _run_agent_turn(
    ap_id: str,
    phase: int,
    role: str,
    instruction: str,
    *,
    logger=None,
    on_event_start: Callable | None = None,
    on_event_chunk: Callable | None = None,
    thinking: str = "off",
    extra_env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    stream_enabled = (
        logger is not None
        or on_event_start is not None
        or on_event_chunk is not None
    )
    streamed = False
    started = False

    def ensure_started() -> None:
        nonlocal started
        if started:
            return
        started = True
        if logger is not None:
            logger.agent_speak_start(ap_id, phase, role)
        if on_event_start:
            on_event_start(role, ap_id)

    def on_text(text: str) -> None:
        nonlocal streamed
        if not text:
            return
        ensure_started()
        streamed = True
        if logger is not None:
            logger.push_chunk(ap_id, text)
            logger.agent_speak_chunk(ap_id, text)
        if on_event_chunk:
            on_event_chunk(role, ap_id, text)

    if stream_enabled:
        ensure_started()
    if logger is not None:
        s = _SESSION
        logger.context_manifest(ap_id, {
            "shared_turns": len(s.transcript),
            "shared_memory_revision": s.memory_manager.memory.revision,
            "local_memory_revision": s.memory_revisions.get(ap_id, 0),
            "positive_episode_ids": [e.get("episode_id") for e in s.recalled_episodes],
            "warning_episode_ids": [e.get("episode_id") for e in s.recalled_warnings],
            "semantic_rule_ids": [r.get("rule_id") for r in s.recalled_rules],
        })
    try:
        driver = _agent_driver or drive_ap
        reply = driver(
            ap_id,
            instruction,
            thinking=thinking,
            extra_env=extra_env,
            on_text_delta=on_text if stream_enabled else None,
        )
    except TypeError as exc:
        if "on_text_delta" not in str(exc):
            raise
        driver = _agent_driver or drive_ap
        reply = driver(
            ap_id,
            instruction,
            thinking=thinking,
            extra_env=extra_env,
        )
    if stream_enabled and not streamed:
        on_text(reply)
    if logger is not None:
        logger.agent_speak(
            agent=ap_id,
            phase=phase,
            role=role,
            instruction=instruction,
            response=reply,
            duration_ms=0.0,
        )
    return reply, streamed


def run_propose(
    proposer_id: str,
    strategy_hint: str | None = None,
    *,
    logger=None,
    on_event_start: Callable | None = None,
    on_event_chunk: Callable | None = None,
) -> dict:
    instruction = propose_instruction(
        proposer_id, strategy_hint, _SESSION.recalled_episodes, _SESSION.recalled_rules,
        _SESSION.recalled_warnings,
    )
    if logger is not None:
        # R3：注入即记账——本次提案依赖了哪些记忆、注入时信任分与可证伪预测。
        conclusive_local = [
            item for item in _SESSION.agent_recalled_episodes.get(proposer_id.lower(), [])
            if (item.get("evaluation") or {}).get("final_verdict")
            in {"improved", "neutral", "degraded"}
        ][:20]
        logger.memory_reliance(
            proposer_id,
            episodes=_SESSION.recalled_episodes[:3],
            warnings=_SESSION.recalled_warnings[:2],
            rules=_SESSION.recalled_rules[:3],
            agent_episodes=[(proposer_id.lower(), item) for item in conclusive_local],
            proposal_num=_SESSION.proposal_num + 1,
        )
    anchor_decision, anchor_memory = _best_improved_memory_decision(_SESSION.recalled_episodes)
    if (
        anchor_decision is not None
        and _SESSION.proposal_num == 0
        and _memory_fast_proposal_enabled()
    ):
        proposal = _with_sr_concurrent_group(anchor_decision, _SESSION.ap_state)
        strategy = resolve_strategy(proposal)
        score = (anchor_memory.get("evaluation") or {}).get("final_score")
        quality = anchor_memory.get("quality_score")
        reply = (
            "【记忆锚定提案】召回到高质量 improved 历史案例，"
            "本轮直接复用该已验证动作以减少重复试错。\n"
            f"quality={quality}, score={score}\n"
            "```json\n"
            f"{json.dumps(proposal, ensure_ascii=False, indent=2)}\n"
            "```"
        )
        _SESSION.record(
            "MEMORY",
            "【记忆锚定】召回到高质量 improved 历史案例，"
            f"直接生成首个提案。quality={quality}, score={score}, "
            f"action={json.dumps(proposal, ensure_ascii=False)}",
            kind="memory_anchor",
        )
        if logger is not None:
            logger.agent_speak(
                agent=proposer_id,
                phase=3,
                role="proposer",
                instruction=instruction,
                response=reply,
                duration_ms=0.0,
            )
        if on_event_start:
            on_event_start("proposer", proposer_id)
        if on_event_chunk:
            on_event_chunk("proposer", proposer_id, reply)
        _SESSION.record(proposer_id.upper(), reply, kind="proposal")
        _SESSION.proposer = proposer_id
        _SESSION.proposal = proposal
        _SESSION.strategy = strategy
        _SESSION.proposal_num += 1
        _mark_memory_anchor_proposal(proposal, anchor_memory)
        if logger is not None and proposal is not None:
            logger.record_proposal(
                _SESSION.proposal_num, proposer_id, strategy, proposal,
            )
        return {"proposer": proposer_id, "reply": reply, "proposal": proposal,
                "strategy": strategy, "proposal_num": _SESSION.proposal_num,
                "parsed": proposal is not None}
    reply, _ = _run_agent_turn(
        proposer_id,
        3,
        "proposer",
        instruction,
        logger=logger,
        on_event_start=on_event_start,
        on_event_chunk=on_event_chunk,
    )
    _SESSION.record(proposer_id.upper(), reply, kind="proposal")
    proposal = _extract_proposal(reply)
    if proposal is not None:
        proposal = _with_sr_concurrent_group(proposal, _SESSION.ap_state)
    if anchor_decision is not None and _SESSION.proposal_num == 0:
        anchored = _with_sr_concurrent_group(anchor_decision, _SESSION.ap_state)
        if proposal is None or anchored != proposal:
            score = (anchor_memory.get("evaluation") or {}).get("final_score")
            quality = anchor_memory.get("quality_score")
            _SESSION.record(
                "MEMORY",
                "【记忆锚定】召回到高质量 improved 历史案例，"
                f"将首个提案锚定为该历史动作以减少重复试错。"
                f"quality={quality}, score={score}, "
                f"action={json.dumps(anchored, ensure_ascii=False)}",
                kind="memory_anchor",
            )
            proposal = anchored
    strategy = resolve_strategy(proposal)
    _SESSION.proposer = proposer_id
    _SESSION.proposal = proposal
    _SESSION.strategy = strategy
    _SESSION.proposal_num += 1
    if anchor_decision is not None and anchor_memory is not None:
        anchored = _with_sr_concurrent_group(anchor_decision, _SESSION.ap_state)
        if proposal is not None and _canonical_json(proposal) == _canonical_json(anchored):
            _mark_memory_anchor_proposal(proposal, anchor_memory)
    if logger is not None and proposal is not None:
        # 参数时间线：结构化保留每个候选提案的参数（与发言原文互补）。
        logger.record_proposal(
            _SESSION.proposal_num, proposer_id, strategy, proposal,
        )
    return {"proposer": proposer_id, "reply": reply, "proposal": proposal,
            "strategy": strategy, "proposal_num": _SESSION.proposal_num,
            "parsed": proposal is not None}


def run_vote(
    voter_id: str,
    *,
    logger=None,
    on_event_start: Callable | None = None,
    on_event_chunk: Callable | None = None,
) -> dict:
    s = _SESSION
    if s.proposal is None or s.proposer is None:
        return {"error": "当前无有效提案，请先 run_propose"}
    instruction = vote_instruction(
        voter_id, s.proposer, s.strategy or "co_edca", s.proposal, s.proposal_num)
    if _memory_fast_vote_enabled() and _current_proposal_is_memory_anchor():
        memory = s.memory_anchor_memory or {}
        evaluation = memory.get("evaluation") or {}
        quality = memory.get("quality_score")
        score = evaluation.get("final_score")
        reply = json.dumps({
            "agreed": True,
            "reason": (
                "当前提案与高质量 improved 记忆案例完全一致，"
                f"历史 quality={quality}, score={score}；"
                "本轮优先复用已验证动作以减少重复试错。"
            ),
        }, ensure_ascii=False)
        if logger is not None:
            logger.agent_speak_start(voter_id, 4, "memory_fast_vote")
            logger.push_chunk(voter_id, reply)
            logger.agent_speak_chunk(voter_id, reply)
            logger.agent_speak(
                agent=voter_id,
                phase=4,
                role="memory_fast_vote",
                instruction=instruction,
                response=reply,
                duration_ms=0.0,
            )
        if on_event_start:
            on_event_start("voter", voter_id)
        if on_event_chunk:
            on_event_chunk("voter", voter_id, reply)
        s.record(voter_id.upper(), reply, kind="vote")
        return {
            "voter": voter_id,
            "reply": reply,
            "vote": "agree",
            "counter_proposal": None,
            "memory_fast_vote": True,
        }
    try:
        reply, _ = _run_agent_turn(
            voter_id,
            4,
            "voter",
            instruction,
            logger=logger,
            on_event_start=on_event_start,
            on_event_chunk=on_event_chunk,
        )
    except RuntimeError as exc:
        reply = _deterministic_vote_fallback(voter_id, exc)
        if reply is None:
            raise
        if logger is not None:
            logger.push_chunk(voter_id, reply)
            logger.agent_speak_chunk(voter_id, reply)
            logger.agent_speak(
                agent=voter_id,
                phase=4,
                role="voter_fallback",
                instruction=instruction,
                response=reply,
                duration_ms=0.0,
            )
        if on_event_chunk:
            on_event_chunk("voter", voter_id, reply)
    s.record(voter_id.upper(), reply, kind="vote")
    vote = read_vote(reply)
    counter = None
    if vote == "reject":
        counter = _extract_proposal(reply)
        if counter is not None:
            counter = _with_sr_concurrent_group(counter, s.ap_state)
    return {"voter": voter_id, "reply": reply, "vote": vote,
            "counter_proposal": counter}


def run_repair_vote(
    voter_id: str,
    *,
    logger=None,
    on_event_start: Callable | None = None,
    on_event_chunk: Callable | None = None,
) -> dict:
    """修复格式无效的投票回复。

    与反提案修复不同，这一步仍处于投票阶段：只有补充的投票显式
    agreed=false 时，才允许解析并提升反提案。
    """
    s = _SESSION
    if s.proposal is None or s.proposer is None:
        return {"error": "当前无有效提案，请先 run_propose"}
    instruction = repair_vote_instruction(
        voter_id,
        s.proposer,
        s.strategy or "co_edca",
        s.proposal,
        s.proposal_num,
    )
    reply, _ = _run_agent_turn(
        voter_id,
        4,
        "vote_json_repair",
        instruction,
        logger=logger,
        on_event_start=on_event_start,
        on_event_chunk=on_event_chunk,
    )
    s.record(voter_id.upper(), reply, kind="vote")
    vote = read_vote(reply)
    counter = None
    if vote == "reject":
        counter = _extract_proposal(reply)
        if counter is not None:
            counter = _with_sr_concurrent_group(counter, s.ap_state)
    return {"voter": voter_id, "reply": reply, "vote": vote,
            "counter_proposal": counter, "repair": True}


def run_repair_counter(
    voter_id: str,
    *,
    logger=None,
    on_event_start: Callable | None = None,
    on_event_chunk: Callable | None = None,
) -> dict:
    """修复轮：反对者未给出可解析反提案时，再驱动它一次只输出纯 JSON 反提案。
    返回 {"reply", "counter_proposal"}；解析失败则 counter_proposal=None。"""
    instruction = repair_counter_instruction()
    reply, _ = _run_agent_turn(
        voter_id,
        3,
        "counter_proposal_json_repair",
        instruction,
        logger=logger,
        on_event_start=on_event_start,
        on_event_chunk=on_event_chunk,
    )
    _SESSION.record(voter_id.upper(), reply, kind="proposal")
    counter = _extract_proposal(reply)
    if counter is not None:
        counter = _with_sr_concurrent_group(counter, _SESSION.ap_state)
    return {"voter": voter_id, "reply": reply, "counter_proposal": counter}


def promote_counter(new_proposer: str, counter_proposal: dict) -> dict:
    """反对者接管：把反提案设为当前提案。"""
    s = _SESSION
    s.proposer = new_proposer
    s.proposal = _with_sr_concurrent_group(counter_proposal, s.ap_state)
    s.strategy = resolve_strategy(s.proposal)
    s.proposal_num += 1
    s.memory_anchor_proposal_num = None
    s.memory_anchor_decision = None
    s.memory_anchor_memory = None
    return {"proposer": new_proposer, "proposal": s.proposal,
            "strategy": s.strategy, "proposal_num": s.proposal_num}


def run_final(
    *,
    logger=None,
    on_event_start: Callable | None = None,
    on_event_chunk: Callable | None = None,
) -> dict:
    s = _SESSION
    if s.proposal is None or s.proposer is None:
        return {"error": "当前无通过的提案"}
    decision = json.loads(json.dumps(s.proposal, ensure_ascii=False))
    reply = json.dumps(decision, ensure_ascii=False)
    if on_event_start:
        on_event_start("decision", s.proposer)
    if on_event_chunk:
        on_event_chunk("decision", s.proposer, reply + "\n协商结束")
    if logger is not None:
        instruction = final_instruction(s.proposer, s.proposal)
        logger.agent_speak_start(s.proposer, 5, "decision")
        logger.push_chunk(s.proposer, reply + "\n协商结束")
        logger.agent_speak_chunk(s.proposer, reply + "\n协商结束")
        logger.agent_speak(
            agent=s.proposer,
            phase=5,
            role="decision",
            instruction=instruction,
            response=reply + "\n协商结束",
            duration_ms=0.0,
        )
    s.record(s.proposer.upper(), reply, kind="decision")
    s.decision = decision
    return {"decision": decision, "strategy": s.strategy, "reply": reply + "\n协商结束"}


# ══════════════════════════════════════════════════════════════════════
# 结构化接力（推荐）：thin relay 确定性编排四阶段「轮次顺序」，
# 每个 AP 仍经 OpenClaw agent 完全自主决定「发言内容」。
# 不是 agent、不是 LLM、不是协调者——复刻原 orchestrator.run() 控制流，
# 把 speak() 换成 openclaw agent。这是可靠复现结果的路径。
# ══════════════════════════════════════════════════════════════════════

def _log_agent_reply(logger, ap_id: str, phase: int, role: str,
                     instruction: str, reply: str) -> None:
    if logger is None:
        return
    logger.agent_speak_start(ap_id, phase, role)
    logger.push_chunk(ap_id, reply)
    logger.agent_speak_chunk(ap_id, reply)
    logger.agent_speak(
        agent=ap_id,
        phase=phase,
        role=role,
        instruction=instruction,
        response=reply,
        duration_ms=0.0,
    )


def _log_phase(logger, phase: int, label: str) -> None:
    if logger is not None:
        logger.phase_start(phase, label)


def _push_decision(decision: dict, strategy: str,
                   endpoints: dict[str, str] | None,
                   session_id: str = "",
                   logger=None,
                   action_type: str = "executor_apply") -> dict[str, dict]:
    if not endpoints:
        return {}

    step_id = logger.start_step(
        action_type,
        retry_budget=1,
        input_data={
            "strategy": strategy,
            "endpoints": endpoints,
            "decision": decision,
            "action_type": action_type,
        },
    ) if logger is not None else None

    def send(ap_id: str, url: str) -> tuple[str, bool, str, dict]:
        params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
        params = encode_params_edca(params)
        payload = {
            "session_id": session_id,
            "strategy": strategy,
            "ap_id": ap_id,
            "params": params,
        }
        canonical = json.dumps(
            {
                "session_id": session_id,
                "ap_id": ap_id,
                "strategy": strategy,
                "params": params,
                "action_type": action_type,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        idem_key = f"{action_type}:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        action = None
        if logger is not None:
            action, _ = logger.prepare_action(
                idempotency_key=idem_key,
                action_type=action_type,
                target=ap_id,
                request={"url": f"{url.rstrip('/')}/apply", "payload": payload},
                step_id=step_id,
            )
            if action is not None and action.status == "succeeded":
                cached = action.response if isinstance(action.response, dict) else {}
                return ap_id, True, str(cached.get("response", "幂等缓存命中")), payload
            if action is not None and action.status in {"running", "unknown"}:
                return (
                    ap_id,
                    False,
                    f"action={action.action_id} 状态={action.status}，需核对 AP /status 后再恢复",
                    payload,
                )
            if action is not None and action.status == "failed" and action.attempts >= 2:
                return (
                    ap_id,
                    False,
                    f"action={action.action_id} 已达到最大尝试次数 {action.attempts}",
                    payload,
                )
            if action is not None:
                logger.mark_action_running(action.action_id)
        try:
            import requests
            timeout = float(os.environ.get("MULTIAP_EXECUTOR_TIMEOUT", "15") or 15)
            resp = requests.post(f"{url.rstrip('/')}/apply", json=payload, timeout=timeout)
            ok = resp.status_code == 200
            try:
                body = resp.json()
                msg = body.get("details", body)
            except Exception:
                msg = resp.text
            if logger is not None and action is not None:
                logger.finish_action(
                    action.action_id,
                    status="succeeded" if ok else "failed",
                    response={"ok": ok, "status_code": resp.status_code, "response": msg},
                    error=None if ok else f"HTTP {resp.status_code}",
                )
            return ap_id, ok, str(msg), payload
        except Exception as exc:  # noqa: BLE001
            if logger is not None and action is not None:
                # 请求可能已到达 AP，但响应丢失。保守标 unknown，禁止自动重发。
                logger.finish_action(
                    action.action_id,
                    status="unknown",
                    error=str(exc),
                )
            return ap_id, False, str(exc), payload

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(endpoints))) as pool:
        futures = {
            pool.submit(send, ap_id, url): (ap_id, url)
            for ap_id, url in endpoints.items()
        }
        for future in as_completed(futures):
            ap_id, url = futures[future]
            got_ap, ok, msg, payload = future.result()
            results[got_ap] = {
                "ok": ok,
                "url": url,
                "payload": payload,
                "response": msg,
            }
    if logger is not None:
        all_ok = bool(results) and all(item["ok"] for item in results.values())
        logger.finish_step(
            step_id,
            status="succeeded" if all_ok else "failed",
            result=results,
            error=None if all_ok else "one or more executor actions failed or are uncertain",
        )
    return results


_ROLLBACK_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "tx_power_dbm": ("tx_power_dbm",),
    "obss_pd_dbm": ("obss_pd_dbm",),
    "cwmin": ("cwmin", "CWmin"),
    "cwmax": ("cwmax", "CWmax"),
    "aifsn": ("aifsn", "AIFSN"),
    "be_cwmin": ("be_cwmin", "BE_CWmin"),
    "be_cwmax": ("be_cwmax", "BE_CWmax"),
    "be_aifsn": ("be_aifsn", "BE_AIFSN"),
    "vi_cwmin": ("vi_cwmin", "VI_CWmin"),
    "vi_cwmax": ("vi_cwmax", "VI_CWmax"),
    "vi_aifsn": ("vi_aifsn", "VI_AIFSN"),
}

_ROLLBACK_FIELDS_BY_STRATEGY: dict[str, set[str]] = {
    "co_sr": {"tx_power_dbm", "obss_pd_dbm"},
    "co_edca": {
        "cwmin", "cwmax", "aifsn",
        "be_cwmin", "be_cwmax", "be_aifsn",
        "vi_cwmin", "vi_cwmax", "vi_aifsn",
    },
}


def _rollback_decision_for_failed_candidate(
    baseline_state: dict,
    candidate: dict,
    strategy: str,
) -> dict:
    """Restore only fields touched by a failed, already-applied candidate."""
    allowed_fields = _ROLLBACK_FIELDS_BY_STRATEGY.get(strategy, set())
    if not allowed_fields:
        return {}

    rollback: dict[str, dict] = {}
    for ap_id in AP_IDS:
        changed = (candidate or {}).get(ap_id) or (candidate or {}).get(ap_id.upper()) or {}
        baseline = (baseline_state or {}).get(ap_id) or {}
        if not isinstance(changed, dict) or not isinstance(baseline, dict):
            continue
        changed_keys = {str(key).lower() for key in changed.keys()}
        restore: dict[str, object] = {}
        for canonical, aliases in _ROLLBACK_PARAM_ALIASES.items():
            if canonical not in allowed_fields:
                continue
            if not any(alias.lower() in changed_keys for alias in aliases):
                continue
            for alias in aliases:
                if baseline.get(alias) is not None:
                    restore[canonical] = baseline[alias]
                    break
        if restore:
            rollback[ap_id] = restore
    return rollback


def _rollback_failed_candidate(
    baseline_state: dict,
    decision: dict,
    strategy: str,
    endpoints: dict[str, str] | None,
    session_id: str,
    logger=None,
) -> dict:
    rollback = _rollback_decision_for_failed_candidate(
        baseline_state, decision, strategy
    )
    if not rollback:
        return {}
    results = _push_decision(
        rollback,
        strategy,
        endpoints,
        f"{session_id}:rollback",
        logger,
        action_type="executor_rollback",
    )
    if logger is not None:
        logger.record_decision_parameters(
            rollback, strategy=strategy, source="executor_rollback"
        )
    return {"decision": rollback, "push_results": results}


def _collect_observed_state(
    fallback_state: dict,
    observation_state_getter: Callable[[], dict] | None,
    observation_wait_seconds: float,
    require_real_observation: bool = False,
) -> tuple[dict, str | None, bool]:
    if not require_real_observation or observation_state_getter is None:
        return fallback_state, None, False

    wait_seconds = max(0.0, observation_wait_seconds)
    if wait_seconds:
        time.sleep(wait_seconds)
    sample_count = max(1, _env_int("MULTIAP_QOS_SAMPLE_COUNT", 1))
    sample_interval = max(0.0, _env_float("MULTIAP_QOS_SAMPLE_INTERVAL", 1.0))
    samples: list[dict] = []
    try:
        for index in range(sample_count):
            if index > 0 and sample_interval:
                time.sleep(sample_interval)
            samples.append(apply_profile(observation_state_getter()))
        return _average_observed_states(samples), None, True
    except Exception as exc:  # noqa: BLE001
        return {}, f"观测状态获取失败: {exc}", False


def _average_observed_states(samples: list[dict]) -> dict:
    if not samples:
        return {}
    if len(samples) == 1:
        return samples[0]

    def merge(values: list):
        numeric = []
        for value in values:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                numeric.append(float(value))
        if len(numeric) == len(values):
            avg = sum(numeric) / len(numeric)
            return int(avg) if all(isinstance(value, int) and not isinstance(value, bool) for value in values) else avg
        dict_values = [value for value in values if isinstance(value, dict)]
        if len(dict_values) == len(values):
            keys = set().union(*(value.keys() for value in dict_values))
            return {
                key: merge([value[key] for value in dict_values if key in value])
                for key in keys
            }
        return values[-1]

    ap_ids = set().union(*(sample.keys() for sample in samples if isinstance(sample, dict)))
    return {
        ap_id: merge([sample[ap_id] for sample in samples if ap_id in sample])
        for ap_id in sorted(ap_ids)
    }


def _qos_acceptance_result(
    baseline_state: dict,
    observed_state: dict | None,
    *,
    observed_is_real: bool,
) -> dict:
    if not observed_is_real or not observed_state:
        return {
            "approved": False,
            "verdict": "inconclusive",
            "confidence": 0.0,
            "deltas": {"coverage": 0.0, "score": 0.0},
            "summary": "QoS 效果验收失败：没有真实下发后的观测状态",
        }
    deltas = _evaluate_qos_deltas(baseline_state, observed_state)
    verdict, confidence = _classify_qos_delta(deltas)
    score = float(deltas.get("score") or 0.0)
    approved = verdict == "improved"
    return {
        "approved": approved,
        "verdict": verdict,
        "confidence": confidence,
        "deltas": deltas,
        "summary": (
            f"QoS 效果验收{'通过' if approved else '未通过'}："
            f"verdict={verdict}, score={score:.4f}, confidence={confidence:.4f}"
        ),
    }


def _validation_only_sta_qoe_blocked(validation: dict | None) -> bool:
    """Return true when deterministic checks passed and only STA QoE blocked.

    In QoS acceptance mode, post-apply STA feedback is a short-window signal.
    The multi-sample QoS scorer is the stronger arbiter for aggregate effect, so
    a transient new STA violation can be overridden only when no parameter or
    self-harm errors are present and QoS verdict is improved.
    """
    if not validation or validation.get("approved"):
        return False
    errors = validation.get("global_errors") or []
    if not errors:
        return False
    if any(not str(item).startswith("决策后引入新的 STA SLA 违规") for item in errors):
        return False
    for entry in (validation.get("per_ap") or {}).values():
        if isinstance(entry, dict) and entry.get("errors"):
            return False
    sta_qoe = validation.get("sta_qoe") or {}
    return bool(sta_qoe.get("checked") and not sta_qoe.get("approved"))


def _finish(logger, outcome: str, rounds: int, started_at: float) -> None:
    if logger is not None:
        logger.session_end(
            outcome,
            rounds,
            negotiation_duration_s=time.time() - started_at,
        )
    from src.memory.workspace import archive_current_session
    for agent in AP_IDS:
        _SESSION.sync_agent_workspace(agent)
        archive_current_session(agent, _SESSION.run_id, outcome)


def _save_checkpoint(
    logger,
    boundary: str,
    *,
    retry: int,
    agree: set[str] | None = None,
    vote_cursor: int = 0,
) -> None:
    if logger is None:
        return
    s = _SESSION
    logger.save_negotiation_checkpoint(
        boundary,
        {
            "ap_state": s.ap_state,
            "transcript": s.transcript,
            "proposer": s.proposer,
            "proposal": s.proposal,
            "strategy": s.strategy,
            "proposal_num": s.proposal_num,
            "decision": s.decision,
            "session_memory": s.memory_manager.memory.to_dict(),
            "agent_session_memories": {
                agent: manager.memory.to_dict()
                for agent, manager in s.agent_memory_managers.items()
            },
            "agent_local_transcripts": s.local_transcripts,
            "private_slas": s.private_slas,
            "memory_run_id": s.run_id,
            "agent_memory_revisions": s.memory_revisions,
            "agent_recalled_episodes": s.agent_recalled_episodes,
            "recalled_episode_ids": [
                item.get("episode_id") for item in s.recalled_episodes
            ],
            "recalled_warning_ids": [item.get("episode_id") for item in s.recalled_warnings],
            "recalled_rule_ids": [item.get("rule_id") for item in s.recalled_rules],
            "retry": retry,
            "agree": sorted(agree or set()),
            "vote_cursor": vote_cursor,
        },
    )
    logger.save_session_memory(
        s.memory_manager.memory.to_dict(), s.memory_manager.render_summary(),
        s.memory_manager.budget_chars,
    )
    for agent, manager in s.agent_memory_managers.items():
        logger.save_agent_session_memory(
            agent, manager.memory.to_dict(), manager.render_summary(),
            manager.budget_chars,
        )
        if not s.sync_agent_workspace(agent):
            logger.workspace_memory_failed(
                agent, s.workspace_memory_errors[-1]["error"]
            )


def _restore_projection(projection: dict) -> Session:
    reset_session(projection.get("ap_state") or {})
    s = _SESSION
    s.run_id = str(projection.get("memory_run_id") or s.run_id)
    s.memory_revisions = {
        agent: int((projection.get("agent_memory_revisions") or {}).get(agent) or 0)
        for agent in AP_IDS
    }
    s.transcript = list(projection.get("transcript") or [])
    s.proposer = projection.get("proposer")
    s.proposal = projection.get("proposal")
    s.strategy = projection.get("strategy")
    s.proposal_num = int(projection.get("proposal_num") or 0)
    s.decision = projection.get("decision")
    s.private_slas = dict(projection.get("private_slas") or s.private_slas)
    s.agent_recalled_episodes = {
        agent: list((projection.get("agent_recalled_episodes") or {}).get(agent) or [])
        for agent in AP_IDS
    }
    s.memory_manager.memory = SessionMemory.from_dict(projection.get("session_memory"))
    stored_memories = projection.get("agent_session_memories") or {}
    for agent, manager in s.agent_memory_managers.items():
        manager.memory = SessionMemory.from_dict(stored_memories.get(agent))
    stored_transcripts = projection.get("agent_local_transcripts")
    if isinstance(stored_transcripts, dict):
        s.local_transcripts = {
            agent: [
                item for item in list(stored_transcripts.get(agent) or [])
                if item.get("kind") == "private_constraint"
                or item.get("speaker") == "PRIVATE_MEMORY"
            ] for agent in AP_IDS
        }
    return s


def structured_relay(max_validation_retries: int = 3, max_turns: int = 30,
                     on_event=None,
                     on_event_start: Callable | None = None,
                     on_event_chunk: Callable | None = None,
                     on_tool: Callable | None = None,
                     logger=None,
                     observation_state_getter: Callable[[], dict] | None = None,
                     observation_wait_seconds: float = 0.0,
                     executor_endpoints: dict[str, str] | None = None,
                     initial_state: dict | None = None,
                     resume_projection: dict | None = None,
                     evaluation_windows: tuple[float, ...] | None = None,
                     goal_context: dict | None = None,
                     agent_driver: Callable | None = None,
                     on_proposal_precheck: Callable | None = None,
                     acceptance: str = "validator") -> dict:
    """阶段级快速协商。on_tool：进程内 structured_relay 路径传入工具调用展示回调，
    在 drive_ap 每次 AP 发言后从 trajectory 提取并回调；coordinator 路径
    （run_fast_negotiation）不传，保持 None。"""
    global _tool_callback, _tool_logger, _agent_driver
    if not _relay_lock.acquire(blocking=False):
        raise RuntimeError("当前进程已有协商运行；全局 MCP 会话暂不支持并发 structured_relay")
    _tool_callback = on_tool
    _tool_logger = logger
    _agent_driver = agent_driver
    memory_sink = None
    if logger is not None:
        def persist_memory(
            agent_id: str | None, memory: SessionMemory, summary: str
        ) -> None:
            if agent_id is None:
                logger.save_session_memory(
                    memory.to_dict(), summary, _SESSION.memory_manager.budget_chars
                )
            else:
                logger.save_agent_session_memory(
                    agent_id, memory.to_dict(), summary,
                    _SESSION.agent_memory_managers[agent_id].budget_chars,
                )
        memory_sink = persist_memory
    try:
        return _structured_relay_impl(
            max_validation_retries=max_validation_retries,
            max_turns=max_turns,
            on_event=on_event,
            on_event_start=on_event_start,
            on_event_chunk=on_event_chunk,
            logger=logger,
            observation_state_getter=observation_state_getter,
            observation_wait_seconds=observation_wait_seconds,
            executor_endpoints=executor_endpoints,
            initial_state=initial_state,
            resume_projection=resume_projection,
            evaluation_windows=evaluation_windows,
            memory_callback=memory_sink,
            goal_context=goal_context,
            on_proposal_precheck=on_proposal_precheck,
            acceptance=acceptance,
        )
    finally:
        _tool_callback = None
        _tool_logger = None
        _agent_driver = None
        _SESSION.memory_callback = None
        _relay_lock.release()


def _structured_relay_impl(max_validation_retries: int = 3, max_turns: int = 30,
                     on_event=None,
                     on_event_start: Callable | None = None,
                     on_event_chunk: Callable | None = None,
                     logger=None,
                     observation_state_getter: Callable[[], dict] | None = None,
                     observation_wait_seconds: float = 0.0,
                     executor_endpoints: dict[str, str] | None = None,
                     initial_state: dict | None = None,
                     resume_projection: dict | None = None,
                     evaluation_windows: tuple[float, ...] | None = None,
                     memory_callback: Callable[[str | None, SessionMemory, str], None] | None = None,
                     goal_context: dict | None = None,
                     on_proposal_precheck: Callable | None = None,
                     acceptance: str = "validator") -> dict:
    global _tool_callback

    def emit(phase, who, reply):
        if on_event:
            on_event(phase, who, reply)

    def emit_proposal_precheck(
        proposer: str, proposal_num: int, strategy: str | None, result: dict
    ) -> None:
        if on_proposal_precheck is None:
            return
        try:
            on_proposal_precheck(proposer, proposal_num, strategy, result)
        except Exception:
            pass

    started_at = time.time()
    acceptance_mode = str(acceptance or "validator").strip().lower()
    if acceptance_mode not in {"validator", "qos"}:
        acceptance_mode = "validator"
    if resume_projection:
        s = _restore_projection(resume_projection)
    else:
        reset_session(initial_state)
        s = _SESSION
        if logger is not None:
            s.run_id = str(logger.session_id)
            for agent in AP_IDS:
                s.sync_agent_workspace(agent)
    s.memory_callback = memory_callback
    s.goal_context = goal_context

    use_memory = memory_enabled()
    if logger is not None and use_memory and not resume_projection:
        from src.memory.workspace import try_save_long_term_memory
        s.agent_recalled_episodes = {
            agent: logger.recall_agent_episodes(
                agent, s.ap_state, limit=20, min_quality=0.0
            )
            for agent in AP_IDS
        }
        for agent, episodes in s.agent_recalled_episodes.items():
            if not try_save_long_term_memory(agent, episodes):
                s.workspace_memory_errors.append({
                    "agent": agent, "error": "failed to update long-term workspace memory"
                })
    elif logger is not None and use_memory and resume_projection:
        restored = logger.load_recalled_memory(
            list(resume_projection.get("recalled_episode_ids") or []),
            list(resume_projection.get("recalled_warning_ids") or []),
            list(resume_projection.get("recalled_rule_ids") or []),
        )
        s.recalled_episodes = restored["positive"]
        s.recalled_warnings = restored["warnings"]
        s.recalled_rules = restored["rules"]

    # 阶段一：广播。三台 AP 互不依赖，始终并发驱动以省模型回合时间；
    # 若需要终端/Dashboard 实时事件，则先缓存回复，再按 ap1→ap2→ap3 顺序回放。
    if not resume_projection:
        _log_phase(logger, 1, "广播自身状态")
        bcast_inst = {ap: broadcast_instruction(ap) for ap in AP_IDS}
        streaming_broadcast = bool(
            logger is not None or on_event_start is not None or on_event_chunk is not None
        )
        saved_tool_callback = _tool_callback
        if streaming_broadcast:
            _tool_callback = None
        try:
            broadcast_workers = max(1, min(
                len(AP_IDS), int(os.environ.get("MULTIAP_BROADCAST_WORKERS", len(AP_IDS)))
            ))
            with ThreadPoolExecutor(max_workers=broadcast_workers) as ex:
                futures = {
                    ap: ex.submit(_run_agent_turn, ap, 1, "broadcast", bcast_inst[ap])
                    for ap in AP_IDS
                }
                for ap in AP_IDS:
                    reply, streamed = futures[ap].result()
                    if _invalid_broadcast_reply(reply):
                        reply = _broadcast_fallback(ap)
                        streamed = False
                    s.record(ap.upper(), reply, kind="broadcast")
                    if streaming_broadcast:
                        if logger is not None:
                            logger.agent_speak_start(ap, 1, "broadcast")
                            logger.push_chunk(ap, reply)
                            logger.agent_speak_chunk(ap, reply)
                            logger.agent_speak(
                                agent=ap, phase=1, role="broadcast",
                                instruction=bcast_inst[ap], response=reply, duration_ms=0.0,
                            )
                        if on_event_start:
                            on_event_start("broadcast", ap)
                        if on_event_chunk:
                            on_event_chunk("broadcast", ap, reply)
                    elif not streamed:
                        emit("broadcast", ap, reply)
        finally:
            if streaming_broadcast:
                _tool_callback = saved_tool_callback
        _save_checkpoint(logger, "broadcast_complete", retry=0)

    strategy_hint = determine_strategy(s.ap_state)
    if strategy_hint == "noop":
        _log_phase(logger, 2, "NOOP：无需协商")
        _finish(logger, "noop", 0, started_at)
        return {
            "outcome": "noop",
            "decision": None,
            "strategy": "noop",
            "validation": None,
            "push_results": {},
            "observed_is_real": False,
            "transcript_turns": len(s.transcript),
        }

    if logger is not None and use_memory and not (
        resume_projection
        and str(resume_projection.get("boundary") or "")
        in {"proposal_ready", "vote_progress", "counter_proposal_ready"}
    ):
        channels = logger.recall_episode_memory(s.ap_state, positive_limit=3, warning_limit=2)
        s.recalled_episodes = channels["positive"]
        s.recalled_warnings = channels["warnings"]
        s.recalled_rules = logger.recall_rules(s.ap_state, min_confidence=0.5, limit=3)

    _log_phase(logger, 2, "协商触发，等待 AP1 自主选路")

    # 外层：Validator 未通过则从 ap1 重新提案。恢复时可直接进入已持久化投票边界。
    resume_boundary = str((resume_projection or {}).get("boundary") or "")
    start_retry = int((resume_projection or {}).get("retry") or 0)
    use_saved_proposal = bool(
        resume_projection
        and resume_boundary in {"proposal_ready", "vote_progress", "counter_proposal_ready"}
        and s.proposal is not None
        and s.proposer is not None
    )
    last_validation = None
    for retry in range(start_retry, max_validation_retries):
        if use_saved_proposal:
            proposer = s.proposer or "ap1"
            p = {"parsed": True, "reply": "[从持久化 checkpoint 恢复现有提案]"}
            agree = set(resume_projection.get("agree") or [])
            vote_cursor = int(resume_projection.get("vote_cursor") or 1)
            use_saved_proposal = False
        else:
            proposer = "ap1"
            _log_phase(logger, 3, f"{proposer.upper()} 发起提案（自主选路）")
            p = run_propose(
                proposer,
                strategy_hint,
                logger=logger,
                on_event_start=on_event_start,
                on_event_chunk=on_event_chunk,
            )
            if not on_event_start and not on_event_chunk:
                emit("propose", proposer, p["reply"])
            agree = set()
            vote_cursor = 1
        if not p["parsed"]:
            # 提案方基于证据判定"无需调整"是合理结果，不当作解析错误。
            if _proposer_declares_noop(p["reply"]):
                _log_phase(logger, 5, "提案方判定无需调整")
                _finish(logger, "noop", s.proposal_num, started_at)
                return {"outcome": "noop", "decision": None, "strategy": "noop",
                        "validation": None, "push_results": {},
                        "observed_is_real": False, "transcript_turns": len(s.transcript)}
            _finish(logger, "proposal_parse_error", s.proposal_num, started_at)
            return {"outcome": "proposal_parse_error", "decision": None,
                    "strategy": None, "validation": None, "push_results": {},
                    "observed_is_real": False, "transcript_turns": len(s.transcript)}

        _repair_current_proposal(
            logger=logger,
            proposer=s.proposer or proposer,
            proposal_num=s.proposal_num,
            strategy=s.strategy,
        )
        proposal_check = _proposal_precheck(s.proposal, s.strategy)
        _record_proposal_precheck(
            logger=logger,
            proposer=s.proposer or proposer,
            proposal_num=s.proposal_num,
            strategy=s.strategy,
            result=proposal_check,
        )
        emit_proposal_precheck(s.proposer or proposer, s.proposal_num, s.strategy, proposal_check)
        if not proposal_check.get("approved"):
            _save_checkpoint(
                logger, "broadcast_complete", retry=retry + 1,
            )
            continue
        _save_checkpoint(
            logger, "proposal_ready", retry=retry,
            agree=agree, vote_cursor=vote_cursor,
        )

        for _ in range(max_turns):
            voter = AP_IDS[vote_cursor % len(AP_IDS)]
            vote_cursor += 1
            if voter == s.proposer:
                continue
            _log_phase(
                logger,
                4,
                f"{voter.upper()} 投票（提案#{s.proposal_num}，提案方 {s.proposer.upper()}）",
            )
            vote_inst = vote_instruction(
                voter, s.proposer, s.strategy or "co_edca", s.proposal, s.proposal_num)
            rv = run_vote(
                voter,
                logger=logger,
                on_event_start=on_event_start,
                on_event_chunk=on_event_chunk,
            )
            if not on_event_start and not on_event_chunk:
                emit("vote", voter, rv["reply"])
            if logger is not None:
                logger.vote(voter, s.proposal_num, rv["vote"], rv["reply"])

            if rv["vote"] == "invalid":
                invalid_msg = (
                    f"[投票格式无效] {voter.upper()} 对提案#{s.proposal_num}的回复缺少显式 "
                    '`{"agreed": ...}` 表态，或包含裸参数/伪工具 JSON；'
                    "本回复不会被解释为反提案，编排层要求同一 AP 补投票。"
                )
                s.record("VALIDATOR", invalid_msg, kind="validator")
                repaired = run_repair_vote(
                    voter,
                    logger=logger,
                    on_event_start=on_event_start,
                    on_event_chunk=on_event_chunk,
                )
                if not on_event_start and not on_event_chunk:
                    emit("vote", voter, repaired["reply"])
                if logger is not None:
                    logger.vote(
                        voter, s.proposal_num, repaired["vote"], repaired["reply"]
                    )
                rv = repaired

                if rv["vote"] == "invalid":
                    fallback_reply = _deterministic_vote_fallback(
                        voter, ValueError("vote reply missing explicit agreed JSON")
                    )
                    if fallback_reply is None:
                        payload = {
                            "agreed": "abstain",
                            "reason": (
                                f"{voter.upper()} 连续输出无效投票格式；"
                                "为避免协议错位，编排层按弃权处理且不提升任何反提案。"
                            ),
                            "fallback": "invalid_vote_abstain",
                        }
                        fallback_reply = (
                            "投票格式持续无效，按弃权处理。\n"
                            "```json\n"
                            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
                            "```"
                        )
                    fallback_vote = read_vote(fallback_reply)
                    s.record(
                        "VALIDATOR",
                        (
                            f"[投票格式兜底] {voter.upper()} 的修复投票仍无效；"
                            "使用确定性兜底票，不解析原回复中的任何反提案。"
                        ),
                        kind="validator",
                    )
                    s.record(voter.upper(), fallback_reply, kind="vote")
                    if not on_event_start and not on_event_chunk:
                        emit("vote", voter, fallback_reply)
                    if logger is not None:
                        logger.vote(voter, s.proposal_num, fallback_vote, fallback_reply)
                    rv = {
                        "voter": voter,
                        "reply": fallback_reply,
                        "vote": fallback_vote,
                        "counter_proposal": None,
                        "fallback": True,
                    }

            if rv["vote"] in ("agree", "abstain"):
                agree.add(voter)
                _save_checkpoint(
                    logger, "vote_progress", retry=retry,
                    agree=agree, vote_cursor=vote_cursor,
                )
                non_proposers = {a for a in AP_IDS if a != s.proposer}
                if agree >= non_proposers:
                    if logger is not None:
                        logger.round_result(
                            s.proposal_num, True, len(agree), len(non_proposers)
                        )
                    _log_phase(logger, 5, "输出最终决策")
                    final_inst = final_instruction(s.proposer, s.proposal)
                    fr = run_final(
                        logger=logger,
                        on_event_start=on_event_start,
                        on_event_chunk=on_event_chunk,
                    )
                    if not on_event_start and not on_event_chunk:
                        emit("decide", s.proposer, fr["reply"])
                    decision, strategy = fr["decision"], s.strategy
                    if logger is not None:
                        logger.final_decision(decision, fr["reply"])
                        logger.record_decision_parameters(decision, strategy=strategy)

                    precheck = (_validate_decision(
                        s.ap_state, decision, strategy,
                        observed_state=s.ap_state, observed_is_real=False)
                        if decision and strategy else None)
                    if logger is not None and precheck is not None:
                        logger.validation_result(precheck)

                    if precheck and precheck["approved"]:
                        session_id = logger.session_id if logger is not None else ""
                        push_results = _push_decision(
                            decision, strategy or "", executor_endpoints, session_id, logger)
                        if acceptance_mode == "qos" and not push_results:
                            val = {
                                **precheck,
                                "approved": False,
                                "global_errors": [
                                    "QoS 效果验收要求先下发决策，但当前未配置 executor /apply 端点"
                                ],
                                "summary": (
                                    f"验证失败（策略={strategy}）：QoS 效果验收要求先下发决策，"
                                    "请使用 --ap-config config/ap_endpoints.json 或 --ap-endpoints"
                                ),
                            }
                            if logger is not None:
                                logger.validation_result(val)
                            _finish(logger, "qos_not_applied", s.proposal_num, started_at)
                            s.decision = decision
                            return {"outcome": "qos_not_applied", "decision": decision,
                                    "strategy": strategy, "validation": val,
                                    "push_results": push_results,
                                    "observed_is_real": False,
                                    "transcript_turns": len(s.transcript)}
                        if (
                            acceptance_mode == "qos"
                            and push_results
                            and not all(item.get("ok") for item in push_results.values())
                        ):
                            val = {
                                **precheck,
                                "approved": False,
                                "global_errors": ["QoS 效果验收要求所有 executor 下发成功"],
                                "summary": (
                                    f"验证失败（策略={strategy}）：executor 下发未全部成功，"
                                    "无法进行 QoS 效果验收"
                                ),
                            }
                            if logger is not None:
                                logger.validation_result(val)
                            _finish(logger, "qos_apply_failed", s.proposal_num, started_at)
                            s.decision = decision
                            return {"outcome": "qos_apply_failed", "decision": decision,
                                    "strategy": strategy, "validation": val,
                                    "push_results": push_results,
                                    "observed_is_real": False,
                                    "transcript_turns": len(s.transcript)}
                        require_observation = bool(push_results) and all(
                            item.get("ok") for item in push_results.values()
                        )
                        if logger is not None:
                            for ap_id, item in push_results.items():
                                logger.record_executor_apply(
                                    ap_id,
                                    ok=item["ok"],
                                    url=item["url"],
                                    payload=item["payload"],
                                    response=item["response"],
                                )
                        observed, obs_error, obs_real = _collect_observed_state(
                            s.ap_state,
                            observation_state_getter,
                            observation_wait_seconds,
                            require_real_observation=require_observation,
                        )
                        val = _validate_decision(
                            s.ap_state, decision, strategy,
                            observed_state=observed, observed_is_real=obs_real)
                        if obs_error:
                            val["approved"] = False
                            val["global_errors"].insert(0, obs_error)
                            val["summary"] = f"验证失败（策略={strategy}）：{obs_error}"
                        sta_qoe_only_block = _validation_only_sta_qoe_blocked(val)
                        if (
                            acceptance_mode == "qos"
                            and (val["approved"] or sta_qoe_only_block)
                        ):
                            qos = _qos_acceptance_result(
                                s.ap_state,
                                observed,
                                observed_is_real=obs_real,
                            )
                            val["qos_acceptance"] = qos
                            if qos["approved"] and sta_qoe_only_block:
                                val["approved"] = True
                                val["global_errors"] = []
                                val["summary"] = (
                                    f"验证通过（策略={strategy}）："
                                    f"{qos['summary']}；瞬时 STA QoE 新违规由多样本 "
                                    "QoS improved 结果覆盖"
                                )
                                sta_qoe = val.get("sta_qoe")
                                if isinstance(sta_qoe, dict):
                                    sta_qoe["overridden_by_qos_acceptance"] = True
                            elif not qos["approved"]:
                                val["approved"] = False
                                val["global_errors"].insert(0, qos["summary"])
                                val["summary"] = (
                                    f"验证失败（策略={strategy}）：{qos['summary']}"
                                )
                        if require_observation and not val["approved"]:
                            rollback_result = _rollback_failed_candidate(
                                s.ap_state,
                                decision,
                                strategy or "",
                                executor_endpoints,
                                session_id,
                                logger,
                            )
                            if rollback_result:
                                val["rollback"] = rollback_result
                        if logger is not None:
                            logger.record_state_snapshot(
                                "final_observed" if obs_real else "final_fallback",
                                observed if observed else s.ap_state,
                                source=(
                                    "validator_observation"
                                    if obs_real else "validator_fallback"
                                ),
                            )
                            logger.validation_result(val)
                        if val["approved"]:
                            # 决策已生效（真实推送或 mock 注入即将发生）：登记多窗口
                            # 效果评估，基线取协商前状态；收割不阻塞本进程退出。
                            if logger is not None and evaluation_windows:
                                logger.schedule_outcome_evaluations(
                                    s.ap_state, evaluation_windows
                                )
                            _finish(logger, "success", s.proposal_num, started_at)
                            s.decision = decision
                            return {"outcome": "success", "decision": decision,
                                    "strategy": strategy, "validation": val,
                                    "push_results": push_results,
                                    "observed_is_real": obs_real,
                                    "transcript_turns": len(s.transcript)}
                    else:
                        val = precheck

                    if val and val["approved"]:
                        _finish(logger, "success", s.proposal_num, started_at)
                        return {"outcome": "success", "decision": decision,
                                "strategy": strategy, "validation": val,
                                "push_results": {},
                                "observed_is_real": False,
                                "transcript_turns": len(s.transcript)}
                    # 验收未过：写入对话记录，外层从 ap1 重提案
                    last_validation = val
                    errs = "；".join((val or {}).get("global_errors") or []) if val else "无法解析决策"
                    s.record("VALIDATOR", f"[验证未通过] {(val or {}).get('summary','')}\n具体问题：{errs}", kind="validator")
                    _save_checkpoint(
                        logger, "broadcast_complete", retry=retry + 1,
                    )
                    break
            elif rv["vote"] == "reject":
                counter = rv["counter_proposal"]
                if counter is None:
                    # 修复轮：反对者未给出可解析反提案，再驱动一次让其补纯 JSON
                    rep = run_repair_counter(
                        voter,
                        logger=logger,
                        on_event_start=on_event_start,
                        on_event_chunk=on_event_chunk,
                    )
                    if not on_event_start and not on_event_chunk:
                        emit("propose", voter, rep["reply"])
                    counter = rep["counter_proposal"]
                if counter is not None:
                    promoted = promote_counter(voter, counter)
                    if logger is not None:
                        logger.record_proposal(
                            promoted["proposal_num"], voter,
                            promoted["strategy"], promoted["proposal"],
                            kind="counter",
                        )
                    _repair_current_proposal(
                        logger=logger,
                        proposer=voter,
                        proposal_num=promoted["proposal_num"],
                        strategy=promoted["strategy"],
                    )
                    proposal_check = _proposal_precheck(
                        s.proposal, s.strategy
                    )
                    _record_proposal_precheck(
                        logger=logger,
                        proposer=voter,
                        proposal_num=promoted["proposal_num"],
                        strategy=s.strategy,
                        result=proposal_check,
                    )
                    emit_proposal_precheck(
                        voter, promoted["proposal_num"], s.strategy, proposal_check
                    )
                    if not proposal_check.get("approved"):
                        _save_checkpoint(
                            logger, "broadcast_complete", retry=retry + 1,
                        )
                        break
                    agree = set()
                    _save_checkpoint(
                        logger, "counter_proposal_ready", retry=retry,
                        agree=agree, vote_cursor=vote_cursor,
                    )
                # 修复后仍解析失败则跳过本轮，继续轮转
            else:
                _save_checkpoint(
                    logger, "vote_progress", retry=retry,
                    agree=agree, vote_cursor=vote_cursor,
                )
                continue
        else:
            _finish(logger, "max_turns_exceeded", s.proposal_num, started_at)
            return {"outcome": "max_turns_exceeded", "decision": None,
                    "strategy": s.strategy, "validation": None, "push_results": {},
                    "observed_is_real": False,
                    "transcript_turns": len(s.transcript)}

    _finish(logger, "max_retries_exceeded", s.proposal_num, started_at)
    return {"outcome": "max_retries_exceeded", "decision": None,
            "strategy": s.strategy, "validation": last_validation, "push_results": {},
            "observed_is_real": False,
            "transcript_turns": len(s.transcript)}

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
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openclaw.mcp.proposal_utils import (
    _infer_strategy_from_proposal,
    _extract_proposal,
    _extract_json,
    _with_sr_concurrent_group,
)
from src.tools import sr as _sr
from src.tools.edca import encode_params_edca
from src.profile import agent_view, apply_profile
from src.state_client import get_all_states
from src.validator import validate_decision as _validate_decision
from src.memory import SessionMemory, SessionMemoryManager

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
RAW_STREAM_ENV = os.environ.get("MULTIAP_OPENCLAW_RAW_STREAM", "1")


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


def _raw_stream_enabled(on_text_delta: Callable[[str], None] | None) -> bool:
    return on_text_delta is not None and _truthy(RAW_STREAM_ENV)


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


# 工具调用展示回调（进程内 structured_relay 路径用）。structured_relay 进入时设置、退出时清理；
# coordinator 路径（run_fast_negotiation）保持 None，drive_ap 自动跳过解析。
_tool_callback: Callable | None = None
_memory_callback: Callable[[SessionMemory, str], None] | None = None


# ──────────────────────────────────────────────────────────────────────
# 会话状态
# ──────────────────────────────────────────────────────────────────────

class Session:
    def __init__(self) -> None:
        self.transcript: list[dict] = []          # [{"speaker","content"}]
        self.ap_state: dict = {}                   # 已 apply_profile 的全网状态（含内部字段）
        self.proposer: str | None = None
        self.proposal: dict | None = None
        self.strategy: str | None = None
        self.proposal_num: int = 0
        self.decision: dict | None = None
        self.recalled_episodes: list[dict] = []
        self.memory_manager = SessionMemoryManager(
            budget_chars=int(os.environ.get("MULTIAP_CONTEXT_BUDGET_CHARS", "14000")),
            recent_turns=int(os.environ.get("MULTIAP_CONTEXT_RECENT_TURNS", "6")),
            on_update=self._memory_updated,
        )

    @staticmethod
    def _memory_updated(memory: SessionMemory, summary_text: str) -> None:
        if _memory_callback is not None:
            _memory_callback(memory, summary_text)

    def record(self, speaker: str, content: str) -> None:
        self.transcript.append({"speaker": speaker, "content": content})

    def transcript_text(self) -> str:
        return self.memory_manager.build_context(self.transcript)


_SESSION = Session()


def session() -> Session:
    return _SESSION


def reset_session(ap_state: dict | None = None) -> dict:
    global _SESSION
    _SESSION = Session()
    _SESSION.ap_state = apply_profile(ap_state if ap_state is not None else get_all_states(STATE_SERVER))
    return {"ok": True, "ap_states": agent_view(_SESSION.ap_state),
            "ap_ids": AP_IDS, "next": "对 ap1→ap2→ap3 依次调用 broadcast"}


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
    import uuid
    ap = ap_id.lower()
    transcript = _SESSION.transcript_text()
    msg = instruction if not transcript else (
        f"当前对话记录：\n\n{transcript}\n\n{'─' * 40}\n\n{instruction}"
    )
    env = dict(os.environ)
    env.setdefault("OLLAMA_API_KEY", "ollama-local")
    env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY"))
    env["no_proxy"] = env["NO_PROXY"]
    if extra_env:
        env.update(extra_env)
    if _raw_stream_enabled(on_text_delta):
        env.setdefault("OPENCLAW_RAW_STREAM", "1")
        env.setdefault("OPENCLAW_RAW_STREAM_PATH", str(_raw_stream_path(env)))

    # 常驻 gateway 在线则走它（热 runtime/MCP）；否则 embedded 冷启动。
    use_gateway = _gateway_up(_gateway_port())

    # 云端/本地模型偶发「incomplete terminal response」（payloads=0），多为瞬时；重试。
    last_err = ""
    sid = None
    for attempt in range(DRIVE_RETRIES):
        sid = f"{ap}-{uuid.uuid4().hex[:12]}"
        cmd = [OPENCLAW_BIN, "--profile", PROFILE, "agent"]
        if not use_gateway:
            cmd.append("--local")
        cmd += ["--agent", ap, "--session-id", sid,
                "--thinking", thinking, "--message", msg, "--json"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        streamed_tool_count = _stream_agent_session(ap, sid, proc, on_text_delta, env)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        if proc.returncode == 0:
            try:
                reply = _reply_text(json.loads(stdout))
            except json.JSONDecodeError:
                reply = stdout.strip()
            if reply.strip():
                if streamed_tool_count == 0:
                    _emit_tool_calls(ap, sid)
                return reply
            last_err = "空回复(payloads=0)"
        else:
            last_err = (stderr or stdout)[-300:]
            if use_gateway:
                # gateway 模式失败（连接/进程级）→ 回退 embedded，后续尝试不再走 gateway
                use_gateway = False
        if attempt < DRIVE_RETRIES - 1:
            __import__("time").sleep(2.0)
    raise RuntimeError(f"drive_ap({ap}) 连续 {DRIVE_RETRIES} 次失败: {last_err}")


def _stream_agent_session(
    ap_id: str,
    session_id: str,
    proc: subprocess.Popen,
    on_text_delta: Callable[[str], None] | None,
    env: dict[str, str] | None = None,
) -> int:
    """Tail OpenClaw 的 agent session/raw-stream JSONL，尽早推送工具结果和文本。

    OpenClaw CLI `agent --json` 当前只在 stdout 返回整轮结果；但 session JSONL 会在
    工具调用/工具结果/assistant 消息完成时写入。若 gateway/local runtime 启用了
    raw-stream，则 raw-stream JSONL 会提供真正的 text_delta，本函数优先转发它。
    """
    session_path = _openclaw_agents_dir() / ap_id.lower() / "sessions" / f"{session_id}.jsonl"
    raw_path = _raw_stream_path(env) if _raw_stream_enabled(on_text_delta) else None
    pending_tools: dict[str, tuple[str, dict, float]] = {}
    streamed_tool_ids: set[str] = set()
    pos = 0
    raw_pos = 0
    partial = ""
    raw_partial = ""
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
        ts_ms = float(msg.get("timestamp") or 0)
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
                if item.get("type") == "toolCall":
                    tid = item.get("id")
                    if not tid:
                        continue
                    name = (item.get("name") or "").removeprefix("multiap-tools__")
                    args = item.get("arguments") or {}
                    if not isinstance(args, dict):
                        args = {}
                    pending_tools[tid] = (name, args, ts_ms)
                elif item.get("type") == "text" and isinstance(item.get("text"), str):
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
        elif role == "toolResult":
            tid = msg.get("toolCallId")
            if not tid or tid in streamed_tool_ids:
                return
            name, args, start_ms = pending_tools.get(
                tid,
                ((msg.get("toolName") or "").removeprefix("multiap-tools__"), {}, ts_ms),
            )
            text = "".join(
                cc.get("text", "") for cc in content
                if isinstance(cc, dict) and isinstance(cc.get("text"), str)
            )
            result = text
            if text:
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    pass
            dur_ms = max(0.0, ts_ms - start_ms) if ts_ms and start_ms else None
            if _tool_callback is not None:
                try:
                    _tool_callback(name, args, result, dur_ms)
                except Exception:
                    pass
            streamed_tool_ids.add(tid)

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

    while proc.poll() is None and time.time() < deadline:
        drain_raw_stream()
        try:
            if session_path.exists():
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
    try:
        if session_path.exists():
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
    return len(streamed_tool_ids)


def _emit_tool_calls(ap_id: str, session_id: str) -> None:
    """若设置了 _tool_callback，从本次 AP 会话的 trajectory 提取工具调用并逐条回调。

    structured_relay 非流式：工具行无法穿插在发言中间，故由调用方在发言前成块打印。
    任何解析/显示异常都被吞掉——工具展示永不打断协商。"""
    if _tool_callback is None or not session_id:
        return
    try:
        tpath = _trajectory_path_for(ap_id, session_id)
        if not tpath or not tpath.exists():
            return
        tools = None
        for _ in range(3):  # 防 trajectory flush 竞态
            try:
                tools = _parse_trajectory_tools(tpath)
                break
            except (json.JSONDecodeError, OSError):
                time.sleep(0.3)
        for rec in tools or []:
            _tool_callback(rec["name"], rec["args"], rec["result"], rec["dur_ms"])
    except Exception:
        pass


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


# ──────────────────────────────────────────────────────────────────────
# trajectory 工具调用提取（structured_relay 展示用）
# ──────────────────────────────────────────────────────────────────────

def _openclaw_agents_dir() -> Path:
    """openclaw profile 的 agents 目录。

    默认 $HOME/.openclaw-{PROFILE}/agents（与 openclaw/setup.sh 一致）；
    若未来 openclaw 支持 OPENCLAW_HOME 环境变量，应在此优先读取。"""
    return _profile_state_dir() / "agents"


def _trajectory_path_for(ap_id: str, session_id: str) -> Path | None:
    """根据 session-id 推导本次会话的 trajectory 文件路径。

    优先读 <sid>.trajectory-path.json 指针的 runtimeFile（权威，能跨 openclaw
    内部 session-id 重写）；缺失则回退构造路径 <sid>.trajectory.jsonl。"""
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


def _parse_trajectory_tools(path: Path) -> list[dict]:
    """从 trajectory jsonl 提取本次会话的工具调用记录（按调用顺序）。

    取最后一条 model.completed 的 messagesSnapshot（单回合会话即完整记录）。
    toolCall/ toolResult 通过 toolCallId 关联。参数可能含 {truncated:true,...}
    标记（trajectory-depth-limit），原样透传给 formatter 处理。"""
    snapshot = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "model.completed":
                    snap = obj.get("data", {}).get("messagesSnapshot")
                    if isinstance(snap, list):
                        snapshot = snap
    except OSError:
        return []
    if not snapshot:
        return []

    def _is_trunc(v: object) -> bool:
        return isinstance(v, dict) and v.get("truncated") is True

    def _any_trunc(obj) -> bool:
        if _is_trunc(obj):
            return True
        if isinstance(obj, dict):
            return any(_any_trunc(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_any_trunc(v) for v in obj)
        return False

    calls: list[tuple] = []  # (id, name, arguments)
    results: dict[str, tuple] = {}  # toolCallId -> (isError, text)
    for msg in snapshot:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        role = msg.get("role") or msg.get("type")
        if role == "assistant" or role == "toolCall":
            for c in content:
                if isinstance(c, dict) and c.get("type") == "toolCall":
                    calls.append((c.get("id"), c.get("name"), c.get("arguments") or {}))
        elif role == "toolResult":
            tid = msg.get("toolCallId")
            text = "".join(
                cc.get("text", "") for cc in content
                if isinstance(cc, dict) and isinstance(cc.get("text"), str)
            )
            results[tid] = (bool(msg.get("isError")), text)

    records: list[dict] = []
    for cid, name, args in calls:
        is_error, text = results.get(cid, (False, ""))
        result = None
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (dict, list)):
                    result = parsed
                else:
                    result = text
            except json.JSONDecodeError:
                result = text
        stripped = (name or "").removeprefix("multiap-tools__")
        records.append({
            "name": stripped,
            "raw_name": name,
            "args": args if isinstance(args, dict) else {},
            "args_truncated": _any_trunc(args),
            "result": result,
            "is_error": is_error,
            "dur_ms": None,  # trajectory 无单工具耗时
        })
    return records


# ──────────────────────────────────────────────────────────────────────
# 表决解析
# ──────────────────────────────────────────────────────────────────────

def read_vote(content: str) -> str:
    """返回 'agree' | 'reject' | 'abstain'（移植自 orchestrator._vote_result）。"""
    vote = _extract_json(content)
    if isinstance(vote, dict):
        agreed = vote.get("agreed")
        if agreed == "abstain":
            return "abstain"
        if isinstance(agreed, bool):
            return "agree" if agreed else "reject"
    if "弃权" in content:
        return "abstain"
    without_negative = content.replace("不同意", "").replace("反对", "")
    return "agree" if "同意" in without_negative else "reject"


def resolve_strategy(proposal: dict | None) -> str | None:
    """从提案字段推断策略，不按 AP 编号或固定业务身份预设路径。"""
    if not proposal:
        return None
    return _infer_strategy_from_proposal(proposal)


def determine_strategy(ap_state: dict) -> str:
    """与 Python orchestrator 的确定性触发判断保持一致。"""
    sr_triggered = bool(_sr.analyze_interference(ap_state).get("co_sr_triggered"))
    priorities = {
        state.get("traffic_priority", "medium")
        for state in ap_state.values()
        if isinstance(state, dict)
    }
    edca_triggered = len(priorities) > 1

    if sr_triggered and edca_triggered:
        return "joint"
    if sr_triggered:
        return "co_sr"
    if edca_triggered:
        return "co_edca"
    return "noop"


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
    visible = agent_view(_SESSION.ap_state)
    state_json = json.dumps(visible[ap_id], ensure_ascii=False, indent=2)
    return (
        f"请广播你（{ap_id.upper()}）的当前状态。\n"
        "发言开头先明确说出你是哪个 AP，然后用自然语言完整说明你的实测参数，"
        "最后用一两句话简述你当前状态，例如信道是否偏忙、邻居信号是否偏强、"
        "业务质量是否稳定。\n\n"
        "你的实测数据如下，请覆盖所有字段，但不要只复制 JSON，也不要使用固定模板：\n"
        f"{state_json}\n\n"
        "只播报你自己的数据和你本机扫描到的邻居 RSSI，不要引用或分析其他 AP 自己上报的业务指标。"
    )


def propose_instruction(
    proposer_id: str,
    strategy_hint: str | None = None,
    recalled_episodes: list[dict] | None = None,
) -> str:
    state_summary = json.dumps(agent_view(_SESSION.ap_state), ensure_ascii=False, indent=2)
    if strategy_hint == "co_edca":
        tool_path_hint = (
            "【本轮快速路径提示】当前全网证据已显示：邻居 RSSI 未触发 Co-SR，"
            "但业务优先级/EDCA 存在差异化需求。请优先走 Co-EDCA："
            "调用 get_latest_ap_states 后，直接提出 EDCA 候选并调用 validate_edca_proposal 自检。"
            "除非 get_latest_ap_states 返回的最新状态出现强/中等干扰证据，否则不要调用 "
            "analyze_sr_interference / compute_sr_feasible_ranges / select_sr_concurrent_groups / "
            "evaluate_sr_candidate。\n\n"
        )
    elif strategy_hint == "co_sr":
        tool_path_hint = (
            "【本轮快速路径提示】当前全网证据已显示：邻居 RSSI 触发 Co-SR，且未发现必须做 "
            "EDCA 差异化的证据。请优先走 Co-SR：get_latest_ap_states → analyze_sr_interference "
            "→ select_sr_concurrent_groups → evaluate_sr_candidate。除非最新状态显示明确的 "
            "traffic_priority/QoS/EDCA 差异化需求，否则不要调用 validate_edca_proposal。\n\n"
        )
    elif strategy_hint == "joint":
        tool_path_hint = (
            "【本轮快速路径提示】当前全网证据同时支持 Co-SR 与 Co-EDCA。请走联合调整，"
            "但只调用能支撑最终提案的必要工具，避免重复评估无关候选。\n\n"
        )
    else:
        tool_path_hint = ""
    memory_hint = ""
    if recalled_episodes:
        lines = [
            "【历史案例（仅作参考，必须按当前最新状态重新调用工具验算）】"
        ]
        for item in recalled_episodes[:3]:
            metrics = item.get("metrics") or {}
            evaluation = item.get("evaluation") or {}
            if evaluation.get("final_verdict"):
                verdict_map = {
                    "improved": "实际改善", "degraded": "实际恶化",
                    "neutral": "无明显变化", "inconclusive": "数据不足",
                }
                feedback = (
                    f"执行后评估={verdict_map.get(evaluation['final_verdict'], evaluation['final_verdict'])}"
                    f"(置信度={evaluation.get('final_confidence', 0)})"
                )
            else:
                feedback = f"实测反馈={'可用' if metrics.get('available') else '尚无'}"
            lines.append(
                f"- 相似度={item.get('similarity', 0):.3f}，"
                f"质量={item.get('quality_score', 0):.2f}，"
                f"策略={item.get('strategy')}，结果={item.get('outcome')}，"
                f"决策={json.dumps(item.get('decision'), ensure_ascii=False)}，"
                f"{feedback}"
            )
        memory_hint = "\n".join(lines) + "\n\n"
    history_hint = (
        "【重要】请先完整阅读上方的对话记录。\n"
        "如果记录中有 VALIDATOR 发出的验证失败消息，你的新提案必须直接解决其中列出的具体问题。\n"
        "如果记录中已有历史提案和拒绝原因，你的提案必须明确回应各方此前提出的约束顾虑，"
        "而不是重复一个已被否决的方案。协商历史越长，越需要向各方约束的交集靠拢。\n\n"
    )
    return (
        f"你（{proposer_id.upper()}）是本轮的提案方，请发起参数调整提案。\n\n"
        f"{history_hint}"
        f"{tool_path_hint}"
        f"{memory_hint}"
        f"所有 AP 的初始状态数据（供参考）：\n{state_summary}\n\n"
        "请先调用 get_latest_ap_states 获取最新状态，分析当前网络的核心问题。\n\n"
        "【路径选择规则（基于实时证据，不按 AP 编号或固定业务身份预设）】\n"
        "  · 若存在强干扰（邻居 RSSI 偏强，或 analyze_sr_interference 的 co_sr_triggered=true）→ 可选 Co-SR。\n"
        "  · 若 traffic_priority、QoS 或当前 EDCA 参数显示需要差异化竞争机会 → 可选 Co-EDCA。\n"
        "  · 若两类问题同时成立 → 可选联合调整；若证据不足 → 说明暂不调整或提出最小改动方案。\n"
        "  · 不要为了完成协商强行制造 SR 或 EDCA 问题。\n\n"
        "【Co-SR】降低各 AP 的 TX Power 减少 OBSS 干扰。第一步必须先判断"
        "可用并发组：get_latest_ap_states → analyze_sr_interference → select_sr_concurrent_groups；"
        "再用 evaluate_sr_candidate（传入 proposed_powers，部分并发再传 concurrent_group）自检。"
        "功率取最大必要降幅且为整数 dBm。提案 JSON 只含每个 AP 的 tx_power_dbm，并附 "
        '`"_sr": {"concurrent_group": [...], "non_concurrent_aps": [...]}`。\n\n'
        "【Co-EDCA】按当前状态中的 traffic_priority、QoS 和 EDCA 参数差异调整 CWmin/CWmax/AIFSN。"
        "当优先级确实不同，满足 high.CWmin ≤ medium ≤ low、high.AIFSN ≤ medium ≤ low；"
        "同优先级或未知优先级时不要强行制造梯度。用 validate_edca_proposal（传 proposed_edca）自检。\n"
        "【重要】若各 AP 优先级确实不同（如 high/medium/low）但当前 EDCA 参数相同（未差异化），"
        "这本身就是需要 Co-EDCA 的证据：应让高优先级获得更小的 CWmin/CWmax/AIFSN、低优先级更大，"
        "切勿以『统一参数已平凡满足单调性』为由判定无需调整——未体现优先级差异即未达成本场景目标。\n\n"
        "【联合调整】只有当强干扰与 EDCA 竞争问题同时有证据支持时使用，"
        "同时调用 Co-SR 和 Co-EDCA 的相关验算工具，提案 JSON 可同时包含两类字段。\n\n"
        "提案须简洁说明：选哪条路径及原因、每个 AP 的最终参数与依据、预期改善与权衡。\n"
        "提交前必须调用 evaluate_sr_candidate（Co-SR/联合）或 validate_edca_proposal（Co-EDCA/联合）自检；"
        "提案阶段自检必须把你打算提的参数显式作为工具参数传入。\n"
        "提案末尾必须用 ```json 代码块附参数摘要，顶层键必须是 ap1/ap2/ap3，"
        "每个 AP 的值必须是对象（参数写在对象内部，严禁裸数值）。"
    )


def vote_instruction(voter_id: str, proposer_id: str, strategy: str,
                     proposal: dict, proposal_num: int) -> str:
    verify_hint = {
        "co_sr":   "关注你自己的 TX Power、evaluate_sr_candidate 返回的 valid/errors、STA RSSI/SINR/CCA 余量",
        "co_edca": "关注你自己的 traffic_priority、QoS 指标、当前 EDCA 参数、工具返回的 valid/errors 与优先级排序",
        "joint":   "关注你自己的 TX Power 与 EDCA 建议值、工具返回的 valid/errors，以及组合调整是否可接受",
    }.get(strategy, "关注工具返回的 valid/errors 以及参数对你的影响")
    if proposal_num >= 4:
        stall_hint = (f"\n\n【死锁警告：已是第 {proposal_num} 个提案】若各方在重复相似参数，"
                      "应选择弃权让当前折中方案通过，而不是再提一个同样无法满足约束的新方案。")
    elif proposal_num >= 2:
        stall_hint = f"\n\n【注意：已是第 {proposal_num} 个提案】若出现重复请考虑弃权，避免死锁。"
    else:
        stall_hint = ""
    proposal_json = json.dumps(proposal, ensure_ascii=False, indent=2)
    return (
        "【第一步】请完整阅读上方对话记录，梳理此前所有提案及每次拒绝的原因。\n\n"
        f"【第二步】验算 {proposer_id.upper()} 的最新提案（提案#{proposal_num}）中针对你自己（{voter_id.upper()}）的参数。\n\n"
        f"最新提案参数：\n{proposal_json}\n\n"
        "请先调用 get_latest_ap_states，再调用验算工具。"
        "本架构下验算工具不会自动回填提案：你必须把上方提案中针对各 AP 的参数"
        "（Co-SR 传 proposed_powers，部分并发连同 concurrent_group；Co-EDCA 传 proposed_edca）"
        "显式填入工具参数。然后用自然语言给出判断。"
        f"重点参考：{verify_hint}。\n\n"
        "三种表态：\n"
        "【同意】满足约束或可接受折中。末尾附 ```json\n{\"agreed\": true, \"reason\": \"...\"}\n```\n"
        "【弃权】未完全满足但找不到更好方案，或协商已重复。等同同意，无需反提案。末尾附 "
        "```json\n{\"agreed\": \"abstain\", \"reason\": \"...\"}\n```\n"
        "【反对】你有具体替代方案。同一条回复中先附 ```json\n{\"agreed\": false, \"reason\": \"...\"}\n``` "
        "再附完整反提案 JSON（顶层键 ap1/ap2/ap3）。反提案须兼顾各方约束；若选 Co-SR 或联合，"
        "须先 get_latest_ap_states→analyze_sr_interference→select_sr_concurrent_groups 并写 _sr.concurrent_group。"
        f"{stall_hint}"
    )


def repair_counter_instruction() -> str:
    """反对者回复中未解析出反提案 JSON 时的「修复轮」指令（移植自
    orchestrator._phase_counter_propose）。"""
    return (
        "你已表示反对，但回复中未找到可解析的参数 JSON。\n"
        "请回顾上方完整协商历史，综合所有 AP 此前提出的约束和顾虑，"
        "给出一个能兼顾所有人需求的反提案。\n"
        "你可以根据实时证据选择协商路径：Co-SR 使用 tx_power_dbm 字段，"
        "Co-EDCA 使用 CWmin/CWmax/AIFSN 字段，联合路径两类字段均出现；"
        "证据不足时不要为了形成反提案强行改变无关参数。\n"
        "如果选择 Co-SR 或联合路径，第一步必须真实调用 get_latest_ap_states、"
        "analyze_sr_interference、select_sr_concurrent_groups，先判断可用并发组，"
        "并在 JSON 中写入 _sr.concurrent_group。\n"
        "请只输出一个 ```json 代码块，JSON 顶层键必须是 ap1、ap2、ap3。不要写解释。"
    )


def final_instruction(proposer_id: str, proposal: dict) -> str:
    proposal_json = json.dumps(proposal, ensure_ascii=False, indent=2)
    return (
        "所有 AP 已同意提案。\n"
        f"已通过的提案参数 JSON 如下，请最终决策必须与它保持一致：\n{proposal_json}\n\n"
        "请输出最终的 JSON 决策方案（严格合法、JSON 内不得有注释），顶层键必须是 ap1/ap2/ap3，"
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
    try:
        reply = drive_ap(
            ap_id,
            instruction,
            thinking=thinking,
            extra_env=extra_env,
            on_text_delta=on_text if stream_enabled else None,
        )
    except TypeError as exc:
        if "on_text_delta" not in str(exc):
            raise
        reply = drive_ap(
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
        proposer_id, strategy_hint, _SESSION.recalled_episodes
    )
    reply, _ = _run_agent_turn(
        proposer_id,
        3,
        "proposer",
        instruction,
        logger=logger,
        on_event_start=on_event_start,
        on_event_chunk=on_event_chunk,
    )
    _SESSION.record(proposer_id.upper(), reply)
    proposal = _extract_proposal(reply)
    if proposal is not None:
        proposal = _with_sr_concurrent_group(proposal, _SESSION.ap_state)
    strategy = resolve_strategy(proposal)
    _SESSION.proposer = proposer_id
    _SESSION.proposal = proposal
    _SESSION.strategy = strategy
    _SESSION.proposal_num += 1
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
    context_env = {
        "MULTIAP_CURRENT_PROPOSAL": json.dumps(s.proposal, ensure_ascii=False),
        "MULTIAP_CURRENT_STRATEGY": s.strategy or "",
    }
    instruction = vote_instruction(
        voter_id, s.proposer, s.strategy or "co_edca", s.proposal, s.proposal_num)
    reply, _ = _run_agent_turn(
        voter_id,
        4,
        "voter",
        instruction,
        logger=logger,
        on_event_start=on_event_start,
        on_event_chunk=on_event_chunk,
        extra_env=context_env,
    )
    s.record(voter_id.upper(), reply)
    vote = read_vote(reply)
    counter = None
    if vote == "reject":
        counter = _extract_proposal(reply)
        if counter is not None:
            counter = _with_sr_concurrent_group(counter, s.ap_state)
    return {"voter": voter_id, "reply": reply, "vote": vote,
            "counter_proposal": counter}


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
    _SESSION.record(voter_id.upper(), reply)
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
    s.record(s.proposer.upper(), reply)
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
                   logger=None) -> dict[str, dict]:
    if not endpoints:
        return {}

    step_id = logger.start_step(
        "executor_apply",
        retry_budget=1,
        input_data={"strategy": strategy, "endpoints": endpoints, "decision": decision},
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
            {"session_id": session_id, "ap_id": ap_id, "strategy": strategy, "params": params},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        idem_key = "executor_apply:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        action = None
        if logger is not None:
            action, _ = logger.prepare_action(
                idempotency_key=idem_key,
                action_type="executor_apply",
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
            resp = requests.post(f"{url.rstrip('/')}/apply", json=payload, timeout=8)
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
    try:
        return apply_profile(observation_state_getter()), None, True
    except Exception as exc:  # noqa: BLE001
        return {}, f"观测状态获取失败: {exc}", False


def _finish(logger, outcome: str, rounds: int, started_at: float) -> None:
    if logger is not None:
        logger.session_end(
            outcome,
            rounds,
            negotiation_duration_s=time.time() - started_at,
        )


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
            "recalled_episode_ids": [
                item.get("episode_id") for item in s.recalled_episodes
            ],
            "retry": retry,
            "agree": sorted(agree or set()),
            "vote_cursor": vote_cursor,
        },
    )


def _restore_projection(projection: dict) -> Session:
    reset_session(projection.get("ap_state") or {})
    s = _SESSION
    s.transcript = list(projection.get("transcript") or [])
    s.proposer = projection.get("proposer")
    s.proposal = projection.get("proposal")
    s.strategy = projection.get("strategy")
    s.proposal_num = int(projection.get("proposal_num") or 0)
    s.decision = projection.get("decision")
    s.memory_manager.memory = SessionMemory.from_dict(projection.get("session_memory"))
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
                     evaluation_windows: tuple[float, ...] | None = None) -> dict:
    """阶段级快速协商。on_tool：进程内 structured_relay 路径传入工具调用展示回调，
    在 drive_ap 每次 AP 发言后从 trajectory 提取并回调；coordinator 路径
    （run_fast_negotiation）不传，保持 None。"""
    global _tool_callback, _memory_callback
    _tool_callback = on_tool
    if logger is not None:
        _memory_callback = lambda memory, summary: logger.save_session_memory(
            memory.to_dict(), summary, _SESSION.memory_manager.budget_chars
        )
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
        )
    finally:
        _tool_callback = None
        _memory_callback = None


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
                     evaluation_windows: tuple[float, ...] | None = None) -> dict:
    global _tool_callback

    def emit(phase, who, reply):
        if on_event:
            on_event(phase, who, reply)

    started_at = time.time()
    if resume_projection:
        s = _restore_projection(resume_projection)
    else:
        reset_session(initial_state)
        s = _SESSION

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
            with ThreadPoolExecutor(max_workers=len(AP_IDS)) as ex:
                futures = {
                    ap: ex.submit(_run_agent_turn, ap, 1, "broadcast", bcast_inst[ap])
                    for ap in AP_IDS
                }
                for ap in AP_IDS:
                    reply, streamed = futures[ap].result()
                    s.record(ap.upper(), reply)
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

    if logger is not None and not (
        resume_projection
        and str(resume_projection.get("boundary") or "")
        in {"proposal_ready", "vote_progress", "counter_proposal_ready"}
    ):
        s.recalled_episodes = logger.recall_episodes(s.ap_state, limit=3, min_quality=0.5)

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
            if p["parsed"]:
                _save_checkpoint(
                    logger, "proposal_ready", retry=retry,
                    agree=agree, vote_cursor=vote_cursor,
                )
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
                    errs = "；".join((val or {}).get("global_errors") or []) if val else "无法解析决策"
                    s.record("VALIDATOR", f"[验证未通过] {(val or {}).get('summary','')}\n具体问题：{errs}")
                    _save_checkpoint(
                        logger, "broadcast_complete", retry=retry + 1,
                    )
                    break
            else:  # reject
                counter = rv["counter_proposal"]
                if counter is None:
                    # 修复轮：反对者未给出可解析反提案，再驱动一次让其补纯 JSON
                    repair_inst = repair_counter_instruction()
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
                    promote_counter(voter, counter)
                    agree = set()
                    _save_checkpoint(
                        logger, "counter_proposal_ready", retry=retry,
                        agree=agree, vote_cursor=vote_cursor,
                    )
                # 修复后仍解析失败则跳过本轮，继续轮转
        else:
            _finish(logger, "max_turns_exceeded", s.proposal_num, started_at)
            return {"outcome": "max_turns_exceeded", "decision": None,
                    "strategy": s.strategy, "validation": None, "push_results": {},
                    "observed_is_real": False,
                    "transcript_turns": len(s.transcript)}

    _finish(logger, "max_retries_exceeded", s.proposal_num, started_at)
    return {"outcome": "max_retries_exceeded", "decision": None,
            "strategy": s.strategy, "validation": None, "push_results": {},
            "observed_is_real": False,
            "transcript_turns": len(s.transcript)}

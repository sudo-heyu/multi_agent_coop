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


# 工具调用展示回调（direct-relay 路径用）。structured_relay 进入时设置、退出时清理；
# 其余路径（relay 总线、run_fast_negotiation）保持 None，drive_ap 自动跳过解析。
_tool_callback: Callable | None = None


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

    def record(self, speaker: str, content: str) -> None:
        self.transcript.append({"speaker": speaker, "content": content})

    def transcript_text(self) -> str:
        return "\n\n".join(f"### {m['speaker']}\n{m['content']}" for m in self.transcript)


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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if proc.returncode == 0:
            try:
                reply = _reply_text(json.loads(proc.stdout))
            except json.JSONDecodeError:
                reply = proc.stdout.strip()
            if reply.strip():
                _emit_tool_calls(ap, sid)
                return reply
            last_err = "空回复(payloads=0)"
        else:
            last_err = (proc.stderr or proc.stdout)[-300:]
            if use_gateway:
                # gateway 模式失败（连接/进程级）→ 回退 embedded，后续尝试不再走 gateway
                use_gateway = False
        if attempt < DRIVE_RETRIES - 1:
            __import__("time").sleep(2.0)
    raise RuntimeError(f"drive_ap({ap}) 连续 {DRIVE_RETRIES} 次失败: {last_err}")


def _emit_tool_calls(ap_id: str, session_id: str) -> None:
    """若设置了 _tool_callback，从本次 AP 会话的 trajectory 提取工具调用并逐条回调。

    direct-relay 非流式：工具行无法穿插在发言中间，故由调用方在发言前成块打印。
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
# trajectory 工具调用提取（direct-relay 展示用）
# ──────────────────────────────────────────────────────────────────────

def _openclaw_agents_dir() -> Path:
    """openclaw profile 的 agents 目录。

    默认 $HOME/.openclaw-{PROFILE}/agents（与 openclaw/setup.sh 一致）；
    若未来 openclaw 支持 OPENCLAW_HOME 环境变量，应在此优先读取。"""
    home = os.environ.get("OPENCLAW_HOME") or str(Path.home())
    return Path(home) / f".openclaw-{PROFILE}" / "agents"


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


def propose_instruction(proposer_id: str) -> str:
    state_summary = json.dumps(agent_view(_SESSION.ap_state), ensure_ascii=False, indent=2)
    history_hint = (
        "【重要】请先完整阅读上方的对话记录。\n"
        "如果记录中有 VALIDATOR 发出的验证失败消息，你的新提案必须直接解决其中列出的具体问题。\n"
        "如果记录中已有历史提案和拒绝原因，你的提案必须明确回应各方此前提出的约束顾虑，"
        "而不是重复一个已被否决的方案。协商历史越长，越需要向各方约束的交集靠拢。\n\n"
    )
    return (
        f"你（{proposer_id.upper()}）是本轮的提案方，请发起参数调整提案。\n\n"
        f"{history_hint}"
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
        "同优先级或未知优先级时不要强行制造梯度。用 validate_edca_proposal（传 proposed_edca）自检。\n\n"
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

def run_broadcast(ap_id: str) -> dict:
    reply = drive_ap(ap_id, broadcast_instruction(ap_id))
    _SESSION.record(ap_id.upper(), reply)
    return {"ap_id": ap_id, "reply": reply}


def run_propose(proposer_id: str) -> dict:
    reply = drive_ap(proposer_id, propose_instruction(proposer_id))
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


def run_vote(voter_id: str) -> dict:
    s = _SESSION
    if s.proposal is None or s.proposer is None:
        return {"error": "当前无有效提案，请先 run_propose"}
    context_env = {
        "MULTIAP_CURRENT_PROPOSAL": json.dumps(s.proposal, ensure_ascii=False),
        "MULTIAP_CURRENT_STRATEGY": s.strategy or "",
    }
    reply = drive_ap(voter_id, vote_instruction(
        voter_id, s.proposer, s.strategy or "co_edca", s.proposal, s.proposal_num),
        extra_env=context_env)
    s.record(voter_id.upper(), reply)
    vote = read_vote(reply)
    counter = None
    if vote == "reject":
        counter = _extract_proposal(reply)
        if counter is not None:
            counter = _with_sr_concurrent_group(counter, s.ap_state)
    return {"voter": voter_id, "reply": reply, "vote": vote,
            "counter_proposal": counter}


def run_repair_counter(voter_id: str) -> dict:
    """修复轮：反对者未给出可解析反提案时，再驱动它一次只输出纯 JSON 反提案。
    返回 {"reply", "counter_proposal"}；解析失败则 counter_proposal=None。"""
    reply = drive_ap(voter_id, repair_counter_instruction())
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


def run_final() -> dict:
    s = _SESSION
    if s.proposal is None or s.proposer is None:
        return {"error": "当前无通过的提案"}
    reply = drive_ap(s.proposer, final_instruction(s.proposer, s.proposal))
    s.record(s.proposer.upper(), reply)
    decision = _extract_json(reply)
    if decision is not None and isinstance(s.proposal, dict):
        for meta_key in ("_sr", "sr", "concurrent_group"):
            if meta_key in s.proposal and meta_key not in decision:
                decision[meta_key] = s.proposal[meta_key]
    s.decision = decision
    return {"decision": decision, "strategy": s.strategy, "reply": reply}


# ══════════════════════════════════════════════════════════════════════
# 架构 C：无协调者 —— AP 自驱动 + 接力总线
#
# relay 是一根「极薄的接力总线」，不做任何协议决策：
#   - 维护共享 transcript，把它注入每个 AP 的轮次消息；
#   - 调用「当前发言者」，读取该 AP 自己声明的 control 块（next/done）来传棒；
#   - 仅保留两条安全网：硬性步数上限 + AP 漏报 next 时的默认轮转；
#   - AP 全部声明 done 后，对最终决策做一次确定性 Validator 验收（验证而非决策）。
# 协议的全部判断（阶段、谁提案、计票、是否反对、何时结束）都在各 AP 的 AGENTS.md 里。
# ══════════════════════════════════════════════════════════════════════

import re as _re

CTRL_PREFIX = "@@CTRL"


def turn_instruction(speaker: str) -> str:
    return (
        f"现在轮到你（{speaker.upper()}）发言。\n"
        "请阅读上方完整对话记录，自行判断当前处于协议的哪个阶段（广播 / 提案 / 投票 / 最终决策），"
        "严格按你 AGENTS.md 中的协议规则完成你这一步：\n"
        "  · 广播阶段：先调用 get_latest_ap_states，只播报你自己的实测数据与本机扫描到的邻居 RSSI。\n"
        "  · 提案阶段：按路径规则调用计算/验算工具，给出含 ap1/ap2/ap3 的参数 JSON 提案。\n"
        "  · 投票阶段：调用验算工具核对针对你的参数，表态 同意/弃权/反对（反对须附反提案）。\n"
        "  · 最终决策：若你是提案方且其余 AP 都已同意，先调用 validate_decision 自检，"
        "再输出最终决策 JSON（顶层键 ap1/ap2/ap3）。\n\n"
        "【接力规则】在你回复的最后一行，必须输出一行控制标记声明下一位发言者：\n"
        f"  {CTRL_PREFIX} {{\"phase\": \"broadcast|propose|vote|decide\", \"next\": \"ap2\", \"done\": false}}\n"
        "当且仅当你已输出最终决策 JSON 且协商结束时，写 "
        f"{CTRL_PREFIX} {{\"phase\": \"decide\", \"next\": null, \"done\": true}}。\n"
        "next 必须是 ap1/ap2/ap3 之一或 null。不要在控制标记之外解释它。"
    )


def parse_ctrl(text: str) -> dict:
    """从 AP 回复中解析最后一行 @@CTRL 控制块。解析失败返回 {}。"""
    last = {}
    for m in _re.finditer(rf"{_re.escape(CTRL_PREFIX)}\s*(\{{.*?\}})", text, _re.DOTALL):
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                last = obj
        except json.JSONDecodeError:
            continue
    return last


def _default_next(speaker: str) -> str:
    i = AP_IDS.index(speaker) if speaker in AP_IDS else -1
    return AP_IDS[(i + 1) % len(AP_IDS)]


def drive_turn(speaker: str) -> str:
    reply = drive_ap(speaker, turn_instruction(speaker))
    _SESSION.record(speaker.upper(), reply)
    return reply


def relay(max_steps: int = 30, first: str = "ap1", on_turn=None) -> dict:
    """运行无协调者协商。on_turn(step, speaker, reply, ctrl) 可选回调（日志/可视化）。"""
    reset_session()
    speaker = first
    last_reply = ""
    done = False
    steps = 0
    for step in range(max_steps):
        steps = step + 1
        last_reply = drive_turn(speaker)
        ctrl = parse_ctrl(last_reply)
        if on_turn:
            on_turn(step, speaker, last_reply, ctrl)
        if ctrl.get("done"):
            done = True
            break
        nxt = (ctrl.get("next") or "").lower()
        speaker = nxt if nxt in AP_IDS else _default_next(speaker)

    # 收尾：对最终决策做确定性 Validator 验收（安全网，验证而非决策）
    decision = _extract_proposal(last_reply) or _extract_json(last_reply)
    strategy = resolve_strategy(decision)
    validation = None
    if decision is not None and strategy:
        validation = _validate_decision(
            _SESSION.ap_state, decision, strategy,
            observed_state=_SESSION.ap_state, observed_is_real=False)
    return {
        "done": done, "steps": steps, "decision": decision,
        "strategy": strategy, "validation": validation,
        "transcript_turns": len(_SESSION.transcript),
    }


# ══════════════════════════════════════════════════════════════════════
# 结构化接力（推荐）：thin relay 确定性编排四阶段「轮次顺序」，
# 每个 AP 仍经 OpenClaw agent 完全自主决定「发言内容」。
# 不是 agent、不是 LLM、不是协调者——复刻原 orchestrator.run() 控制流，
# 把 speak() 换成 openclaw agent。这是可靠复现结果的路径。
# ══════════════════════════════════════════════════════════════════════

import itertools as _it


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
                   session_id: str = "") -> dict[str, dict]:
    if not endpoints:
        return {}

    def send(ap_id: str, url: str) -> tuple[str, bool, str, dict]:
        params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
        params = encode_params_edca(params)
        payload = {
            "session_id": session_id,
            "strategy": strategy,
            "ap_id": ap_id,
            "params": params,
        }
        try:
            import requests
            resp = requests.post(f"{url.rstrip('/')}/apply", json=payload, timeout=8)
            ok = resp.status_code == 200
            try:
                body = resp.json()
                msg = body.get("details", body)
            except Exception:
                msg = resp.text
            return ap_id, ok, str(msg), payload
        except Exception as exc:  # noqa: BLE001
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


def structured_relay(max_validation_retries: int = 3, max_turns: int = 30,
                     on_event=None,
                     on_tool: Callable | None = None,
                     logger=None,
                     observation_state_getter: Callable[[], dict] | None = None,
                     observation_wait_seconds: float = 0.0,
                     executor_endpoints: dict[str, str] | None = None,
                     initial_state: dict | None = None) -> dict:
    """阶段级快速协商。on_tool：direct-relay 路径传入工具调用展示回调，
    在 drive_ap 每次 AP 发言后从 trajectory 提取并回调；coordinator 路径
    （run_fast_negotiation）不传，保持 None。"""
    global _tool_callback
    _tool_callback = on_tool
    try:
        return _structured_relay_impl(
            max_validation_retries=max_validation_retries,
            max_turns=max_turns,
            on_event=on_event,
            logger=logger,
            observation_state_getter=observation_state_getter,
            observation_wait_seconds=observation_wait_seconds,
            executor_endpoints=executor_endpoints,
            initial_state=initial_state,
        )
    finally:
        _tool_callback = None


def _structured_relay_impl(max_validation_retries: int = 3, max_turns: int = 30,
                     on_event=None,
                     logger=None,
                     observation_state_getter: Callable[[], dict] | None = None,
                     observation_wait_seconds: float = 0.0,
                     executor_endpoints: dict[str, str] | None = None,
                     initial_state: dict | None = None) -> dict:
    def emit(phase, who, reply):
        if on_event:
            on_event(phase, who, reply)

    started_at = time.time()
    reset_session(initial_state)
    s = _SESSION

    # 阶段一：广播（固定顺序 ap1→ap2→ap3）
    _log_phase(logger, 1, "广播自身状态")
    for ap in AP_IDS:
        instruction = broadcast_instruction(ap)
        result = run_broadcast(ap)
        emit("broadcast", ap, result["reply"])
        _log_agent_reply(logger, ap, 1, "broadcast", instruction, result["reply"])

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

    _log_phase(logger, 2, "协商触发，等待 AP1 自主选路")

    # 外层：Validator 未通过则从 ap1 重新提案
    for retry in range(max_validation_retries):
        proposer = "ap1"
        _log_phase(logger, 3, f"{proposer.upper()} 发起提案（自主选路）")
        propose_inst = propose_instruction(proposer)
        p = run_propose(proposer)
        emit("propose", proposer, p["reply"])
        _log_agent_reply(logger, proposer, 3, "proposer", propose_inst, p["reply"])
        if not p["parsed"]:
            _finish(logger, "proposal_parse_error", s.proposal_num, started_at)
            return {"outcome": "proposal_parse_error", "decision": None,
                    "strategy": None, "validation": None, "push_results": {},
                    "observed_is_real": False, "transcript_turns": len(s.transcript)}

        agree: set[str] = set()
        cycle = _it.cycle(AP_IDS)
        while next(cycle) != "ap1":
            pass  # 从 ap1 之后开始

        for _ in range(max_turns):
            voter = next(cycle)
            if voter == s.proposer:
                continue
            _log_phase(
                logger,
                4,
                f"{voter.upper()} 投票（提案#{s.proposal_num}，提案方 {s.proposer.upper()}）",
            )
            vote_inst = vote_instruction(
                voter, s.proposer, s.strategy or "co_edca", s.proposal, s.proposal_num)
            rv = run_vote(voter)
            emit("vote", voter, rv["reply"])
            _log_agent_reply(logger, voter, 4, "voter", vote_inst, rv["reply"])
            if logger is not None:
                logger.vote(voter, s.proposal_num, rv["vote"], rv["reply"])

            if rv["vote"] in ("agree", "abstain"):
                agree.add(voter)
                non_proposers = {a for a in AP_IDS if a != s.proposer}
                if agree >= non_proposers:
                    if logger is not None:
                        logger.round_result(
                            s.proposal_num, True, len(agree), len(non_proposers)
                        )
                    _log_phase(logger, 5, "输出最终决策")
                    final_inst = final_instruction(s.proposer, s.proposal)
                    fr = run_final()
                    emit("decide", s.proposer, fr["reply"])
                    _log_agent_reply(
                        logger, s.proposer, 5, "decision", final_inst, fr["reply"])
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
                            decision, strategy or "", executor_endpoints, session_id)
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
                    break
            else:  # reject
                counter = rv["counter_proposal"]
                if counter is None:
                    # 修复轮：反对者未给出可解析反提案，再驱动一次让其补纯 JSON
                    repair_inst = repair_counter_instruction()
                    rep = run_repair_counter(voter)
                    emit("propose", voter, rep["reply"])
                    _log_agent_reply(
                        logger, voter, 3, "counter_proposal_json_repair",
                        repair_inst, rep["reply"])
                    counter = rep["counter_proposal"]
                if counter is not None:
                    promote_counter(voter, counter)
                    agree = set()  # 新提案方接管，重新计票（cycle 自然推进到 voter 之后）
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

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
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.orchestrator import (
    _infer_strategy_from_proposal,
    _extract_proposal,
    _extract_json,
    _with_sr_concurrent_group,
)
from src.profile import agent_view, apply_profile
from src.state_client import get_all_states
from src.validator import validate_decision as _validate_decision

AP_IDS = ["ap1", "ap2", "ap3"]
STATE_SERVER = os.environ.get("MULTIAP_STATE_SERVER", "http://localhost:5001")
PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", str(Path.home() / ".openclaw" / "bin" / "openclaw"))
DRIVE_RETRIES = int(os.environ.get("MULTIAP_DRIVE_RETRIES", "3"))


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


def reset_session() -> dict:
    global _SESSION
    _SESSION = Session()
    _SESSION.ap_state = apply_profile(get_all_states(STATE_SERVER))
    return {"ok": True, "ap_states": agent_view(_SESSION.ap_state),
            "ap_ids": AP_IDS, "next": "对 ap1→ap2→ap3 依次调用 broadcast"}


# ──────────────────────────────────────────────────────────────────────
# 驱动子 agent
# ──────────────────────────────────────────────────────────────────────

def drive_ap(ap_id: str, instruction: str, thinking: str = "off") -> str:
    """让某个 AP agent 跑一个回合，返回其发言文本。底层 openclaw agent --local。

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

    # qwen3:14b 偶发「incomplete terminal response」（payloads=0），多为瞬时；重试。
    last_err = ""
    for attempt in range(DRIVE_RETRIES):
        cmd = [OPENCLAW_BIN, "--profile", PROFILE, "agent", "--local",
               "--agent", ap, "--session-id", f"{ap}-{uuid.uuid4().hex[:12]}",
               "--thinking", thinking, "--message", msg, "--json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if proc.returncode == 0:
            try:
                reply = _reply_text(json.loads(proc.stdout))
            except json.JSONDecodeError:
                reply = proc.stdout.strip()
            if reply.strip():
                return reply
            last_err = "空回复(payloads=0)"
        else:
            last_err = (proc.stderr or proc.stdout)[-300:]
        if attempt < DRIVE_RETRIES - 1:
            __import__("time").sleep(2.0)
    raise RuntimeError(f"drive_ap({ap}) 连续 {DRIVE_RETRIES} 次失败: {last_err}")


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
    """只允许 Co-SR / Co-EDCA 两种策略（联合已取消）。
    提案含功率字段即按 Co-SR 验收（强干扰优先）；否则按 Co-EDCA。"""
    if not proposal:
        return None
    s = _infer_strategy_from_proposal(proposal)
    return "co_sr" if s == "joint" else s


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
        "【路径选择规则（只有 Co-SR、Co-EDCA 两种，没有联合路径，必须二选一）】\n"
        "  · 若存在强干扰（邻居 RSSI 偏强，或 analyze_sr_interference 的 co_sr_triggered=true）→ 选 Co-SR。\n"
        "  · 否则（无强干扰）→ 选 Co-EDCA，按 traffic_priority 差异化。\n"
        "  · 严禁同一提案同时包含功率与 EDCA 两类字段。\n\n"
        "【Co-SR】降低各 AP 的 TX Power 减少 OBSS 干扰。第一步必须先判断"
        "可用并发组：get_latest_ap_states → analyze_sr_interference → select_sr_concurrent_groups；"
        "再用 evaluate_sr_candidate（传入 proposed_powers，部分并发再传 concurrent_group）自检。"
        "功率取最大必要降幅且为整数 dBm。提案 JSON 只含每个 AP 的 tx_power_dbm，并附 "
        '`"_sr": {"concurrent_group": [...], "non_concurrent_aps": [...]}`。\n\n'
        "【Co-EDCA】按各 AP 的 traffic_priority 差异化 CWmin/CWmax/AIFSN：high 用更小、low 用更大，"
        "满足 high.CWmin ≤ low.CWmin、high.AIFSN ≤ low.AIFSN；用 validate_edca_proposal（传 proposed_edca）自检。"
        "提案 JSON 只含每个 AP 的 CWmin/CWmax/AIFSN。\n\n"
        "提案须简洁说明：选哪条路径及原因、每个 AP 的最终参数与依据、预期改善与权衡。\n"
        "提交前必须调用 evaluate_sr_candidate（Co-SR）或 validate_edca_proposal（Co-EDCA）自检；"
        "提案阶段自检必须把你打算提的参数显式作为工具参数传入。\n"
        "提案末尾必须用 ```json 代码块附参数摘要，顶层键必须是 ap1/ap2/ap3，"
        "每个 AP 的值必须是对象（参数写在对象内部，严禁裸数值）。"
    )


def vote_instruction(voter_id: str, proposer_id: str, strategy: str,
                     proposal: dict, proposal_num: int) -> str:
    verify_hint = {
        "co_sr":   "关注你自己的 TX Power、evaluate_sr_candidate 返回的 valid/errors、STA RSSI/SINR/CCA 余量",
        "co_edca": "关注你自己的 traffic_priority（高优先级需更小 CWmin/AIFSN，低优先级需更大）、工具返回的 valid/errors 与优先级排序",
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
        "再附完整反提案 JSON（顶层键 ap1/ap2/ap3）。反提案须兼顾各方约束；若选 Co-SR，"
        "须先 get_latest_ap_states→analyze_sr_interference→select_sr_concurrent_groups 并写 _sr.concurrent_group。"
        f"{stall_hint}"
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
    reply = drive_ap(voter_id, vote_instruction(
        voter_id, s.proposer, s.strategy or "co_edca", s.proposal, s.proposal_num))
    s.record(voter_id.upper(), reply)
    vote = read_vote(reply)
    counter = None
    if vote == "reject":
        counter = _extract_proposal(reply)
        if counter is not None:
            counter = _with_sr_concurrent_group(counter, s.ap_state)
    return {"voter": voter_id, "reply": reply, "vote": vote,
            "counter_proposal": counter}


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


def structured_relay(max_validation_retries: int = 3, max_turns: int = 30,
                     on_event=None) -> dict:
    def emit(phase, who, reply):
        if on_event:
            on_event(phase, who, reply)

    reset_session()
    s = _SESSION

    # 阶段一：广播（固定顺序 ap1→ap2→ap3）
    for ap in AP_IDS:
        emit("broadcast", ap, run_broadcast(ap)["reply"])

    # 外层：Validator 未通过则从 ap1 重新提案
    for retry in range(max_validation_retries):
        proposer = "ap1"
        p = run_propose(proposer)
        emit("propose", proposer, p["reply"])
        if not p["parsed"]:
            return {"outcome": "proposal_parse_error", "decision": None,
                    "strategy": None, "validation": None, "transcript_turns": len(s.transcript)}

        agree: set[str] = set()
        cycle = _it.cycle(AP_IDS)
        while next(cycle) != "ap1":
            pass  # 从 ap1 之后开始

        for _ in range(max_turns):
            voter = next(cycle)
            if voter == s.proposer:
                continue
            rv = run_vote(voter)
            emit("vote", voter, rv["reply"])

            if rv["vote"] in ("agree", "abstain"):
                agree.add(voter)
                non_proposers = {a for a in AP_IDS if a != s.proposer}
                if agree >= non_proposers:
                    fr = run_final()
                    emit("decide", s.proposer, fr["reply"])
                    decision, strategy = fr["decision"], s.strategy
                    val = (_validate_decision(s.ap_state, decision, strategy,
                                              observed_state=s.ap_state, observed_is_real=False)
                           if decision and strategy else None)
                    if val and val["approved"]:
                        return {"outcome": "success", "decision": decision,
                                "strategy": strategy, "validation": val,
                                "transcript_turns": len(s.transcript)}
                    # 验收未过：写入对话记录，外层从 ap1 重提案
                    errs = "；".join((val or {}).get("global_errors") or []) if val else "无法解析决策"
                    s.record("VALIDATOR", f"[验证未通过] {(val or {}).get('summary','')}\n具体问题：{errs}")
                    break
            else:  # reject
                counter = rv["counter_proposal"]
                if counter is not None:
                    promote_counter(voter, counter)
                    agree = set()  # 新提案方接管，重新计票（cycle 自然推进到 voter 之后）
                # counter 解析失败则跳过本轮，继续轮转
        else:
            return {"outcome": "max_turns_exceeded", "decision": None,
                    "strategy": s.strategy, "validation": None,
                    "transcript_turns": len(s.transcript)}

    return {"outcome": "max_retries_exceeded", "decision": None,
            "strategy": s.strategy, "validation": None,
            "transcript_turns": len(s.transcript)}

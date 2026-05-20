import json
import re
import time
from pathlib import Path

from .agent import APAgent
from .console_style import ap_content_color, divider, format_ap_name, reset, section, status_label
from .logger import SessionLogger
from .tools.registry import TOOL_DEFINITIONS, make_executor
from .validator import validate_decision

AP_IDS = ["ap1", "ap2", "ap3"]
MAX_VOTE_ROUNDS = 3

# 策略触发阈值
SR_RSSI_TRIGGER_DBM = -70.0
EDCA_BUSY_TRIGGER   = 0.60
EDCA_RETRY_TRIGGER  = 0.15

# 按工具名预分组，避免重复过滤
_COMPUTE_SR    = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "compute_sr_recommendations"]
_COMPUTE_EDCA  = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "compute_edca_recommendations"]
_VALIDATE_SR   = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "validate_sr_proposal"]
_VALIDATE_EDCA = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "validate_edca_proposal"]


def _tools_for_propose(strategy: str) -> list:
    if strategy == "co_sr":
        return _COMPUTE_SR + _VALIDATE_SR
    if strategy == "co_edca":
        return _COMPUTE_EDCA + _VALIDATE_EDCA
    return TOOL_DEFINITIONS  # joint


def _tools_for_vote(strategy: str) -> list:
    if strategy == "co_sr":
        return _VALIDATE_SR
    if strategy == "co_edca":
        return _VALIDATE_EDCA
    return _VALIDATE_SR + _VALIDATE_EDCA  # joint


def _extract_json(text: str) -> dict | None:
    """从 agent 回复中提取第一个合法 JSON 对象。"""
    # 优先匹配 markdown 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 回退：找裸 JSON
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


class NegotiationOrchestrator:
    def __init__(
        self,
        agents_dir: Path,
        model: str = "qwen3:14b",
        logger: SessionLogger | None = None,
    ):
        self.agents: dict[str, APAgent] = {
            ap_id: APAgent(ap_id, agents_dir, model)
            for ap_id in AP_IDS
        }
        self.conversation_log: list[dict] = []
        self.logger = logger
        self._current_ap_states: dict | None = None  # 由 run() 注入，供工具执行器使用

    # ──────────────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────────────

    def _record(self, speaker: str, content: str) -> None:
        """追加到对话记录。"""
        self.conversation_log.append({"speaker": speaker, "content": content})

    def _determine_strategy(self, ap_state: dict) -> str:
        need_sr = any(
            max(s.get("neighbor_rssi_dbm", {}).values(), default=-200) >= SR_RSSI_TRIGGER_DBM
            for s in ap_state.values()
        )
        need_edca = any(
            s.get("channel_busy_ratio", 0) >= EDCA_BUSY_TRIGGER
            or s.get("tx_retries_ratio", 0) >= EDCA_RETRY_TRIGGER
            for s in ap_state.values()
        )
        if need_sr and need_edca:
            return "joint"
        return "co_sr" if need_sr else "co_edca"

    def _find_worst_ap(self, ap_state: dict, strategy: str) -> str:
        ap_ids = list(ap_state.keys())

        def sr_score(s):
            return max(s.get("neighbor_rssi_dbm", {}).values(), default=-200) + 200

        def edca_score(s):
            return s.get("channel_busy_ratio", 0) + s.get("tx_retries_ratio", 0) * 2

        if strategy == "co_sr":
            scores = {ap: sr_score(ap_state[ap]) for ap in ap_ids}
        elif strategy == "co_edca":
            scores = {ap: edca_score(ap_state[ap]) for ap in ap_ids}
        else:
            sr_max   = max(sr_score(ap_state[ap])   for ap in ap_ids) or 1
            edca_max = max(edca_score(ap_state[ap]) for ap in ap_ids) or 1
            scores = {
                ap: sr_score(ap_state[ap]) / sr_max + edca_score(ap_state[ap]) / edca_max
                for ap in ap_ids
            }
        return max(scores, key=scores.get)

    def _agreed(self, content: str) -> bool:
        if "不同意" in content:
            return False
        return "同意" in content

    def _speak_and_log(
        self,
        ap_id: str,
        instruction: str,
        phase: int,
        role: str,
        tools: list | None = None,
        speaker: str | None = None,
    ) -> str:
        """调用 agent.speak_stream()，流式打印、处理工具调用、记录日志。"""
        executor = make_executor(self._current_ap_states) if tools else None
        tool_log: list[dict] = []
        speaker = speaker or ap_id.upper()

        def on_tool(tool_name: str, raw_args: dict, result_dict: dict, dur_ms: float) -> None:
            result_str = json.dumps(result_dict, ensure_ascii=False)
            args_str = json.dumps(raw_args, ensure_ascii=False) if raw_args else ""
            if len(args_str) > 60:
                args_str = args_str[:60] + "..."
            if len(result_str) > 120:
                result_str = result_str[:120] + "..."
            print(f"  {status_label('工具')} {tool_name}({args_str}) -> {result_str}", flush=True)

        t0 = time.time()
        chunks: list[str] = []

        print(f"\n{divider()}")
        print(f"{format_ap_name(speaker)}:")
        content_color = ap_content_color(speaker)
        content_started = False
        for chunk in self.agents[ap_id].speak_stream(
            self.conversation_log,
            instruction,
            tools=tools,
            tool_executor=executor,
            tool_log=tool_log,
            tool_callback=on_tool,
        ):
            chunks.append(chunk)
            if content_color and not content_started:
                print(content_color, end="", flush=True)
                content_started = True
            print(chunk, end="", flush=True)
        if content_color and content_started:
            print(reset(), end="", flush=True)
        print()

        content = "".join(chunks).strip()
        duration_ms = (time.time() - t0) * 1000
        self._record(speaker, content)

        if self.logger:
            self.logger.agent_speak(
                agent=ap_id,
                phase=phase,
                role=role,
                instruction=instruction,
                response=content,
                duration_ms=duration_ms,
            )
            for tc in tool_log:
                self.logger.tool_call(
                    tc["tool"],
                    tc["input"],
                    tc["output"],
                    tc["duration_ms"],
            )

        return content

    # ──────────────────────────────────────────────────────────────────────
    # 协商阶段
    # ──────────────────────────────────────────────────────────────────────

    def _phase_broadcast(self, ap_state: dict) -> None:
        print(f"\n{divider()}")
        print(section("第一阶段：广播自身状态"))
        if self.logger:
            self.logger.phase_start(1, "广播自身状态")

        for ap_id in AP_IDS:
            state_json = json.dumps(ap_state[ap_id], ensure_ascii=False, indent=2)
            instruction = (
                f"请广播你（{ap_id.upper()}）的当前状态。\n"
                f"你的实测数据如下（请用自然语言播报，不要只复制 JSON）：\n"
                f"{state_json}\n\n"
                "只播报你自己的数据，不要提及或分析其他 AP 的情况。"
            )
            self._speak_and_log(
                ap_id,
                instruction,
                phase=1,
                role="broadcast",
                speaker=ap_id.upper(),
            )

    def _phase_propose(
        self,
        proposer_id: str,
        ap_state: dict,
        strategy: str,
    ) -> None:
        print(f"\n{divider()}")
        print(section(f"第二阶段：{proposer_id.upper()} 发起提案 | 策略={strategy}"))
        if self.logger:
            self.logger.phase_start(2, f"{proposer_id.upper()} 发起提案 | {strategy}")

        strategy_hint = {
            "co_sr":   "Co-SR（降低 TX Power，减少 OBSS 干扰）",
            "co_edca": "Co-EDCA（调整 CWmin / CWmax / AIFSN，缓解信道拥塞）",
            "joint":   "联合（同时调整 TX Power 与 EDCA 参数）",
        }[strategy]

        state_summary = json.dumps(ap_state, ensure_ascii=False, indent=2)
        tools = _tools_for_propose(strategy)

        instruction = (
            f"协调者判断你（{proposer_id.upper()}）当前状况最差，请发起参数调整提案。\n\n"
            f"协商路径已判断为：{strategy_hint}\n\n"
            f"所有 AP 的完整状态数据：\n{state_summary}\n\n"
            "请先调用对应的计算工具获取推荐值，然后按 AGENTS.md 要求完整阐述提案：\n"
            "  · 当前网络面临什么问题，核心指标数据是什么\n"
            "  · 为什么走这条协商路径，而不是另一条\n"
            "  · 每个 AP 的参数最终定为多少，工具推荐了什么，你是否调整过，为什么\n"
            "  · 调整后预期改善什么，主要的权衡取舍是什么\n"
            "如需自检，可继续调用验算工具确认约束是否满足。\n"
            "提案末尾用 ```json 代码块附上参数摘要（供其他 AP 对照验算）。"
        )
        self._speak_and_log(
            proposer_id,
            instruction,
            phase=2,
            role="proposer",
            tools=tools,
            speaker=f"{proposer_id.upper()}（提案）",
        )

    def _phase_vote(
        self,
        proposer_id: str,
        strategy: str,
        round_num: int,
    ) -> bool:
        """投票阶段。返回 True 表示全票通过。"""
        print(f"\n{divider()}")
        print(section("第三阶段：投票验算"))
        if self.logger:
            self.logger.phase_start(3, f"投票验算（第{round_num}轮）")

        verify_hint = {
            "co_sr":   "验算降功率后己方 STA RSSI 是否仍 > -75 dBm，并检查 TX Power 是否在 [1, 23] dBm 范围内",
            "co_edca": "验算 CWmin ∈ [3, 1023]、CWmax ∈ [7, 1023]、AIFSN ∈ [1, 15] 且 CWmax > CWmin",
            "joint":   "验算 TX Power 和 EDCA 参数各自的合法范围，并确认 STA RSSI 安全下界",
        }[strategy]

        tools = _tools_for_vote(strategy)
        voter_ids = [ap for ap in AP_IDS if ap != proposer_id]
        agree_count = 0

        for voter_id in voter_ids:
            instruction = (
                f"请验算 {proposer_id.upper()} 的提案中针对你自己（{voter_id.upper()}）的参数调整建议。\n\n"
                "先从对话记录中提取提案参数 JSON，调用验算工具确认约束是否满足，"
                f"核心检查项：{verify_hint}。\n\n"
                "然后按 AGENTS.md 要求表态——说清楚验算结果、"
                "这次调整对你自己实际意味着什么（参数怎么变、业务有无影响），"
                "再给出明确结论：同意或不同意。"
            )
            content = self._speak_and_log(
                voter_id,
                instruction,
                phase=3,
                role="voter",
                tools=tools,
                speaker=f"{voter_id.upper()}（投票）",
            )

            agreed = self._agreed(content)
            if self.logger:
                self.logger.vote(voter_id, round_num, agreed, content)
            if agreed:
                agree_count += 1

        all_agreed = agree_count >= len(voter_ids)
        if self.logger:
            self.logger.round_result(round_num, all_agreed, agree_count, len(voter_ids))
        return all_agreed

    def _emit_final_decision(self, proposer_id: str) -> dict | None:
        """输出最终决策，返回解析出的 JSON dict（供 validator 使用）。"""
        print(f"\n{divider()}")
        print(section("输出最终决策"))
        if self.logger:
            self.logger.phase_start(4, "输出最终决策")

        instruction = (
            "所有 AP 已同意提案。\n"
            "请输出最终的 JSON 决策方案（严格遵守格式，JSON 内不得有注释），"
            "然后在下一行写【协商结束】。"
        )
        content = self._speak_and_log(
            proposer_id,
            instruction,
            phase=4,
            role="decision",
            speaker=f"{proposer_id.upper()}（最终决策）",
        )

        decision = _extract_json(content)
        if self.logger:
            self.logger.final_decision(decision, content)
        return decision

    # ──────────────────────────────────────────────────────────────────────
    # 公开入口
    # ──────────────────────────────────────────────────────────────────────

    def run(self, ap_state: dict) -> list[dict]:
        """
        执行完整协商流程。

        Returns:
            conversation_log — list of {"speaker": str, "content": str}
        """
        self._current_ap_states = ap_state

        strategy    = self._determine_strategy(ap_state)
        proposer_id = self._find_worst_ap(ap_state, strategy)

        print(f"\n{status_label('策略判断')} {strategy.upper()} | 提案方: {proposer_id.upper()}")
        if self.logger:
            self.logger.strategy_decided(strategy, proposer_id)

        # 阶段一：广播
        self._phase_broadcast(ap_state)

        # 阶段二：提案
        self._phase_propose(proposer_id, ap_state, strategy)

        # 阶段三：投票（最多 MAX_VOTE_ROUNDS 轮）
        outcome = "max_rounds_reached"
        for round_num in range(1, MAX_VOTE_ROUNDS + 1):
            print(f"\n{status_label(f'投票第 {round_num} 轮')}")
            all_agreed = self._phase_vote(proposer_id, strategy, round_num)

            if all_agreed:
                decision = self._emit_final_decision(proposer_id)

                # 确定性验证
                print(f"\n{divider()}")
                print(section("Validator 验算"))
                validation = validate_decision(ap_state, decision, strategy)
                print(f"  结果: {'通过' if validation['approved'] else '未通过'}")
                print(f"  摘要: {validation['summary']}")
                if self.logger:
                    self.logger.validation_result(validation)

                if validation["approved"]:
                    print(f"\n{divider()}")
                    print("协商成功完成。")
                    outcome = "success"
                    if self.logger:
                        self.logger.session_end(outcome, round_num)
                    return self.conversation_log

                # Validator 拒绝：注入物理约束错误，要求提案方修订，继续下一轮投票
                if round_num < MAX_VOTE_ROUNDS:
                    print(f"\n{status_label(f'Validator 未通过，要求 {proposer_id.upper()} 修订提案')}")
                    error_summary = "\n".join(
                        f"  - {e}" for e in validation["global_errors"][:5]
                    )
                    revise_instruction = (
                        f"Validator 对最终决策执行了物理约束验算，发现以下问题：\n"
                        f"{error_summary}\n\n"
                        "请根据上述错误修改提案，重新给出每个 AP 的具体参数（含数值），"
                        "确保满足 CCA、SINR、STA RSSI 等物理约束。\n"
                        "可调用验算工具自检修订后的参数。"
                    )
                    content = self._speak_and_log(
                        proposer_id, revise_instruction,
                        phase=3, role="revise",
                        tools=_tools_for_propose(strategy),
                        speaker=f"{proposer_id.upper()}（Validator修订）",
                    )
                continue  # 跳过下方"AP不同意"修订块，直接进入下一轮投票

            # all_agreed = False（有 AP 不同意）
            if round_num < MAX_VOTE_ROUNDS:
                print(f"\n{status_label(f'有 AP 不同意，请 {proposer_id.upper()} 修改提案')}")
                revise_instruction = (
                    "部分 AP 对提案表示不同意。\n"
                    "请根据他们的反馈修改提案，再次给出每个 AP 的具体参数调整建议（含数值）。\n"
                    "可调用验算工具自检修订后的参数。"
                )
                content = self._speak_and_log(
                    proposer_id, revise_instruction,
                    phase=3, role="revise",
                    tools=_tools_for_propose(strategy),
                    speaker=f"{proposer_id.upper()}（修订提案）",
                )

        print(f"\n{divider()}")
        print(f"达到最大投票轮数（{MAX_VOTE_ROUNDS}），协商未能收敛。")
        if self.logger:
            self.logger.session_end(outcome, MAX_VOTE_ROUNDS)
        return self.conversation_log

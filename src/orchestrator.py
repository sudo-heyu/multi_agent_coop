import json
import re
import time
from pathlib import Path

from .agent import APAgent
from .logger import SessionLogger
from .tools.edca import compute_all as edca_compute
from .tools.sr   import compute_all as sr_compute, compute_validation as sr_compute_validation
from .validator import validate_decision

AP_IDS = ["ap1", "ap2", "ap3"]
MAX_VOTE_ROUNDS = 3
DIVIDER = "=" * 60

# 策略触发阈值
SR_RSSI_TRIGGER_DBM = -70.0
EDCA_BUSY_TRIGGER   = 0.60
EDCA_RETRY_TRIGGER  = 0.15


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
        # 工具结果缓存：_phase_propose() 写入，_phase_vote() 读取
        self._last_sr_result:   dict | None = None
        self._last_edca_result: dict | None = None

    # ──────────────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────────────

    def _record(self, speaker: str, content: str) -> None:
        """追加到对话记录并打印到控制台。"""
        self.conversation_log.append({"speaker": speaker, "content": content})
        print(f"\n{DIVIDER}")
        print(f"### {speaker}")
        print(content)

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
        if "不同意" in content or "❌" in content:
            return False
        return "同意" in content or "✅" in content

    def _build_voter_check(self, voter_id: str, strategy: str) -> str:
        """
        构建工具预计算验证摘要，注入投票指令。

        避免 LLM 自行推导 RSSI delta 出错（如 Session1 的虚假拒绝）。
        数据来源：_phase_propose() 中缓存的工具结果（优先使用提案实际参数重算值）。
        """
        ok_mark = lambda b: "✅" if b else "❌"
        lines = ["【工具预计算验证结果（供参考，以提案实际数值为准）】"]

        if strategy in ("co_sr", "joint") and self._last_sr_result:
            v   = self._last_sr_result["validation"].get(voter_id, {})
            rec = self._last_sr_result["recommendations"].get(voter_id, {})
            lines.append(
                f"Co-SR：{rec.get('current_dbm')} dBm → {rec.get('recommended_dbm')} dBm，"
                f"预计 STA RSSI = {v.get('sta_rssi_dbm')} dBm"
                f"（安全下界 -75 dBm {ok_mark(v.get('sta_rssi_ok'))}），"
                f"CCA = {v.get('cca_max_dbm')} dBm"
                f"（阈值 -82 dBm {ok_mark(v.get('cca_ok'))}），"
                f"SINR = {v.get('sinr_db')} dB"
                f"（下界 15 dB {ok_mark(v.get('sinr_ok'))}）"
            )

        if strategy in ("co_edca", "joint") and self._last_edca_result:
            v = self._last_edca_result.get(voter_id, {})
            lines.append(
                f"Co-EDCA：推荐 CWmin={v.get('CWmin')} CWmax={v.get('CWmax')} AIFSN={v.get('AIFSN')}"
                f"（拥塞等级={v.get('congestion_level')}，"
                f"{'✅ 合规' if v.get('valid') else '❌ 不合规'}）"
            )

        lines.append("如提案参数与上述推荐值不同，请以提案实际数值重新验算后再表态。")
        return "\n".join(lines)

    def _speak_and_log(
        self,
        ap_id: str,
        instruction: str,
        phase: int,
        role: str,
    ) -> str:
        """调用 agent.speak()，记录耗时和日志，返回回复文本。"""
        t0 = time.time()
        content = self.agents[ap_id].speak(self.conversation_log, instruction)
        duration_ms = (time.time() - t0) * 1000
        if self.logger:
            self.logger.agent_speak(
                agent=ap_id,
                phase=phase,
                role=role,
                instruction=instruction,
                response=content,
                duration_ms=duration_ms,
            )
        return content

    # ──────────────────────────────────────────────────────────────────────
    # 协商阶段
    # ──────────────────────────────────────────────────────────────────────

    def _phase_broadcast(self, ap_state: dict) -> None:
        print(f"\n{DIVIDER}")
        print("【第一阶段：广播自身状态】")
        if self.logger:
            self.logger.phase_start(1, "广播自身状态")

        for ap_id in AP_IDS:
            state_json = json.dumps(ap_state[ap_id], ensure_ascii=False, indent=2)
            instruction = (
                f"第一阶段：请广播你（{ap_id.upper()}）的当前状态。\n"
                f"你的实测数据如下（请用自然语言播报，不要只复制 JSON）：\n"
                f"{state_json}\n\n"
                "只播报你自己的数据，不要提及或分析其他 AP 的情况。"
            )
            content = self._speak_and_log(ap_id, instruction, phase=1, role="broadcast")
            self._record(ap_id.upper(), content)

    def _phase_propose(
        self,
        proposer_id: str,
        ap_state: dict,
        strategy: str,
    ) -> None:
        print(f"\n{DIVIDER}")
        print(f"【第二阶段：{proposer_id.upper()} 发起提案 | 策略={strategy}】")
        if self.logger:
            self.logger.phase_start(2, f"{proposer_id.upper()} 发起提案 | {strategy}")

        state_summary = json.dumps(ap_state, ensure_ascii=False, indent=2)
        tool_section  = ""
        sr_result   = None
        edca_result = None

        if strategy in ("co_sr", "joint"):
            t0 = time.time()
            sr_result = sr_compute(ap_state)
            duration_ms = (time.time() - t0) * 1000

            print("[Co-SR 工具] " + "  ".join(
                f"{ap.upper()}:{sr_result['recommendations'][ap]['current_dbm']}"
                f"→{sr_result['recommendations'][ap]['recommended_dbm']}dBm"
                for ap in AP_IDS
            ))
            print("  干扰矩阵: " + "  ".join(
                f"{k}={v['rssi_dbm']}dBm({v['level']})"
                for k, v in sr_result["interference_matrix"].items()
            ))

            if self.logger:
                self.logger.tool_call("co_sr", ap_state, sr_result, duration_ms)

            tool_section += (
                f"\n【Co-SR 计算工具输出】（基准推荐，可在此基础上调整）：\n"
                f"{json.dumps(sr_result, ensure_ascii=False, indent=2)}\n"
            )

        if strategy in ("co_edca", "joint"):
            t0 = time.time()
            edca_result = edca_compute(ap_state)
            duration_ms = (time.time() - t0) * 1000

            print("[Co-EDCA 工具] " + "  ".join(
                f"{ap.upper()}={v['congestion_level']}"
                f"→CWmin={v['CWmin']},CWmax={v['CWmax']},AIFSN={v['AIFSN']}"
                for ap, v in edca_result.items()
            ))

            if self.logger:
                self.logger.tool_call("co_edca", ap_state, edca_result, duration_ms)

            tool_section += (
                f"\n【Co-EDCA 计算工具输出】（基准推荐，可在此基础上调整）：\n"
                f"{json.dumps(edca_result, ensure_ascii=False, indent=2)}\n"
            )

        # 将工具结果缓存，供 _phase_vote() 注入投票指令
        self._last_sr_result   = sr_result
        self._last_edca_result = edca_result

        strategy_hint = {
            "co_sr":   "Co-SR（降低 TX Power，减少 OBSS 干扰）",
            "co_edca": "Co-EDCA（调整 CWmin / CWmax / AIFSN，缓解信道拥塞）",
            "joint":   "联合（同时调整 TX Power 与 EDCA 参数）",
        }[strategy]

        instruction = (
            f"第二阶段：根据协调者判断，你（{proposer_id.upper()}）当前状况最差，"
            f"由你发起参数调整提案。\n\n"
            f"协商路径已判断为：{strategy_hint}\n\n"
            f"所有 AP 的完整状态数据：\n{state_summary}\n"
            f"{tool_section}\n"
            "请结合工具推荐值给出每个 AP 的最终参数建议（必须包含具体数值），"
            "并为每个 AP 附一句调整理由。\n"
            "最后，在提案末尾用 ```json 代码块附上本次提案的参数摘要，"
            "格式与最终决策 JSON 相同（供其他 AP 对照验算，不是最终决策）。"
        )
        content = self._speak_and_log(
            proposer_id, instruction, phase=2, role="proposer"
        )
        self._record(f"{proposer_id.upper()}（提案）", content)

        # Fix 3：提案包含 JSON 时，基于提案实际功率重算 sr 验证数据
        # 使验证数据与提案对齐，而非仅与工具推荐值对齐
        if strategy in ("co_sr", "joint") and self._last_sr_result is not None:
            proposal_json = _extract_json(content)
            if proposal_json:
                proposed_powers = {
                    ap_id: entry.get("tx_power_dbm")
                    for k, entry in proposal_json.items()
                    if isinstance(entry, dict)
                    for ap_id in [k.lower()]
                    if ap_id in AP_IDS and entry.get("tx_power_dbm") is not None
                }
                if len(proposed_powers) == len(AP_IDS):
                    self._last_sr_result = dict(self._last_sr_result)
                    self._last_sr_result["validation"] = sr_compute_validation(
                        ap_state, proposed_powers
                    )
                    self._last_sr_result["recommendations"] = {
                        ap_id: {
                            "current_dbm":     ap_state[ap_id].get("tx_power_dbm"),
                            "recommended_dbm": pwr,
                            "delta_db":        round(pwr - ap_state[ap_id].get("tx_power_dbm", 20.0), 1),
                        }
                        for ap_id, pwr in proposed_powers.items()
                    }
                    print(f"[提案 JSON 提取成功] 基于提案实际功率重算验证: {proposed_powers}")

    def _phase_vote(
        self,
        proposer_id: str,
        strategy: str,
        round_num: int,
    ) -> bool:
        """投票阶段。返回 True 表示全票通过。"""
        print(f"\n{DIVIDER}")
        print("【第三阶段：投票验算】")
        if self.logger:
            self.logger.phase_start(3, f"投票验算（第{round_num}轮）")

        verify_hint = {
            "co_sr":   "验算降功率后己方 STA RSSI 是否仍 > -75 dBm，并检查 TX Power 是否在 [1, 23] dBm 范围内",
            "co_edca": "验算 CWmin ∈ [3, 1023]、CWmax ∈ [7, 1023]、AIFSN ∈ [1, 15] 且 CWmax > CWmin",
            "joint":   "验算 TX Power 和 EDCA 参数各自的合法范围，并确认 STA RSSI 安全下界",
        }[strategy]

        voter_ids = [ap for ap in AP_IDS if ap != proposer_id]
        agree_count = 0

        for voter_id in voter_ids:
            tool_check = self._build_voter_check(voter_id, strategy)
            instruction = (
                f"第三阶段：请验算 {proposer_id.upper()} 的提案。\n"
                f"找出提案中针对你自己（{voter_id.upper()}）的参数调整建议，"
                f"重点检查：{verify_hint}。\n\n"
                f"{tool_check}\n\n"
                "然后给出明确表态：\n"
                "- 同意：写【同意】并加 ✅\n"
                "- 不同意：写【不同意】并加 ❌，然后给出具体修改建议"
            )
            content = self._speak_and_log(
                voter_id, instruction, phase=3, role="voter"
            )
            self._record(f"{voter_id.upper()}（投票）", content)

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
        print(f"\n{DIVIDER}")
        print("【输出最终决策】")
        if self.logger:
            self.logger.phase_start(4, "输出最终决策")

        instruction = (
            "所有 AP 已同意提案。\n"
            "请输出最终的 JSON 决策方案（严格遵守格式，JSON 内不得有注释），"
            "然后在下一行写【协商结束】。"
        )
        content = self._speak_and_log(
            proposer_id, instruction, phase=4, role="decision"
        )
        self._record(f"{proposer_id.upper()}（最终决策）", content)

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
        strategy    = self._determine_strategy(ap_state)
        proposer_id = self._find_worst_ap(ap_state, strategy)

        print(f"\n[策略判断] {strategy.upper()} | 提案方: {proposer_id.upper()}")
        if self.logger:
            self.logger.strategy_decided(strategy, proposer_id)

        # 阶段一：广播
        self._phase_broadcast(ap_state)

        # 阶段二：提案
        self._phase_propose(proposer_id, ap_state, strategy)

        # 阶段三：投票（最多 MAX_VOTE_ROUNDS 轮）
        outcome = "max_rounds_reached"
        for round_num in range(1, MAX_VOTE_ROUNDS + 1):
            print(f"\n[投票第 {round_num} 轮]")
            all_agreed = self._phase_vote(proposer_id, strategy, round_num)

            if all_agreed:
                decision = self._emit_final_decision(proposer_id)

                # 确定性验证
                print(f"\n{DIVIDER}")
                print("【Validator 验算】")
                validation = validate_decision(ap_state, decision, strategy)
                print(f"  结果: {'✅ 通过' if validation['approved'] else '❌ 未通过'}")
                print(f"  摘要: {validation['summary']}")
                if self.logger:
                    self.logger.validation_result(validation)

                if validation["approved"]:
                    print(f"\n{DIVIDER}")
                    print("协商成功完成。")
                    outcome = "success"
                    if self.logger:
                        self.logger.session_end(outcome, round_num)
                    return self.conversation_log

                # Validator 拒绝：注入物理约束错误，要求提案方修订，继续下一轮投票
                if round_num < MAX_VOTE_ROUNDS:
                    print(f"\n[Validator 未通过，要求 {proposer_id.upper()} 修订提案]")
                    error_summary = "\n".join(
                        f"  - {e}" for e in validation["global_errors"][:5]
                    )
                    revise_instruction = (
                        f"Validator 对最终决策执行了物理约束验算，发现以下问题：\n"
                        f"{error_summary}\n\n"
                        "请根据上述错误修改提案，重新给出每个 AP 的具体参数（含数值），"
                        "确保满足 CCA、SINR、STA RSSI 等物理约束。"
                    )
                    content = self._speak_and_log(
                        proposer_id, revise_instruction, phase=3, role="revise"
                    )
                    self._record(f"{proposer_id.upper()}（Validator修订）", content)
                continue  # 跳过下方"AP不同意"修订块，直接进入下一轮投票

            # all_agreed = False（有 AP 不同意）
            if round_num < MAX_VOTE_ROUNDS:
                print(f"\n[有 AP 不同意，请 {proposer_id.upper()} 修改提案]")
                revise_instruction = (
                    "部分 AP 对提案表示不同意。\n"
                    "请根据他们的反馈修改提案，再次给出每个 AP 的具体参数调整建议（含数值）。"
                )
                content = self._speak_and_log(
                    proposer_id, revise_instruction, phase=3, role="revise"
                )
                self._record(f"{proposer_id.upper()}（修订提案）", content)

        print(f"\n{DIVIDER}")
        print(f"达到最大投票轮数（{MAX_VOTE_ROUNDS}），协商未能收敛。")
        if self.logger:
            self.logger.session_end(outcome, MAX_VOTE_ROUNDS)
        return self.conversation_log

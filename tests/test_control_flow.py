import copy
import unittest
from pathlib import Path

from run import MOCK_SCENES, _is_local_server
from src.agent import APAgent
from src.console_style import strip_ansi
from src.orchestrator import (
    NegotiationOrchestrator,
    _extract_json,
    _extract_proposal,
    _format_tool_console,
    _tools_for_vote,
)
from src.tools.registry import TOOL_DEFINITIONS, make_executor
from src.validator import validate_decision


class FakeToolLoopAgent(APAgent):
    def __init__(self):
        self.agent_id = "ap1"
        self.name = "AP1"
        self.model = "fake"
        self.system_prompt = "fake"
        self.requests = []

    def _request_chat(self, messages, tools=None):
        self.requests.append(copy.deepcopy(messages))
        if len(self.requests) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_analyze",
                        "function": {
                            "name": "analyze_sr_interference",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        if len(self.requests) == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_evaluate",
                        "function": {
                            "name": "evaluate_sr_candidate",
                            "arguments": {"proposed_powers": {"ap1": 6.0}},
                        },
                    }
                ],
            }
        return {"content": "最终提案", "tool_calls": []}

    def _stream_chat(self, messages, tools=None):
        yield "最终提案"

    def speak_stream(
        self,
        conversation_log,
        instruction,
        tools=None,
        tool_executor=None,
        tool_log=None,
        tool_callback=None,
    ):
        messages = self._build_messages(conversation_log, instruction)
        for _ in range(3):
            msg = self._request_chat(messages, tools=tools)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                yield from self._stream_chat(messages)
                return

            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    import json
                    raw_args = json.loads(raw_args)
                result_dict, dur_ms = tool_executor(tool_name, raw_args)
                tool_message = {
                    "role": "tool",
                    "content": "{}",
                    "name": tool_name,
                }
                if tc.get("id"):
                    tool_message["tool_call_id"] = tc["id"]
                messages.append(tool_message)
                if tool_log is not None:
                    tool_log.append({
                        "tool": tool_name,
                        "input": raw_args,
                        "output": result_dict,
                        "duration_ms": dur_ms,
                    })


class FakeUnbackedToolClaimAgent:
    def __init__(self):
        self.calls = []

    def speak_stream(
        self,
        conversation_log,
        instruction,
        tools=None,
        tool_executor=None,
        tool_log=None,
        tool_callback=None,
    ):
        self.calls.append(instruction)
        if len(self.calls) == 1:
            yield "我调用 evaluate_sr_candidate 工具。工具返回结果显示 valid 为 true。"
        else:
            yield "我没有调用工具，直接根据提案给出判断。"


class ControlFlowTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = NegotiationOrchestrator(Path("agents"), logger=None)

    def test_existing_mock_strategy_routes_are_stable(self):
        expected = {
            "sr": "co_sr",
            "edca": "co_edca",
            "joint": "joint",
        }
        for scene, strategy in expected.items():
            with self.subTest(scene=scene):
                self.assertEqual(
                    self.orchestrator._determine_strategy(MOCK_SCENES[scene]),
                    strategy,
                )

    def test_noop_when_no_thresholds_are_triggered(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        for ap_state in state.values():
            ap_state["channel_busy_ratio"] = 0.20
            ap_state["tx_retries_ratio"] = 0.02
            ap_state["neighbor_rssi_dbm"] = {
                neighbor: -90.0
                for neighbor in ap_state["neighbor_rssi_dbm"]
            }

        self.assertEqual(self.orchestrator._determine_strategy(state), "noop")

    def test_extract_nested_json_from_markdown_block(self):
        content = """
        说明文字
        ```json
        {
          "ap1": {"tx_power_dbm": 6.0},
          "ap2": {"tx_power_dbm": 6.0},
          "ap3": {"tx_power_dbm": 7.0}
        }
        ```
        """
        self.assertEqual(_extract_json(content)["ap3"]["tx_power_dbm"], 7.0)
        self.assertIsNotNone(_extract_proposal(content))

    def test_structured_vote_takes_precedence(self):
        self.assertEqual(self.orchestrator._vote_result('```json\n{"agreed": true, "reason": "OK"}\n```'), "agree")
        self.assertEqual(self.orchestrator._vote_result('```json\n{"agreed": false, "reason": "不同意"}\n```'), "reject")
        self.assertEqual(self.orchestrator._vote_result('```json\n{"agreed": "abstain", "reason": "弃权"}\n```'), "abstain")

    def test_local_server_detection(self):
        self.assertTrue(_is_local_server("http://localhost:5001"))
        self.assertTrue(_is_local_server("http://127.0.0.1:5001"))
        self.assertFalse(_is_local_server("http://192.168.1.100:5001"))

    def test_agent_tool_loop_allows_multiple_rounds_before_final_text(self):
        agent = FakeToolLoopAgent()
        tool_log = []
        calls = []

        def executor(tool_name, args):
            calls.append((tool_name, args))
            return {"ok": True, "tool": tool_name}, 1.0

        content = "".join(
            agent.speak_stream(
                [],
                "先计算，再验算，最后输出。",
                tools=[{"type": "function", "function": {"name": "fake"}}],
                tool_executor=executor,
                tool_log=tool_log,
            )
        )

        self.assertEqual(content, "最终提案")
        self.assertEqual(
            [name for name, _ in calls],
            ["analyze_sr_interference", "evaluate_sr_candidate"],
        )
        self.assertEqual(len(tool_log), 2)
        second_request_messages = agent.requests[1]
        self.assertEqual(second_request_messages[-1]["role"], "tool")
        self.assertEqual(second_request_messages[-1]["tool_call_id"], "call_analyze")
        self.assertEqual(agent.requests[2][-1]["tool_call_id"], "call_evaluate")

    def test_vote_tools_fetch_latest_state_first(self):
        for strategy in ("co_sr", "co_edca", "joint"):
            with self.subTest(strategy=strategy):
                self.assertEqual(
                    _tools_for_vote(strategy)[0]["function"]["name"],
                    "get_latest_ap_states",
                )

        # 投票者持有 SR + EDCA 两个验算工具（不含提案专用的分析/排序工具）
        vote_names = [tool["function"]["name"] for tool in _tools_for_vote("co_sr")]
        self.assertIn("evaluate_sr_candidate", vote_names)
        self.assertIn("validate_edca_proposal", vote_names)
        self.assertNotIn("analyze_sr_interference", vote_names)

        # 提案方自主选路，因此提案阶段持有全部工具
        propose_names = [tool["function"]["name"] for tool in TOOL_DEFINITIONS]
        self.assertEqual(propose_names[0], "get_latest_ap_states")
        for expected in (
            "analyze_sr_interference",
            "compute_sr_feasible_ranges",
            "evaluate_sr_candidate",
            "rank_sr_candidates",
        ):
            self.assertIn(expected, propose_names)

    def test_latest_state_tool_refreshes_executor_state(self):
        initial = copy.deepcopy(MOCK_SCENES["edca"])
        latest = copy.deepcopy(initial)
        latest["ap1"]["channel_busy_ratio"] = 0.20
        latest["ap1"]["tx_retries_ratio"] = 0.02
        updates = []

        executor = make_executor(
            initial,
            state_getter=lambda: latest,
            state_setter=updates.append,
        )

        state_result, _ = executor("get_latest_ap_states", {})
        edca_result, _ = executor(
            "validate_edca_proposal",
            {"proposed_edca": {"ap1": {"CWmin": 7, "CWmax": 15, "AIFSN": 2}}},
        )

        self.assertTrue(state_result["ok"])
        self.assertEqual(updates[-1], latest)
        self.assertEqual(
            edca_result["effectiveness"]["per_ap"]["ap1"]["recommended_level"],
            "low",
        )

    def test_decomposed_sr_tools_support_candidate_selection(self):
        executor = make_executor(copy.deepcopy(MOCK_SCENES["sr"]))

        analysis, _ = executor("analyze_sr_interference", {})
        ranges, _ = executor("compute_sr_feasible_ranges", {})
        ranking, _ = executor(
            "rank_sr_candidates",
            {
                "candidates": {
                    "too_high": {"ap1": 14.0, "ap2": 14.0, "ap3": 14.0},
                    "fractional": {"ap1": 6.59, "ap2": 6.69, "ap3": 7.39},
                    "balanced": {"ap1": 6.0, "ap2": 6.0, "ap3": 7.0},
                },
                "objective": "balanced",
            },
        )
        evaluation, _ = executor(
            "evaluate_sr_candidate",
            {"proposed_powers": ranking["best"]["proposed_powers"]},
        )

        self.assertTrue(analysis["co_sr_triggered"])
        self.assertIn("ap1", ranges["ranges"])
        # 功率调整量必须为整数 dB → 候选提示取整为整数 dBm
        self.assertTrue(ranges["integer_power_required"])
        self.assertEqual(
            ranges["candidate_hints"]["minimal_necessary_drop"],
            {"ap1": 6.0, "ap2": 6.0, "ap3": 7.0},
        )
        # 小数功率方案因调整量非整数 dB 被判非法，整数方案胜出
        self.assertEqual(ranking["best"]["name"], "balanced")
        fractional = next(
            c for c in ranking["ranked_candidates"] if c["name"] == "fractional"
        )
        self.assertFalse(fractional["valid"])
        self.assertTrue(evaluation["valid"], evaluation["errors"])

    def test_tool_console_output_is_human_readable_summary(self):
        state_result = {
            "ok": True,
            "source": "current_snapshot",
            "ap_states": copy.deepcopy(MOCK_SCENES["sr"]),
        }
        raw = _format_tool_console("get_latest_ap_states", {}, state_result, 1.2)
        state_text = strip_ansi(raw)
        self.assertIn("[工具] get_latest_ap_states", state_text)
        self.assertNotIn("状态源:", state_text)
        self.assertIn("ap1: TX=20dBm", state_text)
        self.assertIn("[ap2:-74 ap3:-88]", state_text)
        self.assertNotIn('"ap_states"', state_text)

        validation = {
            "valid": True,
            "score": {"total_power_drop_db": 13.41, "max_single_ap_drop_db": 13.41},
            "per_ap": {
                "ap1": {
                    "valid": True,
                    "cca_max_dbm": -82.01,
                    "cca_ok": True,
                    "sinr_db": 22.69,
                    "sinr_ok": True,
                    "sta_rssi_dbm": -58.01,
                    "sta_rssi_ok": True,
                    "errors": [],
                }
            }
        }
        raw2 = _format_tool_console(
            "evaluate_sr_candidate",
            {"proposed_powers": {"ap1": 6.59}},
            validation,
            0.8,
        )
        validation_text = strip_ansi(raw2)
        self.assertIn("evaluate_sr_candidate ap1=6.59dBm", validation_text)
        self.assertIn("全部OK", validation_text)
        self.assertIn("总降功=13.41dB", validation_text)
        self.assertNotIn("CCA=", validation_text)

    def test_orchestrator_retries_textual_tool_claim_without_tool_call(self):
        agent = FakeUnbackedToolClaimAgent()
        self.orchestrator.agents["ap1"] = agent
        self.orchestrator._current_ap_states = copy.deepcopy(MOCK_SCENES["sr"])

        content = self.orchestrator._speak_and_log(
            "ap1",
            "请验算提案。",
            phase=3,
            role="voter",
            tools=[{
                "type": "function",
                "function": {"name": "evaluate_sr_candidate"},
            }],
            speaker="AP1",
        )

        self.assertEqual(content, "我没有调用工具，直接根据提案给出判断。")
        self.assertEqual(self.orchestrator.conversation_log[-1]["content"], content)
        self.assertEqual(len(agent.calls), 2)
        self.assertIn("系统没有收到真实 tool_call", agent.calls[1])

    def test_validator_approves_observed_final_state(self):
        initial = copy.deepcopy(MOCK_SCENES["sr"])
        observed = copy.deepcopy(initial)
        decision = {}
        for ap_id, state in observed.items():
            state["tx_power_dbm"] = 6.0
            state["channel_busy_ratio"] = 0.30
            state["tx_retries_ratio"] = 0.04
            state["neighbor_rssi_dbm"] = {
                neighbor: -82.0
                for neighbor in state["neighbor_rssi_dbm"]
            }
            state["packet_loss_pct"] = 0.1
            state["latency_ms"] = 80.0
            decision[ap_id] = {"tx_power_dbm": 6.0}

        result = validate_decision(
            initial,
            decision,
            "co_sr",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertTrue(result["approved"], result["global_errors"])

    def test_validator_rejects_unapplied_or_bad_observed_state(self):
        initial = copy.deepcopy(MOCK_SCENES["sr"])
        observed = copy.deepcopy(initial)
        observed["ap1"]["tx_power_dbm"] = 20.0
        observed["ap1"]["packet_loss_pct"] = 3.0
        decision = {
            "ap1": {"tx_power_dbm": 6.0},
            "ap2": {"tx_power_dbm": 6.0},
            "ap3": {"tx_power_dbm": 8.0},
        }

        result = validate_decision(
            initial,
            decision,
            "co_sr",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertFalse(result["approved"])
        self.assertTrue(any("未生效" in e for e in result["global_errors"]))
        self.assertTrue(any("Packet Loss" in e for e in result["global_errors"]))


if __name__ == "__main__":
    unittest.main()

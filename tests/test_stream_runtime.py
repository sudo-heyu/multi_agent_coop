import copy
import json
import unittest
from unittest.mock import patch

from tests.mock_scenes import MOCK_SCENES
from openclaw.mcp import orchestration as orch
from openclaw.mcp.stream_runtime import PPIOStreamRuntime


class StreamRuntimeTests(unittest.TestCase):
    def test_structured_relay_accepts_injected_agent_driver(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        for ap_state in state.values():
            ap_state["traffic_priority"] = "medium"
            ap_state["neighbor_rssi_dbm"] = {
                ap: -90.0 for ap in ap_state["neighbor_rssi_dbm"]
            }
        calls = []

        def fake_driver(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            calls.append(ap_id)
            return f"{ap_id.upper()} direct runtime broadcast"

        with patch.object(orch, "get_all_states", return_value=state):
            result = orch.structured_relay(agent_driver=fake_driver)

        self.assertEqual(result["outcome"], "noop")
        self.assertCountEqual(calls, ["ap1", "ap2", "ap3"])

    def test_ppio_stream_runtime_executes_streamed_tool_calls(self):
        runtime = PPIOStreamRuntime(
            model="test-model",
            base_url="http://localhost:9999/openai/v1",
            api_key="test-key",
            max_tool_rounds=2,
        )
        chunks = []

        def event(obj):
            return "data: " + json.dumps(obj, ensure_ascii=False)

        first = _FakeResponse([
            event({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "validate_edca_proposal",
                                "arguments": "",
                            },
                        }]
                    }
                }]
            }),
            event({
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {
                                "arguments": json.dumps({
                                    "proposed_edca": {
                                        "ap1": {"CWmin": 7, "CWmax": 15, "AIFSN": 2}
                                    }
                                }, ensure_ascii=False)
                            },
                        }]
                    }
                }]
            }),
            "data: [DONE]",
        ])
        second = _FakeResponse([
            event({"choices": [{"delta": {"content": "验算完成。"}}]}),
            "data: [DONE]",
        ])

        captured = []

        def fake_call_tool(name, args):
            captured.append((name, args))
            return {"valid": True}, 3.2

        with patch("openclaw.mcp.stream_runtime.requests.Session.post",
                   side_effect=[first, second]), \
             patch("openclaw.mcp.stream_runtime.direct_tools.call_tool",
                   side_effect=fake_call_tool), \
             patch.object(orch, "_log_mcp_tool"):
            reply = runtime._chat_loop(
                "ap1",
                [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                chunks.append,
            )

        self.assertEqual(reply, "验算完成。")
        self.assertEqual(chunks, ["验算完成。"])
        self.assertEqual(captured[0][0], "validate_edca_proposal")
        self.assertEqual(captured[0][1]["proposed_edca"]["ap1"]["CWmin"], 7)


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


if __name__ == "__main__":
    unittest.main()


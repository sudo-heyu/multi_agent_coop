import json
import unittest

from src import agent as agent_mod
from src.agent import APAgent, PPIO_ALIAS


class FakeStreamResponse:
    def __init__(self, events):
        self.events = events
        self.ok = True
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        for event in self.events:
            if event == "[DONE]":
                yield b"data: [DONE]"
            else:
                yield ("data: " + json.dumps(event)).encode("utf-8")


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.requests.append({
            "url": url,
            "headers": headers,
            "json": json,
            "timeout": timeout,
            "stream": stream,
        })
        if not self.responses:
            raise AssertionError("unexpected extra PPIO request")
        return self.responses.pop(0)


def make_ppio_agent():
    agent = APAgent.__new__(APAgent)
    agent.agent_id = "ap1"
    agent.name = "AP1"
    agent.model = PPIO_ALIAS
    agent.system_prompt = "fake"
    agent._ppio_headers = {"Authorization": "Bearer test"}
    return agent


class PPIOStreamingTests(unittest.TestCase):
    def test_ppio_tool_loop_streams_final_content_chunks(self):
        first = FakeStreamResponse([
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_latest_ap_states", "arguments": "{\""},
            }]}}]},
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": "ok\": true}"},
            }]}}]},
            "[DONE]",
        ])
        second = FakeStreamResponse([
            {"choices": [{"delta": {"content": "最"}}]},
            {"choices": [{"delta": {"content": "终"}}]},
            {"choices": [{"delta": {"content": "回复"}}]},
            "[DONE]",
        ])

        original_http = agent_mod._HTTP
        agent_mod._HTTP = FakeHTTP([first, second])
        try:
            agent = make_ppio_agent()
            tool_log = []
            calls = []

            def executor(tool_name, args):
                calls.append((tool_name, args))
                return {"ok": True}, 3.0

            chunks = list(agent._speak_stream_ppio(
                messages=[{"role": "user", "content": "test"}],
                tools=[{"type": "function", "function": {"name": "get_latest_ap_states"}}],
                tool_executor=executor,
                tool_log=tool_log,
                tool_callback=None,
            ))
        finally:
            agent_mod._HTTP = original_http

        self.assertEqual(chunks, ["最", "终", "回复"])
        self.assertEqual(calls, [("get_latest_ap_states", {"ok": True})])
        self.assertEqual(tool_log[0]["tool"], "get_latest_ap_states")


if __name__ == "__main__":
    unittest.main()

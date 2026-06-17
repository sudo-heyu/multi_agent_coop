import json
import unittest

from dashboard import app as dashboard_app


def drain_live_queue():
    # 扇出模型：重置会话 backlog 与订阅者集合，隔离每个用例。
    with dashboard_app._backlog_lock:
        dashboard_app._backlog.clear()
        with dashboard_app._subscribers_lock:
            dashboard_app._subscribers.clear()


class DashboardStreamingTests(unittest.TestCase):
    def setUp(self):
        drain_live_queue()
        dashboard_app._client_ready.clear()
        self.client = dashboard_app.app.test_client()

    def test_live_sse_yields_chunk_events_before_session_end(self):
        dashboard_app.push_event({"event": "agent_speak_start", "agent": "ap1"})
        dashboard_app.push_event({"event": "agent_speak_chunk", "agent": "ap1", "text": "A"})
        dashboard_app.push_event({"event": "agent_speak", "agent": "ap1", "response": "ABC"})
        dashboard_app.push_event({"event": "session_end", "outcome": "success", "total_rounds": 1})

        resp = self.client.get("/events?live=1", buffered=False)
        events = []
        for raw in resp.response:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if text.startswith("data: "):
                events.append(json.loads(text[6:].strip()))
            if events and events[-1].get("event") == "session_end":
                break

        self.assertEqual([e["event"] for e in events[:3]], [
            "agent_speak_start",
            "agent_speak_chunk",
            "agent_speak",
        ])
        self.assertEqual(events[1]["text"], "A")

    def test_dashboard_html_uses_turn_based_streaming_without_end_flush(self):
        html = dashboard_app._HTML

        self.assertIn("function _startTurn", html)
        self.assertIn("function _appendTurnText", html)
        self.assertIn("function _completeTurn", html)
        self.assertIn("turn.received === 0", html)
        self.assertNotIn("_typeFlushAll", html)
        self.assertNotIn("_streamEl", html)
        self.assertNotIn("_curSeg", html)

    def test_dashboard_html_charts_mac_params_on_seconds_axis(self):
        html = dashboard_app._HTML

        for text in ["Txpower (dBm)", "CWmin", "CWmax", "AIFSN"]:
            self.assertIn(text, html)
        for field in ["tx_power_dbm", "cwmin", "cwmax", "aifsn"]:
            self.assertIn(field, html)
        self.assertIn("const DEFAULT_TIME_WINDOW_S = 40", html)
        self.assertIn('type: "linear"', html)
        self.assertIn('text: "时间 (s)"', html)
        self.assertIn("timelineMaxSeconds", html)
        self.assertNotIn("chartjs-adapter-date-fns", html)


if __name__ == "__main__":
    unittest.main()

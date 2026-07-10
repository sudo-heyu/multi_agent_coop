import unittest

from openclaw.mcp import orchestration


class BroadcastFallbackTests(unittest.TestCase):
    def test_empty_json_broadcast_uses_state_fallback(self):
        orchestration._SESSION.ap_state = {
            "ap2": {
                "tx_power_dbm": 10,
                "CWmin": 7,
                "CWmax": 15,
                "AIFSN": 2,
                "vi_cwmin": 7,
                "vi_cwmax": 15,
                "vi_aifsn": 2,
                "neighbor_rssi_dbm": {"ap1": -81.0},
                "sta_rssi_dbm": -45.7,
                "traffic_priority": "high",
                "service_name": "live_stream",
                "throughput_mbps_user": 4.0,
                "latency_ms": 9.0,
                "jitter_ms": 4.0,
                "packet_loss_pct": 0.0,
                "sla_status": "satisfied",
            }
        }

        self.assertTrue(orchestration._invalid_broadcast_reply("{}"))
        self.assertTrue(orchestration._invalid_broadcast_reply(
            '{"name": "get_latest_ap_states", "arguments": {}}'
        ))
        text = orchestration._broadcast_fallback("ap2")

        self.assertIn("我是 AP2", text)
        self.assertIn("7/15/2", text)
        self.assertIn("live_stream", text)
        self.assertIn("satisfied", text)


if __name__ == "__main__":
    unittest.main()

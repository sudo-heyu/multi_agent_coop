import copy
import unittest

from state_server import server


BASE_PAYLOAD = {
    "ap_id": "ap1",
    "timestamp": "2026-06-03T00:00:00+00:00",
    "tx_power_dbm": 15.0,
    "cwmin": 4,
    "cwmax": 10,
    "aifsn": 3,
    "channel_busy_ratio": 0.4,
    "tx_retries_ratio": 0.1,
    "neighbor_rssi_dbm": {"ap2": -57.0, "ap3": -56.0},
    "sta_rssi_dbm": -71.0,
    "noise_floor_dbm": -83.0,
    "throughput_mbps": 45.0,
    "latency_ms": 7.5,
    "packet_loss_pct": 0.0,
}


class StateServerSourcePolicyTest(unittest.TestCase):
    def setUp(self):
        server.ALLOW_MOCK_SOURCE = False
        server._store.clear()
        for rows in server._history.values():
            rows.clear()
        self.client = server.app.test_client()

    def test_default_server_rejects_generated_sources(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "mock"

        resp = self.client.post("/state", json=payload)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("generated data source", resp.get_json()["error"])
        self.assertNotIn("ap1", server._store)

    def test_default_server_accepts_ap_source(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ap"

        resp = self.client.post("/state", json=payload)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(server._store["ap1"]["data"]["source"], "ap")

    def test_history_includes_mac_parameters(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ap"

        resp = self.client.post("/state", json=payload)
        history = self.client.get("/history").get_json()

        self.assertEqual(resp.status_code, 200)
        row = history["ap1"][0]
        self.assertEqual(row["tx_power_dbm"], 15.0)
        self.assertEqual(row["cwmin"], 4)
        self.assertEqual(row["cwmax"], 10)
        self.assertEqual(row["aifsn"], 3)

    def test_index_html_charts_mac_params_on_seconds_axis(self):
        html = server._INDEX_HTML

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

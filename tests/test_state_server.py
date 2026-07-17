import copy
import json
import tempfile
import unittest

from state_server import server


BASE_PAYLOAD = {
    "ap_id": "ap1",
    "timestamp": "2026-06-03T00:00:00+00:00",
    "tx_power_dbm": 15.0,
    "cwmin": 4,
    "cwmax": 10,
    "aifsn": 3,
    "Data_rate_to_bandwidth_ratio": 0.4,
    "tx_retries_ratio": 0.1,
    "neighbor_rssi_dbm": {"ap2": -57.0, "ap3": -56.0},
    "sta_rssi_dbm": -71.0,
    "noise_floor_dbm": -83.0,
    "throughput_mbps_iperf": 45.0,
    "throughput_mbps_user": 27.0,
    "ac_iperf": "BK",
    "ac_user": "BE",
    "latency_ms": 7.5,
    "jitter_ms": 1.2,
    "packet_loss_pct": 0.0,
}


class StateServerSourcePolicyTest(unittest.TestCase):
    def setUp(self):
        server._store.clear()
        for rows in server._history.values():
            rows.clear()
        if server._trace_fh is not None:
            server._trace_fh.close()
        server._trace_fh = None
        server._trace_path = None
        server._trace_session_id = None
        self.client = server.app.test_client()

    def test_default_server_rejects_generated_sources(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "mock"

        resp = self.client.post("/state", json=payload)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("not accepted", resp.get_json()["error"])
        self.assertNotIn("ap1", server._store)

    def test_default_server_accepts_ap_source(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ap"

        resp = self.client.post("/state", json=payload)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(server._store["ap1"]["data"]["source"], "ap")
        self.assertEqual(
            server._store["ap1"]["data"]["business_type"],
            server.DEFAULT_BUSINESS_TYPE,
        )

    def test_default_server_accepts_ns3_source(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ns3"

        resp = self.client.post("/state", json=payload)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(server._store["ap1"]["data"]["source"], "ns3")

    def test_state_accepts_explicit_business_type(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ap"
        payload["business_type"] = "直播"

        resp = self.client.post("/state", json=payload)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(server._store["ap1"]["data"]["business_type"], "直播")

    def test_history_includes_mac_parameters(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ap"
        payload["business_type"] = "直播"

        resp = self.client.post("/state", json=payload)
        history = self.client.get("/history").get_json()

        self.assertEqual(resp.status_code, 200)
        row = history["ap1"][0]
        self.assertEqual(row["business_type"], "直播")
        self.assertEqual(row["tx_power_dbm"], 15.0)
        self.assertEqual(row["cwmin"], 4)
        self.assertEqual(row["cwmax"], 10)
        self.assertEqual(row["aifsn"], 3)
        self.assertEqual(row["jitter_ms"], 1.2)

    def test_state_accepts_sta_feedback_fields(self):
        payload = copy.deepcopy(BASE_PAYLOAD)
        payload["source"] = "ns3"
        payload["stas"] = [{
            "sta_id": "sta1",
            "associated_ap": "ap1",
            "flow_type": "video_call",
            "sla": {"max_latency_ms": 20},
            "measurements": {"latency_ms": 7.5, "jitter_ms": 1.2},
            "sla_status": "satisfied",
        }]
        payload["sta_feedback_summary"] = {
            "ap_id": "ap1",
            "sta_count": 1,
            "status": "satisfied",
            "violations": [],
        }
        payload["sla_violations"] = []

        resp = self.client.post("/state", json=payload)
        state = self.client.get("/state/ap1").get_json()["data"]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(state["stas"][0]["sta_id"], "sta1")
        self.assertEqual(state["sta_feedback_summary"]["status"], "satisfied")

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

    def test_trace_records_state_posts_to_state_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = self.client.post(
                "/trace/start",
                json={"session_id": "testsession", "dir": tmp},
            ).get_json()
            payload = copy.deepcopy(BASE_PAYLOAD)
            payload["source"] = "ap"

            self.client.post("/state", json=payload)
            stop = self.client.post("/trace/stop").get_json()

            self.assertEqual(start["path"], stop["path"])
            with open(stop["path"], encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(rows[0]["event"], "trace_start")
            self.assertTrue(any(row["event"] == "state_post" for row in rows))
            self.assertEqual(rows[-1]["event"], "trace_stop")


if __name__ == "__main__":
    unittest.main()

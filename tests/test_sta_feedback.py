import copy
import unittest

from src.profile import agent_view, apply_profile
from src.sta_feedback import evaluate_sta_qoe, summarize_sta_feedback
from src.validator import validate_decision


BASE_STATE = {
    "ap1": {
        "service_name": "video_call",
        "business_type": "视频会议",
        "traffic_priority": "high",
        "tx_power_dbm": 16.0,
        "cwmin": 3,
        "cwmax": 4,
        "aifsn": 2,
        "neighbor_rssi_dbm": {"ap2": -72.0, "ap3": -86.0},
        "sta_rssi_dbm": -54.0,
        "noise_floor_dbm": -94.0,
        "throughput_mbps_user": 8.0,
        "latency_ms": 35.0,
        "packet_loss_pct": 0.1,
        "stas": [
            {
                "sta_id": "sta1",
                "flow_type": "video_call",
                "sla": {
                    "min_throughput_mbps": 4.0,
                    "max_latency_ms": 80.0,
                    "max_jitter_ms": 30.0,
                    "max_packet_loss_pct": 1.0,
                },
                "measurements": {
                    "throughput_mbps": 8.0,
                    "latency_ms": 35.0,
                    "jitter_ms": 9.0,
                    "packet_loss_pct": 0.1,
                    "rssi_dbm": -54.0,
                    "sinr_db": 32.0,
                },
            }
        ],
    },
    "ap2": {
        "service_name": "bulk_download",
        "business_type": "大文件下载",
        "traffic_priority": "low",
        "tx_power_dbm": 16.0,
        "cwmin": 3,
        "cwmax": 4,
        "aifsn": 2,
        "neighbor_rssi_dbm": {"ap1": -72.0, "ap3": -86.0},
        "sta_rssi_dbm": -55.0,
        "noise_floor_dbm": -94.0,
        "throughput_mbps_user": 20.0,
        "latency_ms": 80.0,
        "packet_loss_pct": 0.0,
    },
    "ap3": {
        "service_name": "best_effort",
        "business_type": "普通业务",
        "traffic_priority": "medium",
        "tx_power_dbm": 16.0,
        "cwmin": 3,
        "cwmax": 4,
        "aifsn": 2,
        "neighbor_rssi_dbm": {"ap1": -86.0, "ap2": -86.0},
        "sta_rssi_dbm": -56.0,
        "noise_floor_dbm": -94.0,
        "throughput_mbps_user": 10.0,
        "latency_ms": 60.0,
        "packet_loss_pct": 0.0,
    },
}


class StaFeedbackTests(unittest.TestCase):
    def test_profile_keeps_and_summarizes_sta_feedback(self):
        profiled = apply_profile(copy.deepcopy(BASE_STATE))
        visible = agent_view(profiled)

        self.assertEqual(visible["ap1"]["sta_feedback_summary"]["status"], "satisfied")
        self.assertEqual(visible["ap1"]["stas"][0]["sla_status"], "satisfied")
        self.assertEqual(visible["ap1"]["stas"][0]["associated_ap"], "ap1")

    def test_summarize_detects_sla_violation(self):
        state = apply_profile(copy.deepcopy(BASE_STATE))
        state["ap1"]["stas"][0]["measurements"]["latency_ms"] = 120.0
        summary = summarize_sta_feedback(state)

        self.assertEqual(summary["violated_stas"], 1)
        self.assertEqual(summary["violations"][0]["sta_id"], "sta1")

    def test_validator_rejects_new_real_sta_sla_violation(self):
        baseline = apply_profile(copy.deepcopy(BASE_STATE))
        observed = apply_profile(copy.deepcopy(BASE_STATE))
        observed["ap1"]["stas"][0]["measurements"]["latency_ms"] = 140.0
        decision = {
            "ap1": {"tx_power_dbm": 16.0},
            "ap2": {"tx_power_dbm": 16.0},
            "ap3": {"tx_power_dbm": 16.0},
        }

        report = validate_decision(
            baseline,
            decision,
            "co_sr",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertFalse(report["approved"], report)
        self.assertTrue(report["sta_qoe"]["checked"])
        self.assertIn("新的 STA SLA 违规", report["global_errors"][0])

    def test_non_real_observation_reports_but_does_not_gate(self):
        baseline = apply_profile(copy.deepcopy(BASE_STATE))
        observed = apply_profile(copy.deepcopy(BASE_STATE))
        observed["ap1"]["stas"][0]["measurements"]["latency_ms"] = 140.0
        result = evaluate_sta_qoe(
            baseline,
            observed,
            observed_is_real=False,
        )

        self.assertTrue(result["checked"])
        self.assertTrue(result["approved"])
        self.assertEqual(len(result["new_violations"]), 1)


if __name__ == "__main__":
    unittest.main()

import copy
import unittest

from src.validator import validate_decision


BASE_STATE = {
    "ap1": {
        "tx_power_dbm": 10,
        "throughput_mbps_iperf": 20.0,
        "throughput_mbps_user": 5.0,
        "latency_ms": 5.0,
        "packet_loss_pct": 0.0,
    },
    "ap2": {
        "tx_power_dbm": 10,
        "throughput_mbps_iperf": 20.0,
        "throughput_mbps_user": 5.0,
        "latency_ms": 5.0,
        "packet_loss_pct": 0.0,
    },
    "ap3": {
        "tx_power_dbm": 10,
        "throughput_mbps_iperf": 20.0,
        "throughput_mbps_user": 5.0,
        "latency_ms": 5.0,
        "packet_loss_pct": 0.0,
    },
}

SR_DECISION = {
    "ap1": {"tx_power_dbm": 8},
    "ap2": {"tx_power_dbm": 8},
    "ap3": {"tx_power_dbm": 8},
}

EDCA_DECISION = {
    "ap1": {"CWmin": 31, "CWmax": 1023, "AIFSN": 5},
    "ap2": {"CWmin": 7, "CWmax": 63, "AIFSN": 2},
    "ap3": {"CWmin": 15, "CWmax": 127, "AIFSN": 3},
}


class ValidatorQosTest(unittest.TestCase):
    def test_real_observation_rejects_throughput_drop(self):
        observed = copy.deepcopy(BASE_STATE)
        for state in observed.values():
            state["tx_power_dbm"] = 8
            state["throughput_mbps_iperf"] = 10.0
            state["throughput_mbps_user"] = 2.0

        report = validate_decision(
            BASE_STATE,
            SR_DECISION,
            "co_sr",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertFalse(report["approved"], report)
        self.assertIn("QoS 下降：聚合吞吐", report["summary"])

    def test_real_observation_accepts_non_regression(self):
        observed = copy.deepcopy(BASE_STATE)
        for state in observed.values():
            state["tx_power_dbm"] = 8
            state["throughput_mbps_iperf"] = 21.0
            state["throughput_mbps_user"] = 5.5
            state["latency_ms"] = 4.8

        report = validate_decision(
            BASE_STATE,
            SR_DECISION,
            "co_sr",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertTrue(report["approved"], report)

    def test_precheck_does_not_apply_qos_without_real_observation(self):
        observed = copy.deepcopy(BASE_STATE)
        for state in observed.values():
            state["throughput_mbps_iperf"] = 1.0
            state["throughput_mbps_user"] = 1.0

        report = validate_decision(
            BASE_STATE,
            SR_DECISION,
            "co_sr",
            observed_state=observed,
            observed_is_real=False,
        )

        self.assertTrue(report["approved"], report)

    def test_edca_accepts_priority_gain_with_bounded_low_priority_tradeoff(self):
        before = copy.deepcopy(BASE_STATE)
        before["ap1"]["traffic_priority"] = "low"
        before["ap2"]["traffic_priority"] = "high"
        before["ap3"]["traffic_priority"] = "medium"
        observed = copy.deepcopy(before)
        observed["ap1"]["throughput_mbps_iperf"] = 18.0
        observed["ap1"]["throughput_mbps_user"] = 4.0
        observed["ap1"]["latency_ms"] = 6.0
        observed["ap1"]["packet_loss_pct"] = 2.0
        observed["ap2"]["throughput_mbps_iperf"] = 22.0
        observed["ap2"]["throughput_mbps_user"] = 5.5
        observed["ap2"]["latency_ms"] = 4.0
        observed["ap2"]["packet_loss_pct"] = 0.0

        report = validate_decision(
            before,
            EDCA_DECISION,
            "co_edca",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertTrue(report["approved"], report)
        qos_check = report["per_ap"]["_qos"]["checks"][0]
        self.assertEqual(qos_check["check"], "EDCA priority-aware QoS")

    def test_edca_rejects_unbounded_overall_degradation(self):
        before = copy.deepcopy(BASE_STATE)
        before["ap1"]["traffic_priority"] = "low"
        before["ap2"]["traffic_priority"] = "high"
        before["ap3"]["traffic_priority"] = "medium"
        observed = copy.deepcopy(before)
        for state in observed.values():
            state["throughput_mbps_iperf"] = 5.0
            state["throughput_mbps_user"] = 1.0
            state["latency_ms"] = 10.0
        observed["ap2"]["throughput_mbps_iperf"] = 21.0
        observed["ap2"]["throughput_mbps_user"] = 5.5
        observed["ap2"]["latency_ms"] = 4.0

        report = validate_decision(
            before,
            EDCA_DECISION,
            "co_edca",
            observed_state=observed,
            observed_is_real=True,
        )

        self.assertFalse(report["approved"], report)
        self.assertIn("EDCA 整体退化超限", report["summary"])


if __name__ == "__main__":
    unittest.main()

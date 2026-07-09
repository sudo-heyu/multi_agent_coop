import copy
import unittest

from tests.mock_scenes import MOCK_SCENES
from tests.mock_feeder import MockTelemetryFeeder


EDCA_DECISION_AP2_HIGH = {
    "ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
    "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
    "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
}


class MockTelemetryFeederTests(unittest.TestCase):
    def test_edca_decision_improves_live_ap2_and_deprioritizes_downloads(self):
        feeder = MockTelemetryFeeder(
            "http://localhost:5001",
            copy.deepcopy(MOCK_SCENES["edca"]),
        )
        before = copy.deepcopy(feeder._perf_target)

        feeder.apply_decision(EDCA_DECISION_AP2_HIGH)

        self.assertGreater(
            feeder._perf_target["ap2"]["throughput_mbps_user"],
            before["ap2"]["throughput_mbps_user"],
        )
        self.assertLess(
            feeder._perf_target["ap2"]["latency_ms"],
            before["ap2"]["latency_ms"],
        )
        for ap_id in ("ap1", "ap3"):
            self.assertLess(
                feeder._perf_target[ap_id]["throughput_mbps_user"],
                before[ap_id]["throughput_mbps_user"],
            )
            self.assertGreater(
                feeder._perf_target[ap_id]["latency_ms"],
                before[ap_id]["latency_ms"],
            )

    def test_all_aggressive_enters_negative_sum_region(self):
        feeder = MockTelemetryFeeder(
            "http://localhost:5001", copy.deepcopy(MOCK_SCENES["contention"])
        )
        before = copy.deepcopy(feeder._perf_target)
        all_aggressive = {
            ap: {"CWmin": 7, "CWmax": 15, "AIFSN": 2} for ap in ("ap1", "ap2", "ap3")
        }
        feeder.apply_decision(all_aggressive)
        self.assertTrue(all(
            feeder._perf_target[ap]["throughput_mbps_user"] < before[ap]["throughput_mbps_user"]
            for ap in ("ap1", "ap2", "ap3")
        ))

    def test_all_high_power_has_interference_cost(self):
        feeder = MockTelemetryFeeder(
            "http://localhost:5001", copy.deepcopy(MOCK_SCENES["sr"])
        )
        before = copy.deepcopy(feeder._perf_target)
        feeder.apply_decision({ap: {"tx_power_dbm": 20} for ap in ("ap1", "ap2", "ap3")})
        self.assertTrue(all(
            feeder._perf_target[ap]["throughput_mbps_user"] < before[ap]["throughput_mbps_user"]
            for ap in ("ap1", "ap2", "ap3")
        ))


if __name__ == "__main__":
    unittest.main()

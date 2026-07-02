import copy
import unittest

from openclaw.scenes import MOCK_SCENES
from state_server.mock_feeder import MockTelemetryFeeder


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


if __name__ == "__main__":
    unittest.main()

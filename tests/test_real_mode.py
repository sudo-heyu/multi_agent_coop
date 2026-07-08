import unittest
from unittest.mock import patch

import run_openclaw
from openclaw.scenes import MOCK_SCENES
from openclaw.mcp import orchestration as orch
import copy


class RealModeTests(unittest.TestCase):
    def test_real_endpoints_require_all_three_aps(self):
        with self.assertRaisesRegex(ValueError, "缺少 ap3"):
            run_openclaw._validate_real_endpoints({
                "ap1": "http://10.0.0.1:5002",
                "ap2": "http://10.0.0.2:5002",
            })

    def test_real_endpoints_accept_exact_ap_set(self):
        run_openclaw._validate_real_endpoints({
            "ap1": "http://10.0.0.1:5002",
            "ap2": "http://10.0.0.2:5002",
            "ap3": "http://10.0.0.3:5002",
        })

    def test_real_mode_never_constructs_mock_feeder(self):
        with patch.object(run_openclaw, "MockTelemetryFeeder") as feeder:
            result = run_openclaw._start_telemetry(
                "real", False, "http://localhost:5001", {}, 1.0
            )

        self.assertIsNone(result)
        feeder.assert_not_called()

    def test_ns3_mode_never_constructs_mock_feeder(self):
        with patch.object(run_openclaw, "MockTelemetryFeeder") as feeder:
            result = run_openclaw._start_telemetry(
                "ns3", False, "http://localhost:5001", {}, 1.0
            )

        self.assertIsNone(result)
        feeder.assert_not_called()

    def test_ns3_eval_windows_default_to_short_feedback(self):
        with patch.dict(run_openclaw.os.environ, {"MULTIAP_EVAL_WINDOWS": ""}):
            self.assertEqual(run_openclaw._resolve_eval_windows("", "ns3"), (10.0, 30.0))

    def test_resume_rejects_changed_ap_parameters(self):
        stored = orch.apply_profile(copy.deepcopy(MOCK_SCENES["edca"]))
        latest = {
            ap: {"data": copy.deepcopy(state), "stale": False}
            for ap, state in MOCK_SCENES["edca"].items()
        }
        latest["ap2"]["data"]["tx_power_dbm"] += 1

        compatible, reason = run_openclaw._resume_state_compatible(stored, latest)

        self.assertFalse(compatible)
        self.assertIn("ap2.tx_power_dbm", reason)

    def test_resume_allows_qos_drift_when_parameters_match(self):
        stored = orch.apply_profile(copy.deepcopy(MOCK_SCENES["edca"]))
        latest = {
            ap: {"data": copy.deepcopy(state), "stale": False}
            for ap, state in MOCK_SCENES["edca"].items()
        }
        latest["ap2"]["data"]["latency_ms"] += 25

        compatible, _ = run_openclaw._resume_state_compatible(stored, latest)

        self.assertTrue(compatible)


if __name__ == "__main__":
    unittest.main()

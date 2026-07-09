import unittest
from unittest.mock import patch

import run_openclaw
from tests.mock_scenes import MOCK_SCENES
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

    def test_runtime_has_no_mock_feeder_path(self):
        """mock 已降级为测试夹具：运行时入口不得再引用 feeder 或 mock 模式。"""
        self.assertFalse(hasattr(run_openclaw, "MockTelemetryFeeder"))
        self.assertFalse(hasattr(run_openclaw, "_start_telemetry"))
        source = open(run_openclaw.__file__, encoding="utf-8").read()
        self.assertNotIn("MockTelemetryFeeder(", source)

    def test_mode_choices_exclude_mock(self):
        source = open(run_openclaw.__file__, encoding="utf-8").read()
        self.assertIn('choices=["real", "ns3"]', source)
        self.assertNotIn('choices=["mock"', source)

    def test_scene_names_match_test_fixtures(self):
        from openclaw.scenes import SCENE_NAMES
        self.assertEqual(set(SCENE_NAMES), set(MOCK_SCENES))

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

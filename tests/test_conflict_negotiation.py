import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from openclaw.mcp import orchestration as orch
from openclaw.scenes import MOCK_SCENES
from src.memory.outcome import evaluate_deltas


class PrivateSlaTests(unittest.TestCase):
    def setUp(self):
        self._workspace_td = tempfile.TemporaryDirectory()
        self._workspace_patch = patch.dict(
            os.environ, {"MULTIAP_AGENT_WORKSPACES_ROOT": self._workspace_td.name}
        )
        self._workspace_patch.start()
        orch.reset_session(copy.deepcopy(MOCK_SCENES["hidden_sla"]))

    def tearDown(self):
        self._workspace_patch.stop()
        self._workspace_td.cleanup()

    def test_private_sla_not_in_global_agent_view(self):
        visible = orch.agent_view(orch.session().ap_state)
        self.assertNotIn("private_sla", visible["ap3"])
        self.assertNotIn("deadline_minutes", str(visible))

    def test_only_current_agent_sla_is_injected(self):
        instruction = orch.vote_instruction(
            "ap3", "ap1", "co_edca",
            {ap: {"CWmin": 15, "CWmax": 63, "AIFSN": 6} for ap in ("ap1", "ap2", "ap3")},
            1,
        )
        message = orch._build_agent_message("ap3", "", instruction)
        self.assertNotIn("deadline_minutes", instruction)
        self.assertEqual(message.count("deadline_minutes"), 1)
        self.assertNotIn("max_latency_ms\": 100", message)  # AP1 的私有底线


class SlaQualityTests(unittest.TestCase):
    def test_quality_reports_sla_and_fairness(self):
        baseline = copy.deepcopy(MOCK_SCENES["contention"])
        observed = copy.deepcopy(baseline)
        for row in observed.values():
            row["throughput_mbps_user"] = 16.0
            row["latency_ms"] = 70.0
        result = evaluate_deltas(baseline, observed)
        self.assertTrue(result["sla"]["all_satisfied"])
        self.assertGreater(result["sla"]["fairness_jain"], 0.9)
        self.assertIn("performance_score", result)


if __name__ == "__main__":
    unittest.main()

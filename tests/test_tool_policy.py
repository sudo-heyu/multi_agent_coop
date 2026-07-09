import copy
import os
import unittest
from unittest.mock import patch

from openclaw.mcp import direct_tools, multiap_mcp
from tests.mock_scenes import MOCK_SCENES


class ToolPolicyTests(unittest.TestCase):
    def test_none_profile_exposes_no_tools(self):
        with patch.dict(os.environ, {"MULTIAP_TOOL_PROFILE": "none"}):
            tools = direct_tools.openai_tools()
            result, _ = direct_tools.call_tool("get_latest_ap_states", {})

        self.assertEqual(tools, [])
        self.assertFalse(result["available"])
        self.assertIn("none", result["error"])

    def test_diagnostic_profile_hides_answer_like_sr_tools(self):
        with patch.dict(os.environ, {"MULTIAP_TOOL_PROFILE": "diagnostic"}):
            names = [tool["function"]["name"] for tool in direct_tools.openai_tools()]
            result, _ = direct_tools.call_tool("select_sr_concurrent_groups", {})

        self.assertNotIn("select_sr_concurrent_groups", names)
        self.assertNotIn("rank_sr_candidates", names)
        self.assertIn("evaluate_sr_candidate", names)
        self.assertFalse(result["available"])
        self.assertIn("不可用", result["error"])

    def test_validator_only_profile_redacts_edca_effectiveness(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        proposal = {
            "ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3},
            "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
            "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 3},
        }

        with patch.dict(os.environ, {"MULTIAP_TOOL_PROFILE": "validator_only"}), \
                patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.validate_edca_proposal(proposal)

        self.assertNotIn("effectiveness", result)
        self.assertNotIn("safety_validation", result)
        self.assertNotIn("all_ok", result)
        self.assertTrue(result["effectiveness_redacted"])
        self.assertTrue(result["safety_validation_redacted"])
        self.assertEqual(
            [result[ap]["valid"] for ap in ("ap1", "ap2", "ap3")],
            ["unknown", "unknown", "unknown"],
        )
        self.assertTrue(all(result[ap]["range_valid"] for ap in ("ap1", "ap2", "ap3")))
        self.assertIn("弱工具档位", result["policy_note"])

    def test_diagnostic_profile_does_not_confirm_sr_candidate_success(self):
        state = copy.deepcopy(MOCK_SCENES["sr"])
        profiled = multiap_mcp.apply_profile(copy.deepcopy(state))
        best = multiap_mcp._sr.select_concurrent_groups(profiled)["best_group"]

        with patch.dict(os.environ, {"MULTIAP_TOOL_PROFILE": "diagnostic"}), \
                patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.evaluate_sr_candidate(
                best["recommended_powers"],
                best["concurrent_group"],
            )

        self.assertEqual(result["valid"], "unknown")
        self.assertNotIn("score", result)
        self.assertEqual(result["constraint_check_scope"], "negative_only")

    def test_state_only_profile_blocks_validators(self):
        with patch.dict(os.environ, {"MULTIAP_TOOL_PROFILE": "state_only"}):
            result = multiap_mcp.validate_edca_proposal({
                "ap1": {"CWmin": 7, "CWmax": 15, "AIFSN": 2}
            })

        self.assertFalse(result["available"])
        self.assertIn("state_only", result["error"])

    def test_memory_challenge_coarsens_latest_state(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])

        with patch.dict(os.environ, {"MULTIAP_TOOL_PROFILE": "memory_challenge"}), \
                patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.get_latest_ap_states()

        ap1 = result["ap_states"]["ap1"]
        self.assertEqual(ap1["traffic_priority"], state["ap1"]["traffic_priority"])
        self.assertFalse(ap1["exact_parameters_visible"])
        self.assertIn("qoe_summary", ap1)
        self.assertIn("neighbor_interference_level", ap1)
        self.assertNotIn("cwmin", ap1)
        self.assertNotIn("throughput_mbps_user", ap1)
        self.assertNotIn("neighbor_rssi_dbm", ap1)
        self.assertIn("ap_states.exact_metrics", result["policy_redactions"])


if __name__ == "__main__":
    unittest.main()

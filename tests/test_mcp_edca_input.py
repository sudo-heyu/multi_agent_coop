import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from openclaw.mcp import multiap_mcp
from tests.mock_scenes import MOCK_SCENES


class TestMcpEdcaInput(unittest.TestCase):
    def setUp(self):
        self.proposal = {
            "ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3},
            "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
            "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 3},
        }

    def test_accepts_json_string_from_tool_model(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.validate_edca_proposal(json.dumps(self.proposal))

        self.assertNotIn("error", result)
        self.assertTrue(result["effectiveness"]["all_ok"], result)
        self.assertTrue(all(result[ap]["valid"] for ap in ("ap1", "ap2", "ap3")))

    def test_reports_invalid_json_string(self):
        result = multiap_mcp.validate_edca_proposal("{bad json")
        self.assertIn("不是合法 JSON", result["error"])

    def test_accepts_per_ac_vi_fields(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        proposal = {
            "ap1": {"VI_CWmin": 7, "VI_CWmax": 15, "VI_AIFSN": 2},
            "ap2": {"VI_CWmin": 7, "VI_CWmax": 15, "VI_AIFSN": 2},
            "ap3": {"VI_CWmin": 15, "VI_CWmax": 31, "VI_AIFSN": 3},
        }
        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.validate_edca_proposal(proposal)

        self.assertNotIn("error", result)
        self.assertTrue(all(result[ap]["valid"] for ap in ("ap1", "ap2", "ap3")))
        self.assertEqual(result["ap1"]["VI_CWmin"], 7)

    def test_edca_tool_reports_self_harm_guard(self):
        state = {
            "ap1": {"cwmin": 15, "cwmax": 1023, "aifsn": 3,
                    "traffic_priority": "low", "sta_feedback_summary": {"status": "satisfied"}},
            "ap2": {"cwmin": 15, "cwmax": 1023, "aifsn": 3,
                    "traffic_priority": "high", "sta_feedback_summary": {"status": "satisfied"}},
            "ap3": {"cwmin": 15, "cwmax": 1023, "aifsn": 3,
                    "traffic_priority": "medium", "sta_feedback_summary": {"status": "satisfied"}},
        }
        proposal = {
            "AP1": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
            "AP2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
            "AP3": {"CWmin": 7, "CWmax": 31, "AIFSN": 3},
        }
        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.validate_edca_proposal(proposal)

        self.assertFalse(result["all_ok"], result)
        self.assertFalse(result["safety_validation"]["approved"], result)
        self.assertIn("自伤", ";".join(result["ap1"]["errors"]))

    def test_empty_edca_call_requires_explicit_argument(self):
        result = multiap_mcp.validate_edca_proposal()

        self.assertIn("需要 proposed_edca 参数", result["error"])

    def test_sr_accepts_full_proposal_json_string(self):
        state = copy.deepcopy(MOCK_SCENES["sr"])
        profiled = multiap_mcp.apply_profile(copy.deepcopy(state))
        best = multiap_mcp._sr.select_concurrent_groups(profiled)["best_group"]
        proposal = {
            ap_id: {"tx_power_dbm": power}
            for ap_id, power in best["recommended_powers"].items()
        }
        proposal["_sr"] = {"concurrent_group": best["concurrent_group"]}

        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.evaluate_sr_candidate(json.dumps(proposal))

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["concurrent_group"], best["concurrent_group"])

    def test_empty_sr_call_requires_explicit_argument(self):
        result = multiap_mcp.evaluate_sr_candidate()

        self.assertIn("需要显式 proposed_powers 参数", result["error"])

    def test_tool_call_emits_source_event(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tool-events.jsonl")
            with patch.dict(os.environ, {"MULTIAP_TOOL_EVENT_PATH": path}), \
                 patch.object(multiap_mcp, "get_all_states", return_value=state):
                result = multiap_mcp.validate_edca_proposal(self.proposal)

            with open(path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]

        self.assertTrue(result["effectiveness"]["all_ok"], result)
        self.assertEqual(rows[-1]["event"], "mcp_tool_call")
        self.assertEqual(rows[-1]["tool"], "validate_edca_proposal")
        self.assertEqual(rows[-1]["args"]["proposed_edca"], self.proposal)
        self.assertTrue(rows[-1]["result"]["effectiveness"]["all_ok"])

    def test_get_sta_feedback_returns_ns3_qoe_summary(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        state["ap2"]["stas"] = [{
            "sta_id": "sta2-live",
            "associated_ap": "ap2",
            "flow_type": "live_stream",
            "sla": {"max_latency_ms": 100, "max_jitter_ms": 30},
            "measurements": {"latency_ms": 140, "jitter_ms": 45},
        }]
        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.get_sta_feedback("ap2")

        self.assertEqual(result["per_ap"]["ap2"]["status"], "violated")
        self.assertEqual(result["per_ap"]["ap2"]["stas"][0]["sta_id"], "sta2-live")
        self.assertEqual(result["violated_stas"], 1)


if __name__ == "__main__":
    unittest.main()

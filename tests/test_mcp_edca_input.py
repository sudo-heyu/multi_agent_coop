import copy
import json
import unittest
from unittest.mock import patch

from openclaw.mcp import multiap_mcp
from openclaw.scenes import MOCK_SCENES


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


if __name__ == "__main__":
    unittest.main()

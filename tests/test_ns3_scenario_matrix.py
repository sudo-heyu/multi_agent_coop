import unittest

from state_server.ns3_scenario_matrix import build_matrix, get_case, get_case_by_id


class Ns3ScenarioMatrixTest(unittest.TestCase):
    def test_matrix_has_six_tuned_cases(self):
        cases = build_matrix()
        self.assertEqual(len(cases), 6)
        self.assertEqual({c.family for c in cases}, {"sr", "edca"})
        self.assertEqual({c.role for c in cases}, {"clear", "representative", "fuzzy"})
        self.assertEqual({c.scenario for c in cases}, {"line", "triangle"})
        self.assertEqual(
            {c.business_profile for c in cases},
            {"live_bulk", "mixed_qoe", "deadline_backup", "uniform"},
        )

    def test_sr_cases_are_three_roles(self):
        cases = [c for c in build_matrix() if c.family == "sr"]
        self.assertEqual({c.role for c in cases}, {"clear", "representative", "fuzzy"})
        self.assertTrue(all(c.expected_strategy == "co_sr" for c in cases))
        self.assertEqual(get_case_by_id("sr_fuzzy_mixed_business").business_profile, "mixed_qoe")

    def test_edca_cases_are_three_roles(self):
        cases = [c for c in build_matrix() if c.family == "edca"]
        self.assertEqual({c.role for c in cases}, {"clear", "representative", "fuzzy"})
        self.assertTrue(all(c.expected_strategy == "co_edca" for c in cases))
        self.assertEqual(get_case_by_id("edca_clear_line_deadline").business_profile, "live_bulk")
        self.assertEqual(get_case_by_id("edca_representative_line_live_bulk").business_profile, "live_bulk")
        self.assertEqual(get_case_by_id("edca_fuzzy_triangle_deadline").scenario, "triangle")

    def test_get_case_returns_default_for_topology_profile(self):
        self.assertEqual(get_case("triangle", "uniform").case_id, "sr_clear_dense_uniform")
        self.assertEqual(get_case("line", "live_bulk").case_id, "edca_clear_line_deadline")


if __name__ == "__main__":
    unittest.main()

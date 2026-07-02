import copy
import tempfile
import unittest
from pathlib import Path

from openclaw.scenes import MOCK_SCENES
from src.memory import find_matching_rules, format_rule, induce_rules, materialize_episode
from src.persistence import EventStore


class SemanticMemoryTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "sem.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _episode(self, run_id, verdict, decision, *, scene="edca",
                 strategy="co_edca", state=None, confidence=0.9):
        state = state or copy.deepcopy(self.baseline)
        self.store.start_run(run_id, mode="mock", scene=scene, model="openclaw")
        self.store.append_event(
            run_id, "session_start",
            {"model": "openclaw", "scene": scene, "ap_state": state},
        )
        self.store.record_snapshot(run_id, label="initial", source="s", state=state)
        self.store.append_event(run_id, "final_decision", {"decision": decision, "raw_response": "{}"})
        self.store.append_event(
            run_id, "validation_result",
            {"approved": True, "strategy": strategy, "summary": "ok"},
        )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        materialize_episode(self.store, run_id)
        if verdict is not None:
            self.store.update_episode_evaluation(
                run_id,
                evaluation={"final_verdict": verdict, "final_confidence": confidence,
                            "needs_rollback": verdict == "degraded"},
                quality_score=0.9 if verdict == "improved" else 0.2,
            )

    def _edca(self, cwmin):
        return {"ap1": {"CWmin": cwmin, "CWmax": 63, "AIFSN": 3},
                "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
                "ap3": {"CWmin": cwmin, "CWmax": 63, "AIFSN": 3}}

    # ── 归纳 ────────────────────────────────────────────────────────

    def test_induce_groups_and_computes_dominant_verdict(self):
        self._episode("r1", "improved", self._edca(15))
        self._episode("r2", "improved", self._edca(7))
        self._episode("r3", "neutral", self._edca(31))
        rules = induce_rules(self.store)
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule["dominant_verdict"], "improved")
        self.assertEqual(rule["support"], 3)
        self.assertAlmostEqual(rule["consistency"], round(2 / 3, 4))
        self.assertEqual(rule["verdict_counts"], {"improved": 2, "neutral": 1})
        # confidence = consistency × min(1, support/FULL_SUPPORT) = 0.6667 × min(1, 3/3)
        self.assertAlmostEqual(rule["confidence"], round((2 / 3) * 1.0, 4))

    def test_single_case_below_min_support_yields_no_rule(self):
        self._episode("solo", "improved", self._edca(15))
        self.assertEqual(induce_rules(self.store), [])

    def test_unevaluated_and_inconclusive_episodes_excluded(self):
        self._episode("eval1", "improved", self._edca(15))
        self._episode("eval2", "improved", self._edca(7))
        self._episode("noeval", None, self._edca(31))          # 无 L4 反馈
        self._episode("incon", "inconclusive", self._edca(63))  # 数据不足
        rules = induce_rules(self.store)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["support"], 2)
        run_ids = {e["run_id"] for e in rules[0]["evidence"]}
        self.assertEqual(run_ids, {"eval1", "eval2"})

    def test_action_summary_is_median_of_dominant_cases(self):
        self._episode("a1", "improved", self._edca(15))
        self._episode("a2", "improved", self._edca(7))
        self._episode("a3", "degraded", self._edca(1023))  # 非 dominant，不入 action_summary
        rule = induce_rules(self.store)[0]
        # 两个 improved 案例 ap1 CWmin=[15,7] → 中位数 11
        self.assertEqual(rule["action_summary"]["ap1"]["CWmin"], 11)

    def test_different_strategy_forms_separate_rule(self):
        self._episode("e1", "improved", self._edca(15), scene="edca", strategy="co_edca")
        self._episode("e2", "improved", self._edca(7), scene="edca", strategy="co_edca")
        sr_dec = {ap: {"tx_power_dbm": 8} for ap in ("ap1", "ap2", "ap3")}
        self._episode("s1", "improved", sr_dec, scene="sr", strategy="co_sr")
        self._episode("s2", "improved", sr_dec, scene="sr", strategy="co_sr")
        rules = induce_rules(self.store)
        strategies = sorted(r["strategy"] for r in rules)
        self.assertEqual(strategies, ["co_edca", "co_sr"])

    # ── upsert 幂等 ──────────────────────────────────────────────────

    def test_reinduce_upserts_and_preserves_created_at(self):
        self._episode("r1", "improved", self._edca(15))
        self._episode("r2", "improved", self._edca(7))
        induce_rules(self.store)
        created = self.store.list_rules()[0]["created_at"]
        # 追加一个案例改变分布，再归纳。
        self._episode("r3", "degraded", self._edca(1023))
        induce_rules(self.store)
        rules = self.store.list_rules()
        self.assertEqual(len(rules), 1)  # 仍是一条（同组 upsert）
        self.assertEqual(rules[0]["support"], 3)
        self.assertEqual(rules[0]["created_at"], created)

    # ── 检索 ────────────────────────────────────────────────────────

    def test_find_matching_filters_by_topology_and_confidence(self):
        self._episode("r1", "improved", self._edca(15))
        self._episode("r2", "improved", self._edca(7))
        induce_rules(self.store)
        # 同拓扑高置信 → 命中
        hit = find_matching_rules(self.store, self.baseline, min_confidence=0.3)
        self.assertEqual(len(hit), 1)
        # 阈值高于该规律置信度（0.6667）→ 不命中
        self.assertEqual(find_matching_rules(self.store, self.baseline, min_confidence=0.9), [])
        # 不同拓扑 → 不命中
        other = copy.deepcopy(self.baseline)
        other["ap1"]["neighbor_rssi_dbm"].pop("ap3")
        self.assertEqual(find_matching_rules(self.store, other, min_confidence=0.3), [])

    def test_format_rule_is_readable(self):
        self._episode("r1", "improved", self._edca(15))
        self._episode("r2", "improved", self._edca(7))
        text = format_rule(induce_rules(self.store)[0])
        self.assertIn("倾向改善", text)
        self.assertIn("典型做法", text)


if __name__ == "__main__":
    unittest.main()

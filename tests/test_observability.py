import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from openclaw.scenes import MOCK_SCENES
from src.memory import induce_rules, materialize_episode, memory_health, schedule_outcome_evaluations
from src.persistence import EventStore


class MemoryHealthTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "obs.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])
        self.t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _episode(self, run_id, verdict, quality, cwmin=15):
        state = copy.deepcopy(self.baseline)
        dec = {"ap1": {"CWmin": cwmin, "CWmax": 63, "AIFSN": 3},
               "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
               "ap3": {"CWmin": cwmin, "CWmax": 63, "AIFSN": 3}}
        self.store.start_run(run_id, mode="mock", scene="edca", model="m")
        self.store.append_event(run_id, "session_start",
                                {"model": "m", "scene": "edca", "ap_state": state})
        self.store.record_snapshot(run_id, label="initial", source="s", state=state)
        self.store.append_event(run_id, "final_decision", {"decision": dec, "raw_response": "{}"})
        self.store.append_event(run_id, "validation_result",
                                {"approved": True, "strategy": "co_edca", "summary": "ok"})
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        materialize_episode(self.store, run_id)
        if verdict is not None:
            self.store.update_episode_evaluation(
                run_id,
                evaluation={"final_verdict": verdict, "final_confidence": 0.9},
                quality_score=quality,
            )

    def test_empty_store(self):
        h = memory_health(self.store)
        self.assertEqual(h["runs"]["total"], 0)
        self.assertEqual(h["episodes"]["alive"], 0)
        self.assertEqual(h["episodes"]["quality"], {"count": 0})
        self.assertEqual(h["rules"]["total"], 0)

    def test_aggregates_all_layers(self):
        self._episode("r1", "improved", 0.9, cwmin=15)
        self._episode("r2", "improved", 0.85, cwmin=7)
        self._episode("r3", "degraded", 0.2, cwmin=1023)
        induce_rules(self.store)
        # 一个 pending 评估窗口
        schedule_outcome_evaluations(self.store, "r1", self.baseline, (60.0,), now=self.t0)
        # 归档一个案例
        self.store.archive_episodes(["r3"])

        h = memory_health(self.store)
        self.assertEqual(h["runs"]["total"], 3)
        self.assertEqual(h["runs"]["completed"], 3)
        self.assertEqual(h["episodes"]["total"], 3)
        self.assertEqual(h["episodes"]["alive"], 2)
        self.assertEqual(h["episodes"]["archived"], 1)
        # 存活案例都是 improved
        self.assertEqual(h["episodes"]["by_verdict"].get("improved"), 2)
        self.assertNotIn("degraded", h["episodes"]["by_verdict"])  # 已归档，不计存活
        self.assertEqual(h["episodes"]["quality"]["median"], 0.875)
        self.assertEqual(h["evaluations"]["pending"], 1)
        self.assertGreaterEqual(h["rules"]["total"], 1)
        self.assertEqual(h["topologies"]["count"], 1)

    def test_conflicted_rule_counted_separately(self):
        # 平票 → 冲突规律
        self._episode("c1", "improved", 0.9, cwmin=15)
        self._episode("c2", "degraded", 0.2, cwmin=7)
        from src.memory import consolidate
        consolidate(self.store)
        h = memory_health(self.store)
        self.assertEqual(h["rules"]["conflicted"], 1)
        self.assertEqual(h["rules"]["active"], 0)

    def test_unevaluated_episode_bucketed(self):
        self._episode("u1", None, 0.8)
        h = memory_health(self.store)
        self.assertEqual(h["episodes"]["by_verdict"].get("unevaluated"), 1)


if __name__ == "__main__":
    unittest.main()

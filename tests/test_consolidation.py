import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from openclaw.scenes import MOCK_SCENES
from src.memory import (
    ConsolidationConfig,
    consolidate,
    find_matching_rules,
    find_similar_episodes,
    materialize_episode,
)
from src.memory.consolidation import LOCK_NAME
from src.persistence import EventStore


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "cons.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])
        self.t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _episode(self, run_id, verdict, quality, *, cwmin=15, state=None):
        state = state or copy.deepcopy(self.baseline)
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

    # ── 容量淘汰 ─────────────────────────────────────────────────────

    def test_over_capacity_archives_lowest_quality(self):
        self._episode("hi1", "improved", 0.9)
        self._episode("hi2", "improved", 0.8)
        self._episode("lo1", "degraded", 0.2)
        self._episode("lo2", "degraded", 0.2)
        result = consolidate(self.store, config=ConsolidationConfig(max_per_topology=2))
        self.assertEqual(result["status"], "done")
        self.assertEqual(set(result["archived_over_capacity"]), {"lo1", "lo2"})
        alive = {e["run_id"] for e in self.store.list_episodes(limit=100)}
        self.assertEqual(alive, {"hi1", "hi2"})
        # 软删不物理删除：include_archived 仍可见。
        allrows = {e["run_id"] for e in self.store.list_episodes(limit=100, include_archived=True)}
        self.assertEqual(allrows, {"hi1", "hi2", "lo1", "lo2"})

    def test_under_capacity_archives_nothing(self):
        self._episode("a", "improved", 0.9)
        self._episode("b", "improved", 0.8)
        result = consolidate(self.store, config=ConsolidationConfig(max_per_topology=5))
        self.assertEqual(result["archived_over_capacity"], [])

    # ── 过期归档 ─────────────────────────────────────────────────────

    def test_expired_low_quality_archived_but_not_recent_or_high_quality(self):
        self._episode("old_bad", "degraded", 0.2)
        self._episode("old_good", "improved", 0.9)
        cfg = ConsolidationConfig(max_per_topology=100, max_age_days=90, min_quality_keep=0.3)
        # now 设为案例创建 +100 天：老低质归档，老高质保留。
        result = consolidate(self.store, config=cfg, now=self.t0 + timedelta(days=100))
        self.assertEqual(result["archived_expired"], ["old_bad"])
        alive = {e["run_id"] for e in self.store.list_episodes(limit=100)}
        self.assertIn("old_good", alive)
        self.assertNotIn("old_bad", alive)

    def test_recent_low_quality_not_expired(self):
        self._episode("new_bad", "degraded", 0.2)
        cfg = ConsolidationConfig(max_per_topology=100, max_age_days=90, min_quality_keep=0.3)
        result = consolidate(self.store, config=cfg, now=self.t0 + timedelta(days=1))
        self.assertEqual(result["archived_expired"], [])

    # ── 冲突检测 ─────────────────────────────────────────────────────

    def test_low_consistency_rule_flagged_conflicted_and_excluded(self):
        # 2 案例平票（improved/degraded）→ consistency=0.5 < 0.6 → conflicted
        self._episode("c1", "improved", 0.9, cwmin=15)
        self._episode("c2", "degraded", 0.2, cwmin=7)
        result = consolidate(self.store, config=ConsolidationConfig(conflict_consistency=0.6))
        self.assertEqual(len(result["conflicted_rules"]), 1)
        # 冲突规律默认不注入提案
        self.assertEqual(find_matching_rules(self.store, self.baseline, min_confidence=0.0), [])
        # 但 include_conflicted 仍可查
        self.assertEqual(len(self.store.list_rules(include_conflicted=True)), 1)

    def test_consistent_rule_not_flagged(self):
        self._episode("g1", "improved", 0.9, cwmin=15)
        self._episode("g2", "improved", 0.8, cwmin=7)
        self._episode("g3", "improved", 0.85, cwmin=11)
        result = consolidate(self.store, config=ConsolidationConfig(conflict_consistency=0.6))
        self.assertEqual(result["conflicted_rules"], [])
        self.assertEqual(len(find_matching_rules(
            self.store, self.baseline, min_confidence=0.5, actionable_min_support=2
        )), 1)

    def test_conflict_flag_clears_when_evidence_realigns(self):
        self._episode("c1", "improved", 0.9, cwmin=15)
        self._episode("c2", "degraded", 0.2, cwmin=7)
        consolidate(self.store)  # 标 conflicted
        self.assertTrue(self.store.list_rules(include_conflicted=True)[0]["conflicted"])
        # 补两个 improved，一致性回升 → 清冲突标记
        self._episode("c3", "improved", 0.88, cwmin=11)
        self._episode("c4", "improved", 0.86, cwmin=9)
        consolidate(self.store)
        rule = self.store.list_rules(include_conflicted=True)[0]
        self.assertFalse(rule["conflicted"])

    # ── 检索排除归档 ─────────────────────────────────────────────────

    def test_archived_episode_excluded_from_similarity(self):
        self._episode("keep", "improved", 0.9)
        self._episode("drop", "degraded", 0.2)
        self.store.archive_episodes(["drop"])
        found = {e["run_id"] for e in find_similar_episodes(self.store, self.baseline)}
        self.assertIn("keep", found)
        self.assertNotIn("drop", found)

    # ── 锁与门控 ─────────────────────────────────────────────────────

    def test_lock_prevents_concurrent_consolidation(self):
        self._episode("a", "improved", 0.9)
        self._episode("b", "improved", 0.8)
        self.store.acquire_lock(LOCK_NAME, "someone-else", ttl_seconds=300)
        result = consolidate(self.store)
        self.assertEqual(result["status"], "skipped")

    def test_lock_released_after_run_allows_next(self):
        self._episode("a", "improved", 0.9)
        self._episode("b", "improved", 0.8)
        first = consolidate(self.store)
        second = consolidate(self.store)
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "done")


if __name__ == "__main__":
    unittest.main()

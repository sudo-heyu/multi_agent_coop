import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from src.memory.goals import (
    auto_create_goal, create_goal, get_active_goal, goal_overview, metric_value,
    record_attempt_result, register_attempt, score_goal_progress, validate_target,
)
from src.persistence import EventStore

TARGET = {"ap": "ap2", "metric": "tx_retries_ratio", "op": "<=", "value": 0.15}


class GoalObjectTests(unittest.TestCase):
    """I1：目标是 Event Store 一等公民，单活跃目标，attempt 成链。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_path = Path(self._td.name) / "goals.sqlite3"
        self.store = EventStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_validate_target_rejects_bad_spec(self):
        with self.assertRaises(ValueError):
            validate_target({"ap": "ap2", "metric": "x", "op": "!=", "value": 1})
        with self.assertRaises(ValueError):
            validate_target({"ap": "ap2", "metric": "x", "op": "<="})

    def test_create_and_single_active_constraint(self):
        goal = create_goal(self.store, target=TARGET, budget_attempts=4)
        self.assertEqual(goal["status"], "active")
        self.assertEqual(goal["metric"], "ap2.tx_retries_ratio<=0.15")
        self.assertEqual(goal["budget_attempts"], 4)
        self.assertEqual(get_active_goal(self.store)["goal_id"], goal["goal_id"])
        with self.assertRaises(ValueError):
            create_goal(self.store, target=TARGET)
        # 放弃后可再建。
        self.store.update_goal_status(goal["goal_id"], "abandoned", reason="test")
        second = create_goal(self.store, target=TARGET)
        self.assertNotEqual(second["goal_id"], goal["goal_id"])

    def test_attempt_chain_sequence_and_parent(self):
        goal = create_goal(self.store, target=TARGET)
        first = register_attempt(self.store, goal["goal_id"], "run-1")
        second = register_attempt(self.store, goal["goal_id"], "run-2")
        self.assertEqual(first["sequence"], 1)
        self.assertIsNone(first["parent_attempt_id"])
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(second["parent_attempt_id"], first["attempt_id"])
        # 同一 run 重复登记（恢复路径）幂等。
        again = register_attempt(self.store, goal["goal_id"], "run-2")
        self.assertEqual(again["attempt_id"], second["attempt_id"])
        overview = goal_overview(self.store, goal["goal_id"])
        self.assertEqual([a["sequence"] for a in overview["attempts"]], [1, 2])

    def test_goals_switch_disables_registration(self):
        goal = create_goal(self.store, target=TARGET)
        with patch.dict(os.environ, {"MULTIAP_GOALS": "0"}):
            self.assertIsNone(register_attempt(self.store, goal["goal_id"], "run-x"))
        self.assertEqual(self.store.list_goal_attempts(goal["goal_id"]), [])

    def test_score_goal_progress(self):
        goal = create_goal(self.store, target=TARGET)
        met = score_goal_progress(goal, {"ap2": {"tx_retries_ratio": 0.10}})
        self.assertTrue(met["met"])
        self.assertEqual(met["gap"], 0.0)
        miss = score_goal_progress(goal, {"ap2": {"tx_retries_ratio": 0.30}})
        self.assertFalse(miss["met"])
        self.assertAlmostEqual(miss["gap"], 0.15, places=6)
        absent = score_goal_progress(goal, {"ap1": {}})
        self.assertIsNone(absent["met"])
        self.assertIsNone(metric_value({"ap2": {"tx_retries_ratio": "n/a"}}, TARGET))

    def test_record_attempt_result_updates_status_and_progress(self):
        goal = create_goal(self.store, target=TARGET)
        register_attempt(self.store, goal["goal_id"], "run-1")
        attempt = record_attempt_result(
            self.store, "run-1", outcome="success",
            observed_state={"ap2": {"tx_retries_ratio": 0.12}},
        )
        self.assertEqual(attempt["status"], "completed")
        self.assertTrue(attempt["progress"]["met"])
        attempt = record_attempt_result(self.store, "run-1", outcome="failed")
        self.assertEqual(attempt["status"], "failed")
        # 无 attempt 的 run 安静返回 None。
        self.assertIsNone(record_attempt_result(self.store, "run-none", outcome="success"))

    def test_goal_context_survives_restart(self):
        """I1：崩溃后重开库仍能按 run 找回目标上下文。"""
        goal = create_goal(self.store, target=TARGET)
        register_attempt(self.store, goal["goal_id"], "run-crash")
        self.store.close()
        reopened = EventStore(self.db_path)
        try:
            attempt = reopened.get_goal_attempt_by_run("run-crash")
            self.assertIsNotNone(attempt)
            restored = reopened.get_goal(attempt["goal_id"])
            self.assertEqual(restored["status"], "active")
            self.assertEqual(restored["target"], TARGET)
        finally:
            reopened.close()
            self.store = EventStore(self.db_path)  # tearDown close 需要有效句柄


class AutoTriggerTests(unittest.TestCase):
    """I1：确定性触发——指标连续 N 个已结算评估窗口越界才建目标。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "trigger.sqlite3")
        self.store.start_run("run-t", mode="mock", scene="edca", model="openclaw")

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _collected_window(self, label, retries):
        due = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        record, _ = self.store.schedule_evaluation(
            "run-t", window_label=label, window_seconds=10.0, due_at=due,
            baseline={"ap2": {"tx_retries_ratio": 0.30}},
        )
        self.store.finish_evaluation(
            record["evaluation_id"], status="collected",
            observed={"ap2": {"tx_retries_ratio": retries}},
            verdict="neutral", confidence=0.8,
        )

    def test_trigger_requires_consecutive_violations(self):
        self._collected_window("w1", 0.30)
        self._collected_window("w2", 0.28)
        # 只有 2 个窗口 < 3 → 不触发。
        self.assertIsNone(auto_create_goal(self.store, target=TARGET, windows=3))
        self._collected_window("w3", 0.26)
        goal = auto_create_goal(self.store, target=TARGET, windows=3)
        self.assertIsNotNone(goal)
        self.assertEqual(goal["source"], "auto")
        self.assertEqual(len(goal["baseline"]["trigger_windows"]), 3)
        # 已有活跃目标 → 不再触发。
        self.assertIsNone(auto_create_goal(self.store, target=TARGET, windows=3))

    def test_trigger_aborts_when_any_window_met(self):
        self._collected_window("w1", 0.30)
        self._collected_window("w2", 0.10)  # 达标窗口
        self._collected_window("w3", 0.28)
        self.assertIsNone(auto_create_goal(self.store, target=TARGET, windows=3))


if __name__ == "__main__":
    unittest.main()

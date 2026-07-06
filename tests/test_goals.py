import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from src.memory.goals import (
    attribution_prompt, auto_create_goal, build_goal_context, create_goal,
    detect_oscillation, get_active_goal, goal_overview, metric_value,
    record_attempt_result, refresh_goal_after_evaluation, register_attempt,
    score_goal_progress, validate_target,
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


class IterationChainTests(unittest.TestCase):
    """I2 归因链、I3 提示注入、I4 停机准则。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "chain.sqlite3")

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _episode(self, run_id, decision):
        import copy
        from openclaw.scenes import MOCK_SCENES
        from src.memory import materialize_episode
        state = copy.deepcopy(MOCK_SCENES["edca"])
        self.store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        self.store.append_event(
            run_id, "session_start",
            {"model": "openclaw", "scene": "edca", "ap_state": state},
        )
        self.store.record_snapshot(run_id, label="initial", source="s", state=state)
        self.store.append_event(
            run_id, "final_decision", {"decision": decision, "raw_response": "{}"},
        )
        self.store.append_event(
            run_id, "validation_result",
            {"approved": True, "strategy": "co_edca", "summary": "ok"},
        )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        materialize_episode(self.store, run_id)

    def _window(self, run_id, label, retries):
        due = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        record, _ = self.store.schedule_evaluation(
            run_id, window_label=label, window_seconds=10.0, due_at=due,
            baseline={"ap2": {"tx_retries_ratio": 0.30}},
        )
        self.store.finish_evaluation(
            record["evaluation_id"], status="collected",
            observed={"ap2": {"tx_retries_ratio": retries}},
            verdict="neutral", confidence=0.8,
        )

    def test_attribution_chain_classifications(self):
        goal = create_goal(self.store, target=TARGET)
        first = register_attempt(self.store, goal["goal_id"], "run-1")
        self.assertEqual(first["attribution"], {})
        self._episode("run-1", {"ap2": {"CWmin": 15}})
        self.store.update_goal_attempt(
            first["attempt_id"],
            progress={"metric": goal["metric"], "value": 0.30, "met": False, "gap": 0.15},
            status="completed",
        )
        second = register_attempt(self.store, goal["goal_id"], "run-2")
        attribution = second["attribution"]
        self.assertEqual(attribution["previous_run_id"], "run-1")
        self.assertEqual(attribution["classification"], "first_probe")
        self.assertEqual(attribution["previous_action"], {"ap2": {"CWmin": 15}})

        self._episode("run-2", {"ap2": {"CWmin": 7}})
        self.store.update_goal_attempt(
            second["attempt_id"],
            progress={"metric": goal["metric"], "value": 0.20, "met": False, "gap": 0.05},
            status="completed",
        )
        third = register_attempt(self.store, goal["goal_id"], "run-3")
        self.assertEqual(third["attribution"]["classification"], "improved_but_insufficient")

        self.store.update_goal_attempt(
            third["attempt_id"],
            progress={"metric": goal["metric"], "value": 0.40, "met": False, "gap": 0.25},
            status="completed",
        )
        fourth = register_attempt(self.store, goal["goal_id"], "run-4")
        self.assertEqual(fourth["attribution"]["classification"], "worsened")

    def test_attribution_prompt_and_goal_context(self):
        goal = create_goal(self.store, target=TARGET)
        first = register_attempt(self.store, goal["goal_id"], "run-1")
        self.store.update_goal_attempt(
            first["attempt_id"],
            progress={"metric": goal["metric"], "value": 0.40, "met": False, "gap": 0.25},
            status="completed",
        )
        second = register_attempt(self.store, goal["goal_id"], "run-2")
        text = attribution_prompt(goal, second)
        self.assertIn("【迭代目标】ap2.tx_retries_ratio<=0.15（第 2/5 次尝试）", text)
        self.assertIn("归因分类=first_probe", text)
        self.assertIn("最小改动", text)
        context = build_goal_context(self.store, goal, second)
        self.assertEqual(context["sequence"], 2)
        self.assertIn("迭代目标", context["prompt"])

    def test_goal_context_injected_into_agent_message(self):
        from openclaw.mcp import orchestration as orch
        saved = orch._SESSION.goal_context
        try:
            orch._SESSION.goal_context = {"prompt": "【迭代目标】ap2.tx_retries_ratio<=0.15"}
            text = orch._build_agent_message("ap1", "", "请提案")
            self.assertIn("【迭代目标】", text)
            # 目标块位于指令之前、正文末尾，截断时优先保留。
            self.assertLess(text.index("【迭代目标】"), text.index("请提案"))
            orch._SESSION.goal_context = None
            self.assertNotIn("【迭代目标】", orch._build_agent_message("ap1", "", "请提案"))
        finally:
            orch._SESSION.goal_context = saved

    def test_stop_achieved_when_held_two_windows(self):
        goal = create_goal(self.store, target=TARGET)
        register_attempt(self.store, goal["goal_id"], "run-a")
        self._episode("run-a", {"ap2": {"CWmin": 7}})
        self._window("run-a", "w1", 0.12)
        self._window("run-a", "w2", 0.10)
        outcome = refresh_goal_after_evaluation(self.store, "run-a")
        self.assertEqual(outcome["goal_status"], "achieved")
        self.assertEqual(self.store.get_goal(goal["goal_id"])["status"], "achieved")

    def test_single_met_window_not_yet_achieved(self):
        goal = create_goal(self.store, target=TARGET, budget_attempts=5)
        register_attempt(self.store, goal["goal_id"], "run-a")
        self._episode("run-a", {"ap2": {"CWmin": 7}})
        self._window("run-a", "w1", 0.30)
        self._window("run-a", "w2", 0.10)
        outcome = refresh_goal_after_evaluation(self.store, "run-a")
        self.assertEqual(outcome["goal_status"], "active")
        attempt = self.store.get_goal_attempt_by_run("run-a")
        self.assertTrue(attempt["progress"]["met"])
        self.assertEqual(attempt["progress"]["windows_scored"], 2)

    def test_stop_budget_exhausted(self):
        goal = create_goal(self.store, target=TARGET, budget_attempts=1)
        register_attempt(self.store, goal["goal_id"], "run-a")
        self._episode("run-a", {"ap2": {"CWmin": 7}})
        record_attempt_result(self.store, "run-a", outcome="success")
        self._window("run-a", "w1", 0.30)
        outcome = refresh_goal_after_evaluation(self.store, "run-a")
        self.assertEqual(outcome["reason"], "budget_exhausted")
        goal_after = self.store.get_goal(goal["goal_id"])
        self.assertEqual(goal_after["status"], "blocked")
        self.assertIn("预算耗尽", goal_after["status_reason"])

    def test_stop_oscillation(self):
        self.assertTrue(detect_oscillation([
            {"ap1": {"CWmin": 15}}, {"ap1": {"CWmin": 31}}, {"ap1": {"CWmin": 15}},
        ]))
        self.assertFalse(detect_oscillation([
            {"ap1": {"CWmin": 15}}, {"ap1": {"CWmin": 31}}, {"ap1": {"CWmin": 63}},
        ]))
        self.assertFalse(detect_oscillation([None, {"ap1": {"CWmin": 15}}]))

        goal = create_goal(self.store, target=TARGET, budget_attempts=10)
        for index, cwmin in enumerate((15, 31, 15), start=1):
            run_id = f"run-{index}"
            register_attempt(self.store, goal["goal_id"], run_id)
            self._episode(run_id, {"ap2": {"CWmin": cwmin}})
            record_attempt_result(self.store, run_id, outcome="success")
        self._window("run-3", "w1", 0.30)
        outcome = refresh_goal_after_evaluation(self.store, "run-3")
        self.assertEqual(outcome["reason"], "oscillation")
        goal_after = self.store.get_goal(goal["goal_id"])
        self.assertEqual(goal_after["status"], "blocked")
        self.assertIn("振荡", goal_after["status_reason"])

    def test_refresh_disabled_by_switch(self):
        goal = create_goal(self.store, target=TARGET)
        register_attempt(self.store, goal["goal_id"], "run-a")
        self._episode("run-a", {"ap2": {"CWmin": 7}})
        self._window("run-a", "w1", 0.10)
        self._window("run-a", "w2", 0.10)
        with patch.dict(os.environ, {"MULTIAP_GOALS": "0"}):
            self.assertIsNone(refresh_goal_after_evaluation(self.store, "run-a"))
        self.assertEqual(self.store.get_goal(goal["goal_id"])["status"], "active")


if __name__ == "__main__":
    unittest.main()

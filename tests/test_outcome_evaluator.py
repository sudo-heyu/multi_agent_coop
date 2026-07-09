import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from tests.mock_scenes import MOCK_SCENES
from src.memory import (
    build_rollback_plan,
    classify,
    collect_due_evaluations,
    evaluate_deltas,
    find_similar_episodes,
    materialize_episode,
    parse_windows,
    schedule_outcome_evaluations,
)
from src.memory.outcome import summarize_agent_evaluations
from src.persistence import EventStore


DECISION = {ap: {"CWmin": 7, "CWmax": 31, "AIFSN": 3} for ap in ("ap1", "ap2", "ap3")}


def _shift(state, *, throughput=1.0, latency=1.0, loss=1.0):
    moved = copy.deepcopy(state)
    for row in moved.values():
        row["throughput_mbps_iperf"] = round(row["throughput_mbps_iperf"] * throughput, 3)
        row["throughput_mbps_user"] = round(row["throughput_mbps_user"] * throughput, 3)
        row["latency_ms"] = round(row["latency_ms"] * latency, 3)
        row["packet_loss_pct"] = round(row["packet_loss_pct"] * loss, 3)
    return moved


class OutcomeEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "outcome.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])
        self.t0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _completed_run(self, run_id, *, with_execution=True, with_observed=True):
        state = copy.deepcopy(self.baseline)
        self.store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        self.store.append_event(
            run_id, "session_start",
            {"model": "openclaw", "scene": "edca", "ap_state": state},
        )
        self.store.append_event(
            run_id, "final_decision", {"decision": DECISION, "raw_response": "{}"}
        )
        self.store.append_event(
            run_id, "validation_result",
            {"approved": True, "strategy": "co_edca", "summary": "ok"},
        )
        if with_execution:
            for ap in ("ap1", "ap2", "ap3"):
                self.store.append_event(
                    run_id, "executor_apply",
                    {"ap_id": ap, "ok": True, "url": "http://x", "payload": {}, "response": "ok"},
                )
        if with_observed:
            self.store.record_snapshot(
                run_id, label="final_observed", source="test", state=state
            )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        return materialize_episode(self.store, run_id)

    # ── 窗口定义与调度 ────────────────────────────────────────────────

    def test_parse_windows_dedupes_sorts_and_rejects_invalid(self):
        self.assertEqual(parse_windows("300, 60,300"), (60.0, 300.0))
        with self.assertRaises(ValueError):
            parse_windows("60,-5")
        with self.assertRaises(ValueError):
            parse_windows(" , ")

    def test_schedule_prefers_raw_initial_snapshot_over_filtered_baseline(self):
        # agent 白名单会滤掉 iperf 吞吐/延迟/丢包；评估基线必须用 initial 快照全量遥测。
        self._completed_run("run-snap")
        self.store.record_snapshot(
            "run-snap", label="initial", source="session_start",
            state=copy.deepcopy(self.baseline),
        )
        filtered = {
            ap: {"traffic_priority": row["traffic_priority"],
                 "throughput_mbps_user": row["throughput_mbps_user"]}
            for ap, row in self.baseline.items()
        }
        schedule_outcome_evaluations(self.store, "run-snap", filtered, (10.0,), now=self.t0)
        baseline = self.store.list_evaluations("run-snap")[0]["baseline"]
        self.assertIn("latency_ms", baseline["ap1"])
        self.assertIn("packet_loss_pct", baseline["ap1"])
        collected = collect_due_evaluations(
            self.store,
            lambda: _shift(self.baseline, throughput=1.3, latency=0.6, loss=0.4),
            now=self.t0 + timedelta(seconds=11),
        )
        self.assertEqual(collected[0]["verdict"], "improved")

    def test_schedule_is_idempotent_per_run_and_window(self):
        self._completed_run("run-a")
        schedule_outcome_evaluations(self.store, "run-a", self.baseline, (10.0, 30.0), now=self.t0)
        schedule_outcome_evaluations(self.store, "run-a", self.baseline, (10.0, 30.0), now=self.t0)
        rows = self.store.list_evaluations("run-a")
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["status"] for row in rows], ["pending", "pending"])
        self.assertEqual([row["window_label"] for row in rows], ["t+10s", "t+30s"])

    # ── 分类阈值 ─────────────────────────────────────────────────────

    def test_classify_improved_degraded_neutral(self):
        improved = evaluate_deltas(self.baseline, _shift(self.baseline, throughput=1.2, latency=0.7, loss=0.5))
        self.assertEqual(classify(improved)[0], "improved")
        degraded = evaluate_deltas(self.baseline, _shift(self.baseline, throughput=0.8, latency=1.5, loss=2.0))
        self.assertEqual(classify(degraded)[0], "degraded")
        neutral = evaluate_deltas(self.baseline, copy.deepcopy(self.baseline))
        verdict, confidence = classify(neutral)
        self.assertEqual(verdict, "neutral")
        self.assertEqual(confidence, 1.0)

    def test_missing_metrics_yield_inconclusive(self):
        observed = {
            ap: {"sta_rssi_dbm": row["sta_rssi_dbm"]}
            for ap, row in self.baseline.items()
        }
        deltas = evaluate_deltas(self.baseline, observed)
        verdict, confidence = classify(deltas)
        self.assertEqual(verdict, "inconclusive")
        self.assertEqual(confidence, 0.0)

    def test_priority_weighting_tolerates_low_priority_giveback(self):
        # 协商预期形态：high 优先级大幅改善，low 优先级小幅让出信道。
        observed = copy.deepcopy(self.baseline)
        for ap, row in observed.items():
            if self.baseline[ap]["traffic_priority"] == "high":
                row["throughput_mbps_iperf"] *= 1.25
                row["throughput_mbps_user"] *= 1.25
                row["latency_ms"] *= 0.6
                row["packet_loss_pct"] *= 0.45
            else:
                row["throughput_mbps_iperf"] *= 0.94
                row["throughput_mbps_user"] *= 0.94
                row["latency_ms"] *= 1.12
                row["packet_loss_pct"] *= 1.10
        verdict, _ = classify(evaluate_deltas(self.baseline, observed))
        self.assertEqual(verdict, "improved")

    # ── 收割与案例质量回写 ────────────────────────────────────────────

    def test_collect_only_due_windows_in_order(self):
        self._completed_run("run-due")
        schedule_outcome_evaluations(self.store, "run-due", self.baseline, (10.0, 30.0), now=self.t0)
        getter = lambda: copy.deepcopy(self.baseline)
        early = collect_due_evaluations(self.store, getter, now=self.t0 + timedelta(seconds=5))
        self.assertEqual(early, [])
        first = collect_due_evaluations(self.store, getter, now=self.t0 + timedelta(seconds=11))
        self.assertEqual([item["window_label"] for item in first], ["t+10s"])
        second = collect_due_evaluations(self.store, getter, now=self.t0 + timedelta(seconds=31))
        self.assertEqual([item["window_label"] for item in second], ["t+30s"])
        self.assertEqual(self.store.list_evaluations("run-due", status="pending"), [])

    def test_degraded_outcome_caps_quality_and_blocks_recall(self):
        episode = self._completed_run("run-bad")
        self.assertGreaterEqual(episode["quality_score"], 0.9)
        schedule_outcome_evaluations(self.store, "run-bad", self.baseline, (10.0,), now=self.t0)
        collect_due_evaluations(
            self.store,
            lambda: _shift(self.baseline, throughput=0.7, latency=1.8, loss=3.0),
            now=self.t0 + timedelta(seconds=11),
        )
        updated = self.store.get_episode(run_id="run-bad")
        self.assertEqual(updated["evaluation"]["final_verdict"], "degraded")
        self.assertTrue(updated["evaluation"]["needs_rollback"])
        self.assertLessEqual(updated["quality_score"], 0.2)
        # 劣化案例不再作为高质量参考进入提案注入（min_quality=0.5）
        recalled = find_similar_episodes(self.store, self.baseline, min_quality=0.5)
        self.assertEqual([item["run_id"] for item in recalled], [])

    def test_improved_outcome_boosts_quality_idempotently(self):
        episode = self._completed_run("run-good", with_execution=False, with_observed=False)
        base = episode["quality_score"]
        self.assertAlmostEqual(base, 0.8)
        schedule_outcome_evaluations(self.store, "run-good", self.baseline, (10.0, 30.0), now=self.t0)
        getter = lambda: _shift(self.baseline, throughput=1.3, latency=0.6, loss=0.4)
        collect_due_evaluations(self.store, getter, now=self.t0 + timedelta(seconds=11))
        once = self.store.get_episode(run_id="run-good")["quality_score"]
        self.assertGreater(once, base)
        collect_due_evaluations(self.store, getter, now=self.t0 + timedelta(seconds=31))
        twice = self.store.get_episode(run_id="run-good")
        # 从流水线基础分重算修订，重复评估不会叠加加成
        self.assertEqual(twice["quality_score"], once)
        self.assertEqual(twice["evaluation"]["final_verdict"], "improved")
        self.assertFalse(twice["evaluation"]["needs_rollback"])
        self.assertEqual(twice["evaluation"]["pending_windows"], 0)

    def test_rollback_plan_restores_only_changed_fields(self):
        plan = build_rollback_plan(self.baseline, DECISION)
        self.assertEqual(sorted(plan), ["ap1", "ap2", "ap3"])
        self.assertEqual(
            plan["ap1"],
            {
                "cwmin": self.baseline["ap1"]["cwmin"],
                "cwmax": self.baseline["ap1"]["cwmax"],
                "aifsn": self.baseline["ap1"]["aifsn"],
            },
        )
        self.assertNotIn("tx_power_dbm", plan["ap1"])

    def test_state_fetch_failure_keeps_windows_pending(self):
        self._completed_run("run-retry")
        schedule_outcome_evaluations(self.store, "run-retry", self.baseline, (10.0,), now=self.t0)

        def boom():
            raise RuntimeError("state server offline")

        with self.assertRaises(RuntimeError):
            collect_due_evaluations(self.store, boom, now=self.t0 + timedelta(seconds=11))
        pending = self.store.list_evaluations("run-retry", status="pending")
        self.assertEqual(len(pending), 1)

    def test_agent_verdicts_do_not_copy_global_credit(self):
        evaluations = [{
            "status": "collected", "window_label": "t+10s",
            "deltas": {"per_ap": {
                "ap1": {"score": 0.3, "metrics": {name: {} for name in range(4)}},
                "ap3": {"score": -0.3, "metrics": {name: {} for name in range(4)}},
            }},
        }]
        self.assertEqual(
            summarize_agent_evaluations(evaluations, "ap1")["final_verdict"], "improved"
        )
        self.assertEqual(
            summarize_agent_evaluations(evaluations, "ap3")["final_verdict"], "degraded"
        )


if __name__ == "__main__":
    unittest.main()

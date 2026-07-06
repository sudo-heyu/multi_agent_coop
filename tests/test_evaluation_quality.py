import copy
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from openclaw.scenes import MOCK_SCENES
from src.memory import (
    evaluation_diagnostics,
    materialize_episode,
    schedule_outcome_evaluations,
    summarize_run_evaluations,
)
from src.memory.outcome import collect_due_evaluations
from src.persistence import EventStore


def _shift(state, *, throughput=1.0, latency=1.0, loss=1.0):
    moved = copy.deepcopy(state)
    for row in moved.values():
        row["throughput_mbps_iperf"] = round(row["throughput_mbps_iperf"] * throughput, 3)
        row["throughput_mbps_user"] = round(row["throughput_mbps_user"] * throughput, 3)
        row["latency_ms"] = round(row["latency_ms"] * latency, 3)
        row["packet_loss_pct"] = round(row["packet_loss_pct"] * loss, 3)
    return moved


class CrossWindowConsistencyTests(unittest.TestCase):
    def _windows(self, *verdict_conf_score):
        # 构造伪 collected 评估记录列表。
        return [
            {
                "status": "collected",
                "run_id": "r",
                "window_label": f"t+{i}s",
                "verdict": v,
                "confidence": c,
                "collected_at": f"2026-07-03T12:00:0{i}Z",
                "deltas": {"score": s},
            }
            for i, (v, c, s) in enumerate(verdict_conf_score)
        ]

    def test_consistent_improvement_keeps_confidence(self):
        summary = summarize_run_evaluations(
            self._windows(("improved", 0.9, 0.12), ("improved", 0.8, 0.11))
        )
        self.assertEqual(summary["cross_window_consistency"], 1.0)
        self.assertEqual(summary["final_verdict"], "improved")
        self.assertEqual(summary["final_confidence"], 0.8)  # 末窗置信度 × 1.0

    def test_swinging_windows_reduce_confidence_and_block_rollback(self):
        # 改善/恶化/恶化：方向摇摆，degraded 主导但一致性仅 2/3。
        summary = summarize_run_evaluations(
            self._windows(("improved", 0.9, 0.2), ("degraded", 0.9, -0.2), ("degraded", 0.9, -0.2))
        )
        self.assertAlmostEqual(summary["cross_window_consistency"], round(2 / 3, 4))
        self.assertEqual(summary["final_verdict"], "degraded")
        # 0.9 × 0.667 = 0.6 ≥ 0.5 → 仍回滚（多数持续恶化）
        self.assertTrue(summary["needs_rollback"])

    def test_even_swing_does_not_trigger_rollback(self):
        # 1 改善 1 恶化：平票 → dominant 取 improved（保守），不回滚。
        summary = summarize_run_evaluations(
            self._windows(("improved", 0.9, 0.2), ("degraded", 0.9, -0.2))
        )
        self.assertEqual(summary["cross_window_consistency"], 0.5)
        self.assertEqual(summary["final_verdict"], "improved")
        self.assertFalse(summary["needs_rollback"])

    def test_low_confidence_swing_degraded_not_rolled_back(self):
        # 恶化主导但一致性把置信度压到阈值以下 → 不自动回滚。
        summary = summarize_run_evaluations(
            self._windows(("improved", 0.6, 0.1), ("improved", 0.6, 0.1),
                          ("degraded", 0.7, -0.1), ("degraded", 0.7, -0.1),
                          ("degraded", 0.7, -0.1))
        )
        # degraded 3/5 主导，一致性 0.6，末窗 0.7 × 0.6 = 0.42 < 0.5
        self.assertEqual(summary["final_verdict"], "degraded")
        self.assertLess(summary["final_confidence"], 0.5)
        self.assertFalse(summary["needs_rollback"])

    def test_all_neutral_consistency_one(self):
        summary = summarize_run_evaluations(
            self._windows(("neutral", 1.0, 0.0), ("neutral", 1.0, 0.0))
        )
        self.assertEqual(summary["cross_window_consistency"], 1.0)
        self.assertEqual(summary["final_verdict"], "neutral")


class ThresholdOverrideTests(unittest.TestCase):
    def test_env_var_overrides_threshold_on_reimport(self):
        with patch.dict(os.environ, {"MULTIAP_IMPROVE_THRESHOLD": "0.2"}):
            import src.memory.outcome as outcome
            importlib.reload(outcome)
            try:
                self.assertEqual(outcome.IMPROVE_THRESHOLD, 0.2)
                # score 0.1 在默认(0.05)下算 improved，覆盖为 0.2 后算 neutral
                verdict, _ = outcome.classify({"coverage": 1.0, "score": 0.1})
                self.assertEqual(verdict, "neutral")
            finally:
                importlib.reload(outcome)  # 还原默认，避免污染其它测试
        importlib.reload(outcome)
        self.assertEqual(outcome.IMPROVE_THRESHOLD, 0.05)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "diag.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])
        self.t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _run_with_window(self, run_id, observed):
        state = copy.deepcopy(self.baseline)
        self.store.start_run(run_id, mode="mock", scene="edca", model="m")
        self.store.append_event(run_id, "session_start",
                                {"model": "m", "scene": "edca", "ap_state": state})
        self.store.record_snapshot(run_id, label="initial", source="s", state=state)
        self.store.append_event(run_id, "final_decision",
                                {"decision": {"ap2": {"CWmin": 7}}, "raw_response": "{}"})
        self.store.append_event(run_id, "validation_result",
                                {"approved": True, "strategy": "co_edca", "summary": "ok"})
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        materialize_episode(self.store, run_id)
        schedule_outcome_evaluations(self.store, run_id, self.baseline, (10.0,), now=self.t0)
        collect_due_evaluations(self.store, lambda: observed, now=self.t0 + timedelta(seconds=11))

    def test_diagnostics_reports_distribution_and_thresholds(self):
        self._run_with_window("d1", _shift(self.baseline, throughput=1.3, latency=0.6, loss=0.4))
        self._run_with_window("d2", _shift(self.baseline, throughput=0.7, latency=1.8, loss=3.0))
        diag = evaluation_diagnostics(self.store)
        self.assertEqual(diag["collected_windows"], 2)
        self.assertEqual(diag["score_distribution"]["count"], 2)
        self.assertIn("improve", diag["active_thresholds"])
        self.assertEqual(diag["active_thresholds"]["improve"], 0.05)
        self.assertIn("improved", diag["verdict_counts"])
        self.assertIn("degraded", diag["verdict_counts"])

    def test_diagnostics_empty_store(self):
        diag = evaluation_diagnostics(self.store)
        self.assertEqual(diag["collected_windows"], 0)
        self.assertEqual(diag["score_distribution"], {"count": 0})


if __name__ == "__main__":
    unittest.main()

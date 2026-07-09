"""决策自预测的确定性单测（反思模块对"复用记忆"之外的补充闭环）。

背景：反思模块原本只对"复用的历史记忆"做预测-实测核账（R4），一次协商
即便完全没有依赖任何历史记忆（比如当场想出的新参数组合），也不会有任何
预测-实测记录进入 R5 校准表——2026-07-09 那次事故（Co-EDCA 把 ap1 压到
吞吐归零）就是这样：没有依赖任何历史记忆，反思模块对它完全失明。这里补的
是 reflection.predict_decision_verdicts / reconcile_decision_predictions：
复用 Validator 自伤门（层 3）已有的闭式估算反推方向性预测，跟评估窗口的
per-AP 实测得分核账，不管有没有依赖记忆、有没有挂 goal 都会跑。
"""
import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from tests.mock_scenes import MOCK_SCENES
from src.memory import materialize_episode
from src.memory.outcome import apply_evaluation_to_episode, evaluate_deltas
from src.memory.reflection import (
    calibration_report, predict_decision_verdicts, reconcile_decision_predictions,
)
from src.persistence import EventStore


INCIDENT_STATE = {
    "ap1": {"cwmin": 15, "cwmax": 1023, "aifsn": 3, "traffic_priority": "low"},
    "ap2": {"cwmin": 15, "cwmax": 1023, "aifsn": 3, "traffic_priority": "high"},
    "ap3": {"cwmin": 15, "cwmax": 1023, "aifsn": 3, "traffic_priority": "medium"},
}
INCIDENT_DECISION = {
    "ap1": {"CWmin": 32, "CWmax": 1023, "AIFSN": 7},
    "ap2": {"CWmin": 7, "CWmax": 15, "AIFSN": 2},
    "ap3": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},
}


class PredictDecisionVerdictsTests(unittest.TestCase):
    def test_reproduces_incident_direction(self):
        predictions = predict_decision_verdicts(INCIDENT_STATE, INCIDENT_DECISION, "co_edca")
        self.assertEqual(predictions["ap1:BE"], "degraded")
        self.assertEqual(predictions["ap2:BE"], "improved")
        self.assertNotIn("ap3:BE", predictions)  # ap3 参数未变，不预测

    def test_co_sr_power_delta_direction(self):
        state = {"ap1": {"tx_power_dbm": 20}, "ap2": {"tx_power_dbm": 20}}
        decision = {"ap1": {"tx_power_dbm": 10}, "ap2": {"tx_power_dbm": 20}}
        predictions = predict_decision_verdicts(state, decision, "co_sr")
        self.assertEqual(predictions["ap1:co_sr"], "degraded")
        self.assertNotIn("ap2:co_sr", predictions)  # ap2 功率未变

    def test_missing_strategy_returns_empty(self):
        self.assertEqual(predict_decision_verdicts(INCIDENT_STATE, INCIDENT_DECISION, None), {})


class ReconcileDecisionPredictionsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "decision.sqlite3")

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _episode(self, run_id, decision, strategy="co_edca", state=None):
        state = copy.deepcopy(state or INCIDENT_STATE)
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
            {"approved": True, "strategy": strategy, "summary": "ok"},
        )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        return materialize_episode(self.store, run_id)

    def _final_deltas(self, ap1_score):
        """构造一份 per-AP deltas：ap1 score 可控，ap2/ap3 恒为 0（neutral）。"""
        return {"per_ap": {
            "ap1": {"weight": 0.6, "score": ap1_score, "metrics": {}},
            "ap2": {"weight": 1.0, "score": 0.0, "metrics": {}},
            "ap3": {"weight": 0.6, "score": 0.0, "metrics": {}},
        }}

    def test_prediction_verified_when_matches_observed(self):
        episode = self._episode("run-verify", INCIDENT_DECISION)
        outcome = reconcile_decision_predictions(
            self.store, "run-verify", episode, self._final_deltas(ap1_score=-0.5),
        )
        self.assertEqual(outcome["processed"], 2)  # ap1:BE + ap2:BE
        self.assertEqual(outcome["verified"], 1)   # ap1 predicted degraded, observed degraded
        self.assertEqual(outcome["contradicted"], 0)

    def test_prediction_contradicted_when_opposite(self):
        episode = self._episode("run-contra", INCIDENT_DECISION)
        # ap1 被预测 degraded，但实测反而 improved（自伤门的启发式没预测准）。
        outcome = reconcile_decision_predictions(
            self.store, "run-contra", episode, self._final_deltas(ap1_score=0.5),
        )
        self.assertEqual(outcome["contradicted"], 1)

    def test_idempotent_on_repeat_harvest(self):
        episode = self._episode("run-idem", INCIDENT_DECISION)
        deltas = self._final_deltas(ap1_score=-0.5)
        first = reconcile_decision_predictions(self.store, "run-idem", episode, deltas)
        second = reconcile_decision_predictions(self.store, "run-idem", episode, deltas)
        self.assertEqual(first["verified"], 1)
        self.assertEqual(second["verified"], 0)  # 重复收割无重复副作用

    def test_no_final_deltas_is_noop(self):
        episode = self._episode("run-nodata", INCIDENT_DECISION)
        outcome = reconcile_decision_predictions(self.store, "run-nodata", episode, None)
        self.assertEqual(outcome["processed"], 0)

    def test_disabled_switch_skips(self):
        episode = self._episode("run-off", INCIDENT_DECISION)
        with patch.dict(os.environ, {"MULTIAP_REFLECTION": "0"}):
            outcome = reconcile_decision_predictions(
                self.store, "run-off", episode, self._final_deltas(ap1_score=-0.5),
            )
        self.assertEqual(outcome["processed"], 0)

    def test_calibration_report_breaks_down_by_kind(self):
        episode = self._episode("run-cal", INCIDENT_DECISION)
        reconcile_decision_predictions(
            self.store, "run-cal", episode, self._final_deltas(ap1_score=-0.5),
        )
        report = calibration_report(self.store)
        self.assertIn("decision_prediction", report["by_kind"])
        kind = report["by_kind"]["decision_prediction"]
        self.assertEqual(kind["verified"], 1)
        self.assertEqual(kind["hit_rate"], 1.0)
        # 没有信任分，理应落进 unknown_trust 桶，不进 low/mid/high。
        self.assertEqual(report["unknown_trust"]["verified"], 1)


class OutcomeHarvestIntegrationTests(unittest.TestCase):
    """验证 apply_evaluation_to_episode 无论有没有挂 goal、有没有依赖记忆，
    都会自动跑决策自预测核账——这是本次改造要解决的"只有特定路径才被看见"
    问题的直接回归测试。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "harvest.sqlite3")

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_decision_reconciliation_runs_without_goal_or_reliance(self):
        run_id = "run-plain"
        state = copy.deepcopy(INCIDENT_STATE)
        self.store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        self.store.append_event(
            run_id, "session_start",
            {"model": "openclaw", "scene": "edca", "ap_state": state},
        )
        self.store.record_snapshot(run_id, label="initial", source="s", state=state)
        self.store.append_event(
            run_id, "final_decision", {"decision": INCIDENT_DECISION, "raw_response": "{}"},
        )
        self.store.append_event(
            run_id, "validation_result",
            {"approved": True, "strategy": "co_edca", "summary": "ok"},
        )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        materialize_episode(self.store, run_id)
        # ap1 观测吞吐真的崩了：拿真实 evaluate_deltas 产出一份 degraded 的窗口。
        observed = copy.deepcopy(MOCK_SCENES["edca"])
        observed["ap1"] = {**observed.get("ap1", {}), "throughput_mbps_iperf": 0.0,
                            "throughput_mbps_user": 0.0, "tx_retries_ratio": 0.9,
                            "packet_loss_pct": 50.0, "latency_ms": 900.0}
        baseline = copy.deepcopy(MOCK_SCENES["edca"])
        deltas = evaluate_deltas(baseline, observed)
        due = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        record, _ = self.store.schedule_evaluation(
            run_id, window_label="w1", window_seconds=10.0, due_at=due, baseline=baseline,
        )
        self.store.finish_evaluation(
            record["evaluation_id"], status="collected",
            observed=observed, deltas=deltas, verdict="degraded", confidence=0.9,
        )
        summary = apply_evaluation_to_episode(self.store, run_id)
        self.assertIn("decision_reconciliation", summary)
        self.assertGreaterEqual(summary["decision_reconciliation"]["processed"], 1)
        # 没挂 goal：goal_progress 应为 None，证明这条核账确实不依赖迭代模块。
        self.assertIsNone(summary["goal_progress"])
        # 没有 memory_reliance 事件：R4 原有闭环应为空，证明这是补上的独立信号。
        self.assertEqual(summary["memory_reconciliation"], {
            "processed": 0, "verified": 0, "contradicted": 0, "quarantined": [],
        })


if __name__ == "__main__":
    unittest.main()

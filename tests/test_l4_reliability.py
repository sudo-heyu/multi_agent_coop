import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from openclaw.scenes import MOCK_SCENES
from src.memory import (
    abandon_stale_evaluations,
    execute_rollback,
    harvest_evaluations,
    materialize_episode,
    schedule_outcome_evaluations,
)
from src.memory.outcome import apply_evaluation_to_episode
from src.persistence import EventStore


DECISION = {ap: {"CWmin": 15, "CWmax": 63, "AIFSN": 3} for ap in ("ap1", "ap2", "ap3")}


def _degraded(state):
    moved = copy.deepcopy(state)
    for row in moved.values():
        row["throughput_mbps_iperf"] *= 0.7
        row["throughput_mbps_user"] *= 0.7
        row["latency_ms"] *= 1.9
        row["packet_loss_pct"] *= 3.0
    return moved


class L4ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "l4.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])
        self.t0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _run(self, run_id, *, decision=DECISION):
        state = copy.deepcopy(self.baseline)
        self.store.start_run(run_id, mode="real", scene="edca", model="openclaw")
        self.store.append_event(
            run_id, "session_start",
            {"model": "openclaw", "scene": "edca", "ap_state": state},
        )
        self.store.record_snapshot(run_id, label="initial", source="session_start", state=state)
        self.store.append_event(run_id, "final_decision", {"decision": decision, "raw_response": "{}"})
        self.store.append_event(
            run_id, "validation_result",
            {"approved": True, "strategy": "co_edca", "summary": "ok"},
        )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        return materialize_episode(self.store, run_id)

    # ── pending 放弃 ─────────────────────────────────────────────────

    def test_abandon_only_windows_past_grace(self):
        self._run("run-ab")
        # 窗口 60s：grace = max(60*4, 3600) = 3600s。
        schedule_outcome_evaluations(self.store, "run-ab", self.baseline, (60.0,), now=self.t0)
        # 刚过 due 但未过 grace：不放弃。
        early = abandon_stale_evaluations(self.store, now=self.t0 + timedelta(seconds=120))
        self.assertEqual(early, [])
        self.assertEqual(len(self.store.list_evaluations("run-ab", status="pending")), 1)
        # 过 grace：放弃。
        late = abandon_stale_evaluations(self.store, now=self.t0 + timedelta(seconds=3700))
        self.assertEqual([i["window_label"] for i in late], ["t+60s"])
        self.assertEqual(late[0]["status"], "abandoned")
        self.assertEqual(self.store.list_evaluations("run-ab", status="pending"), [])

    def test_harvest_abandons_even_when_state_fetch_fails(self):
        self._run("run-hv")
        schedule_outcome_evaluations(self.store, "run-hv", self.baseline, (60.0,), now=self.t0)

        def boom():
            raise RuntimeError("state server offline")

        outcome = harvest_evaluations(self.store, boom, now=self.t0 + timedelta(seconds=3700))
        self.assertIn("error", outcome)
        self.assertEqual(outcome["collected"], [])
        self.assertEqual([i["window_label"] for i in outcome["abandoned"]], ["t+60s"])
        self.assertEqual(self.store.list_evaluations("run-hv", status="pending"), [])

    def test_stale_window_is_still_collected_when_state_available(self):
        # abandon 是"拿不到数据"的兜底，不是"逾期即弃"：只要 state 可用，
        # 即便早已过 grace 的窗口也照常收割（稳态近似），不放弃。
        self._run("run-mix")
        schedule_outcome_evaluations(
            self.store, "run-mix", self.baseline, (900.0,),
            now=self.t0 - timedelta(seconds=5000),
        )
        outcome = harvest_evaluations(
            self.store,
            lambda: copy.deepcopy(self.baseline),
            now=self.t0,
        )
        self.assertEqual([i["window_label"] for i in outcome["collected"]], ["t+900s"])
        self.assertEqual(outcome["abandoned"], [])

    def test_due_window_can_only_be_claimed_by_one_harvester(self):
        self._run("run-claim")
        schedule_outcome_evaluations(
            self.store, "run-claim", self.baseline, (10.0,), now=self.t0
        )
        due = (self.t0 + timedelta(seconds=11)).isoformat(timespec="milliseconds")
        first = self.store.claim_due_evaluations(due_before=due, claimant="worker-1")
        second = self.store.claim_due_evaluations(due_before=due, claimant="worker-2")
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.store.release_evaluation_claims(
            [first[0]["evaluation_id"]], "worker-1"
        )
        self.assertEqual(
            len(self.store.claim_due_evaluations(due_before=due, claimant="worker-2")), 1
        )

    def test_later_negotiation_marks_older_window_confounded(self):
        self._run("older")
        schedule_outcome_evaluations(
            self.store, "older", self.baseline, (60.0,), now=self.t0
        )
        self._run("newer")
        # Test fixtures use wall-clock completed_at; make the evaluation creation precede it.
        with self.store._lock, self.store._conn:
            self.store._conn.execute(
                "UPDATE outcome_evaluations SET created_at=? WHERE run_id='older'",
                ((self.t0 - timedelta(seconds=1)).isoformat(timespec="milliseconds"),),
            )
        result = harvest_evaluations(
            self.store, lambda: copy.deepcopy(self.baseline),
            now=datetime.now(timezone.utc) + timedelta(seconds=61),
        )
        self.assertEqual(result["collected"][0]["verdict"], "inconclusive")
        self.assertTrue(result["collected"][0]["deltas"]["confounded"])

    # ── 回滚执行通道 ─────────────────────────────────────────────────

    def _degraded_episode(self, run_id):
        self._run(run_id)
        schedule_outcome_evaluations(self.store, run_id, self.baseline, (10.0,), now=self.t0)
        harvest_evaluations(
            self.store,
            lambda: _degraded(self.baseline),
            now=self.t0 + timedelta(seconds=11),
        )
        return self.store.get_episode(run_id=run_id)

    def test_degraded_episode_flags_rollback(self):
        ep = self._degraded_episode("run-deg")
        self.assertEqual(ep["evaluation"]["final_verdict"], "degraded")
        self.assertTrue(ep["evaluation"]["needs_rollback"])
        self.assertIn("rollback_plan", ep["evaluation"])
        self.assertEqual(ep["lifecycle"], "warning")
        self.assertGreater(ep["quality_vector"]["metric_coverage"], 0)
        self.assertIn(ep["case_narrative_status"], {"disabled", "failed", "ready"})
        local = self.store.list_agent_episodes("ap1", min_quality=0.0)
        self.assertEqual(local[0]["evaluation"]["final_verdict"], "degraded")
        self.assertEqual(local[0]["quality_score"], ep["quality_score"])

    def test_missing_local_metrics_never_inherit_global_verdict(self):
        self._run("run-local-missing")
        schedule_outcome_evaluations(
            self.store, "run-local-missing", self.baseline, (10.0,), now=self.t0
        )
        observed = copy.deepcopy(self.baseline)
        observed.pop("ap3")
        harvest_evaluations(
            self.store, lambda: observed, now=self.t0 + timedelta(seconds=11)
        )
        ap3 = self.store.list_agent_episodes("ap3", min_quality=0.0)[0]
        self.assertEqual(ap3["evaluation"]["final_verdict"], "inconclusive")
        self.assertIn("禁止继承全局评价", ap3["evaluation"]["reason"])

    def test_dry_run_sends_nothing_and_returns_plan(self):
        self._degraded_episode("run-dry")
        with patch("requests.post") as post:
            result = execute_rollback(self.store, "run-dry", None, confirm=False)
        self.assertEqual(result["mode"], "dry_run")
        self.assertTrue(result["ok"])
        self.assertIn("ap1", result["plan"])
        post.assert_not_called()

    def test_confirm_requires_endpoints(self):
        self._degraded_episode("run-noep")
        result = execute_rollback(self.store, "run-noep", None, confirm=True)
        self.assertFalse(result["ok"])
        self.assertIn("ap-endpoints", result["error"])

    @patch("requests.post")
    def test_confirmed_rollback_applies_and_is_idempotent(self, post):
        post.return_value = Mock(
            status_code=200, json=Mock(return_value={"details": "applied"}), text="applied"
        )
        self._degraded_episode("run-exec")
        endpoints = {ap: f"http://10.0.0.{i}:5002" for i, ap in enumerate(("ap1", "ap2", "ap3"), 1)}
        first = execute_rollback(self.store, "run-exec", endpoints, confirm=True)
        second = execute_rollback(self.store, "run-exec", endpoints, confirm=True)
        self.assertTrue(first["ok"])
        self.assertEqual(first["mode"], "executed")
        self.assertTrue(all(r["ok"] for r in first["results"].values()))
        self.assertTrue(second["results"]["ap1"].get("cached"))
        # 3 个 AP 各发一次，第二次全走幂等缓存 → 仍是 3 次。
        self.assertEqual(post.call_count, 3)

    @patch("requests.post")
    def test_rollback_payload_uses_prewindow_params_without_reencoding(self, post):
        post.return_value = Mock(
            status_code=200, json=Mock(return_value={"details": "applied"}), text="applied"
        )
        self._degraded_episode("run-wire")
        endpoints = {"ap1": "http://10.0.0.1:5002", "ap2": "http://10.0.0.2:5002",
                     "ap3": "http://10.0.0.3:5002"}
        execute_rollback(self.store, "run-wire", endpoints, confirm=True)
        sent = {call.kwargs["json"]["ap_id"]: call.kwargs["json"]["params"]
                for call in post.call_args_list}
        # 回滚下发的是协商前的原始上报值（指数格式），不是决策值，也不再二次编码。
        self.assertEqual(sent["ap1"]["cwmin"], self.baseline["ap1"]["cwmin"])
        self.assertEqual(sent["ap1"]["cwmax"], self.baseline["ap1"]["cwmax"])

    def test_rollback_missing_episode(self):
        result = execute_rollback(self.store, "nope", None, confirm=False)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()

"""参数时间线：一次协商从头到尾的参数变化全程保留与合并回放。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

import src.logger as logger_module
from src.logger import SessionLogger, _extract_ap_parameters
from src.memory.observability import parameter_timeline
from src.persistence import EventStore


class ExtractParametersTests(unittest.TestCase):
    def test_covers_per_ac_and_obss_pd_fields(self):
        params = _extract_ap_parameters({
            "ap1": {"tx_power_dbm": 16, "obss_pd_dbm": -70,
                    "vi_cwmin": 3, "VI_CWmax": 15, "be_aifsn": 3,
                    "CWmin": 7, "irrelevant": "x"},
        })
        self.assertEqual(params["ap1"], {
            "tx_power_dbm": 16, "obss_pd_dbm": -70,
            "vi_cwmin": 3, "VI_CWmax": 15, "be_aifsn": 3, "CWmin": 7,
        })


class ParameterTimelineTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.store = EventStore(root / "timeline.sqlite3")
        self._patches = [
            patch.object(logger_module, "LOG_DIR", root / "logs"),
            patch.object(logger_module, "STATE_LOG_DIR", root / "logs" / "state"),
        ]
        for p in self._patches:
            p.start()
        self.logger = SessionLogger(
            session_id="tl-run", verbose=False, mode="mock", event_store=self.store,
        )

    def tearDown(self):
        self.logger.close()
        for p in self._patches:
            p.stop()
        self.store.close()
        self._td.cleanup()

    def _state(self, cwmin_exp, tx=16, retries=0.3):
        return {
            "ap1": {"tx_power_dbm": tx, "cwmin": cwmin_exp, "cwmax": 10,
                    "aifsn": 3, "tx_retries_ratio": retries},
            "ap2": {"tx_power_dbm": tx, "cwmin": cwmin_exp, "cwmax": 10,
                    "aifsn": 3, "tx_retries_ratio": retries},
        }

    def test_record_proposal_dual_writes(self):
        self.logger.session_start("openclaw", "edca", self._state(4))
        self.logger.record_proposal(
            1, "ap1", "co_edca", {"ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3}},
        )
        events = self.store.load_events("tl-run")
        proposal_events = [e for e in events if e["event"] == "proposal_params"]
        self.assertEqual(len(proposal_events), 1)
        self.assertEqual(proposal_events[0]["proposal_num"], 1)
        self.assertEqual(proposal_events[0]["kind"], "proposal")
        self.assertEqual(proposal_events[0]["parameters"]["ap1"]["CWmin"], 15)
        # state_trace JSONL 同步保留一行结构化参数。
        trace_rows = [
            json.loads(line)
            for line in self.logger.state_trace_path.read_text().splitlines()
        ]
        self.assertIn("proposal_params", [row["event"] for row in trace_rows])

    def test_timeline_merges_all_stages_with_diffs(self):
        self.logger.session_start("openclaw", "edca", self._state(4))
        self.logger.record_proposal(
            1, "ap1", "co_edca", {"ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3}},
        )
        self.logger.record_proposal(
            2, "ap2", "co_edca", {"ap1": {"CWmin": 7, "CWmax": 63, "AIFSN": 3}},
            kind="counter",
        )
        self.logger.final_decision(
            {"ap1": {"CWmin": 7, "CWmax": 63, "AIFSN": 3}}, raw_response="{}",
        )
        self.logger.record_executor_apply(
            "ap1", ok=True, url="http://ap1/apply",
            payload={"CWmin": 7, "CWmax": 63, "AIFSN": 3}, response={"ok": True},
        )
        self.logger.validation_result(
            {"approved": True, "strategy": "co_edca", "parse_ok": True,
             "per_ap": {}, "global_errors": [], "summary": "ok"},
        )
        due = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        record, _ = self.store.schedule_evaluation(
            "tl-run", window_label="t+10s", window_seconds=10.0, due_at=due,
            baseline=self._state(4),
        )
        self.store.finish_evaluation(
            record["evaluation_id"], status="collected",
            observed=self._state(2, retries=0.1), verdict="improved", confidence=0.9,
        )

        timeline = parameter_timeline(self.store, "tl-run")
        stages = [entry["stage"] for entry in timeline]
        self.assertEqual(stages, [
            "snapshot:initial",
            "proposal#1(proposal,ap1)",
            "proposal#2(counter,ap2)",
            "final_decision",
            "executor_apply:ap1",
            "validation",
            "evaluation:t+10s",
        ])
        # target 空间：反提案相对提案#1 的变化被 diff 出来。
        counter = timeline[2]
        self.assertEqual(counter["changes"]["ap1"]["CWmin"], {"from": 15, "to": 7})
        # observed 空间：评估窗口相对初始快照的变化（不与 target 交叉比较）。
        evaluation = timeline[6]
        self.assertEqual(evaluation["changes"]["ap1"]["cwmin"], {"from": 4, "to": 2})
        self.assertEqual(
            evaluation["changes"]["ap1"]["tx_retries_ratio"],
            {"from": 0.3, "to": 0.1},
        )
        # final_decision 与反提案参数相同 → target 空间无新变化。
        self.assertNotIn("changes", timeline[3])

    def test_run_trace_helpers_are_best_effort(self):
        import run_openclaw

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"path": "/tmp/trace.jsonl"}

        with patch("requests.post", return_value=_Resp()):
            self.assertEqual(
                run_openclaw._start_run_trace("http://localhost:5001", "r1"),
                "/tmp/trace.jsonl",
            )
        with patch("requests.post", side_effect=OSError("down")):
            self.assertIsNone(
                run_openclaw._start_run_trace("http://localhost:5001", "r1")
            )
            run_openclaw._stop_run_trace("http://localhost:5001")  # 不抛异常


if __name__ == "__main__":
    unittest.main()

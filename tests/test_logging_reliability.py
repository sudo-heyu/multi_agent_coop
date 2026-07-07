"""日志可靠性：工具调用落盘、失败终态记录、SQLite 降级、goal_context 透传。"""

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

import src.logger as logger_module
from src.logger import SessionLogger
from src.persistence import EventStore
from openclaw.mcp import orchestration as orch


class LoggingReliabilityTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.store = EventStore(root / "rel.sqlite3")
        self._patches = [
            patch.object(logger_module, "LOG_DIR", root / "logs"),
            patch.object(logger_module, "STATE_LOG_DIR", root / "logs" / "state"),
        ]
        for p in self._patches:
            p.start()
        self.logger = SessionLogger(
            session_id="rel-run", verbose=False, mode="mock", event_store=self.store,
        )
        self.logger.session_start("openclaw", "edca", {"ap1": {"tx_power_dbm": 16}})

    def tearDown(self):
        self.logger.close()
        for p in self._patches:
            p.stop()
        self.store.close()
        self._td.cleanup()

    def _jsonl_rows(self):
        return [json.loads(line)
                for line in self.logger.log_path.read_text().splitlines()]

    def test_store_write_failure_degrades_to_jsonl(self):
        with patch.object(
            self.store, "append_event", side_effect=OSError("disk full")
        ):
            self.logger.phase_start(1, "广播")  # 不抛异常
        row = self._jsonl_rows()[-1]
        self.assertEqual(row["event"], "phase_start")
        self.assertIn("disk full", row["store_write_failed"])
        self.assertIsNone(row["sequence"])
        # 库恢复后继续正常双写。
        self.logger.phase_start(2, "提案")
        self.assertNotIn("store_write_failed", self._jsonl_rows()[-1])

    def test_mcp_tool_call_and_retry_dual_write(self):
        self.logger.mcp_tool_call(
            "ap1", "validate_edca_proposal",
            {"proposed_edca": {"ap1": {"CWmin": 7}}},
            {"valid": True, "errors": []}, 42.5,
        )
        self.logger.agent_turn_retry("ap2", 1, "空回复(payloads=0)", via_gateway=False)
        events = {e["event"]: e for e in self.store.load_events("rel-run")}
        self.assertEqual(events["mcp_tool_call"]["tool"], "validate_edca_proposal")
        self.assertEqual(events["mcp_tool_call"]["result"]["valid"], True)
        self.assertEqual(events["agent_turn_retry"]["error"], "空回复(payloads=0)")
        jsonl_events = [row["event"] for row in self._jsonl_rows()]
        self.assertIn("mcp_tool_call", jsonl_events)
        self.assertIn("agent_turn_retry", jsonl_events)

    def test_session_failed_keeps_run_resumable(self):
        self.logger.session_failed("RuntimeError: drive_ap(ap1) 连续 3 次失败",
                                   traceback_text="Traceback ...")
        events = [e for e in self.store.load_events("rel-run")
                  if e["event"] == "session_failed"]
        self.assertEqual(len(events), 1)
        self.assertIn("drive_ap", events[0]["error"])
        # run 仍是 incomplete —— 不因失败事件而失去可恢复性。
        incomplete = [run.run_id for run in self.store.list_incomplete_runs()]
        self.assertIn("rel-run", incomplete)

    def test_log_mcp_tool_uses_active_logger_and_never_raises(self):
        saved = orch._tool_logger
        try:
            orch._tool_logger = self.logger
            orch._log_mcp_tool("ap1", "get_latest_ap_states", {}, {"ap1": {}}, 5.0)
            events = [e for e in self.store.load_events("rel-run")
                      if e["event"] == "mcp_tool_call"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["agent"], "ap1")
            # 无活跃 logger → 静默；logger 抛错 → 吞掉不打断协商。
            orch._tool_logger = None
            orch._log_mcp_tool("ap1", "x", {}, {}, None)

            class _Boom:
                def mcp_tool_call(self, *a, **k):
                    raise RuntimeError("boom")

            orch._tool_logger = _Boom()
            orch._log_mcp_tool("ap1", "x", {}, {}, None)  # 不抛
        finally:
            orch._tool_logger = saved

    def test_structured_relay_accepts_goal_context(self):
        self.assertIn("goal_context", inspect.signature(orch.structured_relay).parameters)


if __name__ == "__main__":
    unittest.main()

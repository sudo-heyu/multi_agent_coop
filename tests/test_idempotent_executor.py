import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openclaw.mcp import orchestration as orch
from src import logger as logger_module
from src.logger import SessionLogger
from src.persistence import EventStore, build_checkpoint


class IdempotentExecutorTests(unittest.TestCase):
    def _logger(self, root: Path, session_id: str):
        store = EventStore(root / "events.sqlite3")
        patches = (
            patch.object(logger_module, "LOG_DIR", root / "logs"),
            patch.object(logger_module, "STATE_LOG_DIR", root / "logs" / "state"),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        logger = SessionLogger(
            session_id=session_id,
            verbose=False,
            mode="real",
            event_store=store,
        )
        logger.session_start("openclaw", "edca", {"ap1": {"source": "ap"}})
        return logger

    @patch("requests.post")
    def test_successful_apply_is_not_sent_twice(self, post):
        post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"details": "applied"}),
            text="applied",
        )
        with tempfile.TemporaryDirectory() as td:
            logger = self._logger(Path(td), "idem-success")
            decision = {"ap1": {"CWmin": 7, "CWmax": 15, "AIFSN": 2}}
            endpoints = {"ap1": "http://10.0.0.1:5002"}

            first = orch._push_decision(
                decision, "co_edca", endpoints, logger.session_id, logger
            )
            second = orch._push_decision(
                decision, "co_edca", endpoints, logger.session_id, logger
            )
            logger.session_end("success", 1)

        self.assertTrue(first["ap1"]["ok"])
        self.assertTrue(second["ap1"]["ok"])
        self.assertEqual(post.call_count, 1)

    @patch("requests.post", side_effect=TimeoutError("response lost"))
    def test_uncertain_apply_is_not_retried_and_blocks_resume(self, post):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            logger = self._logger(root, "idem-unknown")
            decision = {"ap1": {"tx_power_dbm": 10}}
            endpoints = {"ap1": "http://10.0.0.1:5002"}

            first = orch._push_decision(
                decision, "co_sr", endpoints, logger.session_id, logger
            )
            second = orch._push_decision(
                decision, "co_sr", endpoints, logger.session_id, logger
            )
            # Simulate crash: keep run incomplete while closing file/DB handles.
            logger.close()
            store = EventStore(root / "events.sqlite3")
            checkpoint = build_checkpoint(store, "idem-unknown")
            store.close()

        self.assertFalse(first["ap1"]["ok"])
        self.assertFalse(second["ap1"]["ok"])
        self.assertEqual(post.call_count, 1)
        self.assertFalse(checkpoint.can_resume)
        self.assertIn("manual reconciliation", checkpoint.resume_reason)

    @patch("requests.post")
    def test_definitive_http_failure_respects_retry_budget(self, post):
        post.return_value = Mock(
            status_code=500,
            json=Mock(return_value={"error": "rejected"}),
            text="rejected",
        )
        with tempfile.TemporaryDirectory() as td:
            logger = self._logger(Path(td), "idem-retry")
            decision = {"ap1": {"tx_power_dbm": 10}}
            endpoints = {"ap1": "http://10.0.0.1:5002"}

            for _ in range(3):
                result = orch._push_decision(
                    decision, "co_sr", endpoints, logger.session_id, logger
                )
            logger.session_end("executor_failed", 1)

        self.assertFalse(result["ap1"]["ok"])
        self.assertIn("最大尝试次数", result["ap1"]["response"])
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()

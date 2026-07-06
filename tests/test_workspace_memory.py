import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.memory.workspace import (
    archive_current_session, load_current_session, save_current_session,
    save_long_term_memory,
)
from openclaw.mcp import orchestration as orch


class WorkspaceMemoryTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._env = patch.dict(
            os.environ, {"MULTIAP_AGENT_WORKSPACES_ROOT": self._td.name}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def test_session_memory_is_physically_isolated_by_workspace(self):
        save_current_session(
            "ap1", memory={"version": 1, "summarized_turns": 0, "entries": []},
            summary_text="AP1 摘要",
            local_transcript=[{"speaker": "PRIVATE_MEMORY", "content": "secret-ap1"}],
            private_sla={"min_throughput_mbps": 100}, budget_chars=2400,
            run_id="run-1",
            memory_revision=1,
        )
        ap1 = Path(self._td.name) / "ap1" / "memory" / "current-session.md"
        ap2 = Path(self._td.name) / "ap2" / "memory" / "current-session.md"
        self.assertTrue(ap1.exists())
        self.assertFalse(ap2.exists())
        self.assertIn("min_throughput_mbps", ap1.read_text(encoding="utf-8"))
        self.assertEqual(load_current_session("ap1", run_id="run-1")["agent_id"], "ap1")
        self.assertIsNone(load_current_session("ap1", run_id="another-run"))

    def test_long_term_memory_contains_only_target_agents_local_case(self):
        save_long_term_memory("ap2", [{
            "run_id": "r1", "scene": "edca", "strategy": "co_edca",
            "local_state": {"traffic_priority": "high"},
            "local_decision": {"CWmin": 3}, "quality_score": 0.9,
            "evaluation": {"final_verdict": "improved", "final_confidence": 0.8},
        }])
        text = (Path(self._td.name) / "ap2" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("CWmin", text)
        self.assertIn("improved", text)
        self.assertFalse((Path(self._td.name) / "ap1" / "MEMORY.md").exists())

    def test_local_degraded_case_is_rendered_as_failure_warning(self):
        save_long_term_memory("ap1", [{
            "run_id": "bad-local", "scene": "edca", "strategy": "co_edca",
            "local_state": {}, "local_decision": {"CWmin": 63}, "quality_score": 0.2,
            "evaluation": {"final_verdict": "degraded", "final_confidence": 0.8,
                           "global_verdict": "improved", "global_local_conflict": True},
        }])
        text = (Path(self._td.name) / "ap1" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("本地失败警告", text)
        self.assertIn("degraded", text)

    def test_system_refresh_preserves_agents_autonomous_notes(self):
        save_long_term_memory("ap1", [])
        path = Path(self._td.name) / "ap1" / "MEMORY.md"
        text = path.read_text(encoding="utf-8").replace(
            "（可由本 Agent 在此维护不包含敏感凭据的持久经验。）",
            "高负载时优先复核重传率。",
        )
        path.write_text(text, encoding="utf-8")
        save_long_term_memory("ap1", [])
        self.assertIn("高负载时优先复核重传率", path.read_text(encoding="utf-8"))

    def test_autonomous_notes_are_bounded_and_secrets_redacted(self):
        save_long_term_memory("ap3", [])
        path = Path(self._td.name) / "ap3" / "MEMORY.md"
        text = path.read_text(encoding="utf-8").replace(
            "（可由本 Agent 在此维护不包含敏感凭据的持久经验。）",
            "token=abc123 " + "x" * 5000,
        )
        path.write_text(text, encoding="utf-8")
        save_long_term_memory("ap3", [])
        refreshed = path.read_text(encoding="utf-8")
        self.assertNotIn("abc123", refreshed)
        self.assertIn("[REDACTED]", refreshed)
        self.assertLess(len(refreshed), 5000)

    def test_workspace_memory_is_reliably_injected_without_duplicate_transcript(self):
        save_long_term_memory("ap1", [])
        message = orch._build_agent_message("ap1", "AP2: public-turn", "请投票")
        self.assertIn("本 Agent 工作区记忆", message)
        self.assertIn("Agent 自主笔记", message)
        self.assertEqual(message.count("public-turn"), 1)

    def test_shared_warning_is_visible_to_every_agent_message(self):
        warning = {"strategy": "co_sr", "decision": {"ap1": {"tx_power_dbm": 4}},
                   "evaluation": {"final_confidence": 0.9}}
        for agent in ("ap1", "ap2", "ap3"):
            message = orch._build_agent_message(
                agent, "", "请判断", shared_warnings=[warning]
            )
            self.assertIn("共享失败警告", message)
            self.assertIn("tx_power_dbm", message)

    def test_completed_session_is_archived_by_run_id(self):
        save_current_session(
            "ap1", memory={"version": 1, "revision": 0,
                            "summarized_turns": 0, "entries": []},
            summary_text="done", local_transcript=[], private_sla=None,
            budget_chars=2400, run_id="finished-run", memory_revision=1,
        )
        self.assertTrue(archive_current_session("ap1", "finished-run", "success"))
        archived = Path(self._td.name) / "ap1" / "memory" / "sessions" / "finished-run.json"
        self.assertEqual(json.loads(archived.read_text())["outcome"], "success")


if __name__ == "__main__":
    unittest.main()

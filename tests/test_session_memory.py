import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from src.memory import SessionMemory, SessionMemoryManager
from src.persistence import EventStore, build_checkpoint
from openclaw.mcp import orchestration as orch


def messages(count: int, size: int = 420):
    return [
        {"speaker": f"AP{index % 3 + 1}", "content": f"广播 turn {index} " + "x" * size}
        for index in range(count)
    ]


class SessionMemoryTests(unittest.TestCase):
    def test_external_refinement_is_opt_in_and_excludes_private_entries(self):
        manager = SessionMemoryManager(allow_llm=True)
        manager.memory.entries = [
            {"turn": 0, "speaker": "PRIVATE_MEMORY", "kind": "private_constraint", "digest": "secret"},
            *[
                {"turn": i, "speaker": "AP1", "kind": "message", "digest": f"public-{i}"}
                for i in range(1, 14)
            ],
        ]
        with patch("src.memory.llm_backend.session_enabled", return_value=True), patch(
            "src.memory.session_memory.threading.Thread"
        ) as thread:
            manager._schedule_semantic_refinement()
        batch = thread.call_args.kwargs.get("args", thread.call_args.args[0] if thread.call_args.args else ())[0]
        self.assertTrue(batch)
        self.assertNotIn("PRIVATE_MEMORY", {item["speaker"] for item in batch})

    def test_stale_refinement_cannot_overwrite_newer_revision(self):
        manager = SessionMemoryManager(allow_llm=True)
        batch = [
            {"turn": i, "speaker": "AP1", "kind": "message", "digest": f"v{i}"}
            for i in range(6)
        ]
        manager.memory.entries = list(batch)
        manager.memory.revision = 2
        with patch("src.memory.llm_backend.summarize", return_value="summary"):
            manager._refine_batch(batch, expected_revision=1, source_hash="old")
        self.assertEqual(manager.memory.entries, batch)

    def setUp(self):
        self._workspace_td = tempfile.TemporaryDirectory()
        self._workspace_patch = patch.dict(
            os.environ, {"MULTIAP_AGENT_WORKSPACES_ROOT": self._workspace_td.name}
        )
        self._workspace_patch.start()

    def tearDown(self):
        self._workspace_patch.stop()
        self._workspace_td.cleanup()

    def test_agent_local_memory_isolates_private_sla(self):
        state = {
            "ap1": {"private_sla": {"min_throughput_mbps": 100}},
            "ap2": {"private_sla": {"max_latency_ms": 10}},
            "ap3": {},
        }
        orch.reset_session(state)
        s = orch.session()
        ap1 = s.transcript_text("ap1")
        ap2 = s.transcript_text("ap2")
        shared = s.transcript_text()
        self.assertEqual(ap1, shared)
        self.assertEqual(ap2, shared)
        self.assertNotIn("PRIVATE_MEMORY", shared)
        self.assertEqual(len(s.local_transcripts["ap1"]), 1)
        self.assertEqual(len(s.local_transcripts["ap2"]), 1)
        root = Path(self._workspace_td.name)
        self.assertTrue((root / "ap1" / "memory" / "current-session.md").exists())
        ap1_file = (root / "ap1" / "memory" / "current-session.md").read_text(
            encoding="utf-8"
        )
        ap2_file = (root / "ap2" / "memory" / "current-session.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("100", ap1_file)
        self.assertNotIn("min_throughput_mbps", ap2_file)

    def test_public_history_has_single_shared_source_not_local_copies(self):
        orch.reset_session({"ap1": {"private_sla": {"x": 1}}, "ap2": {}, "ap3": {}})
        s = orch.session()
        s.record("AP2", "public proposal", kind="proposal")
        self.assertIn("public proposal", s.transcript_text())
        for agent, overlay in s.local_transcripts.items():
            self.assertNotIn("public proposal", [item.get("content") for item in overlay])
            self.assertTrue(all(
                item.get("kind") == "private_constraint" for item in overlay
            ))

    def test_agent_memories_persist_and_merge_into_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "agent-memory.sqlite3")
            store.start_run("agent-run", mode="mock")
            store.save_projection(
                "agent-run", boundary="vote_progress", state={"transcript": []},
            )
            memory = SessionMemory(
                summarized_turns=2,
                entries=[{"turn": 0, "speaker": "AP1", "kind": "message", "digest": "约束"}],
            )
            store.save_agent_session_memory(
                "agent-run", "ap1", memory=memory.to_dict(),
                summary_text="约束", budget_chars=2400,
            )
            loaded = store.load_agent_session_memories("agent-run")
            checkpoint = build_checkpoint(store, "agent-run")
            store.close()
        self.assertEqual(loaded["ap1"]["summarized_turns"], 2)
        self.assertEqual(
            checkpoint.projection["agent_session_memories"]["ap1"]["summarized_turns"], 2
        )
    def test_context_is_bounded_and_keeps_recent_turns(self):
        updates = []
        manager = SessionMemoryManager(
            budget_chars=2400,
            recent_turns=3,
            on_update=lambda memory, summary: updates.append(memory.to_dict()),
        )
        transcript = messages(12)

        context = manager.build_context(transcript)

        self.assertLessEqual(len(context), 2400)
        self.assertIn("turn 11", context)
        self.assertGreater(manager.memory.summarized_turns, 0)
        self.assertTrue(updates)

    def test_incremental_summary_does_not_duplicate_old_turns(self):
        manager = SessionMemoryManager(budget_chars=2200, recent_turns=2)
        transcript = messages(8)
        manager.build_context(transcript)
        first_turns = [entry["turn"] for entry in manager.memory.entries]

        transcript.extend(messages(3, size=500))
        manager.build_context(transcript)
        all_turns = [entry["turn"] for entry in manager.memory.entries]

        self.assertEqual(len(all_turns), len(set(all_turns)))
        self.assertTrue(set(first_turns).issubset(all_turns))

    def test_memory_persists_and_is_merged_into_recovery_projection(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "memory.sqlite3")
            store.start_run("memory-run", mode="mock", scene="edca")
            store.save_projection(
                "memory-run",
                boundary="vote_progress",
                state={"ap_state": {}, "transcript": []},
            )
            memory = SessionMemory(
                summarized_turns=4,
                entries=[{"turn": 0, "speaker": "AP1", "kind": "broadcast", "digest": "状态"}],
            )
            store.save_session_memory(
                "memory-run",
                memory=memory.to_dict(),
                summary_text="summary",
                budget_chars=3000,
            )

            checkpoint = build_checkpoint(store, "memory-run")
            stored = store.load_session_memory("memory-run")
            store.close()

        self.assertEqual(stored["summarized_turns"], 4)
        self.assertEqual(checkpoint.projection["session_memory"]["summarized_turns"], 4)
        self.assertTrue(checkpoint.can_resume)


if __name__ == "__main__":
    unittest.main()

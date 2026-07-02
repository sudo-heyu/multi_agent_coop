import tempfile
import unittest
from pathlib import Path

from src.memory import SessionMemory, SessionMemoryManager
from src.persistence import EventStore, build_checkpoint


def messages(count: int, size: int = 420):
    return [
        {"speaker": f"AP{index % 3 + 1}", "content": f"广播 turn {index} " + "x" * size}
        for index in range(count)
    ]


class SessionMemoryTests(unittest.TestCase):
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

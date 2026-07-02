import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from src import logger as logger_module
from src.logger import SessionLogger
from src.persistence import EventStore, build_checkpoint


class EventStoreTests(unittest.TestCase):
    def test_v1_database_migrates_through_episodic_memory_v5(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES (1, 'old')")
            conn.commit()
            conn.close()

            store = EventStore(path)
            tables = {
                row[0]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            versions = [
                row[0]
                for row in store._conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            store.close()

            self.assertIn("run_steps", tables)
            self.assertIn("action_journal", tables)
            self.assertIn("negotiation_projections", tables)
            self.assertIn("session_memories", tables)
            self.assertIn("episodic_memories", tables)
            self.assertEqual(versions, [1, 2, 3, 4, 5])

    def test_ordered_append_is_idempotent_and_replayable(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "events.sqlite3")
            store.start_run("run-1", mode="real", scene="edca", model="openclaw")
            first = store.append_event(
                "run-1", "phase_start", {"phase": 1}, event_id="event-1"
            )
            duplicate = store.append_event(
                "run-1", "phase_start", {"phase": 99}, event_id="event-1"
            )
            store.append_event("run-1", "vote", {"voter": "ap2"})

            self.assertEqual(first, duplicate)
            events = store.load_events("run-1")
            self.assertEqual([e["sequence"] for e in events], [1, 2])
            self.assertEqual(events[0]["phase"], 1)
            self.assertEqual(store.get_run("run-1").current_phase, 1)
            store.close()

    def test_incomplete_run_exposes_recovery_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "events.sqlite3")
            store.start_run("run-2", mode="mock")
            store.append_event("run-2", "phase_start", {"phase": 3})
            store.save_projection(
                "run-2", boundary="proposal_ready", state={"proposal": {"ap1": {}}}
            )

            checkpoint = build_checkpoint(store, "run-2")

            self.assertTrue(checkpoint.can_resume)
            self.assertEqual(checkpoint.current_phase, 3)
            self.assertEqual([r.run_id for r in store.list_incomplete_runs()], ["run-2"])
            store.close()

    def test_action_intent_is_idempotent_and_unknown_blocks_resume(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "events.sqlite3")
            store.start_run("run-action", mode="real")
            step_id = store.start_step("run-action", "executor_apply")
            action, created = store.prepare_action(
                "run-action",
                step_id=step_id,
                idempotency_key="apply:ap1:abc",
                action_type="executor_apply",
                target="ap1",
                request={"params": {"tx_power_dbm": 10}},
            )
            same, created_again = store.prepare_action(
                "run-action",
                step_id=step_id,
                idempotency_key="apply:ap1:abc",
                action_type="executor_apply",
                target="ap1",
                request={"params": {"tx_power_dbm": 99}},
            )
            store.mark_action_running(action.action_id)
            store.finish_action(action.action_id, status="unknown", error="timeout")
            store.save_projection(
                "run-action", boundary="vote_progress", state={"agree": ["ap2"]}
            )

            checkpoint = build_checkpoint(store, "run-action")

            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(action.action_id, same.action_id)
            self.assertEqual(same.request["params"]["tx_power_dbm"], 10)
            self.assertFalse(checkpoint.can_resume)
            self.assertEqual(checkpoint.blocking_actions, (action.action_id,))

            store.finish_action(
                action.action_id,
                status="succeeded",
                response={"manual_reconciliation": True},
            )
            reconciled = build_checkpoint(store, "run-action")
            self.assertTrue(reconciled.can_resume)
            store.close()

    def test_concurrent_prepare_collapses_to_one_action(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "events.sqlite3")
            store.start_run("run-concurrent")

            def prepare(_):
                return store.prepare_action(
                    "run-concurrent",
                    idempotency_key="same-key",
                    action_type="executor_apply",
                    target="ap1",
                    request={"value": 1},
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(prepare, range(20)))

            self.assertEqual(len({record.action_id for record, _ in results}), 1)
            self.assertEqual(sum(1 for _, created in results if created), 1)
            self.assertEqual(len(store.list_actions("run-concurrent")), 1)
            store.close()

    def test_session_logger_dual_writes_jsonl_and_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = EventStore(root / "events.sqlite3")
            with patch.object(logger_module, "LOG_DIR", root / "logs"), patch.object(
                logger_module, "STATE_LOG_DIR", root / "logs" / "state"
            ):
                logger = SessionLogger(
                    session_id="dual-write",
                    verbose=False,
                    mode="real",
                    event_store=store,
                )
                logger.session_start("openclaw", "edca", {"ap1": {"source": "ap"}})
                logger.phase_start(1, "broadcast")
                logger.session_end("success", 1)

                replay_store = EventStore(root / "events.sqlite3")
                events = replay_store.load_events("dual-write")
                rows = logger.log_path.read_text(encoding="utf-8").splitlines()
                run = replay_store.get_run("dual-write")
                replay_store.close()

            self.assertEqual([e["event"] for e in events], [
                "session_start", "phase_start", "session_end", "episodic_memory_created"
            ])
            self.assertEqual(len(rows), 4)
            self.assertEqual(run.status, "completed")


if __name__ == "__main__":
    unittest.main()

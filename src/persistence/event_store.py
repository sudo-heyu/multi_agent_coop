"""SQLite event store for durable, replayable agent runs.

JSONL remains the human-readable export. This store is the first durable
control-plane layer used for recovery, memory extraction and later replay.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    mode: str | None
    scene: str | None
    model: str | None
    current_phase: int | None
    outcome: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    run_id: str
    step_id: str | None
    idempotency_key: str
    action_type: str
    target: str
    status: str
    attempts: int
    request: dict[str, Any]
    response: Any
    error: str | None
    created_at: str
    updated_at: str


class EventStore:
    """Small SQLite repository with ordered idempotent event appends."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                mode TEXT,
                scene TEXT,
                model TEXT,
                current_phase INTEGER,
                outcome TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(run_id, sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_run_events_run_type
                ON run_events(run_id, event_type, sequence);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_status_updated
                ON agent_runs(status, updated_at);

            CREATE TABLE IF NOT EXISTS state_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                label TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                state_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_run_sequence
                ON state_snapshots(run_id, sequence);

            CREATE TABLE IF NOT EXISTS run_steps (
                step_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                retry_budget INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                input_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(run_id, sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_run_steps_run_status
                ON run_steps(run_id, status, sequence);

            CREATE TABLE IF NOT EXISTS action_journal (
                action_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                step_id TEXT REFERENCES run_steps(step_id) ON DELETE SET NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                action_type TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                request_json TEXT NOT NULL,
                response_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_action_journal_run_status
                ON action_journal(run_id, status, updated_at);

            CREATE TABLE IF NOT EXISTS negotiation_projections (
                run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                projection_version INTEGER NOT NULL,
                boundary TEXT NOT NULL,
                safe_to_resume INTEGER NOT NULL,
                last_event_sequence INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_memories (
                run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                memory_version INTEGER NOT NULL,
                summarized_turns INTEGER NOT NULL,
                budget_chars INTEGER NOT NULL,
                summary_text TEXT NOT NULL,
                memory_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episodic_memories (
                episode_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                scene TEXT,
                strategy TEXT,
                outcome TEXT NOT NULL,
                topology_signature TEXT NOT NULL,
                feature_json TEXT NOT NULL,
                initial_state_json TEXT NOT NULL,
                decision_json TEXT,
                validation_json TEXT,
                execution_json TEXT NOT NULL,
                observed_state_json TEXT,
                metrics_json TEXT NOT NULL,
                quality_score REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_scene_strategy_quality
                ON episodic_memories(scene, strategy, quality_score DESC);
            CREATE INDEX IF NOT EXISTS idx_episodes_topology
                ON episodic_memories(topology_signature, created_at DESC);
            """
        )
        for version in range(1, SCHEMA_VERSION + 1):
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
        self._conn.commit()

    def start_run(
        self,
        run_id: str,
        *,
        mode: str | None = None,
        scene: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_runs(
                    run_id, status, mode, scene, model, created_at, updated_at,
                    metadata_json
                ) VALUES (?, 'running', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    mode=COALESCE(agent_runs.mode, excluded.mode),
                    scene=COALESCE(agent_runs.scene, excluded.scene),
                    model=COALESCE(agent_runs.model, excluded.model)
                """,
                (run_id, mode, scene, model, now, now, _json(metadata or {})),
            )

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> tuple[str, int]:
        event_id = event_id or uuid.uuid4().hex
        occurred_at = occurred_at or _now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT sequence FROM run_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                return event_id, int(existing["sequence"])
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next"])
            self._conn.execute(
                """
                INSERT INTO run_events(
                    event_id, run_id, sequence, event_type, occurred_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, run_id, sequence, event_type, occurred_at, _json(payload)),
            )
            phase = payload.get("phase") if isinstance(payload.get("phase"), int) else None
            self._conn.execute(
                """
                UPDATE agent_runs SET
                    updated_at=?,
                    current_phase=COALESCE(?, current_phase)
                WHERE run_id=?
                """,
                (occurred_at, phase, run_id),
            )
            return event_id, sequence

    def record_snapshot(
        self,
        run_id: str,
        *,
        label: str,
        source: str,
        state: dict[str, Any],
        snapshot_id: str | None = None,
        observed_at: str | None = None,
    ) -> str:
        snapshot_id = snapshot_id or uuid.uuid4().hex
        observed_at = observed_at or _now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT snapshot_id FROM state_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if existing is not None:
                return snapshot_id
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM state_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO state_snapshots(
                    snapshot_id, run_id, sequence, label, source, observed_at, state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    int(row["next"]),
                    label,
                    source,
                    observed_at,
                    _json(state),
                ),
            )
        return snapshot_id

    def complete_run(self, run_id: str, outcome: str) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE agent_runs SET status='completed', outcome=?,
                    updated_at=?, completed_at=? WHERE run_id=?
                """,
                (outcome, now, now, run_id),
            )

    def start_step(
        self,
        run_id: str,
        name: str,
        *,
        step_id: str | None = None,
        retry_budget: int = 0,
        input_data: dict[str, Any] | None = None,
    ) -> str:
        step_id = step_id or uuid.uuid4().hex
        now = _now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT step_id FROM run_steps WHERE step_id=?", (step_id,)
            ).fetchone()
            if existing is not None:
                return step_id
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM run_steps WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO run_steps(
                    step_id, run_id, sequence, name, status, retry_budget,
                    attempts, input_json, created_at, updated_at, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?, 1, ?, ?, ?, ?)
                """,
                (
                    step_id, run_id, int(row["next"]), name,
                    max(0, int(retry_budget)), _json(input_data or {}), now, now, now,
                ),
            )
        return step_id

    def finish_step(
        self,
        step_id: str,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid terminal step status: {status}")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE run_steps SET status=?, result_json=?, error=?,
                    updated_at=?, completed_at=? WHERE step_id=?
                """,
                (
                    status,
                    _json(result) if result is not None else None,
                    error,
                    now,
                    now,
                    step_id,
                ),
            )

    def prepare_action(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        action_type: str,
        target: str,
        request: dict[str, Any],
        step_id: str | None = None,
    ) -> tuple[ActionRecord, bool]:
        """Create an action intent, or return the existing intent for this key."""
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM action_journal WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return self._action_record(row), False
            action_id = uuid.uuid4().hex
            self._conn.execute(
                """
                INSERT INTO action_journal(
                    action_id, run_id, step_id, idempotency_key, action_type,
                    target, status, request_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    action_id, run_id, step_id, idempotency_key, action_type,
                    target, _json(request), now, now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM action_journal WHERE action_id=?", (action_id,)
            ).fetchone()
            return self._action_record(row), True

    def mark_action_running(self, action_id: str) -> ActionRecord:
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM action_journal WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row["status"] in {"succeeded", "unknown"}:
                return self.get_action(action_id)
            self._conn.execute(
                """
                UPDATE action_journal SET status='running', attempts=attempts+1,
                    updated_at=?, started_at=? WHERE action_id=?
                """,
                (now, now, action_id),
            )
            return self.get_action(action_id)

    def finish_action(
        self,
        action_id: str,
        *,
        status: str,
        response: Any = None,
        error: str | None = None,
    ) -> ActionRecord:
        if status not in {"succeeded", "failed", "unknown", "cancelled"}:
            raise ValueError(f"invalid terminal action status: {status}")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE action_journal SET status=?, response_json=?, error=?,
                    updated_at=?, completed_at=? WHERE action_id=?
                """,
                (
                    status,
                    _json(response) if response is not None else None,
                    error,
                    now,
                    now,
                    action_id,
                ),
            )
            return self.get_action(action_id)

    def get_action(self, action_id: str) -> ActionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM action_journal WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._action_record(row) if row is not None else None

    def get_action_by_key(self, idempotency_key: str) -> ActionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM action_journal WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._action_record(row) if row is not None else None

    def list_actions(self, run_id: str) -> list[ActionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM action_journal WHERE run_id=? ORDER BY created_at, action_id",
                (run_id,),
            ).fetchall()
        return [self._action_record(row) for row in rows]

    def save_projection(
        self,
        run_id: str,
        *,
        boundary: str,
        state: dict[str, Any],
        safe_to_resume: bool = True,
        projection_version: int = 1,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS seq FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO negotiation_projections(
                    run_id, projection_version, boundary, safe_to_resume,
                    last_event_sequence, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    projection_version=excluded.projection_version,
                    boundary=excluded.boundary,
                    safe_to_resume=excluded.safe_to_resume,
                    last_event_sequence=excluded.last_event_sequence,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id, projection_version, boundary, int(safe_to_resume),
                    int(row["seq"]), _json(state), now,
                ),
            )

    def load_projection(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM negotiation_projections WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "projection_version": row["projection_version"],
            "boundary": row["boundary"],
            "safe_to_resume": bool(row["safe_to_resume"]),
            "last_event_sequence": row["last_event_sequence"],
            "state": json.loads(row["state_json"]),
            "updated_at": row["updated_at"],
        }

    def save_session_memory(
        self,
        run_id: str,
        *,
        memory: dict[str, Any],
        summary_text: str,
        budget_chars: int,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO session_memories(
                    run_id, memory_version, summarized_turns, budget_chars,
                    summary_text, memory_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    memory_version=excluded.memory_version,
                    summarized_turns=excluded.summarized_turns,
                    budget_chars=excluded.budget_chars,
                    summary_text=excluded.summary_text,
                    memory_json=excluded.memory_json,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id,
                    int(memory.get("version") or 1),
                    int(memory.get("summarized_turns") or 0),
                    int(budget_chars),
                    summary_text,
                    _json(memory),
                    now,
                ),
            )

    def load_session_memory(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_memories WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "memory_version": row["memory_version"],
            "summarized_turns": row["summarized_turns"],
            "budget_chars": row["budget_chars"],
            "summary_text": row["summary_text"],
            "memory": json.loads(row["memory_json"]),
            "updated_at": row["updated_at"],
        }

    def save_episode(self, episode: dict[str, Any]) -> str:
        episode_id = str(episode.get("episode_id") or uuid.uuid4().hex)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO episodic_memories(
                    episode_id, run_id, scene, strategy, outcome,
                    topology_signature, feature_json, initial_state_json,
                    decision_json, validation_json, execution_json,
                    observed_state_json, metrics_json, quality_score,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    scene=excluded.scene,
                    strategy=excluded.strategy,
                    outcome=excluded.outcome,
                    topology_signature=excluded.topology_signature,
                    feature_json=excluded.feature_json,
                    initial_state_json=excluded.initial_state_json,
                    decision_json=excluded.decision_json,
                    validation_json=excluded.validation_json,
                    execution_json=excluded.execution_json,
                    observed_state_json=excluded.observed_state_json,
                    metrics_json=excluded.metrics_json,
                    quality_score=excluded.quality_score,
                    updated_at=excluded.updated_at
                """,
                (
                    episode_id,
                    episode["run_id"],
                    episode.get("scene"),
                    episode.get("strategy"),
                    episode["outcome"],
                    episode["topology_signature"],
                    _json(episode.get("features") or {}),
                    _json(episode.get("initial_state") or {}),
                    _json(episode["decision"]) if episode.get("decision") is not None else None,
                    _json(episode["validation"]) if episode.get("validation") is not None else None,
                    _json(episode.get("execution") or []),
                    _json(episode["observed_state"]) if episode.get("observed_state") is not None else None,
                    _json(episode.get("metrics") or {}),
                    float(episode.get("quality_score") or 0.0),
                    episode.get("created_at") or now,
                    now,
                ),
            )
        return episode_id

    def get_episode(self, *, run_id: str | None = None, episode_id: str | None = None) -> dict[str, Any] | None:
        if not run_id and not episode_id:
            raise ValueError("run_id or episode_id is required")
        column, value = ("run_id", run_id) if run_id else ("episode_id", episode_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM episodic_memories WHERE {column}=?", (value,)
            ).fetchone()
        return self._episode_record(row) if row is not None else None

    def list_episodes(
        self,
        *,
        topology_signature: str | None = None,
        scene: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if topology_signature:
            clauses.append("topology_signature=?")
            params.append(topology_signature)
        if scene:
            clauses.append("scene=?")
            params.append(scene)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episodic_memories" + where
                + " ORDER BY quality_score DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._episode_record(row) for row in rows]

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run_record(row) if row is not None else None

    def list_incomplete_runs(self) -> list[RunRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE status != 'completed'
                ORDER BY updated_at ASC
                """
            ).fetchall()
        return [self._run_record(row) for row in rows]

    def load_events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_id, sequence, event_type, occurred_at, payload_json
                FROM run_events WHERE run_id=? AND sequence>?
                ORDER BY sequence ASC
                """,
                (run_id, after_sequence),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "sequence": row["sequence"],
                "event": row["event_type"],
                "ts": row["occurred_at"],
                **json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def iter_snapshots(self, run_id: str) -> Iterable[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM state_snapshots WHERE run_id=? ORDER BY sequence ASC
                """,
                (run_id,),
            ).fetchall()
        for row in rows:
            yield {
                "snapshot_id": row["snapshot_id"],
                "sequence": row["sequence"],
                "label": row["label"],
                "source": row["source"],
                "observed_at": row["observed_at"],
                "state": json.loads(row["state_json"]),
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            status=row["status"],
            mode=row["mode"],
            scene=row["scene"],
            model=row["model"],
            current_phase=row["current_phase"],
            outcome=row["outcome"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _action_record(row: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            action_id=row["action_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            idempotency_key=row["idempotency_key"],
            action_type=row["action_type"],
            target=row["target"],
            status=row["status"],
            attempts=row["attempts"],
            request=json.loads(row["request_json"]),
            response=(json.loads(row["response_json"]) if row["response_json"] else None),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _episode_record(row: sqlite3.Row) -> dict[str, Any]:
        def load(column: str, default=None):
            return json.loads(row[column]) if row[column] is not None else default
        return {
            "episode_id": row["episode_id"],
            "run_id": row["run_id"],
            "scene": row["scene"],
            "strategy": row["strategy"],
            "outcome": row["outcome"],
            "topology_signature": row["topology_signature"],
            "features": load("feature_json", {}),
            "initial_state": load("initial_state_json", {}),
            "decision": load("decision_json"),
            "validation": load("validation_json"),
            "execution": load("execution_json", []),
            "observed_state": load("observed_state_json"),
            "metrics": load("metrics_json", {}),
            "quality_score": row["quality_score"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

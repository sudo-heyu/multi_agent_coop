"""SQLite event store for durable, replayable agent runs.

JSONL remains the human-readable export. This store is the first durable
control-plane layer used for recovery, memory extraction and later replay.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 17


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

            CREATE TABLE IF NOT EXISTS agent_session_memories (
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                memory_version INTEGER NOT NULL,
                summarized_turns INTEGER NOT NULL,
                budget_chars INTEGER NOT NULL,
                summary_text TEXT NOT NULL,
                memory_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, agent_id)
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
                evaluation_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_episodic_memories (
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                topology_signature TEXT NOT NULL,
                scene TEXT,
                strategy TEXT,
                local_state_json TEXT NOT NULL,
                local_decision_json TEXT,
                outcome TEXT NOT NULL,
                quality_score REAL NOT NULL,
                evaluation_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id, agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_agent_episodes_recall
                ON agent_episodic_memories(agent_id, topology_signature, quality_score DESC);

            CREATE INDEX IF NOT EXISTS idx_episodes_scene_strategy_quality
                ON episodic_memories(scene, strategy, quality_score DESC);
            CREATE INDEX IF NOT EXISTS idx_episodes_topology
                ON episodic_memories(topology_signature, created_at DESC);

            CREATE TABLE IF NOT EXISTS outcome_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                window_label TEXT NOT NULL,
                window_seconds REAL NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL,
                baseline_json TEXT NOT NULL,
                observed_json TEXT,
                deltas_json TEXT,
                verdict TEXT,
                confidence REAL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                collected_at TEXT,
                UNIQUE(run_id, window_label)
            );

            CREATE INDEX IF NOT EXISTS idx_outcome_eval_due
                ON outcome_evaluations(status, due_at);

            CREATE TABLE IF NOT EXISTS semantic_rules (
                rule_id TEXT PRIMARY KEY,
                topology_signature TEXT NOT NULL,
                scene TEXT,
                strategy TEXT,
                dominant_verdict TEXT NOT NULL,
                support INTEGER NOT NULL,
                consistency REAL NOT NULL,
                confidence REAL NOT NULL,
                verdict_counts_json TEXT NOT NULL,
                action_summary_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(topology_signature, scene, strategy)
            );

            CREATE INDEX IF NOT EXISTS idx_rules_topo_scene
                ON semantic_rules(topology_signature, scene, confidence DESC);

            CREATE TABLE IF NOT EXISTS memory_contradictions (
                contradiction_id TEXT PRIMARY KEY,
                memory_kind TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                run_id TEXT NOT NULL,
                expected TEXT NOT NULL,
                observed TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(memory_kind, memory_key, run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_contradictions_memory
                ON memory_contradictions(memory_kind, memory_key, recorded_at DESC);

            CREATE TABLE IF NOT EXISTS memory_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                memory_kind TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                run_id TEXT NOT NULL,
                predicted TEXT,
                observed TEXT NOT NULL,
                result TEXT NOT NULL,
                trust_at_injection REAL,
                recorded_at TEXT NOT NULL,
                UNIQUE(memory_kind, memory_key, run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_reconciliations_result
                ON memory_reconciliations(result, recorded_at DESC);

            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                metric TEXT NOT NULL,
                target_json TEXT NOT NULL,
                baseline_json TEXT NOT NULL DEFAULT '{}',
                budget_attempts INTEGER NOT NULL,
                deadline TEXT,
                status TEXT NOT NULL,
                status_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_goals_status
                ON goals(status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS goal_attempts (
                attempt_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                parent_attempt_id TEXT,
                sequence INTEGER NOT NULL,
                attribution_json TEXT NOT NULL DEFAULT '{}',
                progress_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(goal_id, sequence)
            );

            CREATE TABLE IF NOT EXISTS maintenance_locks (
                lock_name TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_heartbeats (
                service_name TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # v5 老库的 episodic_memories 缺 evaluation_json，就地补列。
        self._ensure_column("episodic_memories", "evaluation_json", "TEXT")
        # v8：L6 整理软删/冲突标记（旧库就地补列，默认未归档、未冲突）。
        self._ensure_column("episodic_memories", "archived", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("semantic_rules", "conflicted", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("semantic_rules", "llm_summary", "TEXT")
        self._ensure_column("semantic_rules", "llm_model", "TEXT")
        self._ensure_column("semantic_rules", "active", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("semantic_rules", "llm_evidence_hash", "TEXT")
        self._ensure_column("semantic_rules", "llm_prompt_version", "INTEGER")
        self._ensure_column("semantic_rules", "llm_status", "TEXT")
        self._ensure_column("semantic_rules", "llm_error", "TEXT")
        self._ensure_column("semantic_rules", "llm_generated_at", "TEXT")
        self._ensure_column("outcome_evaluations", "claimed_at", "TEXT")
        self._ensure_column("outcome_evaluations", "claimant", "TEXT")
        self._ensure_column("agent_session_memories", "memory_revision", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("episodic_memories", "lifecycle", "TEXT NOT NULL DEFAULT 'draft'")
        self._ensure_column("episodic_memories", "quality_vector_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("episodic_memories", "episode_fingerprint", "TEXT")
        self._ensure_column("episodic_memories", "feature_schema_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("episodic_memories", "evaluation_policy_version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("episodic_memories", "case_narrative", "TEXT")
        self._ensure_column("episodic_memories", "case_narrative_model", "TEXT")
        self._ensure_column("episodic_memories", "case_narrative_evidence_hash", "TEXT")
        self._ensure_column("episodic_memories", "case_narrative_status", "TEXT")
        # v16：反思模块信任字段（矛盾计数缓存 + 隔离标记 + 最近验证时间）。
        for table in ("episodic_memories", "agent_episodic_memories", "semantic_rules"):
            self._ensure_column(table, "last_verified_at", "TEXT")
            self._ensure_column(table, "contradictions", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(table, "quarantined", "INTEGER NOT NULL DEFAULT 0")
        for version in range(1, SCHEMA_VERSION + 1):
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
        self._conn.commit()
        self._backfill_agent_episodes()
        self._backfill_episode_metadata()

    def _backfill_agent_episodes(self) -> None:
        """Materialize per-agent rows for episodes created before schema v12."""
        rows = self._conn.execute(
            "SELECT * FROM episodic_memories WHERE run_id NOT IN "
            "(SELECT DISTINCT run_id FROM agent_episodic_memories)"
        ).fetchall()
        for row in rows:
            initial = json.loads(row["initial_state_json"])
            decision = json.loads(row["decision_json"]) if row["decision_json"] else {}
            evaluation = json.loads(row["evaluation_json"]) if row["evaluation_json"] else None
            for agent_id, local_state in initial.items():
                if not isinstance(local_state, dict):
                    continue
                self.save_agent_episode({
                    "run_id": row["run_id"], "agent_id": agent_id,
                    "topology_signature": row["topology_signature"], "scene": row["scene"],
                    "strategy": row["strategy"], "local_state": local_state,
                    "local_decision": decision.get(agent_id) if isinstance(decision, dict) else None,
                    "outcome": row["outcome"], "quality_score": row["quality_score"],
                    "evaluation": evaluation, "created_at": row["created_at"],
                })

    def _backfill_episode_metadata(self) -> None:
        rows = self._conn.execute(
            "SELECT run_id,topology_signature,scene,strategy,feature_json,decision_json,"
            "quality_score,evaluation_json FROM episodic_memories "
            "WHERE episode_fingerprint IS NULL OR quality_vector_json='{}'"
        ).fetchall()
        for row in rows:
            evaluation = json.loads(row["evaluation_json"]) if row["evaluation_json"] else {}
            verdict = evaluation.get("final_verdict")
            lifecycle = {"improved": "trusted", "degraded": "warning",
                         "inconclusive": "inconclusive", "neutral": "evaluated"}.get(
                             verdict, "awaiting_evaluation"
                         )
            fingerprint = hashlib.sha256(_json({
                "topology": row["topology_signature"], "scene": row["scene"],
                "strategy": row["strategy"], "features": json.loads(row["feature_json"]),
                "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
            }).encode()).hexdigest()[:24]
            vector = {"pipeline_reliability": float(row["quality_score"]),
                      "outcome_confidence": float(evaluation.get("final_confidence") or 0.0),
                      "metric_coverage": 0.0, "causal_confidence": 0.0}
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE episodic_memories SET episode_fingerprint=?, quality_vector_json=?, "
                    "lifecycle=? WHERE run_id=?",
                    (fingerprint, _json(vector), lifecycle, row["run_id"]),
                )

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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

    def save_agent_session_memory(
        self, run_id: str, agent_id: str, *, memory: dict[str, Any],
        summary_text: str, budget_chars: int,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_session_memories(
                    run_id, agent_id, memory_version, summarized_turns,
                    budget_chars, summary_text, memory_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, agent_id) DO UPDATE SET
                    memory_version=excluded.memory_version,
                    summarized_turns=excluded.summarized_turns,
                    budget_chars=excluded.budget_chars,
                    summary_text=excluded.summary_text,
                    memory_json=excluded.memory_json,
                    memory_revision=agent_session_memories.memory_revision + 1,
                    updated_at=excluded.updated_at
                """,
                (
                    run_id, str(agent_id).lower(), int(memory.get("version") or 1),
                    int(memory.get("summarized_turns") or 0), int(budget_chars),
                    summary_text, _json(memory), now,
                ),
            )

    def load_agent_session_memories(self, run_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_session_memories WHERE run_id=? ORDER BY agent_id",
                (run_id,),
            ).fetchall()
        return {
            row["agent_id"]: {
                "run_id": row["run_id"], "agent_id": row["agent_id"],
                "memory_version": row["memory_version"],
                "summarized_turns": row["summarized_turns"],
                "budget_chars": row["budget_chars"],
                "summary_text": row["summary_text"],
                "memory": json.loads(row["memory_json"]),
                "updated_at": row["updated_at"],
                "memory_revision": row["memory_revision"],
            }
            for row in rows
        }

    def agent_session_memory_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent_id,COUNT(*) n FROM agent_session_memories GROUP BY agent_id"
            ).fetchall()
        return {row["agent_id"]: int(row["n"]) for row in rows}

    def update_agent_episode_evaluation(
        self, run_id: str, agent_id: str, *, evaluation: dict[str, Any],
        quality_score: float,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE agent_episodic_memories SET evaluation_json=?, quality_score=?, "
                "updated_at=? WHERE run_id=? AND agent_id=?",
                (_json(evaluation), float(quality_score), _now(), run_id, agent_id.lower()),
            )

    def heartbeat_service(
        self, service_name: str, holder: str, *, status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO service_heartbeats(service_name,holder,status,details_json,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(service_name) DO UPDATE SET holder=excluded.holder,"
                "status=excluded.status,details_json=excluded.details_json,updated_at=excluded.updated_at",
                (service_name, holder, status, _json(details or {}), _now()),
            )

    def list_service_heartbeats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM service_heartbeats").fetchall()
        return [{"service_name": r["service_name"], "holder": r["holder"],
                 "status": r["status"], "details": json.loads(r["details_json"]),
                 "updated_at": r["updated_at"]} for r in rows]

    def interrupt_stale_runs(self, *, stale_before: str) -> int:
        now = _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE agent_runs SET status='interrupted', outcome='interrupted', "
                "updated_at=?, completed_at=? WHERE status='running' AND updated_at<?",
                (now, now, stale_before),
            )
            return cursor.rowcount

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
                    evaluation_json, lifecycle, quality_vector_json, episode_fingerprint,
                    feature_schema_version, evaluation_policy_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    quality_score=CASE
                        WHEN episodic_memories.evaluation_json IS NOT NULL
                        THEN episodic_memories.quality_score
                        ELSE excluded.quality_score
                    END,
                    evaluation_json=COALESCE(
                        episodic_memories.evaluation_json,
                        excluded.evaluation_json
                    ),
                    lifecycle=CASE
                        WHEN episodic_memories.evaluation_json IS NOT NULL
                        THEN episodic_memories.lifecycle
                        ELSE excluded.lifecycle
                    END,
                    quality_vector_json=CASE
                        WHEN episodic_memories.evaluation_json IS NOT NULL
                        THEN episodic_memories.quality_vector_json
                        ELSE excluded.quality_vector_json
                    END,
                    episode_fingerprint=excluded.episode_fingerprint,
                    feature_schema_version=excluded.feature_schema_version,
                    evaluation_policy_version=excluded.evaluation_policy_version,
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
                    _json(episode["evaluation"])
                    if episode.get("evaluation") is not None else None,
                    episode.get("lifecycle") or "awaiting_evaluation",
                    _json(episode.get("quality_vector") or {}),
                    episode.get("episode_fingerprint"),
                    int(episode.get("feature_schema_version") or 1),
                    int(episode.get("evaluation_policy_version") or 1),
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

    def save_agent_episode(self, episode: dict[str, Any]) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_episodic_memories(
                    run_id, agent_id, topology_signature, scene, strategy,
                    local_state_json, local_decision_json, outcome, quality_score,
                    evaluation_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, agent_id) DO UPDATE SET
                    topology_signature=excluded.topology_signature,
                    scene=excluded.scene, strategy=excluded.strategy,
                    local_state_json=excluded.local_state_json,
                    local_decision_json=excluded.local_decision_json,
                    outcome=excluded.outcome,
                    quality_score=CASE
                        WHEN agent_episodic_memories.evaluation_json IS NOT NULL
                             AND excluded.evaluation_json IS NULL
                        THEN agent_episodic_memories.quality_score
                        ELSE excluded.quality_score
                    END,
                    evaluation_json=COALESCE(
                        excluded.evaluation_json,
                        agent_episodic_memories.evaluation_json
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    episode["run_id"], episode["agent_id"], episode["topology_signature"],
                    episode.get("scene"), episode.get("strategy"),
                    _json(episode.get("local_state") or {}),
                    _json(episode["local_decision"])
                    if episode.get("local_decision") is not None else None,
                    episode["outcome"], float(episode.get("quality_score") or 0.0),
                    _json(episode["evaluation"])
                    if episode.get("evaluation") is not None else None,
                    episode.get("created_at") or now, now,
                ),
            )

    def list_agent_episodes(
        self, agent_id: str, *, topology_signature: str | None = None,
        min_quality: float = 0.0, limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = ["agent_id=?", "quality_score>=?"]
        params: list[Any] = [agent_id.lower(), float(min_quality)]
        if topology_signature:
            clauses.append("topology_signature=?")
            params.append(topology_signature)
        params.append(max(1, min(int(limit), 100)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_episodic_memories WHERE " + " AND ".join(clauses)
                + " ORDER BY quality_score DESC, created_at DESC LIMIT ?", params,
            ).fetchall()
        return [{
            "run_id": row["run_id"], "agent_id": row["agent_id"],
            "topology_signature": row["topology_signature"], "scene": row["scene"],
            "strategy": row["strategy"],
            "local_state": json.loads(row["local_state_json"]),
            "local_decision": json.loads(row["local_decision_json"])
            if row["local_decision_json"] else None,
            "outcome": row["outcome"], "quality_score": row["quality_score"],
            "evaluation": json.loads(row["evaluation_json"])
            if row["evaluation_json"] else None,
            "last_verified_at": row["last_verified_at"],
            "contradictions": int(row["contradictions"]),
            "quarantined": bool(row["quarantined"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        } for row in rows]

    def list_episodes(
        self,
        *,
        topology_signature: str | None = None,
        scene: str | None = None,
        limit: int = 200,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if not include_archived:
            clauses.append("archived=0")
        if topology_signature:
            clauses.append("topology_signature=?")
            params.append(topology_signature)
        if scene:
            clauses.append("scene=?")
            params.append(scene)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 100_000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episodic_memories" + where
                + " ORDER BY quality_score DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._episode_record(row) for row in rows]

    def archive_episodes(self, run_ids: list[str]) -> int:
        """软删（archived=1）一批案例：不再参与检索/归纳，但保留可审计。"""
        if not run_ids:
            return 0
        now = _now()
        placeholders = ",".join("?" for _ in run_ids)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"UPDATE episodic_memories SET archived=1, updated_at=? "
                f"WHERE run_id IN ({placeholders}) AND archived=0",
                [now, *run_ids],
            )
            return cursor.rowcount

    def count_episodes_by_topology(self, *, include_archived: bool = False) -> dict[str, int]:
        where = "" if include_archived else " WHERE archived=0"
        with self._lock:
            rows = self._conn.execute(
                "SELECT topology_signature, COUNT(*) AS n FROM episodic_memories"
                + where + " GROUP BY topology_signature",
            ).fetchall()
        return {row["topology_signature"]: int(row["n"]) for row in rows}

    # ---- 反思模块：矛盾账本 / 验证时间 / 隔离区（v16） ----
    # memory_kind → (表, 定位方式)。agent_episode 的 memory_key 形如
    # "<run_id>:<agent_id>"，从右侧拆分（agent_id 不含冒号）。
    def _memory_where(self, memory_kind: str, memory_key: str) -> tuple[str, str, list[Any]]:
        if memory_kind == "episode":
            return "episodic_memories", "run_id=?", [memory_key]
        if memory_kind == "rule":
            return "semantic_rules", "rule_id=?", [memory_key]
        if memory_kind == "agent_episode":
            run_id, _, agent_id = memory_key.rpartition(":")
            if not run_id:
                raise ValueError(f"agent_episode memory_key 需为 '<run_id>:<agent_id>'：{memory_key!r}")
            return "agent_episodic_memories", "run_id=? AND agent_id=?", [run_id, agent_id.lower()]
        raise ValueError(f"未知 memory_kind: {memory_kind!r}")

    def record_contradiction(
        self, *, memory_kind: str, memory_key: str, run_id: str,
        expected: str, observed: str, detail: dict[str, Any] | None = None,
    ) -> int | None:
        """记一笔矛盾账并同步递增记忆行上的矛盾计数缓存。

        幂等：同一 (memory_kind, memory_key, run_id) 只记一次；重复记账返回 None，
        首次记账返回该记忆最新矛盾计数。账本行不删除，可审计。
        """
        table, where, params = self._memory_where(memory_kind, memory_key)
        now = _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO memory_contradictions("
                "contradiction_id, memory_kind, memory_key, run_id, expected, observed,"
                " detail_json, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, memory_kind, memory_key, run_id,
                 expected, observed, _json(detail or {}), now),
            )
            if cursor.rowcount == 0:
                return None
            self._conn.execute(
                f"UPDATE {table} SET contradictions=contradictions+1, updated_at=? WHERE {where}",
                [now, *params],
            )
            row = self._conn.execute(
                f"SELECT contradictions FROM {table} WHERE {where}", params
            ).fetchone()
        return int(row["contradictions"]) if row else 0

    def mark_memory_verified(
        self, memory_kind: str, memory_key: str, *, verified_at: str | None = None,
    ) -> None:
        """记录记忆最近一次被真实反馈证实的时间（时效衰减的锚点）。"""
        table, where, params = self._memory_where(memory_kind, memory_key)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE {table} SET last_verified_at=?, updated_at=? WHERE {where}",
                [verified_at or now, now, *params],
            )

    def set_memory_quarantined(
        self, memory_kind: str, memory_key: str, quarantined: bool,
    ) -> None:
        """隔离/解除隔离：只影响召回注入，不物理删除（反思红线）。"""
        table, where, params = self._memory_where(memory_kind, memory_key)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE {table} SET quarantined=?, updated_at=? WHERE {where}",
                [1 if quarantined else 0, _now(), *params],
            )

    def list_contradictions(
        self, *, memory_kind: str | None = None, memory_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if memory_kind:
            clauses.append("memory_kind=?")
            params.append(memory_kind)
        if memory_key:
            clauses.append("memory_key=?")
            params.append(memory_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_contradictions" + where
                + " ORDER BY recorded_at DESC LIMIT ?", params,
            ).fetchall()
        return [{
            "contradiction_id": row["contradiction_id"],
            "memory_kind": row["memory_kind"], "memory_key": row["memory_key"],
            "run_id": row["run_id"], "expected": row["expected"],
            "observed": row["observed"],
            "detail": json.loads(row["detail_json"]),
            "recorded_at": row["recorded_at"],
        } for row in rows]

    def record_reconciliation(
        self, *, memory_kind: str, memory_key: str, run_id: str,
        predicted: str | None, observed: str, result: str,
        trust_at_injection: float | None,
    ) -> bool:
        """记录一次"注入时预测 vs 实际 verdict"比对（R5 校准数据集）。

        幂等：同一 (kind, key, run) 只记一次；返回是否为首次记录，
        供调用方决定是否执行记矛盾/标验证等副作用。
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO memory_reconciliations("
                "reconciliation_id, memory_kind, memory_key, run_id, predicted,"
                " observed, result, trust_at_injection, recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, memory_kind, memory_key, run_id, predicted,
                 observed, result,
                 float(trust_at_injection) if trust_at_injection is not None else None,
                 _now()),
            )
            return cursor.rowcount > 0

    def list_reconciliations(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_reconciliations ORDER BY recorded_at DESC LIMIT ?",
                (max(1, min(int(limit), 100_000)),),
            ).fetchall()
        return [{
            "memory_kind": row["memory_kind"], "memory_key": row["memory_key"],
            "run_id": row["run_id"], "predicted": row["predicted"],
            "observed": row["observed"], "result": row["result"],
            "trust_at_injection": row["trust_at_injection"],
            "recorded_at": row["recorded_at"],
        } for row in rows]

    def get_memory_record(self, memory_kind: str, memory_key: str) -> dict[str, Any] | None:
        """按 (kind, key) 取一条记忆记录，供信任重算与隔离判定。"""
        if memory_kind == "episode":
            return self.get_episode(run_id=memory_key)
        if memory_kind == "rule":
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM semantic_rules WHERE rule_id=?", (memory_key,)
                ).fetchone()
            return self._rule_record(row) if row else None
        if memory_kind == "agent_episode":
            table, where, params = self._memory_where(memory_kind, memory_key)
            with self._lock:
                row = self._conn.execute(
                    f"SELECT * FROM {table} WHERE {where}", params
                ).fetchone()
            if row is None:
                return None
            return {
                "run_id": row["run_id"], "agent_id": row["agent_id"],
                "quality_score": row["quality_score"],
                "evaluation": json.loads(row["evaluation_json"])
                if row["evaluation_json"] else None,
                "last_verified_at": row["last_verified_at"],
                "contradictions": int(row["contradictions"]),
                "quarantined": bool(row["quarantined"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
        raise ValueError(f"未知 memory_kind: {memory_kind!r}")

    def list_quarantined_memories(self) -> list[dict[str, Any]]:
        """隔离区清单（跨三类记忆），供 memory_admin 审计与再验证。"""
        items: list[dict[str, Any]] = []
        with self._lock:
            for kind, table, key_sql in (
                ("episode", "episodic_memories", "run_id"),
                ("agent_episode", "agent_episodic_memories", "run_id || ':' || agent_id"),
                ("rule", "semantic_rules", "rule_id"),
            ):
                rows = self._conn.execute(
                    f"SELECT {key_sql} AS memory_key, contradictions, last_verified_at,"
                    f" updated_at FROM {table} WHERE quarantined=1"
                ).fetchall()
                items.extend({
                    "memory_kind": kind, "memory_key": row["memory_key"],
                    "contradictions": int(row["contradictions"]),
                    "last_verified_at": row["last_verified_at"],
                    "updated_at": row["updated_at"],
                } for row in rows)
        return items

    # ---- 迭代模块：Goal / attempt 链（v16 表，阶段5 起使用） ----
    def create_goal(
        self, *, metric: str, target: dict[str, Any], source: str,
        budget_attempts: int, baseline: dict[str, Any] | None = None,
        deadline: str | None = None,
    ) -> dict[str, Any]:
        goal_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO goals(goal_id, source, metric, target_json, baseline_json,"
                " budget_attempts, deadline, status, status_reason, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (goal_id, source, metric, _json(target), _json(baseline or {}),
                 int(budget_attempts), deadline, "active", None, now, now),
            )
        return self.get_goal(goal_id)

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
        return self._goal_record(row) if row else None

    def list_goals(self, *, status: str | None = None) -> list[dict[str, Any]]:
        clause, params = ("WHERE status=?", [status]) if status else ("", [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM goals {clause} ORDER BY created_at DESC", params
            ).fetchall()
        return [self._goal_record(row) for row in rows]

    def update_goal_status(
        self, goal_id: str, status: str, *, reason: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE goals SET status=?, status_reason=?, updated_at=? WHERE goal_id=?",
                (status, reason, _now(), goal_id),
            )

    def add_goal_attempt(
        self, goal_id: str, run_id: str, *,
        parent_attempt_id: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS n FROM goal_attempts WHERE goal_id=?",
                (goal_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO goal_attempts(attempt_id, goal_id, run_id, parent_attempt_id,"
                " sequence, attribution_json, progress_json, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, goal_id, run_id, parent_attempt_id, int(row["n"]) + 1,
                 _json(attribution or {}), _json({}), "running", now, now),
            )
        return self.get_goal_attempt(attempt_id)

    def get_goal_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM goal_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        return self._attempt_record(row) if row else None

    def get_goal_attempt_by_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM goal_attempts WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._attempt_record(row) if row else None

    def list_goal_attempts(self, goal_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM goal_attempts WHERE goal_id=? ORDER BY sequence", (goal_id,)
            ).fetchall()
        return [self._attempt_record(row) for row in rows]

    def update_goal_attempt(
        self, attempt_id: str, *, status: str | None = None,
        progress: dict[str, Any] | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> None:
        sets, params = ["updated_at=?"], [_now()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if progress is not None:
            sets.append("progress_json=?")
            params.append(_json(progress))
        if attribution is not None:
            sets.append("attribution_json=?")
            params.append(_json(attribution))
        params.append(attempt_id)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE goal_attempts SET {', '.join(sets)} WHERE attempt_id=?", params
            )

    @staticmethod
    def _goal_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "goal_id": row["goal_id"], "source": row["source"],
            "metric": row["metric"], "target": json.loads(row["target_json"]),
            "baseline": json.loads(row["baseline_json"]),
            "budget_attempts": int(row["budget_attempts"]),
            "deadline": row["deadline"], "status": row["status"],
            "status_reason": row["status_reason"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": row["attempt_id"], "goal_id": row["goal_id"],
            "run_id": row["run_id"], "parent_attempt_id": row["parent_attempt_id"],
            "sequence": int(row["sequence"]),
            "attribution": json.loads(row["attribution_json"]),
            "progress": json.loads(row["progress_json"]),
            "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def mark_rule_conflicted(self, rule_id: str, conflicted: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE semantic_rules SET conflicted=?, updated_at=? WHERE rule_id=?",
                (1 if conflicted else 0, _now(), rule_id),
            )

    def activate_only_rules(self, rule_ids: list[str]) -> None:
        """使本轮未重新归纳出的旧规律失效，避免归档证据继续影响召回。"""
        now = _now()
        with self._lock, self._conn:
            self._conn.execute("UPDATE semantic_rules SET active=0, updated_at=?", (now,))
            if rule_ids:
                placeholders = ",".join("?" for _ in rule_ids)
                self._conn.execute(
                    f"UPDATE semantic_rules SET active=1, updated_at=? WHERE rule_id IN ({placeholders})",
                    [now, *rule_ids],
                )

    def acquire_lock(self, name: str, holder: str, *, ttl_seconds: float) -> bool:
        """获取维护锁：无人持有或已过期才成功；防 consolidation 与写入并发。"""
        now = datetime.now(timezone.utc)
        now_s = now.isoformat(timespec="milliseconds")
        expires_s = (now + timedelta(seconds=max(1.0, ttl_seconds))).isoformat(timespec="milliseconds")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT holder, expires_at FROM maintenance_locks WHERE lock_name=?", (name,)
            ).fetchone()
            if row is not None and row["expires_at"] > now_s and row["holder"] != holder:
                return False
            self._conn.execute(
                """
                INSERT INTO maintenance_locks(lock_name, holder, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_name) DO UPDATE SET
                    holder=excluded.holder, acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at
                """,
                (name, holder, now_s, expires_s),
            )
            return True

    def release_lock(self, name: str, holder: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM maintenance_locks WHERE lock_name=? AND holder=?", (name, holder)
            )

    def schedule_evaluation(
        self,
        run_id: str,
        *,
        window_label: str,
        window_seconds: float,
        due_at: str,
        baseline: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Create a pending outcome-evaluation window, idempotent per (run, window)."""
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM outcome_evaluations WHERE run_id=? AND window_label=?",
                (run_id, window_label),
            ).fetchone()
            if row is not None:
                return self._evaluation_record(row), False
            evaluation_id = uuid.uuid4().hex
            self._conn.execute(
                """
                INSERT INTO outcome_evaluations(
                    evaluation_id, run_id, window_label, window_seconds, due_at,
                    status, baseline_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    evaluation_id, run_id, window_label, float(window_seconds),
                    due_at, _json(baseline), now, now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM outcome_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            return self._evaluation_record(row), True

    def list_evaluations(
        self,
        run_id: str | None = None,
        *,
        status: str | None = None,
        due_before: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if due_before:
            clauses.append("due_at<=?")
            params.append(due_before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outcome_evaluations" + where
                + " ORDER BY due_at ASC, window_label ASC",
                params,
            ).fetchall()
        return [self._evaluation_record(row) for row in rows]

    def claim_due_evaluations(
        self, *, due_before: str, claimant: str, run_id: str | None = None,
        lease_seconds: float = 300.0,
    ) -> list[dict[str, Any]]:
        """Atomically claim due windows so concurrent harvesters cannot collect twice."""
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(seconds=max(1.0, lease_seconds))).isoformat(
            timespec="milliseconds"
        )
        now_s = now.isoformat(timespec="milliseconds")
        run_clause = " AND run_id=?" if run_id else ""
        params: list[Any] = [due_before, stale]
        if run_id:
            params.append(run_id)
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT evaluation_id FROM outcome_evaluations "
                "WHERE due_at<=? AND (status='pending' OR "
                "(status='processing' AND claimed_at<?))" + run_clause,
                params,
            ).fetchall()
            ids = [row["evaluation_id"] for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE outcome_evaluations SET status='processing', claimed_at=?, "
                f"claimant=?, updated_at=? WHERE evaluation_id IN ({placeholders})",
                [now_s, claimant, now_s, *ids],
            )
            claimed = self._conn.execute(
                f"SELECT * FROM outcome_evaluations WHERE evaluation_id IN ({placeholders}) "
                "ORDER BY due_at ASC, window_label ASC", ids,
            ).fetchall()
        return [self._evaluation_record(row) for row in claimed]

    def release_evaluation_claims(self, evaluation_ids: list[str], claimant: str) -> None:
        if not evaluation_ids:
            return
        placeholders = ",".join("?" for _ in evaluation_ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE outcome_evaluations SET status='pending', claimed_at=NULL, "
                f"claimant=NULL, updated_at=? WHERE claimant=? AND status='processing' "
                f"AND evaluation_id IN ({placeholders})",
                [_now(), claimant, *evaluation_ids],
            )

    def finish_evaluation(
        self,
        evaluation_id: str,
        *,
        status: str,
        observed: dict[str, Any] | None = None,
        deltas: dict[str, Any] | None = None,
        verdict: str | None = None,
        confidence: float | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"collected", "failed", "abandoned"}:
            raise ValueError(f"invalid terminal evaluation status: {status}")
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE outcome_evaluations SET status=?, observed_json=?,
                    deltas_json=?, verdict=?, confidence=?, error=?,
                    updated_at=?, collected_at=? WHERE evaluation_id=?
                """,
                (
                    status,
                    _json(observed) if observed is not None else None,
                    _json(deltas) if deltas is not None else None,
                    verdict,
                    confidence,
                    error,
                    now,
                    now,
                    evaluation_id,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM outcome_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(evaluation_id)
            return self._evaluation_record(row)

    def update_episode_evaluation(
        self,
        run_id: str,
        *,
        evaluation: dict[str, Any],
        quality_score: float, quality_vector: dict[str, Any] | None = None,
        lifecycle: str | None = None,
    ) -> bool:
        now = _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE episodic_memories SET evaluation_json=?, quality_score=?,
                    quality_vector_json=?, lifecycle=?, updated_at=? WHERE run_id=?
                """,
                (_json(evaluation), float(quality_score), _json(quality_vector or {}),
                 lifecycle or "evaluated", now, run_id),
            )
            return cursor.rowcount > 0

    def update_episode_narrative(
        self, run_id: str, *, narrative: str | None, model: str | None,
        evidence_hash: str, status: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE episodic_memories SET case_narrative=?, case_narrative_model=?, "
                "case_narrative_evidence_hash=?, case_narrative_status=?, updated_at=? "
                "WHERE run_id=?",
                (narrative, model, evidence_hash, status, _now(), run_id),
            )

    def upsert_rule(self, rule: dict[str, Any]) -> str:
        """按 (topology, scene, strategy) upsert 一条归纳规律，保留 created_at。"""
        now = _now()
        key = (rule["topology_signature"], rule.get("scene"), rule.get("strategy"))
        with self._lock, self._conn:
            existing = self._conn.execute(
                """
                SELECT rule_id, created_at FROM semantic_rules
                WHERE topology_signature IS ? AND scene IS ? AND strategy IS ?
                """,
                key,
            ).fetchone()
            rule_id = existing["rule_id"] if existing else uuid.uuid4().hex
            created_at = existing["created_at"] if existing else now
            self._conn.execute(
                """
                INSERT INTO semantic_rules(
                    rule_id, topology_signature, scene, strategy, dominant_verdict,
                    support, consistency, confidence, verdict_counts_json,
                    action_summary_json, evidence_json, llm_summary, llm_model,
                    llm_evidence_hash, llm_prompt_version, llm_status, llm_error,
                    llm_generated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    dominant_verdict=excluded.dominant_verdict,
                    support=excluded.support,
                    consistency=excluded.consistency,
                    confidence=excluded.confidence,
                    verdict_counts_json=excluded.verdict_counts_json,
                    action_summary_json=excluded.action_summary_json,
                    evidence_json=excluded.evidence_json,
                    llm_summary=excluded.llm_summary,
                    llm_model=excluded.llm_model,
                    llm_evidence_hash=excluded.llm_evidence_hash,
                    llm_prompt_version=excluded.llm_prompt_version,
                    llm_status=excluded.llm_status,
                    llm_error=excluded.llm_error,
                    llm_generated_at=excluded.llm_generated_at,
                    updated_at=excluded.updated_at
                """,
                (
                    rule_id, rule["topology_signature"], rule.get("scene"),
                    rule.get("strategy"), rule["dominant_verdict"],
                    int(rule["support"]), float(rule["consistency"]),
                    float(rule["confidence"]), _json(rule.get("verdict_counts") or {}),
                    _json(rule.get("action_summary") or {}),
                    _json(rule.get("evidence") or []), rule.get("llm_summary"),
                    rule.get("llm_model"), rule.get("llm_evidence_hash"),
                    rule.get("llm_prompt_version"), rule.get("llm_status"),
                    rule.get("llm_error"), rule.get("llm_generated_at"), created_at, now,
                ),
            )
        return rule_id

    def list_rules(
        self,
        *,
        topology_signature: str | None = None,
        scene: str | None = None,
        min_confidence: float = 0.0,
        include_conflicted: bool = False,
    ) -> list[dict[str, Any]]:
        clauses, params = ["confidence>=?", "active=1"], [float(min_confidence)]
        if not include_conflicted:
            clauses.append("conflicted=0")
        if topology_signature:
            clauses.append("topology_signature=?")
            params.append(topology_signature)
        if scene:
            clauses.append("scene=?")
            params.append(scene)
        where = " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM semantic_rules" + where
                + " ORDER BY confidence DESC, support DESC",
                params,
            ).fetchall()
        return [self._rule_record(row) for row in rows]

    def delete_rules(self) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM semantic_rules")
            return cursor.rowcount

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._run_record(row) if row is not None else None

    def has_completed_run_between(
        self, *, after: str, before: str, exclude_run_id: str
    ) -> bool:
        """Return whether another completed negotiation can confound an outcome window."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM agent_runs WHERE run_id<>? AND completed_at>? "
                "AND completed_at<=? LIMIT 1",
                (exclude_run_id, after, before),
            ).fetchone()
        return row is not None

    def run_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM agent_runs GROUP BY status"
            ).fetchall()
        counts = {row["status"]: int(row["n"]) for row in rows}
        total = sum(counts.values())
        completed = counts.get("completed", 0)
        return {"total": total, "completed": completed, "incomplete": total - completed,
                **counts}

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
            "quality_vector": load("quality_vector_json", {}),
            "lifecycle": row["lifecycle"] if "lifecycle" in row.keys() else "evaluated",
            "episode_fingerprint": row["episode_fingerprint"] if "episode_fingerprint" in row.keys() else None,
            "feature_schema_version": row["feature_schema_version"] if "feature_schema_version" in row.keys() else 1,
            "evaluation_policy_version": row["evaluation_policy_version"] if "evaluation_policy_version" in row.keys() else 1,
            "case_narrative": row["case_narrative"] if "case_narrative" in row.keys() else None,
            "case_narrative_model": row["case_narrative_model"] if "case_narrative_model" in row.keys() else None,
            "case_narrative_evidence_hash": row["case_narrative_evidence_hash"] if "case_narrative_evidence_hash" in row.keys() else None,
            "case_narrative_status": row["case_narrative_status"] if "case_narrative_status" in row.keys() else None,
            "evaluation": load("evaluation_json"),
            "archived": bool(row["archived"]) if "archived" in row.keys() else False,
            "last_verified_at": row["last_verified_at"] if "last_verified_at" in row.keys() else None,
            "contradictions": int(row["contradictions"]) if "contradictions" in row.keys() else 0,
            "quarantined": bool(row["quarantined"]) if "quarantined" in row.keys() else False,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _rule_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "topology_signature": row["topology_signature"],
            "scene": row["scene"],
            "strategy": row["strategy"],
            "dominant_verdict": row["dominant_verdict"],
            "support": row["support"],
            "consistency": row["consistency"],
            "confidence": row["confidence"],
            "verdict_counts": json.loads(row["verdict_counts_json"]),
            "action_summary": json.loads(row["action_summary_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "llm_summary": row["llm_summary"] if "llm_summary" in row.keys() else None,
            "llm_model": row["llm_model"] if "llm_model" in row.keys() else None,
            "llm_evidence_hash": row["llm_evidence_hash"] if "llm_evidence_hash" in row.keys() else None,
            "llm_prompt_version": row["llm_prompt_version"] if "llm_prompt_version" in row.keys() else None,
            "llm_status": row["llm_status"] if "llm_status" in row.keys() else None,
            "llm_error": row["llm_error"] if "llm_error" in row.keys() else None,
            "llm_generated_at": row["llm_generated_at"] if "llm_generated_at" in row.keys() else None,
            "conflicted": bool(row["conflicted"]) if "conflicted" in row.keys() else False,
            "active": bool(row["active"]) if "active" in row.keys() else True,
            "last_verified_at": row["last_verified_at"] if "last_verified_at" in row.keys() else None,
            "contradictions": int(row["contradictions"]) if "contradictions" in row.keys() else 0,
            "quarantined": bool(row["quarantined"]) if "quarantined" in row.keys() else False,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _evaluation_record(row: sqlite3.Row) -> dict[str, Any]:
        def load(column: str, default=None):
            return json.loads(row[column]) if row[column] is not None else default
        return {
            "evaluation_id": row["evaluation_id"],
            "run_id": row["run_id"],
            "window_label": row["window_label"],
            "window_seconds": row["window_seconds"],
            "due_at": row["due_at"],
            "status": row["status"],
            "baseline": load("baseline_json", {}),
            "observed": load("observed_json"),
            "deltas": load("deltas_json"),
            "verdict": row["verdict"],
            "confidence": row["confidence"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "collected_at": row["collected_at"],
        }

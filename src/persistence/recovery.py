"""Recovery projections derived from the durable event stream."""

from __future__ import annotations

from dataclasses import dataclass

from .event_store import EventStore, RunRecord


TERMINAL_EVENTS = {"session_end"}


@dataclass(frozen=True)
class RunCheckpoint:
    run: RunRecord
    last_sequence: int
    last_event: str | None
    current_phase: int | None
    can_resume: bool
    resume_reason: str
    blocking_actions: tuple[str, ...]
    boundary: str | None
    projection: dict | None


def build_checkpoint(store: EventStore, run_id: str) -> RunCheckpoint | None:
    """Project an incomplete run into a conservative recovery checkpoint.

    Phase-level continuation is deliberately not enabled yet: replay is safe,
    but resuming after a side effect needs action idempotency in the next slice.
    """
    run = store.get_run(run_id)
    if run is None:
        return None
    events = store.load_events(run_id)
    last = events[-1] if events else None
    terminal = bool(last and last["event"] in TERMINAL_EVENTS)
    actions = store.list_actions(run_id)
    blocking = tuple(
        action.action_id for action in actions if action.status in {"running", "unknown"}
    )
    incomplete = run.status != "completed" and not terminal
    projection = store.load_projection(run_id)
    session_memory = store.load_session_memory(run_id)
    if projection and session_memory:
        projection["state"]["session_memory"] = session_memory["memory"]
    projection_safe = bool(projection and projection["safe_to_resume"])
    return RunCheckpoint(
        run=run,
        last_sequence=int(last["sequence"]) if last else 0,
        last_event=str(last["event"]) if last else None,
        current_phase=run.current_phase,
        can_resume=incomplete and not blocking and projection_safe,
        resume_reason=(
            "run completed"
            if run.status == "completed" or terminal
            else (
                "manual reconciliation required for running/unknown side effects"
                if blocking
                else (
                    "safe negotiation checkpoint available"
                    if projection_safe
                    else "no safe negotiation projection available"
                )
            )
        ),
        blocking_actions=blocking,
        boundary=projection["boundary"] if projection else None,
        projection=projection["state"] if projection else None,
    )

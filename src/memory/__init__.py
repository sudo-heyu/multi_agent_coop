"""Domain memory services."""

from .session_memory import SessionMemory, SessionMemoryManager
from .episodic import (
    encode_features, find_similar_episodes, materialize_episode, pipeline_quality,
)
from .outcome import (
    DEFAULT_WINDOWS,
    abandon_stale_evaluations,
    apply_evaluation_to_episode,
    build_rollback_plan,
    classify,
    collect_due_evaluations,
    evaluate_deltas,
    evaluation_diagnostics,
    harvest_evaluations,
    parse_windows,
    revise_quality,
    schedule_outcome_evaluations,
    summarize_run_evaluations,
)
from .rollback import execute_rollback, resolve_rollback_plan
from .semantic import find_matching_rules, format_rule, induce_rules
from .consolidation import ConsolidationConfig, consolidate

__all__ = [
    "SessionMemory", "SessionMemoryManager", "encode_features",
    "find_similar_episodes", "materialize_episode", "pipeline_quality",
    "DEFAULT_WINDOWS", "abandon_stale_evaluations", "apply_evaluation_to_episode",
    "build_rollback_plan", "classify", "collect_due_evaluations", "evaluate_deltas",
    "evaluation_diagnostics", "harvest_evaluations", "parse_windows",
    "revise_quality", "schedule_outcome_evaluations", "summarize_run_evaluations",
    "execute_rollback", "resolve_rollback_plan",
    "find_matching_rules", "format_rule", "induce_rules",
    "ConsolidationConfig", "consolidate",
]

"""记忆可观测性：把六层记忆的关键健康指标聚合成一个可监控视图。

用于 Dashboard `/memory` 页面和 `memory_admin.py health`，一眼看清：案例规模与
质量分布、评估窗口积压/放弃、规律数量与冲突、runs 完成度。全部只读聚合。
"""

from __future__ import annotations

import statistics
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from src.persistence import EventStore


def memory_health(store: EventStore) -> dict[str, Any]:
    """聚合记忆系统健康度快照（只读）。"""
    episodes = store.list_episodes(limit=1000, include_archived=True)
    alive = [e for e in episodes if not e.get("archived")]
    archived = [e for e in episodes if e.get("archived")]

    verdict_counts: Counter = Counter()
    for episode in alive:
        verdict = (episode.get("evaluation") or {}).get("final_verdict")
        verdict_counts[verdict or "unevaluated"] += 1
    qualities = sorted(e["quality_score"] for e in alive)

    evaluations = store.list_evaluations()
    eval_status = Counter(e["status"] for e in evaluations)

    rules = store.list_rules(include_conflicted=True)
    active_rules = [r for r in rules if not r.get("conflicted")]
    rule_verdicts = Counter(r["dominant_verdict"] for r in active_rules)
    llm_status = Counter(r.get("llm_status") or "legacy" for r in rules)
    avg_conf = (
        round(statistics.mean(r["confidence"] for r in active_rules), 4)
        if active_rules else None
    )

    topo_counts = store.count_episodes_by_topology()
    now = datetime.now(timezone.utc)
    overdue_pending = sum(
        1 for item in evaluations
        if item["status"] == "pending" and datetime.fromisoformat(item["due_at"]) < now
    )
    agent_memory = {}
    session_counts = store.agent_session_memory_counts()
    from .workspace import AGENT_IDS, workspace
    for agent in AGENT_IDS:
        cases = store.list_agent_episodes(agent, min_quality=0.0, limit=100)
        memory_file = workspace(agent) / "MEMORY.md"
        session_file = workspace(agent) / "memory" / "current-session.json"
        agent_memory[agent] = {
            "episodes": len(cases),
            "session_memories": session_counts.get(agent, 0),
            "evaluated": sum(1 for e in cases if (e.get("evaluation") or {}).get("final_verdict")),
            "workspace_memory_exists": memory_file.exists(),
            "workspace_session_exists": session_file.exists(),
            "workspace_memory_bytes": memory_file.stat().st_size if memory_file.exists() else 0,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs": store.run_counts(),
        "episodes": {
            "total": len(episodes),
            "alive": len(alive),
            "archived": len(archived),
            "quality": _distribution(qualities),
            "by_verdict": dict(verdict_counts),
            "degraded_ratio": _ratio(verdict_counts.get("degraded", 0), len(alive)),
        },
        "evaluations": {
            "pending": eval_status.get("pending", 0),
            "collected": eval_status.get("collected", 0),
            "abandoned": eval_status.get("abandoned", 0),
            "failed": eval_status.get("failed", 0),
            "overdue_pending": overdue_pending,
        },
        "rules": {
            "total": len(rules),
            "active": len(active_rules),
            "conflicted": len(rules) - len(active_rules),
            "avg_confidence": avg_conf,
            "by_verdict": dict(rule_verdicts),
            "llm_status": dict(llm_status),
        },
        "topologies": {
            "count": len(topo_counts),
            "max_alive_per_topology": max(topo_counts.values()) if topo_counts else 0,
        },
        "agent_memory": agent_memory,
        "services": store.list_service_heartbeats(),
    }


def _distribution(sorted_values: list[float]) -> dict[str, Any]:
    if not sorted_values:
        return {"count": 0}
    n = len(sorted_values)
    return {
        "count": n,
        "min": round(sorted_values[0], 4),
        "median": round(statistics.median(sorted_values), 4),
        "max": round(sorted_values[-1], 4),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0

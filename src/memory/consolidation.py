"""L6 Consolidation: 带锁、门控、容量/过期归档与规律冲突检测的后台整理。

记忆若只增不整理，案例库会无限膨胀、旧规律会误导决策。L6 定期做确定性整理：
按拓扑容量上限和年龄/质量归档冗余案例（软删，可审计），重新归纳规律，并把
证据分歧过大的规律标记为 conflicted（不再注入提案）。整理是破坏性操作，用
维护锁串行化，避免与协商写入/后台收割并发。

所有判据确定、可测；软删不物理删除，保留完整审计链。
"""

from __future__ import annotations

import socket
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.persistence import EventStore

from .semantic import induce_rules


LOCK_NAME = "consolidation"


@dataclass(frozen=True)
class ConsolidationConfig:
    max_per_topology: int = 50       # 每个拓扑签名保留的存活案例上限
    max_age_days: float = 90.0       # 超过此年龄且低质的案例归档
    min_quality_keep: float = 0.3    # 过期归档的质量下限（低于才归档）
    conflict_consistency: float = 0.6  # 规律一致性低于此值视为证据分歧→conflicted
    lock_ttl_seconds: float = 300.0


def consolidate(
    store: EventStore,
    *,
    config: ConsolidationConfig | None = None,
    holder: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """执行一次整理：加锁 → 容量淘汰 + 过期归档 → 重新归纳 → 冲突标记 → 解锁。

    拿不到锁（他人正在整理）直接返回 skipped，不阻塞。
    """
    config = config or ConsolidationConfig()
    holder = holder or f"{socket.gethostname()}:{os.getpid()}"
    now = now or datetime.now(timezone.utc)
    if not store.acquire_lock(LOCK_NAME, holder, ttl_seconds=config.lock_ttl_seconds):
        return {"status": "skipped", "reason": "another consolidation holds the lock"}
    try:
        capacity = _archive_over_capacity(store, config)
        expired = _archive_expired(store, config, now)
        # 仅后台整理调用可选 LLM，协商关键路径从不等待模型。
        rules = induce_rules(store, use_llm=True)  # archived 已排除
        conflicted = _flag_conflicted_rules(store, rules, config)
        return {
            "status": "done",
            "archived_over_capacity": capacity,
            "archived_expired": expired,
            "rules_total": len(rules),
            "conflicted_rules": conflicted,
        }
    finally:
        store.release_lock(LOCK_NAME, holder)


def _archive_over_capacity(store: EventStore, config: ConsolidationConfig) -> list[str]:
    """每个拓扑存活案例超上限时，保留质量最高/最新的 top-N，其余归档。"""
    archived: list[str] = []
    counts = store.count_episodes_by_topology()
    for topology, count in counts.items():
        if count <= config.max_per_topology:
            continue
        episodes = store.list_episodes(topology_signature=topology, limit=100_000)
        # list_episodes 已按 quality_score DESC, created_at DESC 排序：保留前 N。
        losers = episodes[config.max_per_topology:]
        run_ids = [e["run_id"] for e in losers]
        if run_ids:
            store.archive_episodes(run_ids)
            archived.extend(run_ids)
    return archived


def _archive_expired(
    store: EventStore, config: ConsolidationConfig, now: datetime
) -> list[str]:
    """归档年龄超限且质量低于保留下限的案例（老且不够好的先淘汰）。"""
    cutoff = now.timestamp() - config.max_age_days * 86400.0
    stale: list[str] = []
    for episode in store.list_episodes(limit=100_000):
        if episode["quality_score"] >= config.min_quality_keep:
            continue
        created = _parse_ts(episode["created_at"]).timestamp()
        if created <= cutoff:
            stale.append(episode["run_id"])
    if stale:
        store.archive_episodes(stale)
    return stale


def _flag_conflicted_rules(
    store: EventStore, rules: list[dict[str, Any]], config: ConsolidationConfig
) -> list[str]:
    """一致性低于阈值的规律标 conflicted（证据分歧大，不再注入提案）。

    重归纳后 upsert 保留旧 conflicted 位，这里按最新一致性重新裁决：分歧大的
    置 1，重新变一致的清 0，保证冲突状态随证据演进。
    """
    flagged: list[str] = []
    for rule in rules:
        conflicted = rule["consistency"] < config.conflict_consistency
        store.mark_rule_conflicted(rule["rule_id"], conflicted)
        if conflicted:
            flagged.append(rule["rule_id"])
    return flagged


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

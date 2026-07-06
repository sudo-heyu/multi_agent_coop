"""反思模块：记忆信任模型（确定性纯函数）。

trust = 基础置信度 × 时效衰减 × 矛盾惩罚

- 基础置信度：案例取执行后评估的 final_confidence（未评估时退化为 quality_score），
  规律取归纳 confidence；
- 时效衰减：距最近一次被真实反馈证实（last_verified_at，缺省回退 created_at）
  的时间按半衰期指数衰减——无线环境非平稳，越久未验证越不可信；
- 矛盾惩罚：矛盾账本每记一笔，信任乘性下降。

全部计算同输入必同输出，不调用 LLM；阈值可经环境变量部署级校准。
隔离判定只降权不删除（红线 9），实际隔离动作由数据层 set_memory_quarantined 执行。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# 半衰期：最近验证起 7 天信任减半（mock 校准用 MULTIAP_TRUST_HALF_LIFE_SECONDS 缩短）。
TRUST_HALF_LIFE_SECONDS = _env_float("MULTIAP_TRUST_HALF_LIFE_SECONDS", 7 * 86400.0)
# 每笔矛盾把信任乘以该系数：1 笔 → 0.5，2 笔 → 0.25。
CONTRADICTION_FACTOR = _env_float("MULTIAP_CONTRADICTION_FACTOR", 0.5)
# 信任低于该阈值 → 建议隔离（停止注入，等待再验证）。
QUARANTINE_TRUST_THRESHOLD = _env_float("MULTIAP_QUARANTINE_TRUST", 0.15)
# 矛盾累计达到该笔数 → 无条件建议隔离。
QUARANTINE_CONTRADICTIONS = int(_env_float("MULTIAP_QUARANTINE_CONTRADICTIONS", 3))
# 时效衰减下限：老记忆保留微弱权重用于排序，不会因纯粹变老直接归零。
FRESHNESS_FLOOR = _env_float("MULTIAP_FRESHNESS_FLOOR", 0.05)


def freshness(age_seconds: float, *, half_life: float = TRUST_HALF_LIFE_SECONDS) -> float:
    """按半衰期计算时效系数，落在 [FRESHNESS_FLOOR, 1]。"""
    if age_seconds <= 0:
        return 1.0
    if half_life <= 0:
        return 1.0
    decay = 0.5 ** (age_seconds / half_life)
    return round(max(FRESHNESS_FLOOR, decay), 4)


def contradiction_penalty(count: int, *, factor: float = CONTRADICTION_FACTOR) -> float:
    """矛盾惩罚系数：无矛盾为 1，逐笔乘 factor。"""
    if count <= 0:
        return 1.0
    return round(factor ** count, 4)


def trust_score(
    base_confidence: float, *, age_seconds: float, contradictions: int,
) -> float:
    """信任分主公式，截断到 [0, 1]，四位小数保证可复算比对。"""
    base = min(1.0, max(0.0, float(base_confidence)))
    value = base * freshness(age_seconds) * contradiction_penalty(contradictions)
    return round(min(1.0, max(0.0, value)), 4)


def base_confidence(memory: dict[str, Any]) -> float:
    """从案例/规律记录里取基础置信度。

    案例：优先执行后评估 final_confidence；未评估退化为 quality_score
    （维持"验证后召回"原则下的排序行为，不额外奖励未验证案例）。
    规律：归纳 confidence。
    """
    evaluation = memory.get("evaluation")
    if isinstance(evaluation, dict) and evaluation.get("final_confidence") is not None:
        return min(1.0, max(0.0, float(evaluation["final_confidence"])))
    if memory.get("confidence") is not None:
        return min(1.0, max(0.0, float(memory["confidence"])))
    return min(1.0, max(0.0, float(memory.get("quality_score") or 0.0)))


def memory_age_seconds(memory: dict[str, Any], *, now: datetime | None = None) -> float:
    """距最近验证的秒数：last_verified_at 缺省回退 created_at；无法解析视为 0。"""
    anchor = memory.get("last_verified_at") or memory.get("created_at")
    if not anchor:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(anchor))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - parsed).total_seconds())


def memory_trust(memory: dict[str, Any], *, now: datetime | None = None) -> float:
    """对一条 store 记录（案例 / agent 本地案例 / 规律）计算信任分。"""
    return trust_score(
        base_confidence(memory),
        age_seconds=memory_age_seconds(memory, now=now),
        contradictions=int(memory.get("contradictions") or 0),
    )


def should_quarantine(trust: float, contradictions: int) -> bool:
    """隔离建议：信任跌破阈值，或矛盾笔数到达上限。"""
    if contradictions >= QUARANTINE_CONTRADICTIONS:
        return True
    return trust < QUARANTINE_TRUST_THRESHOLD


def enabled() -> bool:
    """反思门控总开关：MULTIAP_REFLECTION=0 时召回行为回退到 v15 版本。"""
    return os.environ.get("MULTIAP_REFLECTION", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def gate_memories(
    memories: list[dict[str, Any]], *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """召回门控：剔除隔离区记忆和信任跌破隔离阈值的记忆，并附加信任分。

    纯内存计算，不触发额外 SQL / LLM；排序仍由召回方的既有打分决定，
    信任分附加在 "trust" 字段供假设化注入展示。反思关闭时原样返回。
    """
    if not enabled():
        return memories
    reference = now or datetime.now(timezone.utc)
    gated = []
    for memory in memories:
        if memory.get("quarantined"):
            continue
        trust = memory_trust(memory, now=reference)
        if trust < QUARANTINE_TRUST_THRESHOLD:
            continue
        gated.append({**memory, "trust": trust})
    return gated

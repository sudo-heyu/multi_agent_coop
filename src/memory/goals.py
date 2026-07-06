"""迭代模块：目标对象与目标驱动的多轮尝试（确定性）。

Goal 是 Event Store 一等公民（I1）：可度量指标 + 目标值 + 迭代预算 + 状态。
一个目标串起 N 次 attempt（每次 attempt = 一次完整协商 run，parent_attempt_id
成链），进度评分复用效果评估窗口的观测状态，全部纯函数比较，不调用 LLM。

红线 10：目标驱动的每一轮尝试仍走完整协商 → Validator → 幂等 journal 链路；
本模块只创建/记账/评分/停机标记，绝不直接下发参数。
"""

from __future__ import annotations

import os
from typing import Any

VALID_OPS = ("<=", ">=", "<", ">")
GOAL_SOURCES = ("admin", "auto")
# 自动触发：某指标连续 N 个已结算评估窗口越界（可环境变量校准）。
AUTO_TRIGGER_WINDOWS = int(os.environ.get("MULTIAP_GOAL_TRIGGER_WINDOWS", "3") or 3)
DEFAULT_BUDGET_ATTEMPTS = int(os.environ.get("MULTIAP_GOAL_BUDGET_ATTEMPTS", "5") or 5)


def enabled() -> bool:
    """迭代模块总开关：MULTIAP_GOALS=0 时不创建目标、不登记 attempt。"""
    return os.environ.get("MULTIAP_GOALS", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def validate_target(target: dict[str, Any]) -> None:
    if not isinstance(target, dict):
        raise ValueError("target 必须是 dict")
    for field in ("ap", "metric", "op", "value"):
        if field not in target:
            raise ValueError(f"target 缺少字段: {field}")
    if target["op"] not in VALID_OPS:
        raise ValueError(f"op 必须是 {VALID_OPS} 之一: {target['op']!r}")
    float(target["value"])


def create_goal(
    store: Any, *, target: dict[str, Any], source: str = "admin",
    budget_attempts: int = DEFAULT_BUDGET_ATTEMPTS,
    baseline: dict[str, Any] | None = None, deadline: str | None = None,
) -> dict[str, Any]:
    """创建目标。单活跃目标起步：已有 active 目标时拒绝（明确不做多目标调度）。"""
    validate_target(target)
    if source not in GOAL_SOURCES:
        raise ValueError(f"source 必须是 {GOAL_SOURCES} 之一: {source!r}")
    active = store.list_goals(status="active")
    if active:
        raise ValueError(f"已存在活跃目标 {active[0]['goal_id']}，先完成或放弃它")
    metric = f"{target['ap']}.{target['metric']}{target['op']}{target['value']}"
    return store.create_goal(
        metric=metric, target=target, source=source,
        budget_attempts=max(1, int(budget_attempts)),
        baseline=baseline, deadline=deadline,
    )


def get_active_goal(store: Any) -> dict[str, Any] | None:
    goals = store.list_goals(status="active")
    return goals[0] if goals else None


def register_attempt(store: Any, goal_id: str, run_id: str) -> dict[str, Any] | None:
    """把一次协商 run 登记为目标的下一次 attempt（parent 自动指向上一次）。

    幂等：同一 run 已登记过则原样返回（--resume-run 恢复路径）。
    """
    if not enabled():
        return None
    existing = store.get_goal_attempt_by_run(run_id)
    if existing is not None and existing["goal_id"] == goal_id:
        return existing
    attempts = store.list_goal_attempts(goal_id)
    parent = attempts[-1]["attempt_id"] if attempts else None
    return store.add_goal_attempt(goal_id, run_id, parent_attempt_id=parent)


def metric_value(state: dict[str, Any], target: dict[str, Any]) -> float | None:
    """从 AP 状态里取目标指标值；缺失或非数值返回 None（不猜）。"""
    row = state.get(target["ap"])
    if not isinstance(row, dict):
        return None
    value = row.get(target["metric"])
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def target_met(value: float, target: dict[str, Any]) -> bool:
    threshold = float(target["value"])
    op = target["op"]
    if op == "<=":
        return value <= threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    return value > threshold


def score_goal_progress(
    goal: dict[str, Any], state: dict[str, Any],
) -> dict[str, Any]:
    """对一份观测状态给出目标进度（确定性纯函数）。

    gap 统一为"距达标还差多少"（达标方向为正数减小到 0）；观测缺指标时
    met=None，不奖不罚。
    """
    target = goal["target"]
    value = metric_value(state, target)
    if value is None:
        return {"metric": goal["metric"], "value": None, "met": None, "gap": None}
    threshold = float(target["value"])
    gap = value - threshold if target["op"] in {"<=", "<"} else threshold - value
    return {
        "metric": goal["metric"], "value": round(value, 6),
        "met": target_met(value, target), "gap": round(max(0.0, gap), 6),
    }


def record_attempt_result(
    store: Any, run_id: str, *, outcome: str,
    observed_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """协商结束/评估窗口结算时更新 attempt：状态 + 目标进度。"""
    attempt = store.get_goal_attempt_by_run(run_id)
    if attempt is None:
        return None
    goal = store.get_goal(attempt["goal_id"])
    progress = attempt["progress"]
    if goal is not None and observed_state:
        progress = score_goal_progress(goal, observed_state)
    status = "completed" if outcome == "success" else "failed"
    store.update_goal_attempt(attempt["attempt_id"], status=status, progress=progress)
    return store.get_goal_attempt(attempt["attempt_id"])


def auto_create_goal(
    store: Any, *, target: dict[str, Any],
    windows: int = AUTO_TRIGGER_WINDOWS,
    budget_attempts: int = DEFAULT_BUDGET_ATTEMPTS,
) -> dict[str, Any] | None:
    """确定性自动触发（I1）：指标连续 N 个已结算评估窗口越界则建目标。

    已有活跃目标、结算窗口不足 N 个、或任一窗口达标 → 不创建（返回 None）。
    """
    if not enabled():
        return None
    validate_target(target)
    if get_active_goal(store) is not None:
        return None
    collected = [
        item for item in store.list_evaluations(status="collected")
        if isinstance(item.get("observed"), dict)
    ]
    recent = collected[-max(1, int(windows)):]
    if len(recent) < max(1, int(windows)):
        return None
    for evaluation in recent:
        value = metric_value(evaluation["observed"], target)
        if value is None or target_met(value, target):
            return None
    baseline = {"trigger_windows": [e["evaluation_id"] for e in recent]}
    return create_goal(
        store, target=target, source="auto",
        budget_attempts=budget_attempts, baseline=baseline,
    )


def goal_overview(store: Any, goal_id: str) -> dict[str, Any] | None:
    """目标 + 完整 attempt 链（I2 审计视图，memory_admin goal show）。"""
    goal = store.get_goal(goal_id)
    if goal is None:
        return None
    return {**goal, "attempts": store.list_goal_attempts(goal_id)}

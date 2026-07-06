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
    goal = store.get_goal(goal_id)
    attribution = build_attribution(store, goal) if goal is not None else {}
    attempts = store.list_goal_attempts(goal_id)
    parent = attempts[-1]["attempt_id"] if attempts else None
    return store.add_goal_attempt(
        goal_id, run_id, parent_attempt_id=parent, attribution=attribution,
    )


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


# ---- 阶段6：迭代链归因 / 停机准则 / 目标提示注入 ----

def build_attribution(store: Any, goal: dict[str, Any]) -> dict[str, Any]:
    """基于上一次 attempt 的确定性归因（I2）：上次动作 → 观测 → 分类。

    分类规则（纯比较，不依赖 LLM）：
    - achieved：上次进度已达标；
    - no_data：上次观测缺目标指标；
    - improved_but_insufficient：gap 比上上次收窄但未达标（方向对，力度不够）；
    - worsened：gap 比上上次扩大（方向错了）；
    - no_effect：gap 未变化；
    - first_probe：这是第一次有观测的尝试，无参照。
    """
    attempts = store.list_goal_attempts(goal["goal_id"])
    if not attempts:
        return {}
    parent = attempts[-1]
    episode = store.get_episode(run_id=parent["run_id"])
    action = (episode or {}).get("decision")
    progress = parent.get("progress") or {}
    gap, met = progress.get("gap"), progress.get("met")
    if met is True:
        classification = "achieved"
    elif progress.get("value") is None:
        classification = "no_data"
    else:
        reference_gap = None
        for earlier in reversed(attempts[:-1]):
            earlier_gap = (earlier.get("progress") or {}).get("gap")
            if earlier_gap is not None:
                reference_gap = float(earlier_gap)
                break
        if reference_gap is None:
            classification = "first_probe"
        elif float(gap) < reference_gap:
            classification = "improved_but_insufficient"
        elif float(gap) > reference_gap:
            classification = "worsened"
        else:
            classification = "no_effect"
    return {
        "previous_run_id": parent["run_id"],
        "previous_attempt_id": parent["attempt_id"],
        "previous_action": action,
        "observed_progress": progress,
        "classification": classification,
    }


_CLASSIFICATION_HINTS = {
    "achieved": "上次已达标，本次仅需保持，不要过度调整。",
    "no_data": "上次观测缺目标指标，先确认数据来源，再做最小改动。",
    "improved_but_insufficient": "上次方向正确但力度不够：沿同方向加大调整幅度，"
                                 "不要原样重复上次参数。",
    "worsened": "上次调整使目标指标恶化：必须改变方向或换策略，禁止重复上次动作。",
    "no_effect": "上次调整对目标指标无影响：该参数可能不是瓶颈，换一个假设"
                 "（不同参数或不同策略）。",
    "first_probe": "首次有观测的尝试，无参照：提出保守的最小改动假设。",
}


def attribution_prompt(goal: dict[str, Any], attempt: dict[str, Any]) -> str:
    """把目标与归因渲染为提案提示块（I3）。"""
    import json as _json_mod
    lines = [
        f"【迭代目标】{goal['metric']}（第 {attempt['sequence']}/"
        f"{goal['budget_attempts']} 次尝试）。"
        "本轮提案必须显式说明预计如何推动该指标达标。"
    ]
    attribution = attempt.get("attribution") or {}
    if attribution:
        progress = attribution.get("observed_progress") or {}
        lines.append(
            "上次尝试归因："
            f"动作={_json_mod.dumps(attribution.get('previous_action'), ensure_ascii=False)}，"
            f"观测值={progress.get('value')}（距达标 gap={progress.get('gap')}），"
            f"归因分类={attribution.get('classification')}。"
        )
        hint = _CLASSIFICATION_HINTS.get(str(attribution.get("classification")))
        if hint:
            lines.append(f"要求：{hint}")
    return "\n".join(lines)


def build_goal_context(
    store: Any, goal: dict[str, Any], attempt: dict[str, Any],
) -> dict[str, Any]:
    """供 structured_relay 注入的目标上下文（含提示文本）。"""
    return {
        "goal_id": goal["goal_id"], "metric": goal["metric"],
        "attempt_id": attempt["attempt_id"], "sequence": attempt["sequence"],
        "budget_attempts": goal["budget_attempts"],
        "prompt": attribution_prompt(goal, attempt),
    }


def detect_oscillation(decisions: list[dict[str, Any] | None]) -> bool:
    """参数振荡（I4）：同一参数在最近三次尝试里 A→B→A 来回改。"""
    series: dict[tuple[str, str], list[Any]] = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        for ap, params in decision.items():
            if not isinstance(params, dict):
                continue
            for field, value in params.items():
                if isinstance(value, (int, float)):
                    series.setdefault((ap, field), []).append(float(value))
    for values in series.values():
        for i in range(len(values) - 2):
            a, b, c = values[i], values[i + 1], values[i + 2]
            if a == c and a != b:
                return True
    return False


def refresh_goal_after_evaluation(store: Any, run_id: str) -> dict[str, Any] | None:
    """评估窗口结算后回填目标进度并执行停机准则（I4）。

    - achieved：最近两个已结算窗口均达标（达标且在下一窗口保持）；
    - blocked(参数振荡)：最近三次尝试同一参数来回改；
    - blocked(预算耗尽)：attempt 数达预算且仍未达标。
    任何路径都不自动追加轮次、不自动回滚（红线 10）。
    """
    if not enabled():
        return None
    attempt = store.get_goal_attempt_by_run(run_id)
    if attempt is None:
        return None
    goal = store.get_goal(attempt["goal_id"])
    if goal is None or goal["status"] != "active":
        return None
    windows = [
        item for item in store.list_evaluations(run_id)
        if item.get("status") == "collected" and isinstance(item.get("observed"), dict)
    ]
    window_progress = [score_goal_progress(goal, item["observed"]) for item in windows]
    scored = [p for p in window_progress if p.get("met") is not None]
    if scored:
        progress = {**scored[-1], "windows_scored": len(scored)}
        store.update_goal_attempt(attempt["attempt_id"], progress=progress)
    # 停机准则 1：达标且在下一窗口保持。
    if len(scored) >= 2 and scored[-1]["met"] and scored[-2]["met"]:
        store.update_goal_status(
            goal["goal_id"], "achieved",
            reason=f"attempt #{attempt['sequence']} 连续两个评估窗口达标",
        )
        return {"goal_status": "achieved", "progress": scored[-1]}
    attempts = store.list_goal_attempts(goal["goal_id"])
    # 停机准则 2：参数振荡。
    recent_decisions = [
        (store.get_episode(run_id=item["run_id"]) or {}).get("decision")
        for item in attempts[-3:]
    ]
    if len(attempts) >= 3 and detect_oscillation(recent_decisions):
        store.update_goal_status(
            goal["goal_id"], "blocked",
            reason="参数振荡：同一参数在最近尝试中来回改，环境可能非平稳，需人工介入",
        )
        return {"goal_status": "blocked", "reason": "oscillation"}
    # 停机准则 3：预算耗尽。
    terminal = [item for item in attempts if item["status"] in {"completed", "failed"}]
    if len(terminal) >= goal["budget_attempts"]:
        store.update_goal_status(
            goal["goal_id"], "blocked",
            reason=f"预算耗尽：{goal['budget_attempts']} 次尝试后仍未达标",
        )
        return {"goal_status": "blocked", "reason": "budget_exhausted"}
    latest = scored[-1] if scored else None
    return {"goal_status": "active", "progress": latest}

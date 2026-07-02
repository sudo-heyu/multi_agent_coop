"""Human-approved rollback of a degraded decision, via the idempotent action journal.

Outcome 评估判定某次决策"实际恶化"时，只生成 rollback_plan（恢复到协商前参数），
不自动执行。本模块提供人工审批后的执行通道：默认 dry-run 仅回显计划，显式确认后
才经 action journal 幂等下发——成功动作不重发，网络不确定标 unknown 并阻塞，
必须人工核对 AP /status 后 resolve-action。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.persistence import EventStore

from .outcome import build_rollback_plan


def resolve_rollback_plan(episode: dict[str, Any]) -> dict[str, Any]:
    """取回滚参数计划：优先用评估已固化的 plan，缺失时按 initial+decision 现算。"""
    evaluation = episode.get("evaluation") or {}
    plan = evaluation.get("rollback_plan")
    if plan:
        return plan
    return build_rollback_plan(
        episode.get("initial_state") or {}, episode.get("decision") or {}
    )


def execute_rollback(
    store: EventStore,
    run_id: str,
    endpoints: dict[str, str] | None,
    *,
    confirm: bool = False,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """回滚某次决策到协商前参数。

    - confirm=False：dry-run，只返回计划与提示，不发任何请求；
    - confirm=True：对 plan 覆盖的每个 AP 走 action journal 幂等下发。

    rollback_plan 的值已是 executor /apply 期望的 wire 格式（CW 为指数、
    tx_power 为实际 dBm），直接下发，不做二次编码。
    """
    episode = store.get_episode(run_id=run_id)
    if episode is None:
        return {"ok": False, "error": f"episode not found for run: {run_id}"}
    evaluation = episode.get("evaluation") or {}
    plan = resolve_rollback_plan(episode)
    strategy = episode.get("strategy") or "co_edca"
    context = {
        "run_id": run_id,
        "final_verdict": evaluation.get("final_verdict"),
        "needs_rollback": evaluation.get("needs_rollback"),
        "strategy": strategy,
        "plan": plan,
    }
    if not plan:
        return {**context, "ok": False, "error": "无可回滚参数（决策未改动或缺少评估）"}
    if not confirm:
        return {
            **context,
            "ok": True,
            "mode": "dry_run",
            "hint": (
                "这是预演，未下发任何参数。确认无误后加 --confirm 并提供 "
                "--ap-endpoints 执行回滚。"
            ),
        }
    if not endpoints:
        return {**context, "ok": False, "error": "回滚执行需要 --ap-endpoints 指定各 AP 执行端点"}

    results: dict[str, dict[str, Any]] = {}
    for ap_id in sorted(plan):
        url = endpoints.get(ap_id) or endpoints.get(ap_id.upper())
        if not url:
            results[ap_id] = {"ok": False, "error": "缺少该 AP 的执行端点"}
            continue
        results[ap_id] = _apply_one(
            store, run_id, ap_id, url, strategy, plan[ap_id], timeout
        )
    all_ok = bool(results) and all(item.get("ok") for item in results.values())
    return {**context, "ok": all_ok, "mode": "executed", "results": results}


def _apply_one(
    store: EventStore,
    run_id: str,
    ap_id: str,
    url: str,
    strategy: str,
    params: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "session_id": f"rollback:{run_id}",
        "strategy": strategy,
        "ap_id": ap_id,
        "params": params,
    }
    canonical = json.dumps(
        {"run_id": run_id, "ap_id": ap_id, "strategy": strategy, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    idem_key = "rollback_apply:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    action, _ = store.prepare_action(
        run_id,
        idempotency_key=idem_key,
        action_type="rollback_apply",
        target=ap_id,
        request={"url": f"{url.rstrip('/')}/apply", "payload": payload},
    )
    if action.status == "succeeded":
        cached = action.response if isinstance(action.response, dict) else {}
        return {"ok": True, "cached": True, "response": cached.get("response", "幂等缓存命中")}
    if action.status in {"running", "unknown"}:
        return {
            "ok": False,
            "action_id": action.action_id,
            "status": action.status,
            "error": "存在未决副作用，需核对 AP /status 后 resolve-action 再重试",
        }
    if action.status == "failed" and action.attempts >= 2:
        return {
            "ok": False,
            "action_id": action.action_id,
            "error": f"已达最大尝试次数 {action.attempts}",
        }
    store.mark_action_running(action.action_id)
    try:
        import requests
        resp = requests.post(f"{url.rstrip('/')}/apply", json=payload, timeout=timeout)
        ok = resp.status_code == 200
        try:
            body = resp.json()
            msg = body.get("details", body)
        except Exception:  # noqa: BLE001
            msg = resp.text
        store.finish_action(
            action.action_id,
            status="succeeded" if ok else "failed",
            response={"ok": ok, "status_code": resp.status_code, "response": msg},
            error=None if ok else f"HTTP {resp.status_code}",
        )
        return {"ok": ok, "action_id": action.action_id, "response": str(msg)}
    except Exception as exc:  # noqa: BLE001 — 网络结果不确定，保守标 unknown 禁止自动重发
        store.finish_action(action.action_id, status="unknown", error=str(exc))
        return {
            "ok": False,
            "action_id": action.action_id,
            "status": "unknown",
            "error": f"{exc}（请求可能已到达 AP，需人工核对）",
        }

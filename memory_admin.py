"""Local SQLite event-store inspection utility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.logger import DEFAULT_EVENT_DB
from src.persistence import EventStore, build_checkpoint
from src.memory import (
    consolidate,
    evaluation_diagnostics,
    execute_rollback,
    find_similar_episodes,
    harvest_evaluations,
    induce_rules,
    summarize_run_evaluations,
)
from src.memory.workspace import AGENT_IDS, sync_long_term_memories
from openclaw.scenes import _parse_executor_endpoints


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-AP 本地 Agent 运行存储查询")
    parser.add_argument("--db", default=str(DEFAULT_EVENT_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("incomplete", help="列出未完成运行")
    show = sub.add_parser("show", help="显示运行和有序事件")
    show.add_argument("run_id")
    resolve = sub.add_parser("resolve-action", help="人工核对不确定副作用后更新 action")
    resolve.add_argument("action_id")
    resolve.add_argument("--status", choices=["succeeded", "failed"], required=True)
    resolve.add_argument("--note", required=True)
    episodes = sub.add_parser("episodes", help="列出已生成的 Episodic Memory")
    episodes.add_argument("--scene")
    episodes.add_argument("--limit", type=int, default=20)
    similar = sub.add_parser("similar", help="以某个 run 的初始状态检索相似案例")
    similar.add_argument("run_id")
    similar.add_argument("--limit", type=int, default=5)
    similar.add_argument("--min-quality", type=float, default=0.0)
    evaluate = sub.add_parser("evaluate", help="结算到期评估窗口并放弃逾期窗口，回写案例质量")
    evaluate.add_argument("--server", default="http://localhost:5001")
    evaluate.add_argument("--run")
    evaluations = sub.add_parser("evaluations", help="查看某个 run 的评估窗口、结论与回滚建议")
    evaluations.add_argument("run_id")
    rollback = sub.add_parser("rollback", help="人工审批后回滚某次恶化决策到协商前参数")
    rollback.add_argument("run_id")
    rollback.add_argument("--ap-endpoints", default="",
                          help="各 AP 执行端点，格式 ap1=host:port,ap2=...；执行回滚必填")
    rollback.add_argument("--confirm", action="store_true",
                          help="确认下发（缺省仅预演打印计划，不发请求）")
    rules = sub.add_parser("rules", help="查看/归纳 L5 语义规律（跨案例统计）")
    rules.add_argument("--induce", action="store_true", help="先从已评估案例重新归纳规律")
    rules.add_argument("--scene")
    rules.add_argument("--min-confidence", type=float, default=0.0)
    rules.add_argument("--include-conflicted", action="store_true", help="含被标记冲突的规律")
    consolidate_cmd = sub.add_parser("consolidate", help="L6 整理：容量/过期归档 + 重归纳 + 冲突标记")
    consolidate_cmd.add_argument("--max-per-topology", type=int, default=50)
    consolidate_cmd.add_argument("--max-age-days", type=float, default=90.0)
    sync_workspace = sub.add_parser(
        "sync-workspace-memory",
        help="从 SQLite 刷新 openclaw/workspaces/<agent>/MEMORY.md",
    )
    sync_workspace.add_argument("--agent", choices=AGENT_IDS, action="append",
                                help="只同步指定 Agent；可重复传入")
    sync_workspace.add_argument("--topology-signature",
                                help="只同步指定拓扑签名的本地案例")
    sync_workspace.add_argument("--limit", type=int, default=20,
                                help="每个 Agent 最多读取的本地案例数")
    sub.add_parser("calibrate", help="评估阈值校准诊断：score/verdict 分布与摇摆率")
    sub.add_parser("health", help="记忆系统健康度快照（案例/评估/规律/runs）")
    quarantine = sub.add_parser("quarantine", help="反思隔离区：列出被停止注入的记忆")
    quarantine.add_argument("--set", dest="set_key", metavar="KIND:KEY",
                            help="手动隔离一条记忆，如 episode:run-42 / rule:<rule_id> "
                                 "/ agent_episode:<run_id>:<agent>")
    revalidate = sub.add_parser(
        "revalidate", help="再验证解除隔离：确认记忆仍适用后恢复注入并重置时效锚点",
    )
    revalidate.add_argument("memory_kind", choices=["episode", "agent_episode", "rule"])
    revalidate.add_argument("memory_key")
    contradictions_cmd = sub.add_parser("contradictions", help="查看矛盾账本（不可删除，仅审计）")
    contradictions_cmd.add_argument("--kind", choices=["episode", "agent_episode", "rule"])
    contradictions_cmd.add_argument("--key")
    contradictions_cmd.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    store = EventStore(Path(args.db))
    try:
        if args.command == "incomplete":
            result = [
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "mode": run.mode,
                    "scene": run.scene,
                    "phase": run.current_phase,
                    "updated_at": run.updated_at,
                }
                for run in store.list_incomplete_runs()
            ]
        elif args.command == "show":
            checkpoint = build_checkpoint(store, args.run_id)
            if checkpoint is None:
                raise SystemExit(f"run not found: {args.run_id}")
            result = {
                "checkpoint": {
                    "run_id": checkpoint.run.run_id,
                    "status": checkpoint.run.status,
                    "mode": checkpoint.run.mode,
                    "scene": checkpoint.run.scene,
                    "last_sequence": checkpoint.last_sequence,
                    "last_event": checkpoint.last_event,
                    "current_phase": checkpoint.current_phase,
                    "can_resume": checkpoint.can_resume,
                    "resume_reason": checkpoint.resume_reason,
                    "blocking_actions": checkpoint.blocking_actions,
                    "boundary": checkpoint.boundary,
                },
                "events": store.load_events(args.run_id),
                "snapshots": list(store.iter_snapshots(args.run_id)),
                "actions": [action.__dict__ for action in store.list_actions(args.run_id)],
                "session_memory": store.load_session_memory(args.run_id),
            }
        elif args.command == "resolve-action":
            action = store.get_action(args.action_id)
            if action is None:
                raise SystemExit(f"action not found: {args.action_id}")
            updated = store.finish_action(
                args.action_id,
                status=args.status,
                response={"manual_reconciliation": True, "note": args.note},
                error=None if args.status == "succeeded" else args.note,
            )
            result = updated.__dict__
        elif args.command == "episodes":
            result = store.list_episodes(scene=args.scene, limit=args.limit)
        elif args.command == "evaluate":
            from src.state_client import get_all_states
            # 全量原始遥测，不套 agent 字段白名单（评估需要 iperf/延迟/丢包）。
            # harvest = 尽力收割到期窗口 + 放弃逾期太久的窗口。
            result = harvest_evaluations(
                store,
                lambda: get_all_states(args.server),
                run_id=args.run,
            )
        elif args.command == "rollback":
            endpoints = _parse_executor_endpoints(args.ap_endpoints) if args.ap_endpoints else None
            result = execute_rollback(
                store, args.run_id, endpoints, confirm=args.confirm
            )
        elif args.command == "rules":
            induced = induce_rules(store) if args.induce else None
            result = {
                "induced_count": len(induced) if induced is not None else None,
                "rules": store.list_rules(
                    scene=args.scene, min_confidence=args.min_confidence,
                    include_conflicted=args.include_conflicted,
                ),
            }
        elif args.command == "consolidate":
            from src.memory import ConsolidationConfig
            result = consolidate(
                store,
                config=ConsolidationConfig(
                    max_per_topology=args.max_per_topology,
                    max_age_days=args.max_age_days,
                ),
            )
        elif args.command == "sync-workspace-memory":
            agents = tuple(args.agent) if args.agent else AGENT_IDS
            result = sync_long_term_memories(
                store,
                agents=agents,
                topology_signature=args.topology_signature,
                limit=args.limit,
            )
        elif args.command == "quarantine":
            if args.set_key:
                kind, _, key = args.set_key.partition(":")
                if kind not in {"episode", "agent_episode", "rule"} or not key:
                    raise SystemExit(f"格式应为 KIND:KEY，收到: {args.set_key}")
                store.set_memory_quarantined(kind, key, True)
            result = store.list_quarantined_memories()
        elif args.command == "revalidate":
            store.set_memory_quarantined(args.memory_kind, args.memory_key, False)
            store.mark_memory_verified(args.memory_kind, args.memory_key)
            result = {
                "memory_kind": args.memory_kind, "memory_key": args.memory_key,
                "quarantined": False, "revalidated": True,
            }
        elif args.command == "contradictions":
            result = store.list_contradictions(
                memory_kind=args.kind, memory_key=args.key, limit=args.limit,
            )
        elif args.command == "calibrate":
            result = evaluation_diagnostics(store)
        elif args.command == "health":
            from src.memory import memory_health
            result = memory_health(store)
        elif args.command == "evaluations":
            windows = store.list_evaluations(args.run_id)
            if not windows:
                raise SystemExit(f"no evaluations for run: {args.run_id}")
            episode = store.get_episode(run_id=args.run_id)
            result = {
                "windows": windows,
                "summary": summarize_run_evaluations(windows),
                "episode_quality": episode["quality_score"] if episode else None,
                "episode_evaluation": episode["evaluation"] if episode else None,
            }
        else:
            source = store.get_episode(run_id=args.run_id)
            if source is None:
                raise SystemExit(f"episode not found for run: {args.run_id}")
            result = find_similar_episodes(
                store,
                source["initial_state"],
                limit=args.limit,
                min_quality=args.min_quality,
                exclude_run_id=args.run_id,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()

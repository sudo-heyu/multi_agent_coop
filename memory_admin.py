"""Local SQLite event-store inspection utility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.logger import DEFAULT_EVENT_DB
from src.persistence import EventStore, build_checkpoint
from src.memory import (
    collect_due_evaluations,
    find_similar_episodes,
    summarize_run_evaluations,
)


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
    evaluate = sub.add_parser("evaluate", help="结算所有到期的效果评估窗口并回写案例质量")
    evaluate.add_argument("--server", default="http://localhost:5001")
    evaluate.add_argument("--run")
    evaluations = sub.add_parser("evaluations", help="查看某个 run 的评估窗口、结论与回滚建议")
    evaluations.add_argument("run_id")
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
            result = collect_due_evaluations(
                store,
                lambda: get_all_states(args.server),
                run_id=args.run,
            )
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

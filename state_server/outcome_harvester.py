"""后台效果评估收割器 —— 常驻进程，定期结算到期窗口并放弃逾期窗口。

L4 Outcome Evaluator 的评估窗口需要在决策生效后一段时间才到期。此前收割只发生在
下次 run_openclaw 启动、mock 保活循环或手动 evaluate；一次性/低频运行时，real 模式
的长窗口（可达 15 分钟）可能永远收不到结算。本收割器由 serve.sh 常驻拉起，让评估
不依赖后续协商即可自动完成，是 L4 在真实部署下可靠的前提。

用法：
  python state_server/outcome_harvester.py --server http://localhost:5001 --interval 30
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import socket
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logger import DEFAULT_EVENT_DB
from src.persistence import EventStore
from src.memory import consolidate, harvest_evaluations
from src.state_client import get_all_states


_STOP = False


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    _STOP = True


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(server: str, interval: float, db_path: str) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    print(f"[harvester] 启动 server={server} interval={interval:g}s db={db_path}", flush=True)
    while not _STOP:
        store = EventStore(db_path)
        try:
            stale_seconds = float(os.environ.get("MULTIAP_RUN_STALE_SECONDS", "3600"))
            interrupted = store.interrupt_stale_runs(
                stale_before=(datetime.now(timezone.utc) - timedelta(
                    seconds=stale_seconds
                )).isoformat(timespec="milliseconds")
            )
            outcome = harvest_evaluations(store, lambda: get_all_states(server))
            store.heartbeat_service(
                "outcome_harvester", f"{socket.gethostname()}:{os.getpid()}",
                details={"collected": len(outcome.get("collected", [])),
                         "abandoned": len(outcome.get("abandoned", [])),
                         "interrupted_runs": interrupted},
            )
        except Exception as exc:  # noqa: BLE001 — 单轮异常不应终止常驻收割器
            print(f"[harvester] {_stamp()} 收割异常（忽略，下轮重试）：{exc}", flush=True)
            try:
                store.heartbeat_service(
                    "outcome_harvester", f"{socket.gethostname()}:{os.getpid()}",
                    status="error", details={"error": str(exc)},
                )
            except Exception:
                pass
            outcome = {}
        finally:
            store.close()
        for item in outcome.get("collected", []):
            deltas = item.get("deltas") or {}
            print(f"[harvester] {_stamp()} 收割 run={item['run_id']} "
                  f"{item['window_label']} → {item['verdict']} "
                  f"(得分={deltas.get('score')} 置信度={item['confidence']})", flush=True)
        for item in outcome.get("abandoned", []):
            print(f"[harvester] {_stamp()} 放弃 run={item['run_id']} "
                  f"{item['window_label']}（逾期未收割）", flush=True)
        if outcome.get("error"):
            print(f"[harvester] {_stamp()} 收割失败（保持 pending）：{outcome['error']}", flush=True)
        # 有新反馈落地就做一次整理（含 L5 归纳 + L6 容量/过期归档 + 冲突标记），
        # 让规律随真实效果自动演进、案例库不无限膨胀。
        if outcome.get("collected"):
            store = EventStore(db_path)
            try:
                summary = consolidate(store)
                if summary.get("status") == "done":
                    print(f"[harvester] {_stamp()} 整理：规律 {summary['rules_total']} 条"
                          f"（冲突 {len(summary['conflicted_rules'])}），归档案例 "
                          f"{len(summary['archived_over_capacity']) + len(summary['archived_expired'])}",
                          flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[harvester] {_stamp()} 整理异常（忽略）：{exc}", flush=True)
            finally:
                store.close()
        # 可被信号打断的分段睡眠，保证 stop 及时生效。
        slept = 0.0
        while slept < interval and not _STOP:
            time.sleep(min(1.0, interval - slept))
            slept += 1.0
    print(f"[harvester] {_stamp()} 收到停止信号，退出。", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="后台效果评估收割器")
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="两次收割间隔秒数（默认 30）")
    parser.add_argument("--db", default=os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval 必须为正")
    run(args.server, args.interval, args.db)


if __name__ == "__main__":
    main()

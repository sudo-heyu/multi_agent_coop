"""
Direct PPIO stream runtime entrypoint.

This is the fast path beside run_openclaw.py.  It reuses the same structured
relay, workspaces, memory/reflection, validator, executor and outcome pipeline;
only the per-AP runtime is direct PPIO streaming instead of OpenClaw agent/gateway.
"""
from __future__ import annotations

import argparse
import atexit
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "openclaw" / "mcp"))

import orchestration as orch
from openclaw.scenes import SCENE_NAMES
from openclaw.mcp import tool_policy
from openclaw.mcp.stream_runtime import DEFAULT_MODEL, PPIOStreamRuntime
from run_openclaw import (
    _harvest_due_evaluations,
    _http_event_sink,
    _load_executor_endpoints,
    _merge_no_proxy,
    _plot_daemon_alive,
    _port_open,
    _print_event_stream_chunk,
    _print_event_stream_start,
    _print_proposal_precheck,
    _print_tool,
    _default_qos_acceptance_wait,
    _resolve_eval_windows,
    _resume_state_compatible,
    _start_run_trace,
    _stop_run_trace,
    _validate_real_endpoints,
    _wait_state_ready,
    _require_state_server,
)
from src.console_style import dim, divider, section, status_fail, status_label, status_ok
from src.logger import DEFAULT_EVENT_DB, SessionLogger
from src.persistence import EventStore, build_checkpoint


def _load_goal(goal_id: str | None):
    if not goal_id:
        return None
    store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
    try:
        goal = store.get_goal(goal_id)
    finally:
        store.close()
    if goal is None:
        raise ValueError(f"未找到 goal_id: {goal_id}")
    if goal["status"] != "active":
        raise ValueError(
            f"目标状态为 {goal['status']}（{goal.get('status_reason') or '无原因'}），"
            "只有 active 目标可继续迭代"
        )
    return goal


def _build_resume_checkpoint(run_id: str | None):
    if not run_id:
        return None
    store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
    try:
        checkpoint = build_checkpoint(store, run_id)
    finally:
        store.close()
    if checkpoint is None:
        raise ValueError(f"未找到 run_id: {run_id}")
    if not checkpoint.can_resume:
        raise ValueError(f"run 不可安全恢复: {checkpoint.resume_reason}")
    return checkpoint


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(
        description="多 AP 协商（PPIO stream AP runtime / 确定性阶段接力）"
    )
    parser.add_argument("--mode", choices=["real", "ns3"], required=True)
    parser.add_argument("--scene", choices=sorted(SCENE_NAMES), default="sr",
                        help="场景标签（仅用于日志/记忆归组，不影响数据来源）")
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-validation-retries", type=int, default=3,
                        help="Validator/QoS 验收失败后的重新提案次数上限")
    parser.add_argument("--state-wait", type=float, default=None)
    parser.add_argument("--observation-wait", type=float, default=None)
    parser.add_argument("--ap-endpoints", default="")
    parser.add_argument("--ap-config", default="")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--dashboard-port", type=int, default=5050)
    parser.add_argument("--no-academic-plot", action="store_true")
    parser.add_argument("--exit-after-run", action="store_true",
                        help="兼容选项：协商结束后本就直接退出")
    parser.add_argument("--resume-run", default="")
    parser.add_argument("--context-budget-chars", type=int, default=14000)
    parser.add_argument("--context-recent-turns", type=int, default=6)
    parser.add_argument("--eval-windows", default="")
    parser.add_argument("--goal", default="")
    parser.add_argument(
        "--memory",
        choices=["on", "off"],
        default=os.environ.get("MULTIAP_MEMORY_MODE", "on"),
        help="是否启用长期记忆召回/注入：on=启用；off=只保留本轮对话，不注入历史记忆",
    )
    parser.add_argument(
        "--acceptance",
        choices=["validator", "qos"],
        default=os.environ.get("MULTIAP_ACCEPTANCE", "validator"),
        help="验收模式：validator=参数合法即成功；qos=下发后观测 QoS 必须 improved",
    )
    parser.add_argument("--model", default=os.environ.get("MULTIAP_STREAM_MODEL", DEFAULT_MODEL),
                        help="PPIO 模型 ID，默认 qwen/qwen3.6-35b-a3b")
    parser.add_argument("--base-url", default=os.environ.get(
        "MULTIAP_PPIO_BASE_URL",
        os.environ.get("MULTIAP_MEMORY_PPIO_BASE_URL", "https://api.ppio.com/openai/v1"),
    ))
    parser.add_argument("--stream-timeout", type=float, default=None)
    parser.add_argument("--max-tool-rounds", type=int, default=None)
    parser.add_argument(
        "--tool-profile",
        choices=list(tool_policy.PROFILES),
        default=os.environ.get("MULTIAP_TOOL_PROFILE", "full"),
        help=(
            "AP 可见工具能力档位：none/no_tools=完全无工具；basic=基础状态/反馈/验算；"
            "rich/full=完整工具；faulty=完整工具界面但返回错误结果；diagnostic=隐藏答案型 SR 工具；"
            "validator_only=只保留状态/反馈/候选验算；state_only=只保留状态和 STA 反馈；"
            "memory_challenge=粗粒度状态+弱验算，突出记忆作用"
        ),
    )
    args = parser.parse_args()

    if args.context_budget_chars < 2000:
        parser.error("--context-budget-chars 不能小于 2000")
    if args.context_recent_turns < 2:
        parser.error("--context-recent-turns 不能小于 2")
    os.environ["MULTIAP_CONTEXT_BUDGET_CHARS"] = str(args.context_budget_chars)
    os.environ["MULTIAP_CONTEXT_RECENT_TURNS"] = str(args.context_recent_turns)
    os.environ["MULTIAP_STREAM_MODEL"] = args.model
    os.environ["MULTIAP_PPIO_BASE_URL"] = args.base_url
    os.environ["MULTIAP_STATE_SERVER"] = args.server
    os.environ["MULTIAP_TOOL_PROFILE"] = args.tool_profile
    os.environ["MULTIAP_MEMORY_MODE"] = args.memory
    os.environ["NO_PROXY"] = _merge_no_proxy(os.environ.get("NO_PROXY"))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    try:
        goal = _load_goal(args.goal)
        resume_checkpoint = _build_resume_checkpoint(args.resume_run)
    except ValueError as exc:
        parser.error(str(exc))

    if resume_checkpoint:
        if resume_checkpoint.run.mode:
            if resume_checkpoint.run.mode not in {"real", "ns3"}:
                parser.error(
                    f"checkpoint 的运行模式 {resume_checkpoint.run.mode!r} 已不受支持，"
                    "请启动新协商"
                )
            args.mode = resume_checkpoint.run.mode
        if resume_checkpoint.run.scene:
            args.scene = resume_checkpoint.run.scene

    try:
        eval_windows = _resolve_eval_windows(args.eval_windows, args.mode)
    except ValueError as exc:
        parser.error(f"--eval-windows 非法: {exc}")

    observation_wait = (
        args.observation_wait
        if args.observation_wait is not None
        else (_default_qos_acceptance_wait(args.mode) if args.acceptance == "qos" else 0.0)
    )

    os.environ["MULTIAP_SCENE"] = args.scene
    print(
        f"[run] runtime=ppio-stream model={args.model} "
        f"scene={args.scene} server={args.server} max_steps={args.max_steps} "
        f"tool_profile={args.tool_profile} memory={args.memory} "
        f"acceptance={args.acceptance}",
        flush=True,
    )

    executor_endpoints = _load_executor_endpoints(args.ap_config, args.ap_endpoints)
    if args.mode == "real":
        try:
            _validate_real_endpoints(executor_endpoints)
        except ValueError as exc:
            parser.error(str(exc))
    if executor_endpoints:
        print(f"执行推送端点：{executor_endpoints}")
    else:
        print("执行推送：未配置（协商结果仅输出到控制台）")

    _require_state_server(args.server, args.mode)
    print("[Runtime] 使用 PPIO stream runtime（不依赖 OpenClaw gateway）")

    if args.mode == "real":
        print("[State] real 模式：等待三台 AP reporter 真值（source=ap）")
    else:
        print("[State] ns3 模式：等待 ns-3 bridge 上报（source=ns3）")
    wait_s = args.state_wait if args.state_wait is not None else 90.0
    required_source = {"real": "ap", "ns3": "ns3"}.get(args.mode)
    try:
        ready_state = _wait_state_ready(args.server, wait_s, required_source=required_source)
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        if args.mode == "real":
            print("请确认三台香蕉派 reporter 均在持续上报，且 source=ap。")
        else:
            print("请确认 state_server/ns3_bridge.py 正在运行，且 source=ns3。")
        sys.exit(1)

    if resume_checkpoint:
        compatible, reason = _resume_state_compatible(
            (resume_checkpoint.projection or {}).get("ap_state") or {},
            ready_state,
        )
        if not compatible:
            print(f"[错误] checkpoint 与当前网络状态不兼容：{reason}")
            print("请启动新协商，不要恢复旧 run。")
            sys.exit(1)

    _harvest_due_evaluations(args.server)

    push_live = None
    if not args.no_dashboard:
        if not _port_open(args.dashboard_port):
            print(f"[错误] Dashboard 未在线：http://localhost:{args.dashboard_port}/")
            print("请先启动常驻服务：bash openclaw/serve.sh start")
            sys.exit(1)
        print(f"[Dashboard] 复用常驻服务 http://localhost:{args.dashboard_port}/")
        push_live = _http_event_sink(f"http://localhost:{args.dashboard_port}/push")

    if not args.no_academic_plot:
        if _plot_daemon_alive():
            print("[Academic Plot] 复用常驻曲线窗口（serve.sh）")
        else:
            print("[Academic Plot] 常驻窗口未在线（如需曲线：bash openclaw/serve.sh start 已含 plot）")

    orch.STATE_SERVER = args.server
    logger = SessionLogger(
        session_id=args.resume_run or None,
        verbose=False,
        event_sink=push_live,
        mode=args.mode,
        resume=bool(resume_checkpoint),
    )
    print(f"[Run] session_id={logger.session_id} event_db={os.environ.get('MULTIAP_EVENT_DB', str(DEFAULT_EVENT_DB))}")
    initial_ap_state = None
    if resume_checkpoint:
        logger.session_resume({
            "boundary": resume_checkpoint.boundary,
            "projection_version": 1,
        })
    else:
        initial_ap_state = {
            ap: ready_state[ap]["data"] for ap in ("ap1", "ap2", "ap3")
        }
        logger.session_start(
            model=f"ppio-stream/{args.model}", scene=args.scene,
            ap_state=initial_ap_state,
        )

    trace_path = _start_run_trace(args.server, str(logger.session_id))
    logger.record_telemetry_trace("start", trace_path)
    if trace_path:
        print(f"[Trace] 连续遥测落盘：{trace_path}")
        atexit.register(_stop_run_trace, args.server)

    resume_projection = None
    if resume_checkpoint:
        resume_projection = {
            **(resume_checkpoint.projection or {}),
            "boundary": resume_checkpoint.boundary,
        }

    goal_context = None
    if goal is not None:
        from src.memory.goals import build_goal_context, register_attempt
        goal_store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
        try:
            attempt = register_attempt(goal_store, goal["goal_id"], str(logger.session_id))
            if attempt is not None:
                goal_context = build_goal_context(goal_store, goal, attempt)
        finally:
            goal_store.close()
        if attempt is not None:
            print(f"[Goal] 目标 {goal['metric']}：attempt #{attempt['sequence']}"
                  f"（预算 {goal['budget_attempts']} 次）")

    runtime = PPIOStreamRuntime(
        model=args.model,
        base_url=args.base_url,
        timeout=args.stream_timeout,
        max_tool_rounds=args.max_tool_rounds,
    )
    t0 = time.time()
    try:
        result = orch.structured_relay(
            max_validation_retries=args.max_validation_retries,
            max_turns=args.max_steps,
            on_event=None,
            on_event_start=_print_event_stream_start,
            on_event_chunk=_print_event_stream_chunk,
            on_tool=_print_tool,
            logger=logger,
            observation_state_getter=lambda: orch.get_all_states(args.server),
            observation_wait_seconds=observation_wait,
            executor_endpoints=executor_endpoints,
            resume_projection=resume_projection,
            evaluation_windows=eval_windows,
            initial_state=initial_ap_state,
            goal_context=goal_context,
            agent_driver=runtime.speak,
            on_proposal_precheck=_print_proposal_precheck,
            acceptance=args.acceptance,
        )
    except BaseException as exc:
        import traceback as _tb
        try:
            logger.session_failed(
                f"{type(exc).__name__}: {exc}",
                traceback_text=_tb.format_exc(),
            )
            logger.close()
        except Exception:
            pass
        raise
    dur = time.time() - t0

    print(divider())
    outcome = result["outcome"]
    outcome_txt = status_ok(outcome) if outcome == "success" else status_fail(outcome)
    print(f"{section('结果')} outcome={outcome_txt} turns={result['transcript_turns']} 用时 {dur:.0f}s")
    print(f"{section('策略')} {result['strategy']}")
    print(f"{section('最终决策')} {result['decision']}")
    if result.get("push_results"):
        print(f"{section('Executor')} {result['push_results']}")
    v = result["validation"]
    if v:
        flag = status_ok("通过") if v["approved"] else status_fail("未通过")
        print(f"{status_label('Validator')} {flag} — {v['summary']}")
        qos = v.get("qos_acceptance") if isinstance(v, dict) else None
        if isinstance(qos, dict):
            qflag = status_ok("通过") if qos.get("approved") else status_fail("未通过")
            score = (qos.get("deltas") or {}).get("score")
            print(
                f"{status_label('QoS')} {qflag} — verdict={qos.get('verdict')} "
                f"score={score} confidence={qos.get('confidence')}"
            )
    else:
        print(f"{status_label('Validator')} {dim('无可验收决策（协商未收敛或未解析出决策 JSON）')}")

    run_id = logger.session_id
    if goal is not None and run_id:
        from src.memory.goals import record_attempt_result
        goal_store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
        try:
            attempt = record_attempt_result(goal_store, str(run_id), outcome=result["outcome"])
        finally:
            goal_store.close()
        if attempt is not None:
            print(f"[Goal] attempt #{attempt['sequence']} 状态={attempt['status']}；"
                  "目标进度在评估窗口结算后回填")
    pending_eval = bool(eval_windows) and result["outcome"] == "success" and run_id
    if pending_eval:
        print(f"[Outcome] 效果评估窗口已登记（{eval_windows}s）；"
              "到期后由常驻 harvester / 下次 run.py 自动收割，或手动执行 "
              f".venv/bin/python memory_admin.py evaluate --server {args.server}")


if __name__ == "__main__":
    main()

"""
OpenClaw AP agent + 确定性阶段编排入口。

用法：
  python run_openclaw.py --scene joint            # mock A：预设场景
  python run_openclaw.py --scene sr --max-steps 20
  python run_openclaw.py --mode real --ap-endpoints ap1=...

前置：openclaw 已装、ollama 运行、已执行过 `bash openclaw/setup.sh`。
"""
import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "openclaw" / "mcp"))

from openclaw.scenes import MOCK_SCENES, _parse_executor_endpoints
from src.logger import SessionLogger
from src.logger import DEFAULT_EVENT_DB
from src.persistence import EventStore, build_checkpoint
from src.console_style import (
    format_ap_name, strip_md, divider, section, status_label,
    status_ok, status_fail, dim, tool_prefix, tool_name,
)
from openclaw.mcp.tool_console import _format_tool_console
from state_server.mock_feeder import MockTelemetryFeeder
import orchestration as orch


OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN")
    or shutil.which("openclaw")
    or str(Path.home() / ".openclaw" / "bin" / "openclaw")
)
OPENCLAW_PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
_STREAM_AT_LINE_START = True


def _stream_write(text: str) -> None:
    global _STREAM_AT_LINE_START
    sys.stdout.write(text)
    sys.stdout.flush()
    _STREAM_AT_LINE_START = text.endswith("\n")


def _print_event(phase, who, reply):
    # 只保留 agent 名称作为发言头部，不打阶段标签（广播/提案/投票…）
    print(f"\n\n{format_ap_name(who.upper())}:")
    print(strip_md(reply).strip())


_STREAM_PRINT_LOCK = threading.Lock()


def _print_event_stream_start(phase, who):
    with _STREAM_PRINT_LOCK:
        prefix = "\n\n" if not _STREAM_AT_LINE_START else "\n"
        _stream_write(f"{prefix}{format_ap_name(str(who).upper())}:\n")


def _print_event_stream_chunk(phase, who, text):
    with _STREAM_PRINT_LOCK:
        _stream_write(strip_md(text))


def _print_tool(name, args, result, dur_ms):
    """工具调用行回调：复用 orchestrator 的富摘要 formatter，失败则回退一行。"""
    result_dict = result if isinstance(result, dict) else ({"_text": result} if result else {})
    try:
        line = _format_tool_console(name, args, result_dict, dur_ms)
    except Exception:
        line = f"{tool_prefix()} {tool_name(name)} → {status_fail('结果解析失败')}"
    with _STREAM_PRINT_LOCK:
        prefix = "" if _STREAM_AT_LINE_START else "\n"
        _stream_write(prefix + line + "\n")


# ── coordinator 路径的实时对话流式输出（tail 会话 JSONL）──────────────────────
# coordinator 子进程在 MCP 工具里写 JSONL；父进程 tail 这个文件，把广播/提案/投票/
# 验证事件实时打到终端，使默认路径也能“边跑边看对话”（粒度=每个 AP 发言完成即显示）。
_LOG_DIR = Path(__file__).parent / "logs"
_ROLE_PHASE = {
    "broadcast": "broadcast",
    "proposer": "propose",
    "counter_proposal_json_repair": "propose",
    "voter": "vote",
    "decision": "decide",
}


def _port_open(port: int) -> bool:
    """本机端口是否在监听（用于复用 serve.sh 常驻的 Dashboard，不再每轮自起）。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _require_state_server(url: str, mode: str) -> dict:
    """强制复用常驻 state server，并校验数据源策略与运行模式一致。
    不在线则报错提示先 `bash openclaw/serve.sh start`，不再临时起。"""
    import requests
    try:
        r = requests.get(f"{url}/health", timeout=2)
        if r.status_code == 200:
            health = r.json()
            if mode == "real" and health.get("allow_mock_source"):
                print("[错误] real 模式要求 state server 拒收 mock/generated 数据。")
                print("请执行：MULTIAP_STATE_MODE=real bash openclaw/serve.sh restart")
                sys.exit(1)
            print(f"[State] 复用常驻 state server {url}")
            return health
    except Exception:
        pass
    print(f"[错误] state server 未在线：{url}")
    print("请先启动常驻服务：bash openclaw/serve.sh start")
    sys.exit(1)


def _require_gateway(use_coordinator: bool) -> None:
    """默认 structured_relay 路径强制常驻 gateway 在线（AP 回合免每回合 CLI 冷启动）。
    coordinator 路径走 --local，不依赖 gateway，跳过检测。"""
    if use_coordinator:
        return
    port = orch._gateway_port()
    if not orch._gateway_up(port):
        print(f"[错误] openclaw gateway 未在线：ws://127.0.0.1:{port or 18789}")
        print("请先启动常驻服务：bash openclaw/serve.sh start")
        sys.exit(1)
    print(f"[Gateway] 复用常驻 gateway :{port}（AP 回合免冷启动）")


def _http_event_sink(url: str):
    """事件 dict → POST 常驻 dashboard /push 的回调（用作 SessionLogger.event_sink）。
    让独立常驻的 dashboard 进程也能收到实时对话流；推送失败静默，不阻断协商。"""
    import requests
    sess = requests.Session()
    sess.trust_env = False

    def _sink(d: dict) -> None:
        try:
            sess.post(url, json=d, timeout=1.5)
        except Exception:
            pass

    return _sink


def _wait_state_ready(
    server: str,
    timeout_s: float = 1.0,
    *,
    required_source: str | None = None,
) -> dict:
    """等待三台 AP 数据齐全、新鲜，real 模式还要求 source=ap。"""
    import requests

    deadline = time.time() + max(0.0, timeout_s)
    sess = requests.Session()
    sess.trust_env = False
    while True:
        try:
            data = sess.get(f"{server.rstrip('/')}/state", timeout=0.3).json()
            if all(
                isinstance(data.get(ap), dict)
                and data[ap].get("data") is not None
                and not data[ap].get("stale")
                and (
                    required_source is None
                    or str(data[ap]["data"].get("source", "ap")).lower() == required_source
                )
                for ap in ("ap1", "ap2", "ap3")
            ):
                return data
        except Exception:
            pass
        if time.time() >= deadline:
            source_hint = f" 且 source={required_source}" if required_source else ""
            raise RuntimeError(
                f"等待 AP 状态超时（{timeout_s:g}s）：要求 ap1/ap2/ap3 数据齐全、未过期{source_hint}"
            )
        time.sleep(0.05)


def _resume_state_compatible(stored: dict, latest_response: dict) -> tuple[bool, str]:
    """Reject resume when topology, policy identity, or applied parameters changed."""
    latest_raw = {
        ap: (latest_response.get(ap) or {}).get("data") or {}
        for ap in ("ap1", "ap2", "ap3")
    }
    latest = orch.apply_profile(latest_raw)
    fields = ("tx_power_dbm", "cwmin", "cwmax", "aifsn", "traffic_priority")
    for ap in ("ap1", "ap2", "ap3"):
        if ap not in stored or ap not in latest:
            return False, f"{ap} 状态缺失"
        for field in fields:
            if stored[ap].get(field) != latest[ap].get(field):
                return False, (
                    f"{ap}.{field} 已变化: checkpoint={stored[ap].get(field)!r}, "
                    f"latest={latest[ap].get(field)!r}"
                )
        old_neighbors = set((stored[ap].get("neighbor_rssi_dbm") or {}).keys())
        new_neighbors = set((latest[ap].get("neighbor_rssi_dbm") or {}).keys())
        if old_neighbors != new_neighbors:
            return False, f"{ap} 邻居拓扑已变化"
    return True, "compatible"


def _plot_pid_file() -> Path:
    return Path.home() / f".openclaw-{OPENCLAW_PROFILE}" / "run" / "plot.pid"


def _validate_real_endpoints(endpoints: dict[str, str] | None) -> None:
    """真实模式必须为三台 AP 全量配置执行端点。"""
    expected = {"ap1", "ap2", "ap3"}
    actual = {str(ap).lower() for ap in (endpoints or {})}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"缺少 {','.join(missing)}")
        if extra:
            details.append(f"未知 {','.join(extra)}")
        raise ValueError("real 模式执行端点必须恰好覆盖 ap1/ap2/ap3" + (f"（{'；'.join(details)}）" if details else ""))


def _start_telemetry(mode: str, no_feeder: bool, server: str, scene: dict, interval: float):
    """按模式启动遥测；real 路径绝不实例化 MockTelemetryFeeder。"""
    if mode == "real":
        return None
    if not no_feeder:
        feeder = MockTelemetryFeeder(server, scene, interval=interval)
        feeder.start()
        return feeder
    single = MockTelemetryFeeder(server, scene, interval=interval)
    single.start()
    single.stop()
    return None


def _plot_daemon_alive() -> bool:
    """serve.sh 常驻的 academic plot 进程是否存活（matplotlib 窗口在）。"""
    pf = _plot_pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_new_session_log(pre: set) -> str | None:
    cur = set(glob.glob(str(_LOG_DIR / "session_*.jsonl")))
    new = sorted(cur - pre, key=os.path.getmtime, reverse=True)
    return new[0] if new else None


def _stream_log_event(obj: dict) -> bool:
    """打印一条会话事件；返回是否打印了内容（用于心跳节流）。"""
    t = obj.get("event") or obj.get("type")
    if t == "agent_speak":
        who = (obj.get("agent") or "").upper()
        phase = _ROLE_PHASE.get(obj.get("role"), obj.get("role") or "")
        _print_event(phase, who, obj.get("response") or "")
        return True
    if t == "validation_result":
        v = obj.get("result") or obj
        flag = status_ok("通过") if v.get("approved") else status_fail("未通过")
        print(f"\n{status_label('Validator')} {flag} — {v.get('summary', '')}", flush=True)
        return True
    return False


def _drain_session_log(fh) -> bool:
    """读出文件中新追加的完整行并打印。返回本次是否打印了内容。仅处理以换行结尾的完整行。"""
    printed = False
    while True:
        pos = fh.tell()
        line = fh.readline()
        if not line:
            break
        if not line.endswith("\n"):
            fh.seek(pos)  # 不完整行（写入中），回退等下次
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _stream_log_event(obj):
            printed = True
    return printed


def main():
    # 重定向/管道运行时保持行缓冲，避免结果和 [Outcome] 行长时间压在块缓冲里。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="多 AP 协商（OpenClaw AP agent / 确定性阶段接力）")
    ap.add_argument("--mode", choices=["mock", "real"], default="mock",
                    help="mock 使用预设场景持续喂数；real 只接受三台真实 AP reporter 上报")
    ap.add_argument("--scene", choices=["sr", "edca", "joint"], default="joint")
    ap.add_argument("--server", default="http://localhost:5001")
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--no-feeder", action="store_true",
                    help="mock 模式只推一帧后停止（兼容选项）；real 模式始终不创建 feeder")
    ap.add_argument("--state-wait", type=float, default=None,
                    help="等待三台 AP 新鲜状态的最长秒数（默认 mock=5，real=90）")
    ap.add_argument("--use-coordinator", action="store_true",
                    help="走旧的 coordinator LLM 触发路径（默认已停用，仅兼容/对比用，"
                         "会多 ~60s 冷启动+2 次 LLM 调用）")
    ap.add_argument("--observation-wait", type=float, default=0.0,
                    help="最终 Validator 读取观测状态前等待秒数")
    ap.add_argument("--ap-endpoints", default="",
                    help="协商成功后推送决策的执行服务地址，格式 ap1=host:port,ap2=...")
    ap.add_argument("--ap-config", default="",
                    help="从显式指定的 JSON 文件读取执行服务地址；默认不自动读取")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="不要求或推送到常驻 Dashboard")
    ap.add_argument("--dashboard-port", type=int, default=5050)
    ap.add_argument("--no-academic-plot", action="store_true",
                    help="不检查或复用常驻 Matplotlib 学术曲线窗口")
    ap.add_argument("--plot-window", type=float, default=25.0)
    ap.add_argument("--plot-interval", type=float, default=1.0)
    ap.add_argument("--require-qwen80b", action="store_true",
                    help="强制要求 multiap profile 默认模型为 qwen80binstruct")
    ap.add_argument("--exit-after-run", action="store_true",
                    help="协商结束后不保持 mock 曲线展示，直接退出")
    ap.add_argument("--resume-run", default="",
                    help="从 SQLite 中指定 run_id 的安全 negotiation checkpoint 恢复")
    ap.add_argument("--context-budget-chars", type=int, default=14000,
                    help="每个 AP 回合可注入的会话上下文字符预算（最小 2000）")
    ap.add_argument("--context-recent-turns", type=int, default=6,
                    help="上下文中优先保留原文的最近发言数（最小 2）")
    ap.add_argument("--eval-windows", default="",
                    help="决策生效后的效果评估窗口秒数，逗号分隔（如 60,300,900）；"
                         "默认 mock=10,30 / real=60,300,900；传 off 关闭")
    args = ap.parse_args()
    if args.context_budget_chars < 2000:
        ap.error("--context-budget-chars 不能小于 2000")
    if args.context_recent_turns < 2:
        ap.error("--context-recent-turns 不能小于 2")
    os.environ["MULTIAP_CONTEXT_BUDGET_CHARS"] = str(args.context_budget_chars)
    os.environ["MULTIAP_CONTEXT_RECENT_TURNS"] = str(args.context_recent_turns)

    resume_checkpoint = None
    if args.resume_run:
        if args.use_coordinator:
            ap.error("--resume-run 当前只支持默认 structured_relay 路径")
        store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
        try:
            resume_checkpoint = build_checkpoint(store, args.resume_run)
        finally:
            store.close()
        if resume_checkpoint is None:
            ap.error(f"未找到 run_id: {args.resume_run}")
        if not resume_checkpoint.can_resume:
            ap.error(f"run 不可安全恢复: {resume_checkpoint.resume_reason}")
        if resume_checkpoint.run.mode:
            args.mode = resume_checkpoint.run.mode
        if resume_checkpoint.run.scene:
            args.scene = resume_checkpoint.run.scene

    os.environ["NO_PROXY"] = _merge_no_proxy(os.environ.get("NO_PROXY"))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    try:
        eval_windows = _resolve_eval_windows(args.eval_windows, args.mode)
    except ValueError as exc:
        ap.error(f"--eval-windows 非法: {exc}")

    scene = MOCK_SCENES[args.scene]
    print(f"[run_openclaw] scene={args.scene} server={args.server} max_steps={args.max_steps}", flush=True)

    executor_endpoints = _load_executor_endpoints(args.ap_config, args.ap_endpoints)
    if args.mode == "real":
        try:
            _validate_real_endpoints(executor_endpoints)
        except ValueError as exc:
            ap.error(str(exc))
    if executor_endpoints:
        print(f"执行推送端点：{executor_endpoints}")
    else:
        print("执行推送：未配置（协商结果仅输出到控制台）")

    feeder = None
    logger = None
    # 强制常驻：核心服务由 serve.sh 起好；不在线则报错提示先 `serve.sh start`，不再临时起。
    _require_state_server(args.server, args.mode)
    _require_gateway(args.use_coordinator)

    if args.mode == "real":
        print("[State] real 模式：不创建 MockTelemetryFeeder，等待三台 AP reporter 真值")
    feeder = _start_telemetry(
        args.mode,
        args.no_feeder,
        args.server,
        scene,
        args.plot_interval,
    )
    wait_s = args.state_wait if args.state_wait is not None else (90.0 if args.mode == "real" else 5.0)
    try:
        ready_state = _wait_state_ready(
            args.server,
            wait_s,
            required_source="ap" if args.mode == "real" else None,
        )
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        if args.mode == "real":
            print("请确认三台香蕉派 reporter 均在持续上报，且 source=ap。")
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

    # 懒收割：上一轮协商登记的效果评估窗口若已到期，趁 state server 在线先结算，
    # 让本轮提案检索到带真实效果结论的案例。
    _harvest_due_evaluations(args.server)

    # Dashboard：强制复用常驻服务（serve.sh），事件经 HTTP /push 推给它。
    push_live = None
    if not args.no_dashboard:
        if not _port_open(args.dashboard_port):
            print(f"[错误] Dashboard 未在线：http://localhost:{args.dashboard_port}/")
            print("请先启动常驻服务：bash openclaw/serve.sh start")
            sys.exit(1)
        print(f"[Dashboard] 复用常驻服务 http://localhost:{args.dashboard_port}/")
        push_live = _http_event_sink(f"http://localhost:{args.dashboard_port}/push")

    # Academic plot：复用 serve.sh 常驻窗口；未在线则跳过（可选可视化，不阻塞）。
    if not args.no_academic_plot:
        if _plot_daemon_alive():
            print("[Academic Plot] 复用常驻曲线窗口（serve.sh）")
        else:
            print("[Academic Plot] 常驻窗口未在线（如需曲线：bash openclaw/serve.sh start 已含 plot）")

    orch.STATE_SERVER = args.server
    t0 = time.time()
    if not args.use_coordinator:
        # 默认：进程内直接跑阶段接力，绕过 coordinator（省 ~60s 冷启动+2 次 LLM 调用）。
        # coordinator 对协商逻辑无贡献，发言顺序固定在 structured_relay 内，详见 README。
        logger = SessionLogger(
            session_id=args.resume_run or None,
            verbose=False,
            event_sink=push_live,
            mode=args.mode,
            resume=bool(resume_checkpoint),
        )
        if resume_checkpoint:
            logger.session_resume({
                "boundary": resume_checkpoint.boundary,
                "projection_version": 1,
            })
        else:
            # initial 快照/episodic 特征/评估基线都取自这里：real 模式必须用
            # 真实上报（ready_state 的 data 载荷），不能用 mock 场景定义。
            if args.mode == "real":
                initial_ap_state = {
                    ap: ready_state[ap]["data"] for ap in ("ap1", "ap2", "ap3")
                }
            else:
                initial_ap_state = scene
            logger.session_start(
                model="openclaw-direct", scene=args.scene, ap_state=initial_ap_state
            )
        resume_projection = None
        if resume_checkpoint:
            resume_projection = {
                **(resume_checkpoint.projection or {}),
                "boundary": resume_checkpoint.boundary,
            }
        result = orch.structured_relay(
            max_turns=args.max_steps,
            on_event=None,
            on_event_start=_print_event_stream_start,
            on_event_chunk=_print_event_stream_chunk,
            on_tool=_print_tool,
            logger=logger,
            observation_state_getter=lambda: orch.apply_profile(orch.get_all_states(args.server)),
            observation_wait_seconds=args.observation_wait,
            executor_endpoints=executor_endpoints,
            resume_projection=resume_projection,
            evaluation_windows=eval_windows,
        )
    else:
        _require_openclaw_config(require_qwen80b=args.require_qwen80b)
        result = _run_via_coordinator(
            args.max_steps,
            mode=args.mode,
            scene=args.scene,
            server=args.server,
            observation_wait=args.observation_wait,
            executor_endpoints=executor_endpoints,
            eval_windows=eval_windows,
        )
    dur = time.time() - t0

    print(divider())
    outcome = result['outcome']
    outcome_txt = status_ok(outcome) if outcome == "success" else status_fail(outcome)
    print(f"{section('结果')} outcome={outcome_txt} turns={result['transcript_turns']} 用时 {dur:.0f}s")
    print(f"{section('策略')} {result['strategy']}")
    print(f"{section('最终决策')} {result['decision']}")
    if result.get("push_results"):
        print(f"{section('Executor')} {result['push_results']}")
    if result.get("log_path"):
        print(f"{dim('[日志]')} {result['log_path']}")
    v = result["validation"]
    if v:
        flag = status_ok("通过") if v["approved"] else status_fail("未通过")
        print(f"{status_label('Validator')} {flag} — {v['summary']}")
    else:
        print(f"{status_label('Validator')} {dim('无可验收决策（协商未收敛或未解析出决策 JSON）')}")

    run_id = logger.session_id if logger is not None else None
    pending_eval = bool(eval_windows) and result["outcome"] == "success" and run_id
    if feeder is not None and result["decision"]:
        feeder.apply_decision(result["decision"])
        if not args.exit_after_run:
            print("[Mock] 已将决策注入遥测，曲线将体现协商后改善。Ctrl-C 退出。")
            if pending_eval:
                print(f"[Outcome] 效果评估窗口已登记，到期自动结算：{eval_windows}s")
            try:
                while True:
                    time.sleep(2)
                    if pending_eval:
                        collected = _harvest_due_evaluations(args.server, run_id=run_id)
                        if collected and not _has_pending_evaluations(run_id):
                            pending_eval = False
                            print("[Outcome] 全部评估窗口已结算完成。Ctrl-C 退出。")
            except KeyboardInterrupt:
                pass
    if pending_eval:
        print(f"[Outcome] 效果评估窗口已登记（{eval_windows}s）；"
              "到期后由下次 run_openclaw 自动收割，或手动执行 "
              f".venv/bin/python memory_admin.py evaluate --server {args.server}")
    if feeder is not None:
        feeder.stop()
    # state server / dashboard / plot 均为 serve.sh 常驻服务，不由本进程管理，退出不动它们。


def _resolve_eval_windows(spec: str, mode: str) -> tuple[float, ...] | None:
    """解析评估窗口：CLI > 环境变量 > 按模式默认；off 关闭。"""
    from src.memory import DEFAULT_WINDOWS, parse_windows
    spec = (spec or os.environ.get("MULTIAP_EVAL_WINDOWS", "")).strip()
    if spec.lower() == "off":
        return None
    if spec:
        return parse_windows(spec)
    return DEFAULT_WINDOWS[mode]


def _event_store_enabled() -> bool:
    return os.environ.get("MULTIAP_EVENT_STORE", "1").lower() not in {
        "0", "false", "no", "off"
    }


def _harvest_due_evaluations(server: str, run_id: str | None = None) -> list[dict]:
    """结算到期的效果评估窗口；失败保持 pending 可重试，绝不阻塞协商。"""
    if not _event_store_enabled():
        return []
    from src.memory import collect_due_evaluations
    store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
    try:
        # 评估比较用全量原始遥测（含 iperf 吞吐/延迟/丢包），不套 agent 字段白名单。
        collected = collect_due_evaluations(
            store,
            lambda: orch.get_all_states(server),
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Outcome] 到期评估结算失败（保持 pending，稍后重试）：{exc}")
        return []
    finally:
        store.close()
    verdict_map = {
        "improved": status_ok("实际改善"), "degraded": status_fail("实际恶化"),
        "neutral": "无明显变化", "inconclusive": dim("数据不足"),
    }
    for item in collected:
        deltas = item.get("deltas") or {}
        print(f"[Outcome] run={item['run_id']} 窗口{item['window_label']} → "
              f"{verdict_map.get(item['verdict'], item['verdict'])} "
              f"(得分={deltas.get('score')} 置信度={item['confidence']})")
    return collected


def _has_pending_evaluations(run_id: str) -> bool:
    if not _event_store_enabled():
        return False
    store = EventStore(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
    try:
        return bool(store.list_evaluations(run_id, status="pending"))
    finally:
        store.close()


def _load_executor_endpoints(config_arg: str, endpoints_arg: str) -> dict[str, str] | None:
    # 必须显式给端点才推送：mock/演示默认无端点→跳过下发，避免对不可达的真实 AP
    # 反复 8s 超时。真实 AP 模式用 --ap-endpoints 或 --ap-config ap_endpoints.json。
    if config_arg:
        config_path = Path(config_arg)
        if not config_path.exists():
            print(f"[错误] --ap-config 文件不存在: {config_path}")
            sys.exit(1)
        return json.loads(config_path.read_text(encoding="utf-8"))
    if endpoints_arg:
        try:
            return _parse_executor_endpoints(endpoints_arg)
        except argparse.ArgumentTypeError as exc:
            print(f"[错误] {exc}")
            sys.exit(1)
    return None


def _run_via_coordinator(
    max_steps: int,
    *,
    mode: str,
    scene: str,
    server: str,
    observation_wait: float,
    executor_endpoints: dict[str, str] | None,
    eval_windows: tuple[float, ...] | None = None,
) -> dict:
    env = dict(os.environ)
    env.setdefault("OLLAMA_API_KEY", "ollama-local")
    env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY"))
    env["no_proxy"] = env["NO_PROXY"]
    env["MULTIAP_STATE_SERVER"] = server
    env["MULTIAP_SESSION_LOG"] = "1"
    env["MULTIAP_SCENE"] = scene
    env["MULTIAP_MODE"] = mode
    env["MULTIAP_MODEL"] = env.get("MULTIAP_MODEL", "openclaw")
    env["MULTIAP_OBSERVATION_WAIT"] = str(observation_wait)
    env["MULTIAP_EVAL_WINDOWS"] = (
        ",".join(f"{w:g}" for w in eval_windows) if eval_windows else "off"
    )
    if executor_endpoints:
        env["MULTIAP_EXECUTOR_ENDPOINTS"] = json.dumps(executor_endpoints, ensure_ascii=False)
    session_key = f"agent:coordinator:multiap-{scene}-{int(time.time())}"
    message = (
        "开始一次多 AP 协商。为控制时间，只调用 MCP 工具 "
        "multiap-tools__run_fast_negotiation；不要调用 exec/read，也不要检查环境。"
        f"参数 max_validation_retries=3, max_turns={max_steps}, "
        f"observation_wait_seconds={observation_wait:g}。"
        "工具返回后只汇总 outcome、strategy、validation、decision，不要逐句选择发言人。"
        "最后必须附一个 json 代码块，原样包含工具返回结果，且保留 outcome、strategy、decision、"
        "validation、transcript_turns 字段。"
    )
    print("[OpenClaw] 启动 coordinator，调用 run_fast_negotiation；以下实时显示各 AP 发言（来自会话日志）。", flush=True)
    pre_logs = set(glob.glob(str(_LOG_DIR / "session_*.jsonl")))
    proc = subprocess.Popen(
        [OPENCLAW_BIN, "--profile", OPENCLAW_PROFILE, "agent", "--local",
         "--agent", "coordinator", "--session-key", session_key,
         "--thinking", "off", "--message", message, "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    started = time.time()
    fh = None
    last_activity = started
    last_heartbeat = started
    try:
        while proc.poll() is None:
            now = time.time()
            if fh is None:
                path = _find_new_session_log(pre_logs)
                if path:
                    fh = open(path, "r", encoding="utf-8")
            if fh is not None and _drain_session_log(fh):
                last_activity = now
            # 仅在启动期或长间隔（等模型响应）才心跳，避免与对话交错刷屏
            if now - last_activity > 20 and now - last_heartbeat > 20:
                print(dim(f"[OpenClaw] 仍在运行 {int(now - started)}s（等待模型响应）…"), flush=True)
                last_heartbeat = now
            if now - started > 1800:
                proc.kill()
                stdout, stderr = proc.communicate()
                print(stderr or stdout)
                print("[错误] coordinator 超过 1800s 未结束。")
                sys.exit(1)
            time.sleep(0.5)
    finally:
        if fh is not None:
            _drain_session_log(fh)   # 排空收尾事件
            fh.close()
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        print(stderr or stdout)
        sys.exit(proc.returncode)
    result = _extract_fast_result(stdout.strip())
    if result is None:
        print(stdout)
        print("[错误] coordinator 未返回可解析的 run_fast_negotiation 结果。")
        sys.exit(1)
    return result


def _merge_no_proxy(current: str | None) -> str:
    required = ["localhost", "127.0.0.1", "::1"]
    values = [v.strip() for v in (current or "").split(",") if v.strip()]
    for item in required:
        if item not in values:
            values.append(item)
    return ",".join(values)


def _require_openclaw_config(require_qwen80b: bool = False) -> None:
    if not Path(OPENCLAW_BIN).exists():
        print(f"[错误] 未找到 OpenClaw 可执行文件：{OPENCLAW_BIN}")
        print("请先安装 OpenClaw，或通过 OPENCLAW_BIN 指定路径。")
        sys.exit(1)
    cfg = Path.home() / f".openclaw-{OPENCLAW_PROFILE}" / "openclaw.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[错误] 未找到 OpenClaw profile 配置：{cfg}")
        print("请先运行：bash openclaw/setup.sh")
        sys.exit(1)

    if not require_qwen80b:
        return

    defaults = data.get("agents", {}).get("defaults", {})
    primary = (defaults.get("model") or {}).get("primary")
    models = defaults.get("models") or {}
    alias_refs = {
        ref for ref, spec in models.items()
        if isinstance(spec, dict) and spec.get("alias") == "qwen80binstruct"
    }
    ppio_models = data.get("models", {}).get("providers", {}).get("ppio", {}).get("models", [])
    has_ppio_80b = any(
        isinstance(m, dict)
        and (m.get("name") == "qwen80binstruct" or "80b" in str(m.get("id", "")).lower())
        for m in ppio_models
    )
    primary_ok = primary == "qwen80binstruct" or primary in alias_refs
    if not primary_ok or not has_ppio_80b:
        print("[错误] 当前 multiap profile 未配置为 qwen80binstruct 默认模型。")
        print(f"当前 primary={primary!r}，qwen80binstruct refs={sorted(alias_refs)!r}")
        print("请运行：bash openclaw/setup.sh")
        sys.exit(1)


def _extract_fast_result(raw: str) -> dict | None:
    """从 OpenClaw JSON 输出中提取 run_fast_negotiation 工具结果。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def walk(obj):
        if isinstance(obj, dict):
            if "outcome" in obj and "transcript_turns" in obj:
                return obj
            for value in obj.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found is not None:
                    return found
        return None

    found = walk(data)
    if found is not None:
        return found

    text = _collect_text(data)
    if not text:
        return None
    return _extract_result_from_text(text)


def _collect_text(obj) -> str:
    chunks: list[str] = []

    def walk(obj):
        if isinstance(obj, dict):
            for key in ("text", "finalAssistantVisibleText", "finalAssistantRawText"):
                value = obj.get(key)
                if isinstance(value, str):
                    chunks.append(value)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(obj)
    return "\n".join(chunks)


def _extract_result_from_text(text: str) -> dict | None:
    candidates: list[str] = []
    parts = text.split("```")
    for i in range(1, len(parts), 2):
        block = parts[i].strip()
        if block.lower().startswith("json"):
            block = block[4:].strip()
        candidates.append(block)
    candidates.append(text)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "outcome" in obj and "transcript_turns" in obj:
            return obj
    return None


if __name__ == "__main__":
    main()

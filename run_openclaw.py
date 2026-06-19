"""
纯 OpenClaw 架构入口 —— coordinator 触发阶段级快速协商。

用法：
  python run_openclaw.py --scene joint            # mock A：预设场景
  python run_openclaw.py --scene sr --max-steps 20
  python run_openclaw.py --scene edca --no-feeder  # 不喂曲线，仅跑协商

前置：openclaw 已装、ollama 运行、已执行过 `bash openclaw/setup.sh`。
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "openclaw" / "mcp"))

from openclaw.scenes import (
    DEFAULT_AP_CONFIG,
    MOCK_SCENES,
    _parse_executor_endpoints,
    start_academic_plot,
    start_dashboard,
    start_mock_server,
)
from src.logger import SessionLogger
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

def _print_event(phase, who, reply):
    # 只保留 agent 名称作为发言头部，不打阶段标签（广播/提案/投票…）
    print(divider())
    print(f"\n{format_ap_name(who.upper())}:")
    print(strip_md(reply).strip())


def _print_tool(name, args, result, dur_ms):
    """工具调用行回调：复用 orchestrator 的富摘要 formatter，失败则回退一行。"""
    result_dict = result if isinstance(result, dict) else ({"_text": result} if result else {})
    try:
        line = _format_tool_console(name, args, result_dict, dur_ms)
    except Exception:
        line = f"{tool_prefix()} {tool_name(name)} → {status_fail('结果解析失败')}"
    print(line, flush=True)


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
    ap = argparse.ArgumentParser(description="多 AP 协商（纯 OpenClaw / coordinator 阶段级触发）")
    ap.add_argument("--scene", choices=["sr", "edca", "joint"], default="joint")
    ap.add_argument("--server", default="http://localhost:5001")
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--no-feeder", action="store_true", help="不启动曲线喂数器")
    ap.add_argument("--direct-relay", action="store_true",
                    help="调试用：绕过 coordinator，直接运行阶段接力")
    ap.add_argument("--observation-wait", type=float, default=0.0,
                    help="最终 Validator 读取观测状态前等待秒数")
    ap.add_argument("--ap-endpoints", default="",
                    help="协商成功后推送决策的执行服务地址，格式 ap1=host:port,ap2=...")
    ap.add_argument("--ap-config", default="",
                    help="从 JSON 文件读取执行服务地址；默认自动读取 ap_endpoints.json")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="不启动可视化 Dashboard")
    ap.add_argument("--dashboard-port", type=int, default=5050)
    ap.add_argument("--no-academic-plot", action="store_true",
                    help="不弹出 Matplotlib 学术曲线窗口")
    ap.add_argument("--plot-window", type=float, default=25.0)
    ap.add_argument("--plot-interval", type=float, default=1.0)
    ap.add_argument("--require-qwen80b", action="store_true",
                    help="强制要求 multiap profile 默认模型为 qwen80binstruct")
    ap.add_argument("--exit-after-run", action="store_true",
                    help="协商结束后不保持 mock 曲线展示，直接退出")
    args = ap.parse_args()

    os.environ["NO_PROXY"] = _merge_no_proxy(os.environ.get("NO_PROXY"))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    scene = MOCK_SCENES[args.scene]
    print(f"[run_openclaw] scene={args.scene} server={args.server} max_steps={args.max_steps}", flush=True)

    executor_endpoints = _load_executor_endpoints(args.ap_config, args.ap_endpoints)
    if executor_endpoints:
        print(f"执行推送端点：{executor_endpoints}")
    else:
        print("执行推送：未配置（协商结果仅输出到控制台）")

    feeder = None
    plot_proc = None
    logger = None
    ready, proc = start_mock_server(args.server)
    if not ready:
        print("[错误] 状态服务器未就绪。请先 `python3 state_server/server.py --allow-mock`。")
        sys.exit(1)
    if not args.no_feeder:
        feeder = MockTelemetryFeeder(args.server, scene, interval=args.plot_interval)
        feeder.start()
    else:
        # 不持续喂曲线时也推一帧，确保状态服务器可读。
        single = MockTelemetryFeeder(args.server, scene, interval=args.plot_interval)
        single.start()
        time.sleep(0.2)
        single.stop()
    time.sleep(2.0)  # 等首批遥测落库

    push_live = None
    if not args.no_academic_plot:
        plot_proc = start_academic_plot(
            state_server=args.server,
            window_seconds=args.plot_window,
            interval_seconds=args.plot_interval,
        )
    if not args.no_dashboard:
        push_live = start_dashboard(port=args.dashboard_port, state_server=args.server)

    orch.STATE_SERVER = args.server
    t0 = time.time()
    if args.direct_relay:
        logger = SessionLogger(verbose=False, event_sink=push_live)
        logger.session_start(model="openclaw-direct", scene=args.scene, ap_state=scene)
        result = orch.structured_relay(
            max_turns=args.max_steps,
            on_event=_print_event,
            on_tool=_print_tool,
            logger=logger,
            observation_state_getter=lambda: orch.apply_profile(orch.get_all_states(args.server)),
            observation_wait_seconds=args.observation_wait,
            executor_endpoints=executor_endpoints,
        )
    else:
        _require_openclaw_config(require_qwen80b=args.require_qwen80b)
        result = _run_via_coordinator(
            args.max_steps,
            scene=args.scene,
            server=args.server,
            observation_wait=args.observation_wait,
            executor_endpoints=executor_endpoints,
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

    if feeder is not None and result["decision"]:
        feeder.apply_decision(result["decision"])
        if not args.exit_after_run:
            print("[Mock] 已将决策注入遥测，曲线将体现协商后改善。Ctrl-C 退出。")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    if feeder is not None:
        feeder.stop()
    if plot_proc is not None:
        plot_proc.terminate()
    if proc is not None:
        proc.terminate()


def _load_executor_endpoints(config_arg: str, endpoints_arg: str) -> dict[str, str] | None:
    config_path = Path(config_arg) if config_arg else DEFAULT_AP_CONFIG
    if config_arg or (not endpoints_arg and config_path.exists()):
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
    scene: str,
    server: str,
    observation_wait: float,
    executor_endpoints: dict[str, str] | None,
) -> dict:
    env = dict(os.environ)
    env.setdefault("OLLAMA_API_KEY", "ollama-local")
    env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY"))
    env["no_proxy"] = env["NO_PROXY"]
    env["MULTIAP_STATE_SERVER"] = server
    env["MULTIAP_SESSION_LOG"] = "1"
    env["MULTIAP_SCENE"] = scene
    env["MULTIAP_MODEL"] = env.get("MULTIAP_MODEL", "openclaw")
    env["MULTIAP_OBSERVATION_WAIT"] = str(observation_wait)
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

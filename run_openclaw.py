"""
纯 OpenClaw 架构入口 —— coordinator 触发阶段级快速协商。

用法：
  python run_openclaw.py --data-source ns3 --max-steps 20
  python run_openclaw.py --data-source real --server http://localhost:5001

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

from openclaw.scenes import _parse_executor_endpoints
from state_server.ns3_bridge import _normalize as _normalize_ns3_record
from state_server.ns3_bridge import _parse_line as _parse_ns3_line
from state_server.ns3_bridge import _post as _post_ns3_state
from state_server.ns3_bridge import _records_from_obj as _ns3_records_from_obj
from state_server.ns3_scenario_matrix import BUSINESS_PROFILES, TOPOLOGIES, get_case
from src.logger import SessionLogger
from src.console_style import (
    format_ap_name, strip_md, divider, section, status_label,
    status_ok, status_fail, dim, tool_prefix, tool_name,
)
from openclaw.mcp.tool_console import _format_tool_console
import orchestration as orch


OPENCLAW_BIN = (
    os.environ.get("OPENCLAW_BIN")
    or shutil.which("openclaw")
    or str(Path.home() / ".openclaw" / "bin" / "openclaw")
)
OPENCLAW_PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")
_STREAM_AT_LINE_START = True


class Ns3LiveController:
    """Own one ns-3 live process and bridge real TELEMETRY/APPLY traffic."""

    def __init__(
        self,
        *,
        root: str,
        server: str,
        scenario: str,
        business_profile: str,
        sim_time: float,
        report_interval: float,
        extra_args: list[str] | None = None,
        include_case_extra_args: bool = True,
    ) -> None:
        self.root = Path(root).expanduser()
        self.server = server.rstrip("/")
        self.scenario = scenario
        self.business_profile = business_profile
        self.sim_time = sim_time
        self.report_interval = report_interval
        self.extra_args = extra_args or []
        self.include_case_extra_args = include_case_extra_args
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._posted = 0
        self._failed = 0

    def start(self) -> None:
        if not self.root.exists():
            print(f"[错误] ns-3 根目录不存在：{self.root}")
            sys.exit(1)
        if self.include_case_extra_args:
            case = get_case(self.scenario, self.business_profile)
            args = case.ns3_args(
                sim_time=self.sim_time,
                report_interval=self.report_interval,
                live=True,
            )
            expected = case.expected_strategy
            reason = case.reason
        else:
            args = [
                "--live=1",
                f"--scenario={self.scenario}",
                f"--businessProfile={self.business_profile}",
                f"--simTime={self.sim_time:g}",
                f"--reportInterval={self.report_interval:g}",
            ]
            expected = "direct"
            reason = "direct ns-3 scan uses scenario-local parameters"
        args.extend(self.extra_args)
        scratch = "scratch/multiap_coop/multiap_coop " + " ".join(args)
        cmd = ["./ns3", "run", scratch]
        print(f"[ns3] 启动托管 live 仿真：{' '.join(cmd)}")
        print(f"[ns3] 预期策略：{expected}；{reason}")
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            print(f"[错误] 无法启动 ns-3：{exc}")
            sys.exit(1)
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        import requests

        session = requests.Session()
        session.trust_env = False
        for raw in self.proc.stdout:
            if self._stop.is_set():
                break
            line = raw.strip()
            if not line:
                continue
            try:
                obj = _parse_ns3_line(line)
            except json.JSONDecodeError as exc:
                self._failed += 1
                print(f"[ns3] 忽略非法 JSON 输出：{exc}", file=sys.stderr)
                continue
            if obj is None:
                continue
            if not isinstance(obj, dict):
                self._failed += 1
                continue
            records = _ns3_records_from_obj(obj)
            if not records:
                self._failed += 1
                continue
            for record in records:
                payload = _normalize_ns3_record(record)
                if _post_ns3_state(session, self.server, payload):
                    self._posted += 1
                else:
                    self._failed += 1

    def _pump_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for raw in self.proc.stderr:
            if self._stop.is_set():
                break
            line = raw.rstrip()
            if line:
                print(f"[ns3] {line}")

    def wait_until_ready(self, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            if self._posted >= 3:
                return True
            time.sleep(0.1)
        return self._posted >= 3

    def apply_decision(self, decision: dict, strategy: str, session_id: str = "") -> dict[str, dict]:
        if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
            return {
                "ns3": {
                    "ok": False,
                    "url": "ns3://stdin",
                    "payload": {"strategy": strategy, "decision": decision},
                    "response": "ns-3 live process is not running",
                }
            }
        if strategy not in ("co_sr", "co_edca"):
            return {
                "ns3": {
                    "ok": False,
                    "url": "ns3://stdin",
                    "payload": {"strategy": strategy, "decision": decision},
                    "response": "unsupported strategy for ns-3 APPLY",
                }
            }

        results: dict[str, dict] = {}
        for ap_id in ("ap1", "ap2", "ap3"):
            params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
            if not isinstance(params, dict):
                params = {}
            if strategy == "co_sr":
                if "tx_power_dbm" not in params:
                    results[ap_id] = {
                        "ok": False,
                        "url": "ns3://stdin",
                        "payload": {"ap_id": ap_id, "strategy": strategy, "params": params},
                        "response": "missing tx_power_dbm",
                    }
                    continue
                command = f"APPLY {ap_id} tx={float(params['tx_power_dbm']):g}"
            else:
                missing = [k for k in ("CWmin", "CWmax", "AIFSN") if k not in params]
                if missing:
                    results[ap_id] = {
                        "ok": False,
                        "url": "ns3://stdin",
                        "payload": {"ap_id": ap_id, "strategy": strategy, "params": params},
                        "response": f"missing EDCA params {missing}",
                    }
                    continue
                parts = [
                    f"APPLY {ap_id}",
                    f"cwmin={int(params['CWmin'])}",
                    f"cwmax={int(params['CWmax'])}",
                    f"aifsn={int(params['AIFSN'])}",
                ]
                for src, dst in (
                    ("VI_CWmin", "vi_cwmin"),
                    ("VI_CWmax", "vi_cwmax"),
                    ("VI_AIFSN", "vi_aifsn"),
                    ("vi_cwmin", "vi_cwmin"),
                    ("vi_cwmax", "vi_cwmax"),
                    ("vi_aifsn", "vi_aifsn"),
                ):
                    if src in params:
                        parts.append(f"{dst}={int(params[src])}")
                command = " ".join(parts)
            try:
                self.proc.stdin.write(command + "\n")
                self.proc.stdin.flush()
                results[ap_id] = {
                    "ok": True,
                    "url": "ns3://stdin",
                    "payload": {
                        "session_id": session_id,
                        "ap_id": ap_id,
                        "strategy": strategy,
                        "params": params,
                        "command": command,
                    },
                    "response": "queued to ns-3 stdin",
                }
            except OSError as exc:
                results[ap_id] = {
                    "ok": False,
                    "url": "ns3://stdin",
                    "payload": {"ap_id": ap_id, "strategy": strategy, "params": params},
                    "response": str(exc),
                }
        return results

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


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


def _require_state_server(url: str) -> None:
    """强制复用常驻 state server（serve.sh 起，仅接收 ns3/ap）。
    不在线则报错提示先 `bash openclaw/serve.sh start`，不再临时起。"""
    import requests
    try:
        sess = requests.Session()
        sess.trust_env = False
        r = sess.get(f"{url}/health", timeout=2)
        if r.status_code == 200:
            print(f"[State] 复用常驻 state server {url}")
            return
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


def _wait_state_ready(server: str, timeout_s: float = 1.0) -> None:
    """Wait until state server has a fresh row for every AP."""
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
                for ap in ("ap1", "ap2", "ap3")
            ):
                return
        except Exception:
            pass
        if time.time() >= deadline:
            return
        time.sleep(0.05)


def _fetch_required_initial_state(server: str, data_source: str) -> dict:
    """Read current telemetry and ensure it comes from ns-3 or real APs."""
    import requests

    expected_source = "ap" if data_source == "real" else "ns3"
    try:
        data = requests.get(f"{server.rstrip('/')}/state", timeout=2).json()
    except Exception as exc:
        print(f"[错误] 无法读取 state server 状态：{exc}")
        sys.exit(1)

    missing = []
    stale = []
    wrong_source = []
    initial = {}
    for ap_id in ("ap1", "ap2", "ap3"):
        row = data.get(ap_id) or {}
        payload = row.get("data")
        if not isinstance(payload, dict):
            missing.append(ap_id)
            continue
        if row.get("stale"):
            stale.append(ap_id)
        source = str(payload.get("source", "ap")).strip().lower()
        if source != expected_source:
            wrong_source.append(f"{ap_id}:{source or '<empty>'}")
        initial[ap_id] = payload

    if missing or stale or wrong_source:
        print("[错误] 当前状态不能作为实验输入。")
        if missing:
            print(f"  缺少 AP 状态：{missing}")
        if stale:
            print(f"  状态已过期：{stale}")
        if wrong_source:
            print(f"  数据源不匹配，期望 source={expected_source!r}：{wrong_source}")
        if data_source == "ns3":
            print("请先启动 ns-3 bridge，让三台 AP 以 source='ns3' 持续 POST /state。")
        else:
            print("请先启动真实 AP reporter，让三台 AP 以 source='ap' 持续 POST /state。")
        sys.exit(1)
    return initial


def _plot_pid_file() -> Path:
    return Path.home() / f".openclaw-{OPENCLAW_PROFILE}" / "run" / "plot.pid"


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
    ap = argparse.ArgumentParser(description="多 AP 协商（纯 OpenClaw / 进程内阶段接力）")
    ap.add_argument("--data-source", choices=["ns3", "real"], default="ns3",
                    help="实验数据来源：ns3=ns-3 仿真上报；real=真实 AP 上报")
    ap.add_argument("--server", default="http://localhost:5001")
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--ns3-external", action="store_true",
                    help="ns3 数据源下不启动托管 ns-3；改为要求外部 ns-3/bridge 已在线")
    ap.add_argument("--ns3-root", default="/Users/heyu/Developer/ns-3.47",
                    help="ns-3 根目录（默认: /Users/heyu/Developer/ns-3.47）")
    ap.add_argument("--ns3-scenario", choices=TOPOLOGIES, default="line",
                    help="托管 ns-3 拓扑场景")
    ap.add_argument("--ns3-business-profile", choices=BUSINESS_PROFILES, default="live_bulk",
                    help="托管 ns-3 业务画像")
    ap.add_argument("--ns3-sim-time", type=float, default=300.0,
                    help="托管 ns-3 live 仿真时长秒数")
    ap.add_argument("--ns3-report-interval", type=float, default=1.0,
                    help="托管 ns-3 TELEMETRY 采样间隔秒数")
    ap.add_argument("--ns3-extra-arg", action="append", default=[],
                    help="追加传给 ns-3 scratch 的参数，例如 --ns3-extra-arg=--seed=2")
    ap.add_argument("--use-coordinator", action="store_true",
                    help="走旧的 coordinator LLM 触发路径（默认已停用，仅兼容/对比用，"
                         "会多 ~60s 冷启动+2 次 LLM 调用）")
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
                    help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.data_source == "ns3" and not args.ns3_external and args.use_coordinator:
        print("[错误] 托管 ns-3 闭环需要 direct structured_relay，不能使用 --use-coordinator。")
        print("请去掉 --use-coordinator；外部自管 ns-3/bridge 才可使用 --ns3-external。")
        sys.exit(1)

    os.environ["NO_PROXY"] = _merge_no_proxy(os.environ.get("NO_PROXY"))
    os.environ["no_proxy"] = os.environ["NO_PROXY"]

    print(
        f"[run_openclaw] data_source={args.data_source} server={args.server} "
        f"max_steps={args.max_steps}",
        flush=True,
    )

    ns3_controller = None
    executor_endpoints = _load_executor_endpoints(args.ap_config, args.ap_endpoints)
    if executor_endpoints:
        print(f"执行推送端点：{executor_endpoints}")
    elif args.data_source == "ns3" and not args.ns3_external:
        print("执行推送：托管 ns-3 stdin APPLY")
    else:
        print("执行推送：未配置（协商结果仅输出到控制台）")

    logger = None
    # 强制常驻：核心服务由 serve.sh 起好；不在线则报错提示先 `serve.sh start`，不再临时起。
    _require_state_server(args.server)
    _require_gateway(args.use_coordinator)

    if args.data_source == "ns3" and not args.ns3_external:
        ns3_controller = Ns3LiveController(
            root=args.ns3_root,
            server=args.server,
            scenario=args.ns3_scenario,
            business_profile=args.ns3_business_profile,
            sim_time=args.ns3_sim_time,
            report_interval=args.ns3_report_interval,
            extra_args=args.ns3_extra_arg,
        )
        ns3_controller.start()
        if not ns3_controller.wait_until_ready(timeout_s=max(30.0, args.ns3_report_interval * 10.0)):
            print("[错误] 托管 ns-3 未能及时产生三台 AP 的 TELEMETRY。")
            ns3_controller.stop()
            sys.exit(1)

    _wait_state_ready(args.server, timeout_s=max(1.0, args.ns3_report_interval * 2.0))
    initial_state = _fetch_required_initial_state(args.server, args.data_source)
    profiled_initial_state = orch.apply_profile(initial_state)

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
    effective_observation_wait = args.observation_wait
    if ns3_controller is not None and effective_observation_wait <= 0:
        effective_observation_wait = max(3.0, args.ns3_report_interval * 2.5)
        print(f"[ns3] Validator 观测等待 {effective_observation_wait:g}s（等待 APPLY 后真实遥测）")

    t0 = time.time()
    try:
        if not args.use_coordinator:
            # 默认：进程内直接跑阶段接力，绕过 coordinator（省 ~60s 冷启动+2 次 LLM 调用）。
            # coordinator 对协商逻辑无贡献，发言顺序固定在 structured_relay 内，详见 README。
            logger = SessionLogger(verbose=False, event_sink=push_live)
            logger.session_start(
                model="openclaw-direct",
                scene=(
                    f"ns3:{args.ns3_scenario}/{args.ns3_business_profile}"
                    if ns3_controller is not None else args.data_source
                ),
                ap_state=profiled_initial_state,
            )
            result = orch.structured_relay(
                max_turns=args.max_steps,
                on_event=None,
                on_event_start=_print_event_stream_start,
                on_event_chunk=_print_event_stream_chunk,
                on_tool=_print_tool,
                logger=logger,
                observation_state_getter=lambda: orch.apply_profile(orch.get_all_states(args.server)),
                observation_wait_seconds=effective_observation_wait,
                executor_endpoints=executor_endpoints,
                decision_applier=(
                    ns3_controller.apply_decision if ns3_controller is not None else None
                ),
                initial_state=initial_state,
            )
        else:
            _require_openclaw_config(require_qwen80b=args.require_qwen80b)
            result = _run_via_coordinator(
                args.max_steps,
                scene=args.data_source,
                server=args.server,
                observation_wait=effective_observation_wait,
                executor_endpoints=executor_endpoints,
            )
    finally:
        if ns3_controller is not None:
            ns3_controller.stop()
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

    # state server / dashboard / plot 均为 serve.sh 常驻服务，不由本进程管理，退出不动它们。


def _load_executor_endpoints(config_arg: str, endpoints_arg: str) -> dict[str, str] | None:
    # 必须显式给端点才推送，避免对不可达的 AP 反复 8s 超时。
    # 真实 AP 模式用 --ap-endpoints 或 --ap-config ap_endpoints.json。
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

    expected_mcp = (Path(__file__).resolve().parent / "openclaw" / "mcp" / "multiap_mcp.py").resolve()
    mcp_conf = (((data.get("mcp") or {}).get("servers") or {}).get("multiap-tools") or {})
    mcp_args = mcp_conf.get("args") or []
    configured_mcp = Path(mcp_args[0]).expanduser().resolve() if mcp_args else None
    if configured_mcp != expected_mcp:
        print("[错误] OpenClaw multiap profile 的 MCP 工具未指向当前项目。")
        print(f"  当前配置：{configured_mcp or '<missing>'}")
        print(f"  期望配置：{expected_mcp}")
        print("请运行：bash openclaw/setup.sh，然后 bash openclaw/serve.sh restart")
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

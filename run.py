"""
多 AP 协商系统触发脚本

用法：
  python run.py                            # 从状态服务器获取数据（默认 PPIO 云端 qwen:80b）
  python run.py --mock                     # 使用 mock 数据（联合场景，触发 Co-SR + Co-EDCA）
  python run.py --mock --scene sr          # 仅 Co-SR 场景
  python run.py --mock --scene edca        # 仅 Co-EDCA 场景
  python run.py qwen:80b --mock            # 默认即此：PPIO qwen3-next-80b（需在 .env 填写 PPIO_API_KEY）
  python run.py qwen3:14b --mock           # 显式回退到本地 Ollama 模型
  python run.py --server http://192.168.1.100:5001  # 指定服务器地址

  # 协商完成后主动推送决策到香蕉派执行服务（静态 IP）
  python run.py --ap-endpoints ap1=192.168.1.1:5002,ap2=192.168.1.2:5002,ap3=192.168.1.3:5002

  # 或从 JSON 配置文件读取端点；默认自动读取仓库根目录 ap_endpoints.json（如果存在）
  python run.py --ap-config ap_endpoints.json
  # ap_endpoints.json 格式：{"ap1": "http://192.168.1.1:5002", ...}
"""
import argparse
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).parent))

from src.logger import SessionLogger
from src.orchestrator import NegotiationOrchestrator
from src.state_client import get_all_states, StateStaleError
from state_server.mock_feeder import MockTelemetryFeeder

DEFAULT_AP_CONFIG = Path(__file__).parent / "ap_endpoints.json"
STATE_LOG_DIR = Path("logs") / "state"


def start_dashboard(port: int = 5050, state_server: str = "http://localhost:5001"):
    """
    在主进程内以守护线程启动 Dashboard Flask 服务器。
    返回 push_event 回调（用作 SessionLogger 的 event_sink）。
    若启动失败，返回 None。
    """
    try:
        from dashboard.app import start_server_thread, push_event
        start_server_thread(port, state_server)
        print(f"[Dashboard] http://localhost:{port}/  (正在打开浏览器...)")
        return push_event
    except Exception as exc:
        print(f"[Dashboard] 启动失败: {exc}")
        return None


def start_academic_plot(
    state_server: str = "http://localhost:5001",
    window_seconds: float = 25.0,
    interval_seconds: float = 1.0,
) -> subprocess.Popen | None:
    """启动独立 Matplotlib 学术曲线窗口。"""
    plot_script = Path(__file__).parent / "state_server" / "academic_plot.py"
    if not plot_script.exists():
        print(f"[Academic Plot] 启动失败: 未找到 {plot_script}")
        return None

    cmd = [
        sys.executable,
        str(plot_script),
        "--server", state_server,
        "--window", str(window_seconds),
        "--interval", str(interval_seconds),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.8)
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            detail = (stderr or stdout or "").strip()
            msg = f"[Academic Plot] 未能保持运行（退出码 {proc.returncode}）。"
            if detail:
                msg += f" 原因：{detail}"
            print(msg)
            return None
        print(f"[Academic Plot] 已弹出 Matplotlib figure 曲线窗口（固定窗口 {window_seconds:g}s）。")
        return proc
    except Exception as exc:
        print(f"[Academic Plot] 启动失败: {exc}")
        return None


def _server_alive(server_url: str) -> bool:
    try:
        r = requests.get(f"{server_url}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _is_local_server(server_url: str) -> bool:
    parsed = urlparse(server_url)
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def start_mock_server(server_url: str) -> tuple[bool, subprocess.Popen | None]:
    """
    mock 模式下确保本地 state server 以 --allow-mock 运行，供喂数器写入。

    返回 (是否就绪, 我们启动的进程或 None)。复用已在线的服务器时进程为 None。
    """
    if not _is_local_server(server_url):
        return False, None
    if _server_alive(server_url):
        # 复用已在线的服务器（可能未带 --allow-mock，喂数会被拒，曲线为空）
        print("[Mock] 检测到 5001 已有服务器，直接复用。"
              "若曲线仍无数据，请确认它是以 --allow-mock 启动的。")
        return True, None

    server_script = Path(__file__).parent / "state_server" / "server.py"
    if not server_script.exists():
        return False, None

    print("[Mock] 启动 state server（--allow-mock）以驱动曲线 ...")
    proc = subprocess.Popen(
        [sys.executable, str(server_script), "--allow-mock"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(10):
        time.sleep(1)
        if _server_alive(server_url):
            return True, proc
    return False, proc


def ensure_server(server_url: str) -> bool:
    """检测服务器是否在线；本地地址未启动时自动拉起 server.py。"""
    if _server_alive(server_url):
        return True

    if not _is_local_server(server_url):
        return False

    server_script = Path(__file__).parent / "state_server" / "server.py"
    if not server_script.exists():
        return False

    print(f"[提示] 状态服务器未启动，正在后台启动 {server_script.name} ...")
    subprocess.Popen(
        [sys.executable, str(server_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for i in range(10):
        time.sleep(1)
        if _server_alive(server_url):
            print(f"[提示] 状态服务器已就绪（等待 {i + 1}s）。")
            return True

    return False


def start_telemetry_trace(server_url: str, session_id: str) -> str | None:
    try:
        r = requests.post(
            f"{server_url.rstrip('/')}/trace/start",
            json={"session_id": session_id, "dir": str(STATE_LOG_DIR)},
            timeout=3,
        )
        if r.status_code != 200:
            return None
        path = r.json().get("path")
        if path:
            print(f"[Trace] AP 上报参数 JSONL：{path}")
        return path
    except Exception:
        return None


def stop_telemetry_trace(server_url: str) -> None:
    try:
        requests.post(f"{server_url.rstrip('/')}/trace/stop", timeout=3)
    except Exception:
        pass

# ------------------------------------------------------------------
# Mock 数据：三个预设场景
# ------------------------------------------------------------------

# 场景一：Co-SR（三个 AP 功率不对称，AP1 高位干扰邻居，EDCA 正常）
# AP1=20dBm(高), AP2=14dBm(中), AP3=8dBm(低)
# neighbor_rssi 反映各 AP 在其当前功率下被邻居接收到的信号强度（路径损耗 dB 线性）
# AP2 接收到 AP1 的 -68.6 dBm，触发 Co-SR；AP3 距 AP1 较远，受影响较小
# STA 紧靠本 AP（sta_rssi ≈ -45~-50 dBm），降功率后不会断连
#
# 注意：cwmin/cwmax 按真实上报约定使用【指数 n】（CW = 2^n - 1），
# 与硬件 hostapd/iw 上报格式一致。这里 cwmin=3 / cwmax=4 表示实际 CW 7 / 15，
# 进入协商前由 apply_profile 统一解码为实际 CW 值。
MOCK_SCENE_SR = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "medium",
        "Data_rate_to_bandwidth_ratio": 0.45,
        "tx_retries_ratio": 0.08,
        # AP1 处接收：AP2 在 14dBm(-68.7 + (14-20)=-74.7)，AP3 在 8dBm(-76 + (8-20)=-88)
        "neighbor_rssi_dbm": {"ap2": -74.7, "ap3": -88.0},
        "sta_rssi_dbm": -45.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps_iperf": 22.1,
        "latency_ms": 210.0,
        "packet_loss_pct": 0.5,
    },
    "ap2": {
        "tx_power_dbm": 14.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "medium",
        "Data_rate_to_bandwidth_ratio": 0.50,
        "tx_retries_ratio": 0.10,
        # AP2 处接收：AP1 在 20dBm(-68.6)，AP3 在 8dBm(-69.4 + (8-20)=-81.4)
        "neighbor_rssi_dbm": {"ap1": -68.6, "ap3": -81.4},
        "sta_rssi_dbm": -48.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps_iperf": 20.3,
        "latency_ms": 195.0,
        "packet_loss_pct": 0.3,
    },
    "ap3": {
        "tx_power_dbm": 8.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "medium",
        "Data_rate_to_bandwidth_ratio": 0.38,
        "tx_retries_ratio": 0.06,
        # AP3 处接收：AP1 在 20dBm(-76)，AP2 在 14dBm(-70 + (14-20)=-76)
        "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -76.0},
        "sta_rssi_dbm": -50.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps_iperf": 28.5,
        "latency_ms": 120.0,
        "packet_loss_pct": 0.1,
    },
}

# 场景二：Co-EDCA（三 AP 承载不同优先级业务，当前 EDCA 参数未差异化，需协商）
# AP1 承载语音/视频（high），AP2 承载通用数据（medium），AP3 承载后台传输（low）
# 邻居 RSSI 弱，不触发 Co-SR；通过优先级驱动 EDCA 差异化来保障 AP1 的 QoS
MOCK_SCENE_EDCA = {
    "ap1": {
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "high",
        "Data_rate_to_bandwidth_ratio": 0.55,
        "tx_retries_ratio": 0.12,
        "neighbor_rssi_dbm": {"ap2": -85.0, "ap3": -88.0},
        "sta_rssi_dbm": -55.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps_iperf": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap2": {
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "medium",
        "Data_rate_to_bandwidth_ratio": 0.50,
        "tx_retries_ratio": 0.10,
        "neighbor_rssi_dbm": {"ap1": -85.0, "ap3": -87.0},
        "sta_rssi_dbm": -61.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps_iperf": 28.7,
        "latency_ms": 185.0,
        "packet_loss_pct": 0.4,
    },
    "ap3": {
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "low",
        "Data_rate_to_bandwidth_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -88.0, "ap2": -87.0},
        "sta_rssi_dbm": -58.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps_iperf": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}

# 场景三：联合（高功率 + 业务优先级分化，同时触发 Co-SR 和 Co-EDCA）
# AP1 承载语音/视频（high），AP2 承载通用数据（medium），AP3 承载后台传输（low）
# neighbor_rssi 与场景一类似（STA 距本 AP 近，降功率后不会断连）
MOCK_SCENE_JOINT = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "high",
        "Data_rate_to_bandwidth_ratio": 0.55,
        "tx_retries_ratio": 0.12,
        "neighbor_rssi_dbm": {"ap2": -68.4, "ap3": -76.0},
        "sta_rssi_dbm": -45.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps_iperf": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap2": {
        "tx_power_dbm": 20.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "medium",
        "Data_rate_to_bandwidth_ratio": 0.50,
        "tx_retries_ratio": 0.10,
        "neighbor_rssi_dbm": {"ap1": -68.6, "ap3": -69.2},
        "sta_rssi_dbm": -48.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps_iperf": 28.7,
        "latency_ms": 185.0,
        "packet_loss_pct": 0.4,
    },
    "ap3": {
        "tx_power_dbm": 20.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "low",
        "Data_rate_to_bandwidth_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -69.2},
        "sta_rssi_dbm": -50.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps_iperf": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}

MOCK_SCENES = {
    "sr":    MOCK_SCENE_SR,
    "edca":  MOCK_SCENE_EDCA,
    "joint": MOCK_SCENE_JOINT,
}

# 业务优先级 → 用户流量接入类别（AC）：high=语音 VO，medium=尽力而为 BE，low=后台 BK
_USER_AC_BY_PRIORITY = {"high": "VO", "medium": "BE", "low": "BK"}


def _augment_observation_fields(scene: dict) -> None:
    """为 mock 场景补齐新增的只读观测字段（iperf/user 双路吞吐 + AC 类型）。"""
    for state in scene.values():
        iperf = state.get("throughput_mbps_iperf", 0.0)
        state.setdefault("throughput_mbps_user", round(iperf * 0.6, 1))
        state.setdefault("ac_iperf", "BK")  # iperf 测试流走后台队列
        state.setdefault(
            "ac_user", _USER_AC_BY_PRIORITY.get(state.get("traffic_priority", "medium"), "BE")
        )


for _scene in MOCK_SCENES.values():
    _augment_observation_fields(_scene)


def _parse_executor_endpoints(raw: str) -> dict[str, str]:
    """
    解析 --ap-endpoints 参数字符串。

    格式：ap1=192.168.1.11:5002,ap2=192.168.1.12:5002,ap3=192.168.1.13:5002
    支持带或不带 http:// 前缀；解析后统一补全 http://。
    """
    endpoints: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise argparse.ArgumentTypeError(
                f"--ap-endpoints 格式错误，期望 ap_id=host:port，实际收到 {item!r}"
            )
        ap_id, addr = item.split("=", 1)
        ap_id = ap_id.strip().lower()
        addr  = addr.strip()
        if not addr.startswith("http"):
            addr = f"http://{addr}"
        endpoints[ap_id] = addr
    return endpoints


def main():
    parser = argparse.ArgumentParser(description="多 AP 协商系统")
    parser.add_argument("model", nargs="?", default="qwen:80b",
                        help="模型名：qwen:80b（PPIO 云端，默认）或 Ollama 本地模型如 qwen3:14b")
    parser.add_argument("--mock", action="store_true",
                        help="使用 mock 数据，无需启动状态服务器")
    parser.add_argument("--scene", choices=["sr", "edca", "joint"], default="joint",
                        help="mock 场景：sr / edca / joint（默认 joint）")
    parser.add_argument("--server", default="http://localhost:5001",
                        help="状态服务器地址（默认 http://localhost:5001）")
    parser.add_argument("--observation-wait", type=float, default=30.0,
                        help="最终 Validator 观测周期秒数（默认 30）")
    parser.add_argument("--ap-endpoints", default="",
                        help=("协商完成后推送决策的香蕉派执行服务地址，"
                              "格式：ap1=192.168.1.1:5002,ap2=...,ap3=..."))
    parser.add_argument("--ap-config", default="",
                        help=("从 JSON 文件读取执行服务地址，"
                              'JSON 格式：{"ap1": "http://192.168.1.1:5002", ...}；'
                              "未传时自动读取 ap_endpoints.json（如果存在）"))
    parser.add_argument("--no-dashboard", action="store_true",
                        help="不启动可视化 Dashboard（纯 CLI 模式）")
    parser.add_argument("--dashboard-port", type=int, default=5050,
                        help="Dashboard 监听端口（默认 5050）")
    parser.add_argument("--no-academic-plot", action="store_true",
                        help="不弹出 Matplotlib 学术曲线窗口")
    parser.add_argument("--plot-window", type=float, default=25.0,
                        help="Matplotlib 曲线固定滑动窗口秒数（默认 25）")
    parser.add_argument("--plot-interval", type=float, default=1.0,
                        help="Matplotlib 曲线刷新间隔秒数（默认 1）")
    args = parser.parse_args()

    # 解析执行服务端点
    executor_endpoints: dict[str, str] | None = None
    config_path = Path(args.ap_config) if args.ap_config else DEFAULT_AP_CONFIG
    if args.ap_config or (not args.ap_endpoints and config_path.exists()):
        if not config_path.exists():
            print(f"[错误] --ap-config 文件不存在: {config_path}")
            sys.exit(1)
        executor_endpoints = json.loads(config_path.read_text())
    elif args.ap_endpoints:
        try:
            executor_endpoints = _parse_executor_endpoints(args.ap_endpoints)
        except argparse.ArgumentTypeError as e:
            print(f"[错误] {e}")
            sys.exit(1)

    if executor_endpoints:
        print(f"执行推送端点：{executor_endpoints}")
    else:
        print("执行推送：未配置（协商结果仅输出到控制台）")

    print(f"模型：{args.model}")
    print(f"数据来源：{'mock(' + args.scene + ')' if args.mock else args.server}")

    # 获取 AP 状态
    feeder: MockTelemetryFeeder | None = None
    mock_server_proc: subprocess.Popen | None = None
    if args.mock:
        ap_state = MOCK_SCENES[args.scene]
        # mock 模式没有真实遥测源：启动本地 state server + 喂数器驱动曲线。
        # 关闭可视化时无需喂数。
        if not (args.no_academic_plot and args.no_dashboard):
            ready, mock_server_proc = start_mock_server(args.server)
            if ready:
                feeder = MockTelemetryFeeder(
                    args.server, ap_state, interval=args.plot_interval
                )
                feeder.start()
            else:
                print("[Mock] state server 未就绪，曲线将无数据（不影响协商）。")
    else:
        if not ensure_server(args.server):
            print(f"\n[错误] 无法连接或启动状态服务器 {args.server}。")
            print("提示：使用 --mock 参数可跳过服务器，直接以 mock 数据运行。")
            sys.exit(1)
        try:
            ap_state = get_all_states(args.server)
            print(f"已从 {args.server} 获取三台 AP 的最新状态。")
        except (ConnectionError, StateStaleError) as e:
            print(f"\n[错误] {e}")
            print("提示：使用 --mock 参数可跳过服务器，直接以 mock 数据运行。")
            sys.exit(1)

    # 可视化先于 logger 启动，拿到 push_event 回调再创建 logger。
    # Matplotlib 学术图窗默认弹出；--no-academic-plot 才关闭它。
    push_live = None
    plot_proc = None
    if not args.no_academic_plot:
        plot_proc = start_academic_plot(
            state_server=args.server,
            window_seconds=args.plot_window,
            interval_seconds=args.plot_interval,
        )
    if not args.no_dashboard:
        push_live = start_dashboard(port=args.dashboard_port, state_server=args.server)

    scene = args.scene if args.mock else "live"
    logger = SessionLogger(verbose=False, event_sink=push_live)
    logger.session_start(model=args.model, scene=scene, ap_state=ap_state)
    telemetry_trace_path = start_telemetry_trace(args.server, logger.session_id)

    # 浏览器打开实时流页面（?live=1）；等浏览器真正建立 SSE 连接后再开始协商
    if not args.no_dashboard and push_live is not None:
        import webbrowser as _wb
        from dashboard.app import wait_for_client
        _wb.open(f"http://localhost:{args.dashboard_port}/?live=1&log={logger.log_path}")
        wait_for_client(timeout=1)

    agents_dir = Path(__file__).parent / "agents"
    observation_getter = None if args.mock else lambda: get_all_states(args.server)
    orchestrator = NegotiationOrchestrator(
        agents_dir=agents_dir,
        model=args.model,
        logger=logger,
        observation_state_getter=observation_getter,
        observation_wait_seconds=args.observation_wait,
        executor_endpoints=executor_endpoints,
    )
    def _cleanup_mock() -> None:
        if feeder is not None:
            feeder.stop()
        if mock_server_proc is not None:
            mock_server_proc.terminate()
        if plot_proc is not None:
            plot_proc.terminate()

    try:
        orchestrator.run(ap_state)
    except Exception:
        if telemetry_trace_path:
            stop_telemetry_trace(args.server)
        logger.session_end(outcome="error", total_rounds=0)
        _cleanup_mock()
        raise
    finally:
        if telemetry_trace_path:
            stop_telemetry_trace(args.server)

    # mock 模式收尾：把协商决策注入喂数器，让曲线体现协商后状态，
    # 并在可视化开启时保持喂数，直到用户 Ctrl-C。
    if feeder is not None:
        if orchestrator.last_decision:
            feeder.apply_decision(orchestrator.last_decision)
            print("\n[Mock] 已将协商决策注入遥测，曲线将逐步体现协商后改善。")
        if not (args.no_academic_plot and args.no_dashboard):
            print("[Mock] 曲线持续展示中，按 Ctrl-C 退出并清理。")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n[Mock] 收到退出信号，正在清理 ...")
        _cleanup_mock()


if __name__ == "__main__":
    main()

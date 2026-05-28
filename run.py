"""
多 AP 协商系统触发脚本

用法：
  python run.py                            # 从状态服务器获取数据（默认模型 qwen3:14b）
  python run.py --mock                     # 使用 mock 数据（联合场景，触发 Co-SR + Co-EDCA）
  python run.py --mock --scene sr          # 仅 Co-SR 场景
  python run.py --mock --scene edca        # 仅 Co-EDCA 场景
  python run.py qwen3:14b --mock           # 指定模型 + mock 数据
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

DEFAULT_AP_CONFIG = Path(__file__).parent / "ap_endpoints.json"


def _dashboard_alive(port: int) -> bool:
    try:
        r = requests.get(f"http://localhost:{port}/", timeout=1)
        return r.status_code == 200
    except Exception:
        return False


def start_dashboard(log_path: Path, port: int = 5050) -> None:
    """Start dashboard server in background and open browser."""
    script = Path(__file__).parent / "dashboard" / "app.py"
    if not script.exists():
        print("[Dashboard] 未找到 dashboard/app.py，跳过可视化启动。")
        return

    if not _dashboard_alive(port):
        subprocess.Popen(
            [sys.executable, str(script), "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(12):
            time.sleep(0.5)
            if _dashboard_alive(port):
                break

    url = f"http://localhost:{port}/?log={log_path}"
    print(f"[Dashboard] http://localhost:{port}/  (正在打开浏览器...)")
    webbrowser.open(url)


def _server_alive(server_url: str) -> bool:
    try:
        r = requests.get(f"{server_url}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _is_local_server(server_url: str) -> bool:
    parsed = urlparse(server_url)
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"}


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

# ------------------------------------------------------------------
# Mock 数据：三个预设场景
# ------------------------------------------------------------------

# 场景一：Co-SR（三个 AP 功率不对称，AP1 高位干扰邻居，EDCA 正常）
# AP1=20dBm(高), AP2=14dBm(中), AP3=8dBm(低)
# neighbor_rssi 反映各 AP 在其当前功率下被邻居接收到的信号强度（路径损耗 dB 线性）
# AP2 接收到 AP1 的 -68.6 dBm，触发 Co-SR；AP3 距 AP1 较远，受影响较小
# STA 紧靠本 AP（sta_rssi ≈ -45~-50 dBm），降功率后不会断连
MOCK_SCENE_SR = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.45,
        "tx_retries_ratio": 0.08,
        # AP1 处接收：AP2 在 14dBm(-68.7 + (14-20)=-74.7)，AP3 在 8dBm(-76 + (8-20)=-88)
        "neighbor_rssi_dbm": {"ap2": -74.7, "ap3": -88.0},
        "sta_rssi_dbm": -45.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps": 22.1,
        "latency_ms": 210.0,
        "packet_loss_pct": 0.5,
    },
    "ap2": {
        "tx_power_dbm": 14.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.50,
        "tx_retries_ratio": 0.10,
        # AP2 处接收：AP1 在 20dBm(-68.6)，AP3 在 8dBm(-69.4 + (8-20)=-81.4)
        "neighbor_rssi_dbm": {"ap1": -68.6, "ap3": -81.4},
        "sta_rssi_dbm": -48.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps": 20.3,
        "latency_ms": 195.0,
        "packet_loss_pct": 0.3,
    },
    "ap3": {
        "tx_power_dbm": 8.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.38,
        "tx_retries_ratio": 0.06,
        # AP3 处接收：AP1 在 20dBm(-76)，AP2 在 14dBm(-70 + (14-20)=-76)
        "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -76.0},
        "sta_rssi_dbm": -50.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps": 28.5,
        "latency_ms": 120.0,
        "packet_loss_pct": 0.1,
    },
}

# 场景二：Co-EDCA（EDCA 拥塞严重，邻居 RSSI 弱）
MOCK_SCENE_EDCA = {
    "ap1": {
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 7, "aifsn": 1,
        "channel_busy_ratio": 0.82,
        "tx_retries_ratio": 0.31,
        "neighbor_rssi_dbm": {"ap2": -85.0, "ap3": -88.0},
        "sta_rssi_dbm": -55.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap2": {
        "tx_power_dbm": 10.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.55,
        "tx_retries_ratio": 0.12,
        "neighbor_rssi_dbm": {"ap1": -85.0, "ap3": -87.0},
        "sta_rssi_dbm": -61.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps": 28.7,
        "latency_ms": 185.0,
        "packet_loss_pct": 0.4,
    },
    "ap3": {
        "tx_power_dbm": 10.0,
        "cwmin": 15, "cwmax": 63, "aifsn": 4,
        "channel_busy_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -88.0, "ap2": -87.0},
        "sta_rssi_dbm": -58.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}

# 场景三：联合（高功率 + 严重拥塞，同时触发 Co-SR 和 Co-EDCA）
# neighbor_rssi 与场景一类似（STA 距本 AP 近，降功率不断连）
MOCK_SCENE_JOINT = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "cwmin": 3, "cwmax": 7, "aifsn": 1,
        "channel_busy_ratio": 0.82,
        "tx_retries_ratio": 0.31,
        "neighbor_rssi_dbm": {"ap2": -68.4, "ap3": -76.0},
        "sta_rssi_dbm": -45.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap2": {
        "tx_power_dbm": 20.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.55,
        "tx_retries_ratio": 0.12,
        "neighbor_rssi_dbm": {"ap1": -68.6, "ap3": -69.2},
        "sta_rssi_dbm": -48.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps": 28.7,
        "latency_ms": 185.0,
        "packet_loss_pct": 0.4,
    },
    "ap3": {
        "tx_power_dbm": 20.0,
        "cwmin": 15, "cwmax": 63, "aifsn": 4,
        "channel_busy_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -69.2},
        "sta_rssi_dbm": -50.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}

MOCK_SCENES = {
    "sr":    MOCK_SCENE_SR,
    "edca":  MOCK_SCENE_EDCA,
    "joint": MOCK_SCENE_JOINT,
}


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
    parser.add_argument("model", nargs="?", default="qwen3:14b",
                        help="Ollama 模型名（默认 qwen3:14b）")
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
    if args.mock:
        ap_state = MOCK_SCENES[args.scene]
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

    scene = args.scene if args.mock else "live"
    logger = SessionLogger(verbose=False)
    logger.session_start(model=args.model, scene=scene, ap_state=ap_state)

    if not args.no_dashboard:
        start_dashboard(logger.log_path, port=args.dashboard_port)

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
    try:
        orchestrator.run(ap_state)
    except Exception as e:
        logger.session_end(outcome="error", total_rounds=0)
        raise


if __name__ == "__main__":
    main()

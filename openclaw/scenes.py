"""
Mock 场景定义 + 配套服务启动器（纯 OpenClaw 架构共享层）。

从原 run.py 迁移而来（逻辑零改写）：三套预设 mock 场景（Co-SR / Co-EDCA / 联合）、
状态服务器 / Dashboard / 学术曲线 的启动器、执行端点解析。
被 run_openclaw.py 与 tests/test_openclaw_migration.py 复用。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_AP_CONFIG = REPO_ROOT / "ap_endpoints.json"


# ------------------------------------------------------------------
# 配套服务启动器
# ------------------------------------------------------------------

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
    plot_script = REPO_ROOT / "state_server" / "academic_plot.py"
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

    返回 (是否就绪, 本程序启动的进程或 None)。复用已在线的服务器时进程为 None。
    """
    if not _is_local_server(server_url):
        return False, None
    if _server_alive(server_url):
        # 复用已在线的服务器（可能未带 --allow-mock，喂数会被拒，曲线为空）
        print("[Mock] 检测到 5001 已有服务器，直接复用。"
              "若曲线仍无数据，请确认它是以 --allow-mock 启动的。")
        return True, None

    server_script = REPO_ROOT / "state_server" / "server.py"
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
        "service_name": "generic_data",
        "business_type": "后台下载",
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
        "service_name": "generic_data",
        "business_type": "直播",
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
        "service_name": "generic_data",
        "business_type": "后台下载",
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

# 场景二：Co-EDCA（三 AP 当前 EDCA 参数未差异化，需协商）
# 注意：这些业务类型是 mock 场景输入，不是 AP 编号的固定身份。
# 邻居 RSSI 弱，不触发 Co-SR；AP2 承载直播，应获得更高 EDCA 优先级；
# AP1/AP3 为后台下载，应降低竞争优先级，把信道机会让给 AP2。
MOCK_SCENE_EDCA = {
    "ap1": {
        "service_name": "background_download",
        "business_type": "后台下载",
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "low",
        "Data_rate_to_bandwidth_ratio": 0.42,
        "tx_retries_ratio": 0.06,
        "neighbor_rssi_dbm": {"ap2": -85.0, "ap3": -88.0},
        "sta_rssi_dbm": -55.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps_iperf": 30.2,
        "latency_ms": 130.0,
        "packet_loss_pct": 0.2,
    },
    "ap2": {
        "service_name": "live_streaming",
        "business_type": "直播",
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": "high",
        "Data_rate_to_bandwidth_ratio": 0.72,
        "tx_retries_ratio": 0.18,
        "neighbor_rssi_dbm": {"ap1": -85.0, "ap3": -87.0},
        "sta_rssi_dbm": -61.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps_iperf": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap3": {
        "service_name": "background_download",
        "business_type": "后台下载",
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
# 本场景同时给出业务优先级差异，用于观察 Co-SR 与 Co-EDCA 是否需要联合处理。
# neighbor_rssi 与场景一类似（STA 距本 AP 近，降功率后不会断连）
MOCK_SCENE_JOINT = {
    "ap1": {
        "service_name": "interactive_video",
        "business_type": "后台下载",
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
        "service_name": "best_effort_data",
        "business_type": "直播",
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
        "service_name": "background_transfer",
        "business_type": "后台下载",
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

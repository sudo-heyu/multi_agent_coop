"""
配套服务启动器 + 场景标签（纯 OpenClaw 架构共享层）。

包含 Dashboard / 学术曲线启动器、执行端点解析和 --scene 标签定义。
mock 场景数据已降级为测试夹具，见 tests/mock_scenes.py。
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

DEFAULT_AP_CONFIG = REPO_ROOT / "config" / "ap_endpoints.json"

# 场景标签（--scene）：仅用于日志与记忆归组；mock 场景数据本体已降级为
# 测试夹具（tests/mock_scenes.py），运行时状态一律来自 real/ns3 上报。
SCENE_NAMES = ("sr", "edca", "joint", "contention", "hidden_sla")


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

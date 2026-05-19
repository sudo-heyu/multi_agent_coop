"""
Agent skill：从状态服务器获取 AP 全局状态

返回值与 run.py 中 AP_STATE 格式完全兼容，可直接传入 orchestrator.run()。
"""
import requests

DEFAULT_SERVER = "http://localhost:5001"
VALID_AP_IDS = ["ap1", "ap2", "ap3"]


class StateStaleError(Exception):
    """AP 数据缺失或超过新鲜度阈值时抛出。"""


def get_all_states(server_url: str = DEFAULT_SERVER) -> dict:
    """
    获取所有 AP 的最新状态，校验新鲜度。

    Returns:
        {"ap1": {指标字典}, "ap2": {...}, "ap3": {...}}
        格式与 AP_STATE mock 数据完全兼容。

    Raises:
        ConnectionError: 服务器不可达
        StateStaleError: 任意 AP 数据缺失或已过期
    """
    try:
        resp = requests.get(f"{server_url}/state", timeout=5)
        resp.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(f"无法连接到状态服务器 {server_url}，请确认服务已启动")
    except requests.RequestException as e:
        raise ConnectionError(f"状态服务器请求失败: {e}")

    raw = resp.json()

    stale_aps = []
    for ap_id in VALID_AP_IDS:
        entry = raw.get(ap_id, {})
        if entry.get("stale") or entry.get("data") is None:
            stale_aps.append(ap_id)

    if stale_aps:
        raise StateStaleError(
            f"以下 AP 数据缺失或已过期，请检查上报脚本是否正在运行: {stale_aps}"
        )

    return {ap_id: raw[ap_id]["data"] for ap_id in VALID_AP_IDS}


def get_state(ap_id: str, server_url: str = DEFAULT_SERVER) -> dict:
    """
    获取单个 AP 的最新状态。

    Raises:
        ConnectionError: 服务器不可达
        StateStaleError: 该 AP 数据缺失或已过期
    """
    try:
        resp = requests.get(f"{server_url}/state/{ap_id}", timeout=5)
        resp.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(f"无法连接到状态服务器 {server_url}")
    except requests.RequestException as e:
        raise ConnectionError(f"请求失败: {e}")

    entry = resp.json()
    if entry.get("stale") or entry.get("data") is None:
        raise StateStaleError(f"{ap_id} 数据缺失或已过期")

    return entry["data"]

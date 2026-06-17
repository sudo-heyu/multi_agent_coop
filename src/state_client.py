"""
State server client for AP runtime metrics.

The returned data is compatible with the AP_STATE mock shape used by run.py.
"""
import requests

DEFAULT_SERVER = "http://localhost:5001"
VALID_AP_IDS = ["ap1", "ap2", "ap3"]
_SESSION = requests.Session()
_SESSION.trust_env = False


class StateStaleError(Exception):
    """Raised when AP data is missing or older than the freshness threshold."""


def get_all_states(server_url: str = DEFAULT_SERVER) -> dict:
    """
    Fetch the latest state for all APs and validate freshness.

    Returns:
        {"ap1": {metrics}, "ap2": {...}, "ap3": {...}}

    Raises:
        ConnectionError: The state server is unreachable or returned an error.
        StateStaleError: Any AP data is missing or stale.
    """
    try:
        resp = _SESSION.get(f"{server_url}/state", timeout=5)
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
    Fetch the latest state for one AP.

    Raises:
        ConnectionError: The state server is unreachable or returned an error.
        StateStaleError: The AP data is missing or stale.
    """
    try:
        resp = _SESSION.get(f"{server_url}/state/{ap_id}", timeout=5)
        resp.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError(f"无法连接到状态服务器 {server_url}")
    except requests.RequestException as e:
        raise ConnectionError(f"请求失败: {e}")

    entry = resp.json()
    if entry.get("stale") or entry.get("data") is None:
        raise StateStaleError(f"{ap_id} 数据缺失或已过期")

    return entry["data"]

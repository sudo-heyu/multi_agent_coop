"""
AP 状态上报脚本

mock 模式（本地测试）：使用场景二硬编码数据，定期 POST 到状态服务器
真实模式（香蕉派）：从系统命令读取实际指标，定期 POST

用法：
  # mock 模式：模拟全部三个 AP
  python state_server/reporter.py --mock --all

  # mock 模式：只模拟 ap1
  python state_server/reporter.py --mock --ap-id ap1

  # 真实模式（香蕉派上运行，指定自己的 ap_id）
  python state_server/reporter.py --ap-id ap1 --server http://192.168.1.100:5001
"""
import argparse
import copy
import random
import subprocess
import threading
import time
from datetime import datetime, timezone

import requests

DEFAULT_SERVER = "http://localhost:5001"
DEFAULT_INTERVAL = 10  # 秒

# ------------------------------------------------------------------
# 业务优先级配置（按 AP 写死；硬件读不到此信息，由部署人员配置）
# high  = 语音/视频等时延敏感业务 → 协商时调低 EDCA
# medium= 通用数据业务（默认）
# low   = 后台下载/批量传输 → 协商时调高 EDCA
# ------------------------------------------------------------------
AP_TRAFFIC_PRIORITY = {
    "ap1": "low",
    "ap2": "high",
    "ap3": "low",
}

AP_BUSINESS_TYPE = {
    "ap1": "后台下载",
    "ap2": "直播",
    "ap3": "后台下载",
}

# ------------------------------------------------------------------
# 场景二 mock 数据
#
# cwmin/cwmax 与真实硬件上报一致，使用【指数 n】（实际竞争窗口 CW = 2^n - 1）：
# cwmin=3 / cwmax=4 表示实际 CW 7 / 15。协商侧（apply_profile）会解码为实际 CW 值。
# ------------------------------------------------------------------
MOCK_STATE = {
    "ap1": {
        "service_name": "background_download",
        "business_type": AP_BUSINESS_TYPE["ap1"],
        "tx_power_dbm": 16.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": AP_TRAFFIC_PRIORITY["ap1"],
        "Data_rate_to_bandwidth_ratio": 0.42,
        "tx_retries_ratio": 0.06,
        "neighbor_rssi_dbm": {"ap2": -68.0, "ap3": -75.0},
        "sta_rssi_dbm": -55.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps_iperf": 30.2,
        "latency_ms": 130.0,
        "packet_loss_pct": 0.2,
    },
    "ap2": {
        "service_name": "live_streaming",
        "business_type": AP_BUSINESS_TYPE["ap2"],
        "tx_power_dbm": 16.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": AP_TRAFFIC_PRIORITY["ap2"],
        "Data_rate_to_bandwidth_ratio": 0.72,
        "tx_retries_ratio": 0.18,
        "neighbor_rssi_dbm": {"ap1": -68.0, "ap3": -71.0},
        "sta_rssi_dbm": -61.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps_iperf": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap3": {
        "service_name": "background_download",
        "business_type": AP_BUSINESS_TYPE["ap3"],
        "tx_power_dbm": 16.0,
        "cwmin": 3, "cwmax": 4, "aifsn": 2,
        "traffic_priority": AP_TRAFFIC_PRIORITY["ap3"],
        "Data_rate_to_bandwidth_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -75.0, "ap2": -71.0},
        "sta_rssi_dbm": -58.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps_iperf": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}


# 业务优先级 → 用户流量接入类别（AC）：high=语音 VO，medium=尽力而为 BE，low=后台 BK
_USER_AC_BY_PRIORITY = {"high": "VO", "medium": "BE", "low": "BK"}

# 为 mock 数据补齐新增的只读观测字段（iperf/user 双路吞吐 + AC 类型）
for _ap_id, _state in MOCK_STATE.items():
    _state.setdefault("business_type", AP_BUSINESS_TYPE.get(_ap_id, "未声明业务类型"))
    _state.setdefault("throughput_mbps_user", round(_state["throughput_mbps_iperf"] * 0.6, 1))
    _state.setdefault("ac_iperf", "BK")  # iperf 测试流走后台队列
    _state.setdefault(
        "ac_user", _USER_AC_BY_PRIORITY.get(_state.get("traffic_priority", "medium"), "BE")
    )


# ------------------------------------------------------------------
# mock 扰动：对测量指标加小量随机噪声，配置参数保持不变
# ------------------------------------------------------------------
def _perturb(ap_id: str) -> dict:
    """返回带随机扰动的 mock 数据副本（每次调用产生新值）。"""
    d = copy.deepcopy(MOCK_STATE[ap_id])

    def jitter(val: float, sigma: float, lo: float, hi: float) -> float:
        return round(max(lo, min(hi, val + random.gauss(0, sigma))), 2)

    # 测量指标：加噪声
    d["Data_rate_to_bandwidth_ratio"] = jitter(d["Data_rate_to_bandwidth_ratio"], 0.03, 0.0, 1.0)
    d["tx_retries_ratio"]   = jitter(d["tx_retries_ratio"],   0.02, 0.0, 1.0)
    d["sta_rssi_dbm"]       = jitter(d["sta_rssi_dbm"],       1.5, -90.0, -20.0)
    d["noise_floor_dbm"]    = jitter(d["noise_floor_dbm"],    0.5, -110.0, -70.0)
    d["throughput_mbps_iperf"]    = jitter(d["throughput_mbps_iperf"],    1.5,  0.0, 300.0)
    d["throughput_mbps_user"]     = jitter(d["throughput_mbps_user"],     1.5,  0.0, 300.0)
    d["latency_ms"]         = jitter(d["latency_ms"],         15.0, 1.0, 2000.0)
    d["packet_loss_pct"]    = jitter(d["packet_loss_pct"],    0.15, 0.0, 100.0)
    d["neighbor_rssi_dbm"]  = {
        k: jitter(v, 1.5, -100.0, -20.0)
        for k, v in d["neighbor_rssi_dbm"].items()
    }
    # 配置参数（tx_power_dbm / cwmin / cwmax / aifsn）不扰动，由协商决策修改

    return d


# ------------------------------------------------------------------
# 真实模式：从系统命令读取指标（香蕉派上使用）
# ------------------------------------------------------------------
def _run(cmd: str) -> str:
    """运行 shell 命令，返回 stdout，失败返回空字符串。"""
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=5)
    except Exception:
        return ""


def read_real_state(ap_id: str, iface: str = "wlan0") -> dict:  # noqa: ARG001
    """
    从系统命令读取真实指标。
    各字段来源：
      tx_power_dbm      : iw dev <iface> info | grep txpower
      Data_rate_to_bandwidth_ratio: iw dev <iface> survey dump (busy_time / active_time)
      tx_retries_ratio  : iw dev <iface> station dump (tx_retries / tx_packets)
      neighbor_rssi_dbm : iw dev <iface> scan (邻居 BSS signal，按 BSSID 聚合)
      sta_rssi_dbm      : iw dev <iface> station dump (关联 STA signal)
      noise_floor_dbm   : iw dev <iface> survey dump (noise)
      cwmin/cwmax/aifsn : iw dev <iface> get txq 或 /sys/kernel/debug/ieee80211/
                          （cwmin/cwmax 按硬件原生格式上报【指数 n】，不在此转换；
                           协商侧 apply_profile 会统一解码为实际 CW 值）
      throughput_mbps_iperf   : iperf3 客户端测量（需提前启动 iperf3 服务端）
      latency_ms        : ping -c 4 <网关> | tail -1 | awk '{print $4}' | cut -d/ -f2
      packet_loss_pct   : ping -c 20 <网关> | grep loss | awk '{print $6}'
      traffic_priority  : 写死配置（AP_TRAFFIC_PRIORITY），硬件无法读取
    """
    # TODO: 在香蕉派上实现各字段的实际读取逻辑（第八步）
    raise NotImplementedError("真实模式尚未实现，请使用 --mock 进行测试")


# ------------------------------------------------------------------
# 上报逻辑
# ------------------------------------------------------------------
def post_state(ap_id: str, data: dict, server: str, source: str = "ap") -> bool:
    data = dict(data)
    data.setdefault("business_type", AP_BUSINESS_TYPE.get(ap_id, "未声明业务类型"))
    payload = {
        "ap_id": ap_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        **data,
    }
    try:
        resp = requests.post(f"{server}/state", json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[{ap_id}] 上报成功 @ {payload['timestamp']}")
            return True
        print(f"[{ap_id}] 上报失败: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        print(f"[{ap_id}] 连接失败: {e}")
    return False


def report_loop(ap_id: str, mock: bool, server: str, interval: int, iface: str = "wlan0"):
    source = "mock" if mock else "ap"
    while True:
        data = _perturb(ap_id) if mock else read_real_state(ap_id, iface)
        post_state(ap_id, data, server, source=source)
        time.sleep(interval)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="AP 状态上报脚本")
    parser.add_argument("--ap-id", choices=["ap1", "ap2", "ap3"],
                        help="上报的 AP 编号")
    parser.add_argument("--all", action="store_true",
                        help="mock 模式下同时模拟全部三个 AP（多线程）")
    parser.add_argument("--mock", action="store_true",
                        help="使用 mock 数据而非真实采集")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"状态服务器地址（默认 {DEFAULT_SERVER}）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"上报间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--iface", default="wlan0",
                        help="无线接口名称（真实模式，默认 wlan0）")
    args = parser.parse_args()

    if args.all and not args.mock:
        parser.error("--all 仅支持 --mock 模式")
    if not args.all and not args.ap_id:
        parser.error("请指定 --ap-id 或使用 --mock --all")

    if args.all:
        threads = []
        for ap_id in ["ap1", "ap2", "ap3"]:
            t = threading.Thread(
                target=report_loop,
                args=(ap_id, True, args.server, args.interval),
                daemon=True,
            )
            t.start()
            threads.append(t)
        print(f"模拟三台 AP 上报中，间隔 {args.interval}s，服务器 {args.server}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n停止上报。")
    else:
        print(f"[{args.ap_id}] 开始上报，间隔 {args.interval}s，服务器 {args.server}")
        try:
            report_loop(args.ap_id, args.mock, args.server, args.interval, args.iface)
        except KeyboardInterrupt:
            print("\n停止上报。")


if __name__ == "__main__":
    main()

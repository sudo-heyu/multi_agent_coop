"""
AP 状态上报脚本

真实模式（香蕉派）：从系统命令读取实际指标，定期 POST

用法：
  # 真实模式（香蕉派上运行，指定自己的 ap_id）
  python state_server/reporter.py --ap-id ap1 --server http://192.168.1.100:5001
"""
import argparse
import subprocess
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
    # TODO: 在香蕉派上实现各字段的实际读取逻辑。
    raise NotImplementedError("真实 AP 采集尚未实现，请接入硬件采集命令后再运行 reporter")


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


def report_loop(ap_id: str, server: str, interval: int, iface: str = "wlan0"):
    while True:
        data = read_real_state(ap_id, iface)
        post_state(ap_id, data, server, source="ap")
        time.sleep(interval)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="AP 状态上报脚本")
    parser.add_argument("--ap-id", choices=["ap1", "ap2", "ap3"],
                        help="上报的 AP 编号")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"状态服务器地址（默认 {DEFAULT_SERVER}）")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"上报间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--iface", default="wlan0",
                        help="无线接口名称（真实模式，默认 wlan0）")
    args = parser.parse_args()

    if not args.ap_id:
        parser.error("请指定 --ap-id")

    print(f"[{args.ap_id}] 开始真实 AP 上报，间隔 {args.interval}s，服务器 {args.server}")
    try:
        report_loop(args.ap_id, args.server, args.interval, args.iface)
    except KeyboardInterrupt:
        print("\n停止上报。")


if __name__ == "__main__":
    main()

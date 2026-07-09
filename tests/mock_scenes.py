"""Mock 场景与观测字段补齐 —— 纯测试夹具。

自 mock 运行时模式移除后（运行时仅保留 real / ns3），本模块只被测试套件
引用：为确定性单元测试提供固定初始状态。运行时只使用 sr/edca；其它键仅为
单元测试 fixture，不会出现在 openclaw.scenes.SCENE_NAMES。
"""
from __future__ import annotations


# ------------------------------------------------------------------
# Mock 数据：运行时只暴露 sr/edca；额外 fixture 仅供单元测试覆盖冲突/SLA 行为。
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

# 场景四：两个同优先级实时业务争抢同一竞争机会。私有 SLA 不进入 agent_view，
# 仅在对应 AP 的提案/投票回合注入；性能模型会对多个 AP 同时激进施加碰撞惩罚。
MOCK_SCENE_CONTENTION = {
    "ap1": {**MOCK_SCENE_EDCA["ap2"], "service_name": "video_call_a",
            "business_type": "视频会议", "traffic_priority": "high",
            "throughput_mbps_iperf": 16.0, "latency_ms": 95.0,
            "private_sla": {"min_throughput_mbps": 15.0, "max_latency_ms": 80.0}},
    "ap2": {**MOCK_SCENE_EDCA["ap2"], "service_name": "video_call_b",
            "business_type": "视频会议", "traffic_priority": "high",
            "throughput_mbps_iperf": 16.0, "latency_ms": 95.0,
            "private_sla": {"min_throughput_mbps": 15.0, "max_latency_ms": 80.0}},
    "ap3": {**MOCK_SCENE_EDCA["ap3"], "service_name": "best_effort",
            "traffic_priority": "low",
            "private_sla": {"min_throughput_mbps": 12.0, "max_latency_ms": 220.0}},
}

# 场景五：AP3 表面为低优先级后台业务，但有未公开的完成期限所对应的吞吐底线。
MOCK_SCENE_HIDDEN_SLA = {
    "ap1": {**MOCK_SCENE_EDCA["ap2"], "service_name": "interactive_video",
            "private_sla": {"min_throughput_mbps": 14.0, "max_latency_ms": 100.0}},
    "ap2": {**MOCK_SCENE_EDCA["ap1"], "service_name": "best_effort",
            "private_sla": {"min_throughput_mbps": 10.0, "max_latency_ms": 250.0}},
    "ap3": {**MOCK_SCENE_EDCA["ap3"], "service_name": "deadline_backup",
            "business_type": "限时备份", "traffic_priority": "low",
            "private_sla": {"min_throughput_mbps": 18.0, "max_latency_ms": 180.0,
                            "deadline_minutes": 20}},
}

MOCK_SCENES = {
    "sr":    MOCK_SCENE_SR,
    "edca":  MOCK_SCENE_EDCA,
    "contention": MOCK_SCENE_CONTENTION,
    "hidden_sla": MOCK_SCENE_HIDDEN_SLA,
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

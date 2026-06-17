"""
真实硬件实验：业务画像 + 协商字段白名单。

本模块集中定义两件事，供协商流程在状态进入前统一处理：

  1. 各 AP 的【预设业务画像】——业务类型与优先级硬编码，不读取上报数据：
       AP1 = 抖音视频（高优先级，时延敏感）
       AP2 = 下载游戏（低优先级，吞吐敏感的后台传输）
       AP3 = 下载游戏（低优先级，吞吐敏感的后台传输）

  2. 协商实际使用的【字段白名单】——只保留实验关心的参数，其余上报字段忽略。
       AGENT_VISIBLE_FIELDS：展示给 agent、参与决策推理的字段。
       INTERNAL_FIELDS：仅供工具内部计算、不展示给 agent 的字段
                        （Co-SR 的 SINR 求解需要 noise_floor_dbm）。

设计目标：所有状态数据进入协商流程前，统一经 apply_profile() 处理，
确保下游（广播 / 提案 / 工具 / 验证器）只看到这些字段，
且 traffic_priority 一律来自预设而非上报。

此外，AP 上报的 cwmin/cwmax 是指数 n（CW = 2^n - 1），apply_profile 是所有状态
进入协商流程的唯一收敛点，故在此统一解码为实际 CW 值，下游一律按实际 CW 推理。
"""

from .tools.edca import decode_state_edca

# ── 预设业务画像（硬编码，不依赖上报数据）────────────────────────────────────
BUSINESS_PROFILE: dict[str, dict[str, str]] = {
    "ap1": {"service_name": "抖音视频", "traffic_priority": "high"},
    "ap2": {"service_name": "下载游戏", "traffic_priority": "low"},
    "ap3": {"service_name": "下载游戏", "traffic_priority": "low"},
}

# ── 协商对 agent 可见的字段（实验关心的 8 项）────────────────────────────────
AGENT_VISIBLE_FIELDS: tuple[str, ...] = (
    "service_name",          # 预设业务类型（抖音视频 / 下载游戏）
    "traffic_priority",      # 预设业务优先级（high / low，驱动 Co-EDCA）
    "tx_power_dbm",          # 发射功率（Co-SR 可调）
    "cwmin",                 # EDCA 竞争窗口下限（实际 CW 值，由上报指数解码而来）
    "cwmax",                 # EDCA 竞争窗口上限（实际 CW 值，由上报指数解码而来）
    "aifsn",                 # EDCA 仲裁帧间间隔数（Co-EDCA 可调）
    "sta_rssi_dbm",          # 己方 STA 信号强度（降功率安全下界）
    "throughput_mbps_user",  # 用户实际业务吞吐
    "neighbor_rssi_dbm",     # 邻居 AP 信号强度（Co-SR 干扰感知）
)

# ── 仅供工具内部计算、不展示给 agent 的字段 ──────────────────────────────────
# Co-SR 的 SINR 约束需要本底噪声，但它不进入 agent 的推理视野。
INTERNAL_FIELDS: tuple[str, ...] = (
    "noise_floor_dbm",
)

# 协商流程保留的全部字段（白名单之外的上报字段一律忽略）
RETAINED_FIELDS: tuple[str, ...] = AGENT_VISIBLE_FIELDS + INTERNAL_FIELDS


def apply_profile(ap_states: dict) -> dict:
    """对原始上报状态应用业务画像与字段白名单。

    - 只保留 RETAINED_FIELDS 中的字段，其余上报数据忽略。
    - service_name / traffic_priority 一律覆盖为 BUSINESS_PROFILE 的预设值，
      不读取上报值。

    返回新字典，不修改入参。
    """
    result: dict = {}
    for ap_id, state in ap_states.items():
        if not isinstance(state, dict):
            result[ap_id] = state
            continue
        filtered = {k: state[k] for k in RETAINED_FIELDS if k in state}
        filtered = decode_state_edca(filtered)  # cwmin/cwmax: 上报指数 → 实际 CW
        preset = BUSINESS_PROFILE.get(ap_id.lower())
        if preset:
            filtered["service_name"] = preset["service_name"]
            filtered["traffic_priority"] = preset["traffic_priority"]
        result[ap_id] = filtered
    return result


def strip_internal(state: dict) -> dict:
    """剥离单个 AP 状态中仅供内部计算的字段，返回展示给 agent 的视图。"""
    if not isinstance(state, dict):
        return state
    return {k: v for k, v in state.items() if k not in INTERNAL_FIELDS}


def agent_view(ap_states: dict) -> dict:
    """对全网状态剥离 INTERNAL_FIELDS，返回展示给 agent 的状态视图。"""
    return {ap_id: strip_internal(state) for ap_id, state in ap_states.items()}

"""
协商状态规范化：字段白名单 + EDCA 取值解码。

本模块不再给 AP 预设固定业务身份，也不覆盖上报的业务优先级。AP 的
service_name / traffic_priority 来自状态服务器、mock 场景或真实上报；缺失时使用
中性默认值，避免提示词和流程为了固定演示场景过拟合。

保留的职责：
  1. 只保留协商需要的字段，忽略白名单之外的上报数据。
  2. 把 AP 上报的 cwmin/cwmax 指数 n 统一解码为实际 CW 值（CW = 2^n - 1）。
  3. 对缺失或非法的业务字段做保守规范化：未知业务、medium 优先级。

Co-SR / Co-EDCA 只是当前工具支持的两类调参能力。是否使用它们，应由实时状态
中的干扰、EDCA 参数、业务优先级和 QoS 指标共同决定，而不是由 AP 编号决定。
"""

from .tools.edca import decode_state_edca

VALID_TRAFFIC_PRIORITIES: tuple[str, ...] = ("high", "medium", "low")
DEFAULT_SERVICE_NAME = "未声明业务"
DEFAULT_BUSINESS_TYPE = "未声明业务类型"
DEFAULT_TRAFFIC_PRIORITY = "medium"

# ── 协商对 agent 可见的字段 ────────────────────────────────────────────────
AGENT_VISIBLE_FIELDS: tuple[str, ...] = (
    "service_name",          # 上报/场景声明的业务类型；缺省为未声明业务
    "business_type",         # 面向业务语义的类型标签；缺省为未声明业务类型
    "traffic_priority",      # 上报/场景声明的业务优先级；缺省为 medium
    "tx_power_dbm",          # 发射功率（Co-SR 可调）
    "cwmin",                 # EDCA 竞争窗口下限（实际 CW 值，由上报指数解码而来）
    "cwmax",                 # EDCA 竞争窗口上限（实际 CW 值，由上报指数解码而来）
    "aifsn",                 # EDCA 仲裁帧间间隔数（Co-EDCA 可调）
    "be_cwmin",              # AC_BE EDCA 竞争窗口下限（实际 CW 值）
    "be_cwmax",              # AC_BE EDCA 竞争窗口上限（实际 CW 值）
    "be_aifsn",              # AC_BE AIFSN
    "vi_cwmin",              # AC_VI EDCA 竞争窗口下限（实际 CW 值）
    "vi_cwmax",              # AC_VI EDCA 竞争窗口上限（实际 CW 值）
    "vi_aifsn",              # AC_VI AIFSN
    "sta_rssi_dbm",          # 己方 STA 信号强度（降功率安全下界）
    "throughput_mbps_user",  # 用户实际业务吞吐
    "neighbor_rssi_dbm",     # 邻居 AP 信号强度（Co-SR 干扰感知）
)

# ── 仅供工具内部计算、不展示给 agent 的字段 ──────────────────────────────────
# Co-SR 的 SINR 约束需要本底噪声，但它不进入 agent 的推理视野。
INTERNAL_FIELDS: tuple[str, ...] = (
    "noise_floor_dbm",
    "obss_pd_dbm",
    "bss_color",
    "sr_reset_count",
)

# 协商流程保留的全部字段（白名单之外的上报字段一律忽略）
RETAINED_FIELDS: tuple[str, ...] = AGENT_VISIBLE_FIELDS + INTERNAL_FIELDS


def apply_profile(ap_states: dict) -> dict:
    """对原始上报状态应用字段白名单和保守默认值。

    - 只保留 RETAINED_FIELDS 中的字段，其余上报数据忽略。
    - service_name / traffic_priority 来自输入状态，不按 AP 编号覆盖。
    - 缺失或非法 priority 统一视为 medium，使 EDCA 推理保持可用但不强行制造差异。

    返回新字典，不修改入参。
    """
    result: dict = {}
    for ap_id, state in ap_states.items():
        if not isinstance(state, dict):
            result[ap_id] = state
            continue
        filtered = {k: state[k] for k in RETAINED_FIELDS if k in state}
        filtered = decode_state_edca(filtered)  # cwmin/cwmax: 上报指数 → 实际 CW
        service_name = filtered.get("service_name") or DEFAULT_SERVICE_NAME
        filtered["service_name"] = str(service_name)
        business_type = filtered.get("business_type") or DEFAULT_BUSINESS_TYPE
        filtered["business_type"] = str(business_type)
        priority = str(filtered.get("traffic_priority") or DEFAULT_TRAFFIC_PRIORITY).lower()
        if priority not in VALID_TRAFFIC_PRIORITIES:
            priority = DEFAULT_TRAFFIC_PRIORITY
        filtered["traffic_priority"] = priority
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

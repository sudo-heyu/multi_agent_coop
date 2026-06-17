"""
Co-EDCA 计算工具

【优先级与 EDCA 参数的调整规则】
  traffic_priority 来自当前状态上报或场景输入，不是 AP 的固定身份。
  当业务优先级或 QoS 目标存在差异时，可用 EDCA 参数表达竞争差异。

  high：通常使用更小的 CWmin、CWmax、AIFSN。
    小 CWmin → 退避窗口短 → 更快完成退避 → 更早抢占信道 → 降低时延。
    小 AIFSN → AIFS 间隔短 → 更早开始退避竞争。

  low：通常使用更大的 CWmin、CWmax、AIFSN。
    大 CWmin / AIFSN → 退避更长 → 降低自身竞争强度。

  medium：参数通常介于 high 与 low 之间。

【跨 AP 排序约束（硬性规则）】
  提案中不同优先级 AP 的参数必须满足单调性：
    high.CWmin ≤ medium.CWmin ≤ low.CWmin
    high.AIFSN ≤ medium.AIFSN ≤ low.AIFSN
  同优先级的多个 AP 之间无顺序约束。

本模块仅提供机械校验（范围合规 + 优先级排序）；
EDCA 参数的具体取值由 LLM agent 根据实时状态自行推理决定。若所有 AP
优先级相同或缺省为 medium，不应为了形成差异化方案强行制造梯度。
"""

import math

VALID_PRIORITIES: tuple[str, ...] = ("high", "medium", "low")

_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# 参数合法范围（IEEE 802.11 标准）
_LIMITS = {
    "CWmin": (3, 1023),
    "CWmax": (7, 1023),
    "AIFSN": (1, 15),
}


# ── 竞争窗口的指数表示 ⇄ 实际值 ──────────────────────────────────────────────
# 硬件（hostapd / iw）以指数 n 表示竞争窗口，实际竞争窗口 CW = 2^n - 1：
#     n :  0   1   2   3    4    5   ...   10
#    CW :  0   1   3   7   15   31   ...  1023
# AP 上报的 cwmin/cwmax 是指数 n；协商内部统一使用实际 CW 值推理（范围 [3,1023]）；
# 下发硬件时再转回指数。AIFSN 本身是直接计数值，不参与此转换。

def ecw_to_cw(n: int) -> int:
    """指数 n → 实际竞争窗口值 CW = 2^n - 1。"""
    return (1 << int(n)) - 1


def cw_to_ecw(cw: int) -> int:
    """实际竞争窗口值 CW → 最接近的指数 n（硬件只能取 2^n - 1 的离散值）。"""
    return max(0, round(math.log2(int(cw) + 1)))


def decode_state_edca(state: dict) -> dict:
    """把单个 AP 状态里上报的 cwmin/cwmax 指数解码为实际 CW 值（返回新字典）。

    上报数据（来自 hostapd/iw）以指数 n 表示竞争窗口，本函数将其转换为协商内部
    使用的实际 CW 值。其余字段原样保留；非数值的 cwmin/cwmax 跳过。
    """
    if not isinstance(state, dict):
        return state
    out = dict(state)
    for key in ("cwmin", "cwmax"):
        val = out.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = ecw_to_cw(val)
    return out


def encode_params_edca(params: dict) -> dict:
    """把决策参数里的 CWmin/CWmax 实际 CW 值编码为下发硬件用的指数 n（返回新字典）。

    协商内部用实际 CW 值（15,127...）推理；下发到香蕉派 /apply 前在此统一转回指数
    （15→4, 127→7），香蕉派拿到指数后直接写 hostapd。其余字段（tx_power_dbm /
    AIFSN 等）原样保留；兼容大小写键名。返回新字典，不修改入参。
    """
    if not isinstance(params, dict):
        return params
    out = dict(params)
    for key in ("CWmin", "CWmax", "cwmin", "cwmax"):
        val = out.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = cw_to_ecw(val)
    return out


def get_traffic_priority(ap_state: dict) -> str:
    """
    从 AP 状态中读取业务优先级，缺省返回 'medium'。

    Returns:
        "high" | "medium" | "low"
    """
    p = ap_state.get("traffic_priority", "medium")
    return p if p in _PRIORITY_RANK else "medium"


def validate(params: dict) -> tuple[bool, list[str]]:
    """
    验证单个 AP 的 EDCA 参数是否在 IEEE 802.11 合法范围内。

    Args:
        params: 含 CWmin / CWmax / AIFSN 的字典

    Returns:
        (is_valid, errors)  —  errors 为空列表时 is_valid=True
    """
    errors: list[str] = []

    for key, (lo, hi) in _LIMITS.items():
        val = params.get(key)
        if val is None:
            errors.append(f"{key} 缺失")
        elif not (lo <= val <= hi):
            errors.append(f"{key}={val} 超出范围 [{lo}, {hi}]")

    cwmin = params.get("CWmin", 0)
    cwmax = params.get("CWmax", 0)
    if cwmax <= cwmin:
        errors.append(f"CWmax={cwmax} 必须大于 CWmin={cwmin}")

    return len(errors) == 0, errors


def evaluate_edca_effectiveness(ap_states: dict, proposed_edca: dict) -> dict:
    """
    校验提案 EDCA 参数的跨 AP 优先级排序是否满足规则。

    不同优先级的 AP 之间，CWmin 和 AIFSN 必须满足单调性：
      high.CWmin ≤ medium.CWmin ≤ low.CWmin
      high.AIFSN ≤ medium.AIFSN ≤ low.AIFSN
    同优先级 AP 之间无顺序约束。

    Args:
        ap_states:     各 AP 当前状态（须含 traffic_priority 字段）
        proposed_edca: 提案参数 {"ap1": {"CWmin": ..., "CWmax": ..., "AIFSN": ...}, ...}

    Returns:
        {
            "per_ap":            {ap_id: {traffic_priority, cwmin, aifsn}},
            "priority_ordering": {warnings, ok},
            "all_ok":            bool,
        }
    """
    per_ap: dict = {}
    ordering_entries: list[tuple[str, int, int, int]] = []  # (ap_id, rank, cwmin, aifsn)

    for ap_id, state in ap_states.items():
        key = ap_id.lower()
        params = proposed_edca.get(key) or proposed_edca.get(ap_id) or {}
        if not isinstance(params, dict) or not params:
            continue

        priority = get_traffic_priority(state)
        rank     = _PRIORITY_RANK.get(priority, 1)
        cwmin    = int(params.get("CWmin", 15))
        aifsn    = int(params.get("AIFSN", 3))

        ordering_entries.append((key, rank, cwmin, aifsn))
        per_ap[key] = {
            "traffic_priority": priority,
            "cwmin":            cwmin,
            "aifsn":            aifsn,
        }

    # 跨 AP 优先级排序检查
    ordering_warnings: list[str] = []
    if len(ordering_entries) >= 2:
        ordering_entries.sort(key=lambda x: x[1])  # 按优先级从高到低排
        for i in range(len(ordering_entries) - 1):
            ap_a, rank_a, cwmin_a, aifsn_a = ordering_entries[i]
            ap_b, rank_b, cwmin_b, aifsn_b = ordering_entries[i + 1]
            if rank_a < rank_b:  # a 优先级高于 b
                if cwmin_a > cwmin_b:
                    ordering_warnings.append(
                        f"{ap_a}（{_rank_name(rank_a)}优先级）CWmin={cwmin_a} "
                        f"> {ap_b}（{_rank_name(rank_b)}优先级）CWmin={cwmin_b}，"
                        f"违反规则：更高 priority 通常应使用不大于低 priority 的 CWmin"
                    )
                if aifsn_a > aifsn_b:
                    ordering_warnings.append(
                        f"{ap_a}（{_rank_name(rank_a)}优先级）AIFSN={aifsn_a} "
                        f"> {ap_b}（{_rank_name(rank_b)}优先级）AIFSN={aifsn_b}，"
                        f"违反规则：更高 priority 通常应使用不大于低 priority 的 AIFSN"
                    )

    all_ok = len(ordering_warnings) == 0

    return {
        "per_ap": per_ap,
        "priority_ordering": {
            "warnings": ordering_warnings,
            "ok":       len(ordering_warnings) == 0,
        },
        "all_ok": all_ok,
    }


def _rank_name(rank: int) -> str:
    return {0: "高", 1: "中", 2: "低"}.get(rank, "中")

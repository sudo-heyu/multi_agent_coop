"""
Co-EDCA 计算工具

【优先级与 EDCA 参数的调整规则】
  高优先级业务（high）：应调低 EDCA 参数——更小的 CWmin、CWmax、AIFSN。
    小 CWmin → 退避窗口短 → 更快完成退避 → 更早抢占信道 → 降低时延。
    小 AIFSN → AIFS 间隔短 → 比低优先级业务更早开始退避竞争。

  低优先级业务（low）：应调高 EDCA 参数——更大的 CWmin、CWmax、AIFSN。
    大 CWmin / AIFSN → 退避更长 → 主动让出信道竞争机会 → 保障高优先级 QoS。

  中优先级业务（medium）：参数介于高低优先级之间。

【跨 AP 排序约束（硬性规则）】
  提案中不同优先级 AP 的参数必须满足单调性：
    high.CWmin ≤ medium.CWmin ≤ low.CWmin
    high.AIFSN ≤ medium.AIFSN ≤ low.AIFSN
  同优先级的多个 AP 之间无顺序约束。

本模块仅提供机械校验（范围合规 + 优先级排序）；
EDCA 参数的具体取值由 LLM agent 根据上述规则自行推理决定。
"""

VALID_PRIORITIES: tuple[str, ...] = ("high", "medium", "low")

_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# 参数合法范围（IEEE 802.11 标准）
_LIMITS = {
    "CWmin": (3, 1023),
    "CWmax": (7, 1023),
    "AIFSN": (1, 15),
}


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
                        f"违反规则：高优先级业务应使用更小的 CWmin"
                    )
                if aifsn_a > aifsn_b:
                    ordering_warnings.append(
                        f"{ap_a}（{_rank_name(rank_a)}优先级）AIFSN={aifsn_a} "
                        f"> {ap_b}（{_rank_name(rank_b)}优先级）AIFSN={aifsn_b}，"
                        f"违反规则：高优先级业务应使用更小的 AIFSN"
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

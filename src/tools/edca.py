"""
Co-EDCA 计算工具

根据各 AP 的信道拥塞指标（Channel Busy Ratio + TX Retries Ratio）
判断拥塞等级，映射到推荐的 EDCA 参数组合（CWmin / CWmax / AIFSN），
并验证参数合法性。

拥塞等级定义（按优先级从高到低判断）：
  critical  busy ≥ 0.75 且 retries ≥ 0.25  → 严重拥塞，强制大幅退避
  high      busy ≥ 0.60 或 retries ≥ 0.15  → 重度拥塞，增大退避窗口
  medium    busy ≥ 0.40 或 retries ≥ 0.08  → 轻度拥塞，适度退避
  low       其余情况                         → 信道空闲，可用激进参数
"""

# ------------------------------------------------------------------
# 拥塞等级阈值（模块级常量，供外部导入以避免多处硬编码）
# ------------------------------------------------------------------
BUSY_HIGH_THRESHOLD  = 0.60   # high 等级触发阈值（信道占用率）
RETRY_HIGH_THRESHOLD = 0.15   # high 等级触发阈值（重传率）

# 判断顺序：critical → high → medium → low
_THRESHOLDS = [
    ("critical", lambda b, r: b >= 0.75 and r >= 0.25),
    ("high",     lambda b, r: b >= BUSY_HIGH_THRESHOLD or  r >= RETRY_HIGH_THRESHOLD),
    ("medium",   lambda b, r: b >= 0.40 or  r >= 0.08),
    ("low",      lambda b, r: True),
]

# ------------------------------------------------------------------
# 拥塞等级 → EDCA 参数映射
# 原则：拥塞越严重 → CW 越大、AIFSN 越大 → 退避越多 → 占用信道越少
# ------------------------------------------------------------------
_EDCA_MAP: dict[str, dict] = {
    "low":      {"CWmin":  7, "CWmax":  15, "AIFSN": 2},
    "medium":   {"CWmin":  7, "CWmax":  31, "AIFSN": 3},
    "high":     {"CWmin": 15, "CWmax":  63, "AIFSN": 3},
    "critical": {"CWmin": 15, "CWmax": 127, "AIFSN": 4},
}

# 参数合法范围（IEEE 802.11 标准）
_LIMITS = {
    "CWmin": (3, 1023),
    "CWmax": (7, 1023),
    "AIFSN": (1, 15),
}


# ------------------------------------------------------------------
# 公开函数
# ------------------------------------------------------------------

def classify_congestion(channel_busy_ratio: float, tx_retries_ratio: float) -> str:
    """
    根据信道占用率和重传率判断拥塞等级。

    Args:
        channel_busy_ratio: 信道繁忙时间占比，范围 [0, 1]
        tx_retries_ratio:   重传包占比，范围 [0, 1]

    Returns:
        "low" | "medium" | "high" | "critical"
    """
    for level, condition in _THRESHOLDS:
        if condition(channel_busy_ratio, tx_retries_ratio):
            return level
    return "low"


def recommend_edca(congestion_level: str) -> dict:
    """
    将拥塞等级映射为推荐的 EDCA 参数。

    Args:
        congestion_level: classify_congestion() 的返回值

    Returns:
        {"CWmin": int, "CWmax": int, "AIFSN": int}

    Raises:
        ValueError: 未知的拥塞等级
    """
    if congestion_level not in _EDCA_MAP:
        raise ValueError(f"未知拥塞等级: {congestion_level!r}，"
                         f"合法值: {list(_EDCA_MAP)}")
    return dict(_EDCA_MAP[congestion_level])


def validate(params: dict) -> tuple[bool, list[str]]:
    """
    验证 EDCA 参数是否在合法范围内。

    Args:
        params: 包含 CWmin / CWmax / AIFSN 的字典

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
    评估提案 EDCA 参数的合理性：拥塞匹配度、碰撞概率估算、跨 AP 公平性。

    Args:
        ap_states:     各 AP 当前状态（含 channel_busy_ratio / tx_retries_ratio）
        proposed_edca: 提案参数 {"ap1": {"CWmin": 15, "CWmax": 127, "AIFSN": 4}, ...}

    Returns:
        {
            "per_ap":  {ap_id: {warnings, ok, estimated_p_collision, ...}},
            "fairness": {aifsn_spread, warnings, ok},
            "all_ok":  bool,
        }
    """
    SLOT_US = 9    # 802.11ax 时隙 (μs)
    SIFS_US = 16   # SIFS (μs)
    # 每个 AP 假设 2 个 STA，估算全网竞争站点数
    n_competing = 2 * max(len(ap_states), 1)

    per_ap: dict = {}
    aifsn_vals: list[tuple[str, int]] = []

    for ap_id, state in ap_states.items():
        key = ap_id.lower()
        params = proposed_edca.get(key) or proposed_edca.get(ap_id) or {}
        if not isinstance(params, dict) or not params:
            continue

        busy    = float(state.get("channel_busy_ratio", 0.0))
        retries = float(state.get("tx_retries_ratio", 0.0))
        cwmin   = int(params.get("CWmin", 15))
        cwmax   = int(params.get("CWmax", 63))
        aifsn   = int(params.get("AIFSN", 3))

        rec_level = classify_congestion(busy, retries)
        rec       = recommend_edca(rec_level)

        # 碰撞概率简化估算：(n-1)/(CWmin+1)
        # 假设各站在 [0, CWmin] 均匀选退避槽，两站同槽的概率上界
        p_collision = min((n_competing - 1) / (cwmin + 1), 0.99)

        # 含碰撞放大的期望退避时间 (μs)
        avg_backoff_slots = (cwmin + 1) / 2
        expected_backoff_us = (
            avg_backoff_slots * SLOT_US / (1.0 - p_collision)
            if p_collision < 0.99
            else avg_backoff_slots * SLOT_US * 100
        )

        aifs_us = aifsn * SLOT_US + SIFS_US

        warnings: list[str] = []

        if cwmin < rec["CWmin"]:
            warnings.append(
                f"CWmin={cwmin} 比 {rec_level} 级推荐值 {rec['CWmin']} 更激进，"
                f"当前重传率 {retries*100:.0f}%，竞争更激烈可能使重传率进一步上升"
            )
        if aifsn < rec["AIFSN"]:
            warnings.append(
                f"AIFSN={aifsn} 比 {rec_level} 级推荐值 {rec['AIFSN']} 更激进，"
                f"信道占用 {busy*100:.0f}% 下提前接入会加剧碰撞"
            )
        if busy < 0.3 and cwmin > rec["CWmin"] * 2:
            warnings.append(
                f"信道负载低（busy={busy*100:.0f}%）但 CWmin={cwmin} 过大，"
                f"推荐值为 {rec['CWmin']}，过度退避会浪费信道容量"
            )

        aifsn_vals.append((key, aifsn))
        per_ap[key] = {
            "busy_ratio":            busy,
            "retry_ratio":           retries,
            "recommended_level":     rec_level,
            "recommended_params":    rec,
            "cwmin_delta_vs_rec":    cwmin - rec["CWmin"],   # 正=更保守, 负=更激进
            "aifsn_delta_vs_rec":    aifsn - rec["AIFSN"],
            "estimated_p_collision": round(p_collision, 4),
            "expected_backoff_us":   round(expected_backoff_us, 1),
            "aifs_us":               aifs_us,
            "warnings":              warnings,
            "ok":                    len(warnings) == 0,
        }

    # 跨 AP 公平性：AIFSN 差值过大时低 AIFSN 的 AP 系统性占优
    fairness_warnings: list[str] = []
    aifsn_spread = max_aifsn = min_aifsn = None

    if aifsn_vals:
        max_aifsn    = max(v for _, v in aifsn_vals)
        min_aifsn    = min(v for _, v in aifsn_vals)
        aifsn_spread = max_aifsn - min_aifsn

        if aifsn_spread >= 3:
            aggressive   = [ap for ap, v in aifsn_vals if v == min_aifsn]
            conservative = [ap for ap, v in aifsn_vals if v == max_aifsn]
            fairness_warnings.append(
                f"AIFSN 差值 {aifsn_spread}：{aggressive}（AIFSN={min_aifsn}）"
                f"vs {conservative}（AIFSN={max_aifsn}），"
                f"AIFSN 较小的 AP 在信道竞争中占据系统性优势"
            )

    all_ok = (
        all(v["ok"] for v in per_ap.values())
        and len(fairness_warnings) == 0
    )

    return {
        "per_ap": per_ap,
        "fairness": {
            "aifsn_spread": aifsn_spread,
            "max_aifsn":    max_aifsn,
            "min_aifsn":    min_aifsn,
            "warnings":     fairness_warnings,
            "ok":           len(fairness_warnings) == 0,
        },
        "all_ok": all_ok,
    }


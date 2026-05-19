"""
Co-EDCA 计算工具

根据各 AP 的信道拥塞指标（Channel Busy Ratio + TX Retries Ratio）
判断拥塞等级，映射到推荐的 EDCA 参数组合（CWmin / CWmax / AIFSN），
并验证参数合法性。

拥塞等级定义：
  low      busy < 0.40 且 retries < 0.08   → 信道空闲，可用激进参数
  medium   busy < 0.60 且 retries < 0.15   → 轻度拥塞，适度退避
  high     busy < 0.75 或 retries < 0.25   → 重度拥塞，增大退避窗口
  critical busy ≥ 0.75 且 retries ≥ 0.25  → 严重拥塞，强制大幅退避
"""

# ------------------------------------------------------------------
# 拥塞等级阈值
# ------------------------------------------------------------------
# 判断顺序：critical → high → medium → low
_THRESHOLDS = [
    ("critical", lambda b, r: b >= 0.75 and r >= 0.25),
    ("high",     lambda b, r: b >= 0.60 or  r >= 0.15),
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
    "critical": {"CWmin": 15, "CWmax":  63, "AIFSN": 4},
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


def compute_all(ap_states: dict) -> dict:
    """
    主入口：对所有 AP 计算推荐的 EDCA 参数。

    Args:
        ap_states: {
            "ap1": {"channel_busy_ratio": 0.82, "tx_retries_ratio": 0.31, ...},
            "ap2": {...},
            "ap3": {...},
        }

    Returns:
        {
            "ap1": {
                "congestion_level": "critical",
                "CWmin": 15, "CWmax": 63, "AIFSN": 4,
                "valid": True, "errors": []
            },
            ...
        }
    """
    result = {}
    for ap_id, state in ap_states.items():
        busy    = state.get("channel_busy_ratio", 0.0)
        retries = state.get("tx_retries_ratio",   0.0)

        level  = classify_congestion(busy, retries)
        params = recommend_edca(level)
        valid, errors = validate(params)

        result[ap_id] = {
            "congestion_level": level,
            **params,
            "valid": valid,
            "errors": errors,
        }
    return result

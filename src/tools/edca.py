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

CANONICAL_CW_VALUES: tuple[int, ...] = tuple((1 << n) - 1 for n in range(2, 11))

_STATE_CW_KEYS: tuple[str, ...] = (
    "cwmin", "cwmax",
    "be_cwmin", "be_cwmax",
    "vi_cwmin", "vi_cwmax",
)

_PARAM_CW_KEYS: tuple[str, ...] = (
    "CWmin", "CWmax", "cwmin", "cwmax",
    "BE_CWmin", "BE_CWmax", "be_cwmin", "be_cwmax",
    "VI_CWmin", "VI_CWmax", "vi_cwmin", "vi_cwmax",
)

_PARAM_GROUP_ALIASES = {
    "BE": {
        "CWmin": ("BE_CWmin", "be_cwmin", "CWmin", "cwmin"),
        "CWmax": ("BE_CWmax", "be_cwmax", "CWmax", "cwmax"),
        "AIFSN": ("BE_AIFSN", "be_aifsn", "AIFSN", "aifsn"),
    },
    "VI": {
        "CWmin": ("VI_CWmin", "vi_cwmin"),
        "CWmax": ("VI_CWmax", "vi_cwmax"),
        "AIFSN": ("VI_AIFSN", "vi_aifsn"),
    },
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


def is_valid_cw_value(cw: int | float | str) -> bool:
    """Return whether a proposed actual CW value can be represented exactly."""
    try:
        value = int(cw)
    except (TypeError, ValueError):
        return False
    return value in CANONICAL_CW_VALUES


def decode_state_edca(state: dict) -> dict:
    """把单个 AP 状态里上报的 cwmin/cwmax 指数解码为实际 CW 值（返回新字典）。

    上报数据（来自 hostapd/iw）以指数 n 表示竞争窗口，本函数将其转换为协商内部
    使用的实际 CW 值。其余字段原样保留；非数值的 cwmin/cwmax 跳过。
    """
    if not isinstance(state, dict):
        return state
    out = dict(state)
    for key in _STATE_CW_KEYS:
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
    for key in _PARAM_CW_KEYS:
        val = out.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = cw_to_ecw(val)
    return out


def _first_present(params: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in params and params.get(key) is not None:
            return params.get(key)
    return None


def extract_param_groups(params: dict) -> dict[str, dict]:
    """提取 legacy/Per-AC EDCA 参数；旧字段等价于 BE，显式 BE_* 优先。"""
    if not isinstance(params, dict):
        return {}
    out: dict[str, dict] = {}
    for ac, fields in _PARAM_GROUP_ALIASES.items():
        group = {
            canonical: _first_present(params, aliases)
            for canonical, aliases in fields.items()
        }
        if any(v is not None for v in group.values()):
            out[ac] = group
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
    groups = extract_param_groups(params)
    errors: list[str] = []
    if not groups:
        groups = {"BE": {"CWmin": None, "CWmax": None, "AIFSN": None}}

    for ac, group in groups.items():
        for key, (lo, hi) in _LIMITS.items():
            val = group.get(key)
            if val is None:
                errors.append(f"{ac}: {key} 缺失")
                continue
            val = int(val)
            if not (lo <= val <= hi):
                errors.append(f"{ac}: {key}={val} 超出范围 [{lo}, {hi}]")

        if group.get("CWmin") is not None and group.get("CWmax") is not None:
            cwmin = int(group.get("CWmin"))
            cwmax = int(group.get("CWmax"))
            if not is_valid_cw_value(cwmin):
                nearest = ecw_to_cw(cw_to_ecw(cwmin))
                errors.append(
                    f"{ac}: CWmin={cwmin} 不是可下发竞争窗口值；"
                    f"必须取 2^n-1（如 {CANONICAL_CW_VALUES}），最接近会被编码为 {nearest}"
                )
            if not is_valid_cw_value(cwmax):
                nearest = ecw_to_cw(cw_to_ecw(cwmax))
                errors.append(
                    f"{ac}: CWmax={cwmax} 不是可下发竞争窗口值；"
                    f"必须取 2^n-1（如 {CANONICAL_CW_VALUES}），最接近会被编码为 {nearest}"
                )
            if cwmax <= cwmin:
                errors.append(f"{ac}: CWmax={cwmax} 必须大于 CWmin={cwmin}")

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
        groups = extract_param_groups(params)
        selected_ac = "BE" if "BE" in groups else ("VI" if "VI" in groups else "BE")
        selected = groups.get(selected_ac, {})
        cwmin    = int(selected.get("CWmin", 15))
        aifsn    = int(selected.get("AIFSN", 3))

        ordering_entries.append((key, rank, cwmin, aifsn))
        per_ap[key] = {
            "traffic_priority": priority,
            "edca_ac":          selected_ac,
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


# ── 自伤幅度预警（一阶启发式，非精确碰撞模型）──────────────────────────────
# 优先级单调性检查（evaluate_edca_effectiveness）只看排序，看不出幅度：
# 某 AP 即便排序合规，也可能被参数拉到几乎抢不到信道。这里用一个便宜的闭式
# 权重近似"相对抢占能力"：AIFS 时隙数 + 平均退避时隙数的倒数。不是 Bianchi
# 碰撞概率模型的替代品，只用于方向性预警（幅度门槛），不用于绝对吞吐预测。
_STATE_AC_ALIASES = {
    "BE": {"CWmin": ("be_cwmin", "cwmin"), "AIFSN": ("be_aifsn", "aifsn")},
    "VI": {"CWmin": ("vi_cwmin",), "AIFSN": ("vi_aifsn",)},
}


def access_weight(cwmin: int, aifsn: int) -> float:
    """AIFS + 平均退避时隙数的倒数，作为信道抢占概率的相对权重（越大越易抢占）。"""
    return 1.0 / (max(1, int(aifsn)) + (max(1, int(cwmin)) + 1) / 2.0)


def state_cw_aifsn(state: dict, ac: str) -> tuple[int, int]:
    aliases = _STATE_AC_ALIASES.get(ac, _STATE_AC_ALIASES["BE"])
    cwmin = _first_present(state, aliases["CWmin"])
    aifsn = _first_present(state, aliases["AIFSN"])
    return (int(cwmin) if cwmin is not None else 15, int(aifsn) if aifsn is not None else 3)


def predict_access_share(
    ap_states: dict[str, dict], proposed_edca: dict, *, ac: str = "BE",
) -> dict[str, dict]:
    """预测提案生效前后，各 AP 在指定 AC 上的相对信道抢占份额（假设同信道竞争）。

    未出现在 proposed_edca 里的 AP 视为该 AC 参数不变，沿用 ap_states 当前值。
    返回 {ap_id: {before_share, after_share, share_ratio, disadvantage_ratio}}；
    share_ratio < 1 表示份额下降，disadvantage_ratio 是相对提案后最强邻居的比值。
    """
    before_weights: dict[str, float] = {}
    after_weights: dict[str, float] = {}
    for ap_id, state in (ap_states or {}).items():
        if not isinstance(state, dict):
            continue
        key = ap_id.lower()
        cur_cwmin, cur_aifsn = state_cw_aifsn(state, ac)
        before_weights[key] = access_weight(cur_cwmin, cur_aifsn)

        params = proposed_edca.get(key) or proposed_edca.get(ap_id) or {}
        group = extract_param_groups(params).get(ac) if isinstance(params, dict) else None
        if group and group.get("CWmin") is not None and group.get("AIFSN") is not None:
            after_weights[key] = access_weight(int(group["CWmin"]), int(group["AIFSN"]))
        else:
            after_weights[key] = before_weights[key]

    total_before = sum(before_weights.values()) or 1.0
    total_after = sum(after_weights.values()) or 1.0

    result: dict[str, dict] = {}
    for ap_id in before_weights:
        share_before = before_weights[ap_id] / total_before
        share_after = after_weights[ap_id] / total_after
        rivals_after = [w for k, w in after_weights.items() if k != ap_id]
        strongest_rival = max(rivals_after) if rivals_after else 0.0
        result[ap_id] = {
            "before_share": round(share_before, 6),
            "after_share": round(share_after, 6),
            "share_ratio": round(share_after / share_before, 6) if share_before > 0 else None,
            "disadvantage_ratio": (
                round(after_weights[ap_id] / strongest_rival, 6) if strongest_rival > 0 else None
            ),
        }
    return result


def detect_self_harm(
    ap_states: dict[str, dict], proposed_edca: dict, *,
    ac: str = "BE", share_ratio_floor: float = 0.5,
) -> list[dict]:
    """找出提案里"合法但自伤"的 AP：确实改了该 AC 的参数，且预测份额跌破地板。

    只标记提案里显式改动了该 AC 参数的 AP（未被改动的 AP 即便份额被动下降也
    不算"自伤"，那是别人变强的正常结果，不是它自己的决策问题）。
    """
    shares = predict_access_share(ap_states, proposed_edca, ac=ac)
    flagged = []
    for ap_id, item in shares.items():
        params = proposed_edca.get(ap_id) or proposed_edca.get(ap_id.upper()) or {}
        touched = bool(extract_param_groups(params).get(ac)) if isinstance(params, dict) else False
        ratio = item.get("share_ratio")
        if touched and ratio is not None and ratio < share_ratio_floor:
            flagged.append({"ap_id": ap_id, "ac": ac, **item})
    return flagged

"""
Co-SR 计算工具

根据 AP 间 RSSI 感知干扰强度，求解满足 CCA / SINR / STA-RSSI
三重约束的连续最优 TX Power。

物理假设（RSSI 线性模型，不依赖坐标）：
  • 路径损耗在 dB 尺度线性：AP_j 功率变化 Δ dBm，AP_i 处接收也变化 Δ dBm
  • STA_i 处来自 AP_j 的干扰以 AP_i 处 neighbor_rssi 保守近似（最坏情况）
  • SINR 分母 = 来自所有邻居的干扰（线性求和）+ 本底噪声
"""
import itertools
import math

# ------------------------------------------------------------------
# 阈值常量
# ------------------------------------------------------------------
# 802.11ax SR (Spatial Reuse) 阈值说明：
#
#  OBSS_PD_MIN (-82 dBm)：默认 CCA 门限下限。低于此值信号可完全忽略；
#                          在非 SR 上下文中用作 OBSS 干扰的严格判定基准。
#
#  OBSS_PD_MAX (-62 dBm)：SR 可行上限。RSSI 高于此值时，即使将 OBSS PD
#                          调到最高也无法做 SR（结构性障碍）；
#                          在组选择阶段用作 CCA 约束：降功后组内互相 RSSI
#                          必须 ≤ OBSS_PD_MAX，才能设置合法的 OBSS PD 阈值。
#
#  TX power offset：当组内最强 RSSI = T（-82 ≤ T ≤ -62），AP 须将
#                   OBSS PD 设为 T，同时 TX power ≤ TX_POWER_MAX - (T - OBSS_PD_MIN)。
OBSS_PD_MIN_DBM = -82.0  # 默认 CCA 门限 / 非 SR 上下文严格基准
OBSS_PD_MAX_DBM = -62.0  # SR 可行上限：组选择 CCA 约束 & 结构性诊断阈值
SINR_THRESHOLD_DB    = 10.0   # Co-SR 降功后 SINR 下界（dB）
SINR_SELECT_THRESHOLD_DB = 10.0  # 并发组选择阶段 SINR 下界
STA_RSSI_MIN_DBM   = -75.0  # STA 关联安全下界（低于此值 STA 可能断连）
TX_POWER_MIN_DBM   = 1      # 优化下界（dBm，与 validator.py 保持一致）
TX_POWER_STEP_DB   = 1      # 保留给历史辅助函数使用；主求解器不按 1 dB 扫描
TX_POWER_MAX_DBM   = 23.0   # 与 validator.py 保持一致
OPT_TOLERANCE_DB   = 0.001  # 连续优化停止精度
DELTA_INT_TOLERANCE_DB = 1e-6  # 判定"功率调整量是否为整数 dB"的容差
SINR_WEIGHT            = 0.5   # 功率-信噪比权衡系数：每 1 dB SINR 余量等价于 0.5 dBm 功率收益

# 干扰强度分级阈值
_INTERFERENCE_THRESHOLDS = [
    ("strong",   lambda r: r >= -70.0),
    ("moderate", lambda r: r >= -80.0),
    ("weak",     lambda r: True),
]


# ------------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------------

def _fget(d: dict, key: str, default: float) -> float:
    """安全读取浮点字段：key 不存在或值为 None 时返回 default。"""
    v = d.get(key)
    return float(v) if v is not None else float(default)


def _dbm_to_mw(dbm: float) -> float:
    return 10 ** (dbm / 10)


def _mw_to_dbm(mw: float) -> float:
    if mw <= 0:
        return -200.0
    return 10 * math.log10(mw)


def _power_delta(ap_id: str, proposed_powers: dict, ap_states: dict) -> float:
    """计算 AP 的功率变化量（新功率 - 当前功率，dBm）。"""
    current = ap_states[ap_id].get("tx_power_dbm", 20.0)
    new = proposed_powers.get(ap_id)
    if new is None:
        new = current
    return new - current


def _delta_is_integer(delta: float) -> bool:
    """功率调整量是否为整数 dB（在容差内）。"""
    return abs(delta - round(delta)) <= DELTA_INT_TOLERANCE_DB


# ------------------------------------------------------------------
# 1. 感知干扰 — 构建 AP 间干扰矩阵
# ------------------------------------------------------------------

def classify_interference(rssi_dbm: float) -> str:
    """
    判断单对 AP 间的干扰强度等级。

    Returns:
        "strong" | "moderate" | "weak"
    """
    for level, condition in _INTERFERENCE_THRESHOLDS:
        if condition(rssi_dbm):
            return level
    return "weak"


def compute_interference_matrix(ap_states: dict) -> dict:
    """
    构建 AP 间当前干扰矩阵。

    Returns:
        {
            "ap1->ap2": {"rssi_dbm": -68.0, "level": "strong"},
            "ap1->ap3": {"rssi_dbm": -75.0, "level": "moderate"},
            ...
        }
    """
    matrix = {}
    for ap_id, state in ap_states.items():
        for nbr_id, rssi in state.get("neighbor_rssi_dbm", {}).items():
            key = f"{ap_id}->{nbr_id}"
            matrix[key] = {
                "rssi_dbm": rssi,
                "level": classify_interference(rssi),
            }
    return matrix


def analyze_interference(ap_states: dict) -> dict:
    """
    分析 Co-SR 干扰关系，返回强/中/弱干扰链路、主要干扰源和受害 AP。

    工具只解释当前无线环境，不给出最终功率决策。
    """
    matrix = compute_interference_matrix(ap_states)
    by_source = {
        ap_id: {"strong": 0, "moderate": 0, "weak": 0, "links": []}
        for ap_id in ap_states
    }
    by_victim = {
        ap_id: {"strong": 0, "moderate": 0, "weak": 0, "links": []}
        for ap_id in ap_states
    }
    strong_links = []
    moderate_links = []

    for key, item in matrix.items():
        victim_id, source_id = key.split("->", 1)
        level = item["level"]
        link = {
            "source_ap": source_id,
            "victim_ap": victim_id,
            "rssi_dbm": item["rssi_dbm"],
            "level": level,
        }
        if source_id in by_source:
            by_source[source_id][level] += 1
            by_source[source_id]["links"].append(link)
        if victim_id in by_victim:
            by_victim[victim_id][level] += 1
            by_victim[victim_id]["links"].append(link)
        if level == "strong":
            strong_links.append(link)
        elif level == "moderate":
            moderate_links.append(link)

    def _rank(stats: dict) -> list[dict]:
        rows = []
        for ap_id, item in stats.items():
            score = item["strong"] * 3 + item["moderate"]
            rows.append({
                "ap_id": ap_id,
                "score": score,
                "strong_links": item["strong"],
                "moderate_links": item["moderate"],
            })
        return sorted(rows, key=lambda x: (-x["score"], x["ap_id"]))

    return {
        "interference_matrix": matrix,
        "strong_links": strong_links,
        "moderate_links": moderate_links,
        "primary_interferers": _rank(by_source),
        "primary_victims": _rank(by_victim),
        "co_sr_triggered": bool(strong_links),
        "summary": {
            "strong_link_count": len(strong_links),
            "moderate_link_count": len(moderate_links),
            "strong_threshold_dbm": -70.0,
            "obss_pd_min_dbm": OBSS_PD_MIN_DBM,
            "obss_pd_max_dbm": OBSS_PD_MAX_DBM,
        },
    }


# ------------------------------------------------------------------
# 2. 量化计算 — 约束检查与连续功率优化
# ------------------------------------------------------------------

def _cca_at_ap(target_ap_id: str, ap_states: dict, proposed_powers: dict) -> dict:
    """
    计算目标 AP 在邻居使用 proposed_powers 时所受的 OBSS 干扰。

    802.11ax SR 阈值逻辑：
    - < -82 dBm (OBSS_PD_MIN): 干扰足够弱，可以忽略，不构成约束
    - -82 ~ -62 dBm: 在可接受的 SR 工作范围内
    - > -62 dBm (OBSS_PD_MAX): 干扰太强，无法空间复用

    合并双向 RSSI：若 target 未上报某邻居方向的 RSSI，则用该邻居对 target 的
    反向 RSSI 补全（路径对称假设），避免单向缺失时漏算干扰。

    Returns:
        {
            "ap2": {"received_dbm": -70.5, "ok": True},
            "max_received_dbm": -70.5,
            "ok": True,
        }
    """
    neighbor_rssi = dict(ap_states[target_ap_id].get("neighbor_rssi_dbm", {}))

    # 补全缺失方向：其他 AP 对 target 的反向 RSSI（若 target 未上报该方向）
    for src_id, src_state in ap_states.items():
        if src_id != target_ap_id and src_id not in neighbor_rssi:
            reverse = src_state.get("neighbor_rssi_dbm", {}).get(target_ap_id)
            if reverse is not None:
                neighbor_rssi[src_id] = reverse
    details = {}
    received_values = []

    for nbr_id, base_rssi in neighbor_rssi.items():
        delta = _power_delta(nbr_id, proposed_powers, ap_states) if nbr_id in ap_states else 0.0
        received = base_rssi + delta
        ok = received <= OBSS_PD_MAX_DBM
        details[nbr_id] = {"received_dbm": round(received, 2), "ok": ok}
        received_values.append(received)

    max_received = max(received_values) if received_values else -200.0
    return {
        **details,
        "max_received_dbm": round(max_received, 2),
        "ok": max_received <= OBSS_PD_MAX_DBM,
    }


def _sinr_at_sta(ap_id: str, ap_states: dict, proposed_powers: dict) -> float:
    """
    估算 STA_i 处 SINR（dB）。

    信号来自己方 AP（按功率变化调整），干扰来自所有邻居（保守近似），
    噪声为 noise_floor_dbm。
    """
    state = ap_states[ap_id]

    delta_i = _power_delta(ap_id, proposed_powers, ap_states)
    signal_mw = _dbm_to_mw(state.get("sta_rssi_dbm", -60.0) + delta_i)

    noise_mw = _dbm_to_mw(state.get("noise_floor_dbm", -90.0))
    interference_mw = noise_mw

    for nbr_id, base_rssi in state.get("neighbor_rssi_dbm", {}).items():
        delta_j = _power_delta(nbr_id, proposed_powers, ap_states) if nbr_id in ap_states else 0.0
        interference_mw += _dbm_to_mw(base_rssi + delta_j)

    return round(_mw_to_dbm(signal_mw / interference_mw) if interference_mw > 0 else 100.0, 2)


def _sinr_at_sta_for_group(
    ap_id: str,
    ap_states: dict,
    proposed_powers: dict,
    concurrent_group: set[str],
) -> float:
    """估算组内并发时的 STA SINR；组外 AP 不计入同一并发时刻的干扰。"""
    state = ap_states[ap_id]

    delta_i = _power_delta(ap_id, proposed_powers, ap_states)
    signal_mw = _dbm_to_mw(state.get("sta_rssi_dbm", -60.0) + delta_i)

    interference_mw = _dbm_to_mw(state.get("noise_floor_dbm", -90.0))
    for nbr_id, base_rssi in state.get("neighbor_rssi_dbm", {}).items():
        if nbr_id not in concurrent_group:
            continue
        delta_j = _power_delta(nbr_id, proposed_powers, ap_states) if nbr_id in ap_states else 0.0
        interference_mw += _dbm_to_mw(base_rssi + delta_j)

    return round(_mw_to_dbm(signal_mw / interference_mw) if interference_mw > 0 else 100.0, 2)


def _sta_rssi_after(ap_id: str, ap_states: dict, proposed_powers: dict) -> float:
    """降功率后己方 STA 的估算 RSSI（dBm）。"""
    state = ap_states[ap_id]
    delta = _power_delta(ap_id, proposed_powers, ap_states)
    return round(state.get("sta_rssi_dbm", -60.0) + delta, 2)


def _check_all_constraints(
    ap_states: dict, proposed_powers: dict
) -> tuple[bool, list[str], dict]:
    """
    对所有 AP 检查三重约束：OBSS 干扰 / SINR / STA RSSI。

    Returns:
        (all_ok, global_errors, per_ap_details)
    """
    errors = []
    details = {}

    for ap_id in ap_states:
        cca     = _cca_at_ap(ap_id, ap_states, proposed_powers)
        sinr    = _sinr_at_sta(ap_id, ap_states, proposed_powers)
        sta_rssi = _sta_rssi_after(ap_id, ap_states, proposed_powers)

        ap_errors = []

        if not cca["ok"]:
            ap_errors.append(
                f"OBSS干扰={cca['max_received_dbm']} dBm ≥ SR上限 {OBSS_PD_MAX_DBM} dBm"
            )

        sinr_ok = sinr >= SINR_THRESHOLD_DB
        if not sinr_ok:
            ap_errors.append(f"SINR={sinr} dB < 阈值 {SINR_THRESHOLD_DB} dB")

        sta_ok = sta_rssi >= STA_RSSI_MIN_DBM
        if not sta_ok:
            ap_errors.append(
                f"STA RSSI={sta_rssi} dBm < 安全下界 {STA_RSSI_MIN_DBM} dBm"
            )

        details[ap_id] = {
            "proposed_power_dbm": proposed_powers.get(ap_id, ap_states[ap_id].get("tx_power_dbm")),
            "cca_max_dbm":  cca["max_received_dbm"],
            "cca_ok":       cca["ok"],
            "cca_detail":   {k: v for k, v in cca.items() if k not in ("max_received_dbm", "ok")},
            "sinr_db":      sinr,
            "sinr_ok":      sinr_ok,
            "sta_rssi_dbm": sta_rssi,
            "sta_rssi_ok":  sta_ok,
            "valid":        len(ap_errors) == 0,
            "errors":       ap_errors,
        }
        errors.extend([f"{ap_id.upper()}: {e}" for e in ap_errors])

    return len(errors) == 0, errors, details


def _check_group_constraints(
    ap_states: dict,
    proposed_powers: dict,
    concurrent_group: list[str] | set[str],
) -> tuple[bool, list[str], dict]:
    """
    只验算指定并发组内 AP 的 OBSS 干扰 / SINR / STA RSSI。

    组外 AP 被视为不参与本轮空间复用并发，因此不作为同一并发时刻的
    干扰源，也不要求它们满足组内约束。
    """
    group = {ap.lower() for ap in concurrent_group if ap.lower() in ap_states}
    errors = []
    details = {}

    for ap_id in group:
        full_cca = _cca_at_ap(ap_id, ap_states, proposed_powers)
        cca_detail = {
            nbr_id: item
            for nbr_id, item in full_cca.items()
            if nbr_id in group
        }
        received_values = [
            item["received_dbm"]
            for item in cca_detail.values()
            if isinstance(item, dict) and "received_dbm" in item
        ]
        max_received = max(received_values) if received_values else -200.0
        cca_ok = max_received <= OBSS_PD_MAX_DBM
        sinr = _sinr_at_sta_for_group(ap_id, ap_states, proposed_powers, group)
        sta_rssi = _sta_rssi_after(ap_id, ap_states, proposed_powers)

        # 802.11ax SR TX power offset：OBSS PD = max_received 时，
        # TX power 须 ≤ TX_POWER_MAX - (OBSS_PD - OBSS_PD_MIN)
        sr_obss_pd   = max(max_received, OBSS_PD_MIN_DBM)
        sr_tx_limit  = TX_POWER_MAX_DBM - max(0.0, sr_obss_pd - OBSS_PD_MIN_DBM)
        proposed_tx  = proposed_powers.get(ap_id, ap_states[ap_id].get("tx_power_dbm", 20.0))
        sr_power_ok  = proposed_tx <= sr_tx_limit + OPT_TOLERANCE_DB

        ap_errors = []
        if not cca_ok:
            ap_errors.append(
                f"组内OBSS干扰={round(max_received, 2)} dBm ≥ SR上限 {OBSS_PD_MAX_DBM} dBm"
            )
        if cca_ok and not sr_power_ok:
            ap_errors.append(
                f"SR TX功率超限：OBSS_PD={round(sr_obss_pd, 1)} dBm时"
                f" 最大允许={round(sr_tx_limit, 1)} dBm，提案={round(proposed_tx, 1)} dBm"
            )

        sinr_ok = sinr >= SINR_SELECT_THRESHOLD_DB
        if not sinr_ok:
            ap_errors.append(f"组内 SINR={sinr} dB < 阈值 {SINR_SELECT_THRESHOLD_DB} dB")

        sta_ok = sta_rssi >= STA_RSSI_MIN_DBM
        if not sta_ok:
            ap_errors.append(
                f"STA RSSI={sta_rssi} dBm < 安全下界 {STA_RSSI_MIN_DBM} dBm"
            )

        details[ap_id] = {
            "proposed_power_dbm": proposed_tx,
            "sr_tx_limit_dbm":    round(sr_tx_limit, 2),
            "sr_obss_pd_dbm":     round(sr_obss_pd, 2),
            # 与提案字段同名的别名（钳到 SR 合法窗口），便于 agent 直接填入提案
            "obss_pd_dbm":        round(min(max(sr_obss_pd, OBSS_PD_MIN_DBM), OBSS_PD_MAX_DBM), 2),
            "cca_max_dbm":        round(max_received, 2),
            "cca_ok":             cca_ok,
            "sr_power_ok":        sr_power_ok,
            "cca_detail":         cca_detail,
            "sinr_db":            sinr,
            "sinr_ok":            sinr_ok,
            "sta_rssi_dbm":       sta_rssi,
            "sta_rssi_ok":        sta_ok,
            "valid":              len(ap_errors) == 0,
            "errors":             ap_errors,
        }
        errors.extend([f"{ap_id.upper()}: {e}" for e in ap_errors])

    return len(errors) == 0, errors, details


def _cca_contributions(ap_states: dict, powers: dict) -> dict[str, int]:
    """
    统计每个 AP 在邻居处造成的 OBSS 干扰过强次数。
    OBSS 干扰过强 = 邻居接收到该 AP 的信号 > OBSS_PD_MAX (-62 dBm)，超出 SR 可行范围。
    """
    contributions: dict[str, int] = {ap_id: 0 for ap_id in ap_states}
    for victim_id, victim_state in ap_states.items():
        for src_id, base_rssi in victim_state.get("neighbor_rssi_dbm", {}).items():
            if src_id not in powers:
                continue
            delta = powers[src_id] - ap_states[src_id].get("tx_power_dbm", 20.0)
            if base_rssi + delta > OBSS_PD_MAX_DBM:
                contributions[src_id] += 1
    return contributions


def _sta_rssi_margin(ap_id: str, ap_states: dict, powers: dict) -> float:
    """降功率后 STA RSSI 距安全下界的余量（dBm）；负值表示已违规。"""
    return _sta_rssi_after(ap_id, ap_states, powers) - STA_RSSI_MIN_DBM


def _power_bounds(ap_states: dict) -> tuple[dict, dict, dict]:
    """
    计算连续优化的功率上下界。

    下界由法定最小功率和 STA RSSI 安全下界共同决定。
    上界由当前功率和法定最大功率决定。

    注意：不再基于 -82 dBm CCA 阈值计算功率上限，因为：
    - -82 ~ -62 dBm 是 802.11ax SR 的正常工作范围
    - 干扰约束通过 SINR 验算来保证，而不是硬性功率边界
    """
    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    binding: dict[str, dict] = {}

    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", TX_POWER_MAX_DBM)
        sta = _fget(state, "sta_rssi_dbm", -60.0)
        lower[ap_id] = max(
            float(TX_POWER_MIN_DBM),
            current + STA_RSSI_MIN_DBM - sta,
        )
        upper[ap_id] = min(current, TX_POWER_MAX_DBM)
        binding[ap_id] = {
            "lower_reasons": ["tx_power_min", "sta_rssi_min"],
            "upper_reasons": ["current_power", "tx_power_max"],
        }

    return lower, upper, binding


def compute_feasible_ranges(ap_states: dict) -> dict:
    """
    计算每个 AP 的连续 TX Power 可行区间。

    区间的下界来自法定最小功率和 STA RSSI 安全下界。
    上界来自当前功率和法定最大功率。

    注意：不再基于 CCA 阈值计算功率上限，因为 -82 ~ -62 dBm 是 SR 正常工作范围。
    SINR 是 AP 间耦合约束，需要通过 evaluate_sr_candidate 验算。
    """
    lower, upper, binding = _power_bounds(ap_states)
    int_lower, int_upper = _integer_bounds(ap_states, lower, upper)
    ranges = {}
    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", TX_POWER_MAX_DBM)
        min_dbm = round(lower[ap_id], 3)
        max_dbm = round(upper[ap_id], 3)
        # 功率调整量必须为整数 dB → 可行功率收紧到整数格点。
        min_int = int_lower[ap_id]
        max_int = int_upper[ap_id]
        has_int = min_int <= max_int
        ranges[ap_id] = {
            "current_dbm": current,
            "min_dbm": min_dbm,
            "max_dbm": max_dbm,
            "min_int_dbm": min_int if has_int else None,
            "max_int_dbm": max_int if has_int else None,
            "feasible_individual_range": min_dbm <= max_dbm + OPT_TOLERANCE_DB,
            "has_feasible_integer": has_int,
            "min_delta_db": round(min_dbm - current, 3),
            "max_delta_db": round(max_dbm - current, 3),
            "lower_reasons": binding[ap_id]["lower_reasons"],
            "upper_reasons": binding[ap_id]["upper_reasons"],
            "sta_rssi_margin_at_min_db": round(
                state.get("sta_rssi_dbm", -60.0)
                + (min_dbm - current)
                - STA_RSSI_MIN_DBM,
                3,
            ),
            # 协议级 Co-SR：当前功率下配套的 OBSS_PD 门限提示（提案须带 obss_pd_dbm）。
            # 选定候选功率后由 evaluate_sr_candidate 的 sr_obss_pd_dbm 给出精确值。
            "recommended_obss_pd_dbm": _obss_pd_for_ap(ap_id, ap_states, {}),
        }

    # 候选提示直接给整数 dBm，确保调整量为整数 dB。
    # 保守候选取区间中点
    conservative_candidate = {
        ap_id: float(round((int_lower[ap_id] + int_upper[ap_id]) / 2))
        for ap_id in ap_states
        if int_lower[ap_id] <= int_upper[ap_id]
    }
    return {
        "ranges": ranges,
        "sinr_coupled": True,
        "integer_power_required": True,
        "candidate_hints": {
            "conservative_mid_range": conservative_candidate,
        },
        "all_individual_ranges_feasible": all(
            item["feasible_individual_range"] for item in ranges.values()
        ),
        "notes": [
            "功率调整量必须为整数 dB，候选 tx_power_dbm 只能取整数（参考 min_int_dbm/max_int_dbm）。",
            "候选功率必须继续调用 evaluate_sr_candidate 验证 SINR/STA RSSI。",
            "802.11ax SR 工作范围：邻居干扰 < -62 dBm 即可接受。",
            "Co-SR 目标是综合优化：在约束满足的前提下，同时降低功率和保留足够的 SINR 余量（见 SINR_WEIGHT）。",
        ],
    }


def _objective(ap_states: dict, powers: dict) -> float:
    """综合目标：最小化总功率，同时以 SINR 余量作为正向奖励。
    返回值越小表示方案越优先。
    """
    total = sum(powers[ap_id] for ap_id in ap_states)
    for ap_id in ap_states:
        sinr = _sinr_at_sta(ap_id, ap_states, powers)
        total -= SINR_WEIGHT * max(sinr - SINR_THRESHOLD_DB, 0.0)
    return total


def _objective_for_group(ap_states: dict, powers: dict, group: set[str]) -> float:
    """组内并发的综合目标：最小化总功率，以组内 SINR 余量作为正向奖励。"""
    total = sum(powers[ap_id] for ap_id in ap_states)
    for ap_id in group:
        sinr = _sinr_at_sta_for_group(ap_id, ap_states, powers, group)
        total -= SINR_WEIGHT * max(sinr - SINR_THRESHOLD_DB, 0.0)
    return total


def _is_feasible(ap_states: dict, powers: dict) -> bool:
    ok, _, _ = _check_all_constraints(ap_states, powers)
    return ok


def _integer_bounds(ap_states: dict, lower: dict, upper: dict) -> tuple[dict, dict]:
    """
    把连续可行界收紧到整数 dBm 格点。

    下界向上取整、上界向下取整，确保区间内每个取值都是整数，
    从而保证"相对整数当前功率的调整量"也是整数 dB。
    """
    int_lower: dict[str, int] = {}
    int_upper: dict[str, int] = {}
    for ap_id in ap_states:
        int_lower[ap_id] = math.ceil(lower[ap_id] - OPT_TOLERANCE_DB)
        int_upper[ap_id] = math.floor(upper[ap_id] + OPT_TOLERANCE_DB)
    return int_lower, int_upper


def _power_bounds_for_group(ap_states: dict, concurrent_group: set[str]) -> tuple[dict, dict, dict]:
    """
    计算并发组内的功率上下界。

    下界由法定最小功率和 STA RSSI 安全下界共同决定。
    上界由当前功率和法定最大功率决定。

    注意：不再基于 CCA 阈值计算功率上限，因为 -82 ~ -62 dBm 是 SR 正常工作范围。
    """
    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    binding: dict[str, dict] = {}

    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", TX_POWER_MAX_DBM)
        if ap_id not in concurrent_group:
            lower[ap_id] = current
            upper[ap_id] = current
            binding[ap_id] = {
                "lower_reasons": ["not_in_concurrent_group"],
                "upper_reasons": ["not_in_concurrent_group"],
            }
            continue

        sta = _fget(state, "sta_rssi_dbm", -60.0)
        lower[ap_id] = max(
            float(TX_POWER_MIN_DBM),
            current + STA_RSSI_MIN_DBM - sta,
        )
        upper[ap_id] = min(current, TX_POWER_MAX_DBM)
        binding[ap_id] = {
            "lower_reasons": ["tx_power_min", "sta_rssi_min"],
            "upper_reasons": ["current_power", "tx_power_max"],
        }

    return lower, upper, binding


def _optimize_integer_powers_for_group(
    ap_states: dict,
    concurrent_group: set[str],
) -> tuple[dict | None, dict]:
    lower, upper, binding = _power_bounds_for_group(ap_states, concurrent_group)
    meta_base = {
        "lower_bounds_dbm": lower,
        "upper_bounds_dbm": upper,
        "binding_bounds": binding,
    }
    int_lower, int_upper = _integer_bounds(ap_states, lower, upper)
    ap_ids = sorted(concurrent_group)

    for ap_id in ap_ids:
        if int_lower[ap_id] > int_upper[ap_id]:
            return None, {
                **meta_base,
                "error": (
                    f"{ap_id} 在并发组 {sorted(concurrent_group)} 内的可行区间 "
                    f"[{round(lower[ap_id], 3)}, {round(upper[ap_id], 3)}] dBm "
                    "内不存在整数功率"
                ),
            }

    grid = [range(int_lower[ap_id], int_upper[ap_id] + 1) for ap_id in ap_ids]
    best: dict | None = None
    best_obj: float | None = None
    feasible_count = 0

    for combo in itertools.product(*grid):
        powers = {
            ap_id: _fget(state, "tx_power_dbm", 20.0)
            for ap_id, state in ap_states.items()
        }
        for i, ap_id in enumerate(ap_ids):
            powers[ap_id] = float(combo[i])
        ok, _, _ = _check_group_constraints(ap_states, powers, concurrent_group)
        if not ok:
            continue
        feasible_count += 1
        obj = _objective_for_group(ap_states, powers, concurrent_group)
        if best_obj is None or obj < best_obj - 1e-9:
            best, best_obj = powers, obj

    if best is None:
        return None, {
            **meta_base,
            "error": f"并发组 {sorted(concurrent_group)} 在整数 dBm 格点上无可行解",
        }

    return best, {
        **meta_base,
        "objective": "minimize_power_with_sinr_reward_for_concurrent_group",
        "objective_value": round(best_obj, 6),
        "integer_step_db": 1,
        "feasible_integer_points": feasible_count,
    }


def _optimize_integer_powers(ap_states: dict) -> tuple[dict | None, dict]:
    """
    整数 dB Co-SR 约束优化。

    在满足 CCA / SINR / STA RSSI 的前提下，于整数 dBm 格点上最小化
    所有 AP 相对当前功率的平方调整量。由于 AP 数量很小且每个 AP 的
    整数可行档位有限，这里直接对整数格点做确定性穷举，保证给出的
    每个 AP 功率调整量都是整数 dB。
    """
    lower, upper, binding = _power_bounds(ap_states)
    meta_base = {
        "lower_bounds_dbm": lower,
        "upper_bounds_dbm": upper,
        "binding_bounds": binding,
    }

    int_lower, int_upper = _integer_bounds(ap_states, lower, upper)
    ap_ids = list(ap_states)
    for ap_id in ap_ids:
        if int_lower[ap_id] > int_upper[ap_id]:
            return None, {
                **meta_base,
                "error": (
                    f"{ap_id} 的可行区间 "
                    f"[{round(lower[ap_id], 3)}, {round(upper[ap_id], 3)}] dBm "
                    "内不存在整数功率，无法给出整数 dB 调整量"
                ),
            }

    grid = [range(int_lower[ap_id], int_upper[ap_id] + 1) for ap_id in ap_ids]
    best: dict | None = None
    best_obj: float | None = None
    feasible_count = 0

    for combo in itertools.product(*grid):
        powers = {ap_id: float(combo[i]) for i, ap_id in enumerate(ap_ids)}
        if not _is_feasible(ap_states, powers):
            continue
        feasible_count += 1
        obj = _objective(ap_states, powers)
        if best_obj is None or obj < best_obj - 1e-9:
            best, best_obj = powers, obj

    if best is None:
        return None, {
            **meta_base,
            "error": "整数 dBm 格点上不存在同时满足 CCA/SINR/STA RSSI 的可行解",
        }

    return best, {
        **meta_base,
        "objective": "minimize_power_with_sinr_reward",
        "objective_value": round(best_obj, 6),
        "integer_step_db": 1,
        "feasible_integer_points": feasible_count,
        "solver": "deterministic_integer_lattice_search",
    }


def _active_constraints(ap_id: str, detail: dict, powers: dict, lower: dict, upper: dict) -> list[str]:
    active: list[str] = []
    if abs(powers[ap_id] - lower[ap_id]) <= 0.02:
        active.append("lower_bound")
    if abs(powers[ap_id] - upper[ap_id]) <= 0.02:
        active.append("upper_bound")
    if abs(detail["cca_max_dbm"] - OBSS_PD_MAX_DBM) <= 0.05:
        active.append("obss_interference")
    if abs(detail["sinr_db"] - SINR_THRESHOLD_DB) <= 0.05:
        active.append("sinr")
    if abs(detail["sta_rssi_dbm"] - STA_RSSI_MIN_DBM) <= 0.05:
        active.append("sta_rssi")
    return active


def recommend_tx_power_differentiated(ap_states: dict) -> dict:
    """
    差异化连续最优功率：求解满足物理约束的最小必要降功率方案。

    Returns:
        {
            "ap1": {
                "optimal_dbm": 10.23,
                "recommended_dbm": 10.23,
                "current_dbm": 20.0,
                "delta_db": -9.77
            },
            ...
        }
    """
    powers, meta = _optimize_integer_powers(ap_states)
    if powers is None:
        return {
            ap_id: {
                "optimal_dbm": None,
                "recommended_dbm": None,
                "current_dbm": _fget(state, "tx_power_dbm", 20.0),
                "delta_db": None,
                "error": meta.get("error"),
            }
            for ap_id, state in ap_states.items()
        }

    lower = meta["lower_bounds_dbm"]
    upper = meta["upper_bounds_dbm"]
    _, _, validation = _check_all_constraints(ap_states, powers)

    result = {}
    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", 20.0)
        # 整数格点求解，optimal 为整数 dBm；调整量也是整数 dB。
        optimal = float(round(powers[ap_id]))
        result[ap_id] = {
            "optimal_dbm":     optimal,
            # 兼容旧日志和 agent 文档字段；语义已改为整数最优值。
            "recommended_dbm": optimal,
            "current_dbm":     current,
            "delta_db":        round(optimal - current, 3),
            # 协议级 Co-SR：与 optimal 功率配套的 OBSS_PD 门限（截断到 SR 合法窗口）
            "obss_pd_dbm":     _obss_pd_for_ap(ap_id, ap_states, powers),
            "active_constraints": _active_constraints(
                ap_id, validation[ap_id], powers, lower, upper
            ),
        }
    return result


def _obss_pd_for_ap(ap_id: str, ap_states: dict, proposed_powers: dict) -> float:
    """
    某 AP 在 proposed_powers 下配套的 OBSS_PD 门限（协议级 Co-SR）。

    取该 AP 处最强 OBSS 干扰（_cca_at_ap 的 max_received），截断到 SR 合法窗口
    [OBSS_PD_MIN, OBSS_PD_MAX]：
      • 干扰 < -82：无需 SR，回落默认 CCA -82（不开空间复用）；
      • -82 ~ -62：设为该干扰值，忽略更弱 OBSS 帧以并发发送；
      • > -62：结构性不可行，钳到 -62（最大 SR 努力，另由约束检查报警）。
    与标准耦合约束 tx ≤ TX_POWER_MAX-(OBSS_PD-OBSS_PD_MIN) 一致（ns-3 侧自动限功）。
    """
    max_received = _cca_at_ap(ap_id, ap_states, proposed_powers).get(
        "max_received_dbm", OBSS_PD_MIN_DBM
    )
    obss_pd = min(max(max_received, OBSS_PD_MIN_DBM), OBSS_PD_MAX_DBM)
    return round(obss_pd, 2)


# ------------------------------------------------------------------
# 3. 公开接口
# ------------------------------------------------------------------

def recommend_tx_power(ap_states: dict) -> dict:
    """
    为每个 AP 给出连续最优 TX Power（差异化优化）。

    Args:
        ap_states: 各 AP 当前完整状态

    Returns:
        {
            "ap1": {"optimal_dbm": 10.23, "recommended_dbm": 10.23, "current_dbm": 20.0, "delta_db": -9.77},
            ...
        }
    """
    return recommend_tx_power_differentiated(ap_states)


def validate(ap_states: dict, proposed_powers: dict) -> tuple[bool, list[str]]:
    """
    验证提案中的功率是否满足所有约束。

    Args:
        ap_states:       各 AP 当前状态（含 neighbor_rssi_dbm 等字段）
        proposed_powers: {"ap1": 4.0, "ap2": 4.0, "ap3": 4.0}

    Returns:
        (is_valid, errors)

    Raises:
        ValueError: proposed_powers 包含 ap_states 中不存在的 AP
    """
    unknown = set(proposed_powers) - set(ap_states)
    if unknown:
        raise ValueError(f"proposed_powers 包含未知 AP: {unknown}")

    ok, errors, _ = _check_all_constraints(ap_states, proposed_powers)
    return ok, errors


def compute_validation(ap_states: dict, proposed_powers: dict) -> dict:
    """
    验算指定功率组合下各 AP 的 CCA/SINR/STA RSSI 约束，返回 per-AP 详情字典。

    与 validate() 不同，此函数返回结构化的 per-AP 验算详情，
    供 orchestrator 在投票阶段注入给投票方，避免 LLM 自行计算 delta 出错。

    Args:
        ap_states:       各 AP 当前状态
        proposed_powers: {"ap1": 6.0, "ap2": 6.0, "ap3": 6.0}

    Returns:
        {
            "ap1": {
                "proposed_power_dbm": 6.0,
                "cca_max_dbm": -83.0, "cca_ok": True,
                "sinr_db": 22.8,      "sinr_ok": True,
                "sta_rssi_dbm": -59.0,"sta_rssi_ok": True,
                "valid": True, "errors": []
            }, ...
        }
    """
    _, _, details = _check_all_constraints(ap_states, proposed_powers)
    return details


def evaluate_candidate(ap_states: dict, proposed_powers: dict) -> dict:
    """
    评估一个候选 Co-SR 功率方案，不替 agent 决策。

    Returns:
        {
            "valid": bool,
            "score": {...},
            "per_ap": {...}
        }
    """
    return evaluate_candidate_for_group(ap_states, proposed_powers, None)


def evaluate_candidate_for_group(
    ap_states: dict,
    proposed_powers: dict,
    concurrent_group: list[str] | None = None,
) -> dict:
    """
    评估 Co-SR 候选方案。

    concurrent_group 为空时使用旧语义：全网 AP 同时并发，检查所有 AP。
    concurrent_group 非空时只验算组内并发关系，组外 AP 视为不参与本轮并发。
    """
    normalized = {
        ap_id: float(proposed_powers.get(ap_id, ap_states[ap_id].get("tx_power_dbm", 20.0)))
        for ap_id in ap_states
    }
    group = None
    if concurrent_group:
        group = [ap.lower() for ap in concurrent_group if ap.lower() in ap_states]
    if group:
        ok, errors, details = _check_group_constraints(ap_states, normalized, group)
    else:
        ok, errors, details = _check_all_constraints(ap_states, normalized)
    for item in details.values():
        item.pop("cca_detail", None)

    total_drop = 0.0
    max_drop = 0.0
    squared_change = 0.0
    min_sta_margin = 999.0
    max_cca = -200.0
    min_sinr = 999.0
    total_sinr_margin = 0.0
    integer_errors: list[str] = []
    power_direction_errors: list[str] = []

    checked_aps = set(details) if group else set(ap_states)
    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", 20.0)
        proposed = normalized[ap_id]
        if proposed > current + OPT_TOLERANCE_DB:
            msg = (
                f"{ap_id.upper()}: Co-SR 候选不应提高发射功率"
                f"（当前 {current} → 提案 {proposed}）"
            )
            power_direction_errors.append(msg)
            if ap_id in checked_aps:
                details[ap_id].setdefault("errors", []).append(msg.split(": ", 1)[1])
        drop = max(0.0, current - proposed)
        total_drop += drop
        max_drop = max(max_drop, drop)
        squared_change += (proposed - current) ** 2
        if ap_id in checked_aps:
            min_sta_margin = min(min_sta_margin, _sta_rssi_margin(ap_id, ap_states, normalized))
            max_cca = max(max_cca, details[ap_id]["cca_max_dbm"])
            sinr = details[ap_id]["sinr_db"]
            min_sinr = min(min_sinr, sinr)
            total_sinr_margin += max(sinr - SINR_THRESHOLD_DB, 0.0)
        # 功率调整量必须为整数 dB。
        delta = proposed - current
        if not _delta_is_integer(delta):
            msg = (
                f"{ap_id.upper()}: 功率调整量 {delta:+.2f} dB 必须为整数 dB"
                f"（当前 {current} → 提案 {proposed}）"
            )
            integer_errors.append(msg)
            if ap_id in checked_aps:
                details[ap_id]["delta_is_integer"] = False
                details[ap_id].setdefault("errors", []).append(msg.split(": ", 1)[1])
        else:
            if ap_id in checked_aps:
                details[ap_id]["delta_is_integer"] = True

    errors = errors + integer_errors + power_direction_errors

    return {
        "valid": ok and not integer_errors and not power_direction_errors,
        "errors": errors,
        "concurrent_group": group or list(ap_states),
        "non_concurrent_aps": sorted(set(ap_states) - set(group or ap_states)),
        "proposed_powers": {ap_id: round(power, 3) for ap_id, power in normalized.items()},
        "score": {
            "total_power_drop_db": round(total_drop, 3),
            "max_single_ap_drop_db": round(max_drop, 3),
            "sum_squared_power_change_db": round(squared_change, 6),
            "min_sta_rssi_margin_db": round(min_sta_margin, 3) if checked_aps else None,
            "max_cca_dbm": round(max_cca, 3) if checked_aps else None,
            "min_sinr_db": round(min_sinr, 3) if checked_aps else None,
            "total_sinr_margin_db": round(total_sinr_margin, 3) if checked_aps else None,
        },
        "per_ap": details,
    }


def _diagnose_infeasibility(ap_states: dict) -> dict:
    """
    逐 AP 诊断为何难以进入任何并发组。

    对每个 AP 检查三类"结构性"障碍：
      - OBSS 干扰过强：邻居 RSSI > -62 dBm，超出 SR 工作范围；
      - STA RSSI 已低于安全下界（与并发与否无关，降功率只会更糟）；
      - 噪声受限：仅算噪声底时的 SINR 上界已 < 阈值，任何邻居安排都救不回来。
    """
    diag = {}
    for ap_id, state in ap_states.items():
        sta_rssi = _sta_rssi_after(ap_id, ap_states, {})
        noise = float(state.get("noise_floor_dbm", -90.0))
        sinr_ceiling = round(sta_rssi - noise, 2)  # 零邻居干扰时的 SINR 上界

        nbrs = state.get("neighbor_rssi_dbm", {}) or {}
        strongest = max(nbrs.items(), key=lambda kv: kv[1], default=None)

        reasons = []

        # 检查 OBSS 干扰是否过强（超出 SR 工作范围 -62 dBm）
        for nbr_id, base_rssi in nbrs.items():
            if base_rssi > OBSS_PD_MAX_DBM:
                reasons.append(
                    f"OBSS干扰过强：邻居 {nbr_id} 的 RSSI={base_rssi:.1f} dBm "
                    f"≥ SR上限 {OBSS_PD_MAX_DBM} dBm，超出空间复用工作范围"
                )

        if sta_rssi < STA_RSSI_MIN_DBM:
            reasons.append(
                f"STA RSSI={sta_rssi} dBm < 安全下界 {STA_RSSI_MIN_DBM} dBm"
                f"（降功率只会更糟，无法靠并发改善）"
            )
        if sinr_ceiling < SINR_SELECT_THRESHOLD_DB:
            reasons.append(
                f"噪声受限：SINR 上界 {sinr_ceiling} dB（=STA {sta_rssi} − 噪声底 {noise}）"
                f" < 阈值 {SINR_SELECT_THRESHOLD_DB} dB，关掉所有邻居也达不到"
            )

        diag[ap_id] = {
            "sta_rssi_dbm": sta_rssi,
            "sta_below_floor": sta_rssi < STA_RSSI_MIN_DBM,
            "sinr_ceiling_db": sinr_ceiling,
            "noise_limited": sinr_ceiling < SINR_SELECT_THRESHOLD_DB,
            "strongest_interferer": (
                {"ap": strongest[0], "rssi_dbm": strongest[1]} if strongest else None
            ),
            "reasons": reasons,
        }
    return diag


def select_concurrent_groups(ap_states: dict, min_group_size: int = 2) -> dict:
    """
    枚举可行 Co-SR 并发组。

    返回每个候选组的推荐功率。组外 AP 不参与该并发组，功率保持当前值。
    """
    ap_ids = sorted(ap_states)
    candidates = []
    for size in range(max(2, int(min_group_size)), len(ap_ids) + 1):
        for group_tuple in itertools.combinations(ap_ids, size):
            group = set(group_tuple)
            powers, meta = _optimize_integer_powers_for_group(ap_states, group)
            if powers is None:
                candidates.append({
                    "concurrent_group": list(group_tuple),
                    "valid": False,
                    "error": meta.get("error"),
                })
                continue
            evaluation = evaluate_candidate_for_group(ap_states, powers, list(group_tuple))
            score = evaluation["score"]
            candidates.append({
                "concurrent_group": list(group_tuple),
                "non_concurrent_aps": sorted(set(ap_ids) - group),
                "valid": evaluation["valid"],
                "recommended_powers": evaluation["proposed_powers"],
                # 协议级 Co-SR：与推荐功率配套的 OBSS_PD 门限（提案须一并带上）
                "recommended_obss_pd": {
                    ap_id: detail.get("obss_pd_dbm")
                    for ap_id, detail in evaluation["per_ap"].items()
                },
                "score": score,
                "errors": evaluation["errors"],
            })

    def _sort_key(item: dict) -> tuple:
        if not item.get("valid"):
            return (1, 0, 999.0, 999.0)
        score = item["score"]
        sinr_margin = max((score["min_sinr_db"] or 0.0) - SINR_THRESHOLD_DB, 0.0)
        # 综合评分：降功率 + SINR 余量奖励，与 _objective_for_group 保持一致
        combined = -score["total_power_drop_db"] - SINR_WEIGHT * sinr_margin
        return (
            0,
            -len(item["concurrent_group"]),
            combined,
            score["max_single_ap_drop_db"],
        )

    candidates.sort(key=_sort_key)
    valid = [item for item in candidates if item.get("valid")]
    return {
        "best_group": valid[0] if valid else None,
        "valid_groups": valid,
        "all_groups": candidates,
        # 无可行组时给出逐 AP 的结构性障碍诊断，避免上层只看到"没有可行组"。
        "diagnosis": _diagnose_infeasibility(ap_states) if not valid else {},
        "notes": [
            "concurrent_group 内 AP 按组内 CCA/SINR/STA RSSI 验算，可同一时刻并发。",
            "non_concurrent_aps 不参与该并发组，功率保持当前值，不作为同一并发时刻的干扰源。",
            "最终 Co-SR 提案可在 JSON 中加入 _sr.concurrent_group 标明本轮并发组。",
        ],
    }


def _normalize_candidate_input(candidates: object) -> dict[str, dict]:
    if isinstance(candidates, dict):
        normalized = {}
        for name, value in candidates.items():
            if isinstance(value, dict):
                normalized[str(name)] = value
        return normalized

    if isinstance(candidates, list):
        normalized = {}
        for idx, item in enumerate(candidates, start=1):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or f"candidate_{idx}")
            powers = item.get("proposed_powers", item)
            if isinstance(powers, dict):
                normalized[name] = powers
        return normalized

    return {}


def rank_candidates(ap_states: dict, candidates: object, objective: str = "balanced") -> dict:
    """
    对多个候选 Co-SR 方案排序。

    objective:
        balanced              优先合法，其次以"降功率 + SINR余量奖励"综合评分排序
        minimize_total_drop    优先最小总降功率
        minimize_max_drop      优先避免单 AP 过度降功率
        maximize_sta_margin    优先保留 STA RSSI 余量
    """
    normalized = _normalize_candidate_input(candidates)
    ranked = []

    for name, powers in normalized.items():
        evaluation = evaluate_candidate(ap_states, powers)
        score = evaluation["score"]
        valid_rank = 0 if evaluation["valid"] else 1

        if objective == "minimize_total_drop":
            sort_key = (
                valid_rank,
                score["total_power_drop_db"],
                score["max_single_ap_drop_db"],
                -score["min_sta_rssi_margin_db"],
            )
        elif objective == "minimize_max_drop":
            sort_key = (
                valid_rank,
                score["max_single_ap_drop_db"],
                score["total_power_drop_db"],
                -score["min_sta_rssi_margin_db"],
            )
        elif objective == "maximize_sta_margin":
            sort_key = (
                valid_rank,
                -score["min_sta_rssi_margin_db"],
                score["total_power_drop_db"],
                score["max_single_ap_drop_db"],
            )
        else:
            sort_key = (
                valid_rank,
                score["sum_squared_power_change_db"],
                score["max_single_ap_drop_db"],
                score["total_power_drop_db"],
                -score["min_sta_rssi_margin_db"],
            )

        ranked.append({
            "name": name,
            "valid": evaluation["valid"],
            "proposed_powers": evaluation["proposed_powers"],
            "score": score,
            "errors": evaluation["errors"],
            "_sort_key": sort_key,
        })

    ranked.sort(key=lambda item: item["_sort_key"])
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx
        item.pop("_sort_key", None)

    return {
        "objective": objective,
        "best": ranked[0] if ranked else None,
        "ranked_candidates": ranked,
    }


def detect_self_harm(
    ap_states: dict, proposed_powers: dict, *,
    rssi_floor: float = STA_RSSI_MIN_DBM,
) -> list[dict]:
    """找出提案里"合法但自伤"的 AP：确实改了自己的功率，且预测 STA RSSI 跌破安全下界。

    只做 STA 连接安全性这一条自伤检查——硬性底线，不设"为了救邻居可以牺牲"的
    例外（连接断了就没有讨价还价的余地）。CCA/SINR 等其余约束仍由
    evaluate_sr_candidate/compute_validation 等既有工具供 Agent 自检，
    不在此处重复，避免与既有 OBSS_PD 耦合校验产生交叉判定。
    """
    flagged = []
    for ap_id in ap_states:
        if ap_id not in proposed_powers:
            continue  # 未被提案改动的 AP 不算自伤
        delta = _power_delta(ap_id, proposed_powers, ap_states)
        if abs(delta) < DELTA_INT_TOLERANCE_DB:
            continue
        rssi_after = _sta_rssi_after(ap_id, ap_states, proposed_powers)
        if rssi_after < rssi_floor:
            flagged.append({
                "ap_id": ap_id,
                "sta_rssi_before": ap_states[ap_id].get("sta_rssi_dbm"),
                "sta_rssi_after": rssi_after,
                "floor": rssi_floor,
            })
    return flagged

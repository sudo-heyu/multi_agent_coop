"""
Co-SR 计算工具

根据 AP 间 RSSI 感知干扰强度，求解满足 CCA / SINR / STA-RSSI
三重约束的连续最优 TX Power。

物理假设（RSSI 线性模型，不依赖坐标）：
  • 路径损耗在 dB 尺度线性：AP_j 功率变化 Δ dBm，AP_i 处接收也变化 Δ dBm
  • STA_i 处来自 AP_j 的干扰以 AP_i 处 neighbor_rssi 保守近似（最坏情况）
  • SINR 分母 = 来自所有邻居的干扰（线性求和）+ 本底噪声
"""
import math

# ------------------------------------------------------------------
# 阈值常量
# ------------------------------------------------------------------
CCA_THRESHOLD_DBM = -82.0   # OBSS/BSS 边界 CCA 检测阈值（802.11ax 默认）
SINR_THRESHOLD_DB  = 15.0   # 链路质量下界（dB）
STA_RSSI_MIN_DBM   = -75.0  # STA 关联安全下界（低于此值 STA 可能断连）
TX_POWER_MIN_DBM   = 1      # 优化下界（dBm，与 validator.py 保持一致）
TX_POWER_STEP_DB   = 1      # 保留给历史辅助函数使用；主求解器不按 1 dB 扫描
TX_POWER_MAX_DBM   = 23.0   # 与 validator.py 保持一致
CCA_GUARD_DB       = 0.01   # 避免最优解贴在严格不等式 CCA < -82 dBm 上
OPT_TOLERANCE_DB   = 0.001  # 连续优化停止精度

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
            "cca_threshold_dbm": CCA_THRESHOLD_DBM,
        },
    }


# ------------------------------------------------------------------
# 2. 量化计算 — 约束检查与连续功率优化
# ------------------------------------------------------------------

def _cca_at_ap(target_ap_id: str, ap_states: dict, proposed_powers: dict) -> dict:
    """
    计算目标 AP 在邻居使用 proposed_powers 时所受的 CCA 干扰。

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
        ok = received < CCA_THRESHOLD_DBM
        details[nbr_id] = {"received_dbm": round(received, 2), "ok": ok}
        received_values.append(received)

    max_received = max(received_values) if received_values else -200.0
    return {
        **details,
        "max_received_dbm": round(max_received, 2),
        "ok": max_received < CCA_THRESHOLD_DBM,
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


def _sta_rssi_after(ap_id: str, ap_states: dict, proposed_powers: dict) -> float:
    """降功率后己方 STA 的估算 RSSI（dBm）。"""
    state = ap_states[ap_id]
    delta = _power_delta(ap_id, proposed_powers, ap_states)
    return round(state.get("sta_rssi_dbm", -60.0) + delta, 2)


def _check_all_constraints(
    ap_states: dict, proposed_powers: dict
) -> tuple[bool, list[str], dict]:
    """
    对所有 AP 检查三重约束：CCA / SINR / STA RSSI。

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
                f"CCA={cca['max_received_dbm']} dBm ≥ 阈值 {CCA_THRESHOLD_DBM} dBm"
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


def _cca_contributions(ap_states: dict, powers: dict) -> dict[str, int]:
    """
    统计每个 AP 在邻居处造成的 CCA 违规次数。
    CCA 违规 = 邻居接收到该 AP 的信号 ≥ CCA_THRESHOLD_DBM。
    """
    contributions: dict[str, int] = {ap_id: 0 for ap_id in ap_states}
    for victim_id, victim_state in ap_states.items():
        for src_id, base_rssi in victim_state.get("neighbor_rssi_dbm", {}).items():
            if src_id not in powers:
                continue
            delta = powers[src_id] - ap_states[src_id].get("tx_power_dbm", 20.0)
            if base_rssi + delta >= CCA_THRESHOLD_DBM:
                contributions[src_id] += 1
    return contributions


def _sta_rssi_margin(ap_id: str, ap_states: dict, powers: dict) -> float:
    """降功率后 STA RSSI 距安全下界的余量（dBm）；负值表示已违规。"""
    return _sta_rssi_after(ap_id, ap_states, powers) - STA_RSSI_MIN_DBM


def _power_bounds(ap_states: dict) -> tuple[dict, dict, dict]:
    """
    计算连续优化的功率上下界。

    上界由当前功率、法定最大功率、以及所有 CCA 严格约束共同决定。
    下界由法定最小功率和 STA RSSI 安全下界共同决定。
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

    # CCA 约束：victim 接收到 src 的功率必须严格小于阈值。
    # received = base_rssi_at_victim + (p_src - current_src)
    # p_src <= current_src + threshold - guard - base_rssi_at_victim
    for victim_id, victim_state in ap_states.items():
        for src_id, base_rssi in victim_state.get("neighbor_rssi_dbm", {}).items():
            if src_id not in ap_states:
                continue
            current_src = _fget(ap_states[src_id], "tx_power_dbm", TX_POWER_MAX_DBM)
            cca_upper = current_src + CCA_THRESHOLD_DBM - CCA_GUARD_DB - float(base_rssi)
            if cca_upper < upper[src_id]:
                upper[src_id] = cca_upper
                binding[src_id]["upper_reasons"] = [f"cca_at_{victim_id}"]

    return lower, upper, binding


def compute_feasible_ranges(ap_states: dict) -> dict:
    """
    计算每个 AP 的连续 TX Power 可行区间。

    区间的下界来自法定最小功率和 STA RSSI 安全下界，上界来自当前功率、
    法定最大功率和 CCA 约束。SINR 是 AP 间耦合约束，返回为候选方案
    必须继续评估的全局约束，而不是单 AP 独立边界。
    """
    lower, upper, binding = _power_bounds(ap_states)
    ranges = {}
    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", TX_POWER_MAX_DBM)
        min_dbm = round(lower[ap_id], 3)
        max_dbm = round(upper[ap_id], 3)
        ranges[ap_id] = {
            "current_dbm": current,
            "min_dbm": min_dbm,
            "max_dbm": max_dbm,
            "feasible_individual_range": min_dbm <= max_dbm + OPT_TOLERANCE_DB,
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
        }

    max_cca_candidate = {
        ap_id: round(upper[ap_id], 3)
        for ap_id in ap_states
        if lower[ap_id] <= upper[ap_id] + OPT_TOLERANCE_DB
    }
    conservative_candidate = {
        ap_id: round((lower[ap_id] + upper[ap_id]) / 2, 3)
        for ap_id in ap_states
        if lower[ap_id] <= upper[ap_id] + OPT_TOLERANCE_DB
    }
    return {
        "ranges": ranges,
        "sinr_coupled": True,
        "candidate_hints": {
            "minimal_necessary_drop": max_cca_candidate,
            "conservative_mid_range": conservative_candidate,
        },
        "all_individual_ranges_feasible": all(
            item["feasible_individual_range"] for item in ranges.values()
        ),
        "notes": [
            "候选功率必须继续调用 evaluate_sr_candidate 验证 CCA/SINR/STA RSSI。",
            "max_dbm 不是建议值，只是单 AP 在当前 CCA 模型下的上界。",
            "Co-SR 通常应优先比较接近 max_dbm 的候选，以避免不必要地过度降功率。",
        ],
    }


def _sinr_feasible_seed(ap_states: dict, lower: dict, upper: dict) -> dict | None:
    """
    用固定点迭代求一个满足 SINR 下界的最低功率可行起点。

    SINR 约束可写成“本 AP 至少需要多少信号功率”。从下界开始反复
    抬高不满足 SINR 的 AP；若超过上界，说明当前约束不可行。
    """
    gamma = 10 ** (SINR_THRESHOLD_DB / 10)
    powers = {ap_id: lower[ap_id] for ap_id in ap_states}

    for _ in range(200):
        changed = False
        for ap_id, state in ap_states.items():
            noise_mw = _dbm_to_mw(_fget(state, "noise_floor_dbm", -90.0))
            interference_mw = noise_mw
            for nbr_id, base_rssi in state.get("neighbor_rssi_dbm", {}).items():
                if nbr_id not in ap_states:
                    continue
                delta_j = powers[nbr_id] - _fget(ap_states[nbr_id], "tx_power_dbm", 20.0)
                interference_mw += _dbm_to_mw(float(base_rssi) + delta_j)

            required_signal_dbm = _mw_to_dbm(gamma * interference_mw)
            current = _fget(state, "tx_power_dbm", 20.0)
            sta = _fget(state, "sta_rssi_dbm", -60.0)
            required_power = current + required_signal_dbm - sta
            next_power = max(lower[ap_id], required_power)

            if next_power > upper[ap_id] + OPT_TOLERANCE_DB:
                return None
            if next_power > powers[ap_id] + OPT_TOLERANCE_DB:
                powers[ap_id] = min(next_power, upper[ap_id])
                changed = True
        if not changed:
            break

    return powers if _is_feasible(ap_states, powers) else None


def _objective(ap_states: dict, powers: dict) -> float:
    """目标函数：最小化相对当前功率的平方调整量。"""
    total = 0.0
    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", 20.0)
        total += (powers[ap_id] - current) ** 2
    return total


def _is_feasible(ap_states: dict, powers: dict) -> bool:
    ok, _, _ = _check_all_constraints(ap_states, powers)
    return ok


def _clip_to_bounds(powers: dict, lower: dict, upper: dict) -> dict:
    return {
        ap_id: min(max(float(power), lower[ap_id]), upper[ap_id])
        for ap_id, power in powers.items()
    }


def _optimize_continuous_powers(ap_states: dict) -> tuple[dict | None, dict]:
    """
    连续 Co-SR 约束优化。

    目标：在满足 CCA / SINR / STA RSSI 的前提下，最小化所有 AP
    相对当前功率的平方调整量。由于 AP 数量很小，这里使用确定性的
    可行域模式搜索：在连续 dBm 空间中搜索，逐步收敛到
    OPT_TOLERANCE_DB，而不是用 1 dB 离散贪心。
    """
    lower, upper, binding = _power_bounds(ap_states)
    for ap_id in ap_states:
        if lower[ap_id] > upper[ap_id] + OPT_TOLERANCE_DB:
            return None, {
                "lower_bounds_dbm": lower,
                "upper_bounds_dbm": upper,
                "binding_bounds": binding,
                "error": f"{ap_id} lower bound exceeds upper bound",
            }

    seed = _sinr_feasible_seed(ap_states, lower, upper)
    if seed is None:
        return None, {
            "lower_bounds_dbm": lower,
            "upper_bounds_dbm": upper,
            "binding_bounds": binding,
            "error": "no feasible point satisfies SINR within CCA/STA bounds",
        }

    ap_ids = list(ap_states)
    directions: list[dict[str, float]] = []
    for ap_id in ap_ids:
        directions.append({ap_id: 1.0})
        directions.append({ap_id: -1.0})
    for inc in ap_ids:
        for dec in ap_ids:
            if inc != dec:
                directions.append({inc: 1.0, dec: -1.0})

    powers = dict(seed)
    best_obj = _objective(ap_states, powers)
    max_range = max((upper[ap] - lower[ap] for ap in ap_ids), default=1.0)
    step = max(0.5, max_range / 2)
    iterations = 0

    while step > OPT_TOLERANCE_DB and iterations < 5000:
        iterations += 1
        best_candidate = None
        best_candidate_obj = best_obj

        for direction in directions:
            candidate = dict(powers)
            for ap_id, sign in direction.items():
                candidate[ap_id] += sign * step
            candidate = _clip_to_bounds(candidate, lower, upper)

            if candidate == powers or not _is_feasible(ap_states, candidate):
                continue

            obj = _objective(ap_states, candidate)
            if obj + 1e-9 < best_candidate_obj:
                best_candidate = candidate
                best_candidate_obj = obj

        if best_candidate is None:
            step /= 2
        else:
            powers = best_candidate
            best_obj = best_candidate_obj

    return powers, {
        "lower_bounds_dbm": lower,
        "upper_bounds_dbm": upper,
        "binding_bounds": binding,
        "objective": "minimize_sum_squared_power_change_db",
        "objective_value": round(best_obj, 6),
        "optimality_tolerance_db": OPT_TOLERANCE_DB,
        "iterations": iterations,
        "solver": "deterministic_continuous_pattern_search",
    }


def _active_constraints(ap_id: str, detail: dict, powers: dict, lower: dict, upper: dict) -> list[str]:
    active: list[str] = []
    if abs(powers[ap_id] - lower[ap_id]) <= 0.02:
        active.append("lower_bound")
    if abs(powers[ap_id] - upper[ap_id]) <= 0.02:
        active.append("upper_bound")
    if abs(detail["cca_max_dbm"] - (CCA_THRESHOLD_DBM - CCA_GUARD_DB)) <= 0.05:
        active.append("cca")
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
    powers, meta = _optimize_continuous_powers(ap_states)
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
        optimal = round(powers[ap_id], 3)
        result[ap_id] = {
            "optimal_dbm":     optimal,
            # 兼容旧日志和 agent 文档字段；语义已改为连续最优值。
            "recommended_dbm": optimal,
            "current_dbm":     current,
            "delta_db":        round(optimal - current, 3),
            "active_constraints": _active_constraints(
                ap_id, validation[ap_id], powers, lower, upper
            ),
        }
    return result


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
    normalized = {
        ap_id: float(proposed_powers.get(ap_id, ap_states[ap_id].get("tx_power_dbm", 20.0)))
        for ap_id in ap_states
    }
    ok, errors, details = _check_all_constraints(ap_states, normalized)
    for item in details.values():
        item.pop("cca_detail", None)

    total_drop = 0.0
    max_drop = 0.0
    squared_change = 0.0
    min_sta_margin = 999.0
    max_cca = -200.0
    min_sinr = 999.0

    for ap_id, state in ap_states.items():
        current = _fget(state, "tx_power_dbm", 20.0)
        proposed = normalized[ap_id]
        drop = max(0.0, current - proposed)
        total_drop += drop
        max_drop = max(max_drop, drop)
        squared_change += (proposed - current) ** 2
        min_sta_margin = min(min_sta_margin, _sta_rssi_margin(ap_id, ap_states, normalized))
        max_cca = max(max_cca, details[ap_id]["cca_max_dbm"])
        min_sinr = min(min_sinr, details[ap_id]["sinr_db"])

    return {
        "valid": ok,
        "errors": errors,
        "proposed_powers": {ap_id: round(power, 3) for ap_id, power in normalized.items()},
        "score": {
            "total_power_drop_db": round(total_drop, 3),
            "max_single_ap_drop_db": round(max_drop, 3),
            "sum_squared_power_change_db": round(squared_change, 6),
            "min_sta_rssi_margin_db": round(min_sta_margin, 3),
            "max_cca_dbm": round(max_cca, 3),
            "min_sinr_db": round(min_sinr, 3),
        },
        "per_ap": details,
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
        balanced              优先合法，其次降低总体和单 AP 代价
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


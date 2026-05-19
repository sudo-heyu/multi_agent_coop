"""
Co-SR 计算工具

根据 AP 间 RSSI 感知干扰强度，扫描可行发射功率范围，
给出满足 CCA / SINR / STA-RSSI 三重约束的推荐 TX Power。

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
TX_POWER_MIN_DBM   = 0      # 扫描下界（dBm）
TX_POWER_STEP_DB   = 1      # 扫描步长（dBm）

# 干扰强度分级阈值
_INTERFERENCE_THRESHOLDS = [
    ("strong",   lambda r: r >= -70.0),
    ("moderate", lambda r: r >= -80.0),
    ("weak",     lambda r: True),
]


# ------------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# 2. 量化计算 — 约束检查与功率扫描
# ------------------------------------------------------------------

def _cca_at_ap(target_ap_id: str, ap_states: dict, proposed_powers: dict) -> dict:
    """
    计算目标 AP 在邻居使用 proposed_powers 时所受的 CCA 干扰。

    Returns:
        {
            "ap2": {"received_dbm": -70.5, "ok": True},
            "max_received_dbm": -70.5,
            "ok": True,
        }
    """
    neighbor_rssi = ap_states[target_ap_id].get("neighbor_rssi_dbm", {})
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


def scan_feasible_powers(ap_states: dict) -> dict:
    """
    从当前最高功率向下逐步扫描，找满足全部约束的最高统一功率。

    返回最高可行功率而非最低，是为了在满足干扰约束的前提下保留最大覆盖能力。

    Returns:
        {
            "feasible": bool,
            "recommended_uniform_dbm": float | None,
            "current_max_power_dbm": float,
            "binding_constraint": "cca" | "sinr" | "sta_rssi" | None,
            "scan_log": [{"power_dbm": ..., "ok": ..., "violations": [...]}, ...],
        }
    """
    current_max = max(
        state.get("tx_power_dbm", 20.0) for state in ap_states.values()
    )

    scan_log = []
    recommended = None
    binding_constraint = None

    power = current_max
    while power >= TX_POWER_MIN_DBM:
        proposed = {ap_id: power for ap_id in ap_states}
        ok, violations, details = _check_all_constraints(ap_states, proposed)
        scan_log.append({
            "power_dbm":  power,
            "ok":         ok,
            "violations": violations,
        })

        if ok:
            recommended = power
            break

        # 记录绑定约束类型（首次出现）
        if binding_constraint is None and violations:
            v = violations[0]
            if "CCA" in v:
                binding_constraint = "cca"
            elif "SINR" in v:
                binding_constraint = "sinr"
            elif "STA RSSI" in v:
                binding_constraint = "sta_rssi"

        power -= TX_POWER_STEP_DB

    return {
        "feasible":               recommended is not None,
        "recommended_uniform_dbm": recommended,
        "current_max_power_dbm":   current_max,
        "binding_constraint":      binding_constraint,
        "scan_log":                scan_log,
    }


# ------------------------------------------------------------------
# 3. 公开接口
# ------------------------------------------------------------------

def recommend_tx_power(ap_states: dict) -> dict:
    """
    为每个 AP 给出推荐 TX Power（基于统一功率扫描结果）。

    Args:
        ap_states: 各 AP 当前完整状态

    Returns:
        {
            "ap1": {"recommended_dbm": 4.0, "current_dbm": 16.0, "delta_db": -12.0},
            ...
        }
    """
    scan = scan_feasible_powers(ap_states)
    recommended = scan["recommended_uniform_dbm"]
    if recommended is None:
        recommended = TX_POWER_MIN_DBM

    result = {}
    for ap_id, state in ap_states.items():
        current = state.get("tx_power_dbm", 20.0)
        result[ap_id] = {
            "recommended_dbm": recommended,
            "current_dbm":     current,
            "delta_db":        round(recommended - current, 1),
        }
    return result


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


def compute_all(ap_states: dict) -> dict:
    """
    主入口：完整 Co-SR 计算，供 orchestrator 注入提案阶段。

    Args:
        ap_states: 与 get_all_states() / AP_STATE mock 相同格式

    Returns:
        {
            "interference_matrix": {
                "ap1->ap2": {"rssi_dbm": -68.0, "level": "strong"},
                ...
            },
            "feasible": bool,
            "recommended_uniform_dbm": float | None,
            "binding_constraint": "cca" | "sinr" | "sta_rssi" | None,
            "recommendations": {
                "ap1": {"recommended_dbm": 4.0, "current_dbm": 16.0, "delta_db": -12.0},
                ...
            },
            "validation": {
                "ap1": {
                    "proposed_power_dbm": 4.0,
                    "cca_max_dbm": -85.3, "cca_ok": True,
                    "sinr_db": 26.9,      "sinr_ok": True,
                    "sta_rssi_dbm": -67.0,"sta_rssi_ok": True,
                    "valid": True, "errors": []
                },
                ...
            },
        }
    """
    matrix = compute_interference_matrix(ap_states)
    scan   = scan_feasible_powers(ap_states)
    recs   = recommend_tx_power(ap_states)

    rec_power = scan["recommended_uniform_dbm"]
    proposed = {ap_id: (rec_power if rec_power is not None else TX_POWER_MIN_DBM)
                for ap_id in ap_states}
    _, _, validation = _check_all_constraints(ap_states, proposed)

    return {
        "interference_matrix":    matrix,
        "feasible":               scan["feasible"],
        "recommended_uniform_dbm": rec_power,
        "binding_constraint":     scan["binding_constraint"],
        "recommendations":        recs,
        "validation":             validation,
    }

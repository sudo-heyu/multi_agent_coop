"""
决策验证器 — 对 LLM 输出的最终 JSON 决策做确定性观测验收。

验收分三层：
  1. 参数范围检查：proposed 值在合法区间内（始终执行）
  2. 参数生效检查：观测值与 proposed 吻合（仅真实观测时执行）
  3. KPI 达标检查：根据策略和协商前状态动态选取指标，
                   先检查绝对上/下限，再对"协商前处于差状态"的指标检查是否改善
                   （仅真实观测时执行，mock 模式跳过以避免对协商前坏状态误报）
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# 硬性参数约束
# ─────────────────────────────────────────────────────────────────────────────
TX_POWER_MIN_DBM = 1.0
TX_POWER_MAX_DBM = 23.0
TX_POWER_APPLIED_TOLERANCE_DB = 0.5

EDCA_LIMITS = {
    "CWmin": (3, 1023),
    "CWmax": (7, 1023),
    "AIFSN": (1, 15),
}

# ─────────────────────────────────────────────────────────────────────────────
# KPI 指标规则表
#
# direction       : "lower" = 越低越好 | "higher" = 越高越好
# abs_max/abs_min : 绝对上/下界，观测值超出即报错（真实观测时检查）
# bad_above       : lower 类指标"协商前处于差状态"的判定阈值
# bad_below       : higher 类指标"协商前处于差状态"的判定阈值
# min_rel_improvement : 协商前处于差状态时，要求的最低相对改善比例
# min_abs_improvement : 同上，以绝对量计（dBm 等对数域指标使用）
# max_abs_degradation : higher 类指标允许的最大绝对劣化量
# max_rel_degradation : higher 类指标允许的最大相对劣化比例
# optional        : True 时指标缺失不报硬错（仅警告）
# ─────────────────────────────────────────────────────────────────────────────
_METRIC_RULES: dict[str, dict] = {
    # Co-SR 关注：信号质量
    "sta_rssi_dbm": {
        "label": "STA RSSI", "unit": "dBm", "direction": "higher",
        "abs_min": -75.0,
        # Co-SR 降功后 RSSI 线性下降是预期行为，不限制降幅；仅守住绝对下界
    },
    "max_neighbor_rssi_dbm": {       # 派生指标：max(neighbor_rssi_dbm.values())
        "label": "Max Neighbor RSSI", "unit": "dBm", "direction": "lower",
        "abs_max": -70.0,             # 降至触发阈值以下为理想
        "bad_above": -70.0,
        "min_abs_improvement": 2.0,   # 协商前超阈值时，期望至少降低 2 dBm
    },
    # Co-EDCA 关注：信道拥塞
    "channel_busy_ratio": {
        "label": "Channel Busy Ratio", "unit": "", "direction": "lower",
        "abs_max": 0.75,
        "bad_above": 0.60,
        "min_rel_improvement": 0.10,
    },
    "tx_retries_ratio": {
        "label": "TX Retries Ratio", "unit": "", "direction": "lower",
        "abs_max": 0.25,
        "bad_above": 0.15,
        "min_rel_improvement": 0.10,
    },
    # 通用 QoS（可选，有则检查）
    "latency_ms": {
        "label": "Latency", "unit": "ms", "direction": "lower",
        "abs_max": 500.0,
        "bad_above": 200.0,
        "min_rel_improvement": 0.05,
        "optional": True,
    },
    "packet_loss_pct": {
        "label": "Packet Loss", "unit": "%", "direction": "lower",
        "abs_max": 2.0,
        "bad_above": 1.0,
        "min_rel_improvement": 0.05,
        "optional": True,
    },
    "throughput_mbps": {
        "label": "Throughput", "unit": "Mbps", "direction": "higher",
        "abs_min": None,
        "max_rel_degradation": 0.10,  # 不允许吞吐量下降超过 10%
        "optional": True,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# 策略 → 关注指标（按重要性排列）
# required 指标缺失时报错；optional 指标缺失时忽略
# ─────────────────────────────────────────────────────────────────────────────
_STRATEGY_METRICS: dict[str, list[str]] = {
    "co_sr":   ["sta_rssi_dbm", "max_neighbor_rssi_dbm",
                "packet_loss_pct", "latency_ms"],
    "co_edca": ["channel_busy_ratio", "tx_retries_ratio",
                "latency_ms", "packet_loss_pct", "throughput_mbps"],
    "joint":   ["sta_rssi_dbm", "max_neighbor_rssi_dbm",
                "channel_busy_ratio", "tx_retries_ratio",
                "latency_ms", "packet_loss_pct", "throughput_mbps"],
}


# ─────────────────────────────────────────────────────────────────────────────
# 公开接口
# ─────────────────────────────────────────────────────────────────────────────

def validate_decision(
    ap_state: dict,
    decision: dict | None,
    strategy: str,
    observed_state: dict | None = None,
    observed_is_real: bool = False,
) -> dict:
    """
    对最终决策 JSON 执行确定性观测验收。

    Args:
        ap_state:        各 AP 协商前实测状态
        decision:        LLM 输出的决策字典；None 表示解析失败
        strategy:        "co_sr" | "co_edca" | "joint"
        observed_state:  观测周期结束后重新采集的 AP 状态；None 时回退为 ap_state
        observed_is_real: True 表示 observed_state 来自真实二次采集，
                          才执行参数生效检查和 KPI 改善检查；
                          False（mock/无采集器）时跳过这两项，仅检查参数范围合法性

    Returns:
        标准 ValidationReport dict
    """
    if decision is None:
        return _fail_report(strategy, "LLM 未输出合法 JSON，无法执行验证")

    ap_ids = list(ap_state.keys())
    obs = observed_state if observed_state is not None else ap_state
    per_ap: dict[str, dict] = {}
    global_errors: list[str] = []

    # ── 规范化决策 key ────────────────────────────────────────────────────────
    normalized = _normalize_decision_keys(decision, ap_ids)
    missing = [ap for ap in ap_ids if ap not in normalized]
    if missing:
        global_errors.append(f"决策缺少以下 AP 的参数: {missing}")
        return _build_report(strategy, True, per_ap, global_errors)

    # ── 层 1 + 2：Co-SR 参数范围 & 生效检查 ──────────────────────────────────
    if strategy in ("co_sr", "joint"):
        for ap_id in ap_ids:
            report = per_ap.setdefault(ap_id, _empty_ap_entry())
            entry = normalized[ap_id]
            pwr = entry.get("tx_power_dbm")
            ap_errors: list[str] = []

            if pwr is None:
                ap_errors.append("缺少 tx_power_dbm")
            else:
                pwr = float(pwr)
                report["proposed_params"]["tx_power_dbm"] = pwr
                if not (TX_POWER_MIN_DBM <= pwr <= TX_POWER_MAX_DBM):
                    ap_errors.append(
                        f"tx_power_dbm={pwr} 超出合法范围 "
                        f"[{TX_POWER_MIN_DBM}, {TX_POWER_MAX_DBM}] dBm"
                    )
                if observed_is_real:
                    observed_pwr = obs.get(ap_id, {}).get("tx_power_dbm")
                    report["observed_params"]["tx_power_dbm"] = observed_pwr
                    if observed_pwr is None:
                        ap_errors.append("观测结果缺少 tx_power_dbm")
                    elif abs(float(observed_pwr) - pwr) > TX_POWER_APPLIED_TOLERANCE_DB:
                        ap_errors.append(
                            f"tx_power_dbm 未生效：期望 {pwr} dBm，观测 {observed_pwr} dBm"
                        )

            report["errors"].extend([f"{ap_id.upper()}: {e}" for e in ap_errors])
            report["checks"].append({
                "check": "Co-SR params",
                "ok": len(ap_errors) == 0,
                "errors": [f"{ap_id.upper()}: {e}" for e in ap_errors],
            })

    # ── 层 1 + 2：Co-EDCA 参数范围 & 生效检查 ────────────────────────────────
    if strategy in ("co_edca", "joint"):
        for ap_id in ap_ids:
            report = per_ap.setdefault(ap_id, _empty_ap_entry())
            params_raw = normalized[ap_id]
            edca_params = {k: params_raw.get(k) for k in ("CWmin", "CWmax", "AIFSN")}
            missing_keys = [k for k, v in edca_params.items() if v is None]

            if missing_keys:
                err = f"{ap_id.upper()}: 缺少 EDCA 参数 {missing_keys}"
                global_errors.append(err)
                report["errors"].append(err)
                report["checks"].append({"check": "Co-EDCA params", "ok": False, "errors": [err]})
                continue

            edca_params_int = {k: int(v) for k, v in edca_params.items()}
            report["proposed_params"].update(edca_params_int)
            edca_errors = _validate_edca_range(edca_params_int)

            if observed_is_real:
                observed_edca = _observed_edca_params(obs.get(ap_id, {}))
                has_edca_obs = any(v is not None for v in observed_edca.values())
                if has_edca_obs:
                    # AP 上报了 EDCA 参数（从 hostapd 回读）→ 检查是否生效
                    report["observed_params"].update(
                        {k: v for k, v in observed_edca.items() if v is not None}
                    )
                    for key, expected in edca_params_int.items():
                        actual = observed_edca.get(key)
                        if actual is None:
                            edca_errors.append(f"观测结果缺少 {key}")
                        elif actual != expected:
                            edca_errors.append(f"{key} 未生效：期望 {expected}，观测 {actual}")
                # else: AP 未上报 EDCA 观测值（典型情况），跳过生效检查

            report["errors"].extend([f"{ap_id.upper()}: {e}" for e in edca_errors])
            report["checks"].append({
                "check": "Co-EDCA params",
                "ok": len(edca_errors) == 0,
                "errors": [f"{ap_id.upper()}: {e}" for e in edca_errors],
            })

    # ── 层 3：KPI 达标检查（仅真实观测时执行）────────────────────────────────
    if observed_is_real:
        kpi_metrics = _STRATEGY_METRICS.get(strategy, [])
        for ap_id in ap_ids:
            report = per_ap.setdefault(ap_id, _empty_ap_entry())
            before = ap_state.get(ap_id, {})
            after = obs.get(ap_id, {})

            kpi_errors: list[str] = []
            kpi_details: dict[str, dict] = {}

            for metric in kpi_metrics:
                rule = _METRIC_RULES.get(metric)
                if rule is None:
                    continue

                before_val = _get_metric(before, metric)
                after_val = _get_metric(after, metric)

                errs = _check_metric(metric, before_val, after_val, rule)
                kpi_errors.extend(errs)
                kpi_details[metric] = {
                    "before": before_val,
                    "after":  after_val,
                    "errors": errs,
                }
                if after_val is not None:
                    report["observed_params"][metric] = after_val

            report["errors"].extend([f"{ap_id.upper()}: {e}" for e in kpi_errors])
            report["checks"].append({
                "check":   "KPI",
                "ok":      len(kpi_errors) == 0,
                "details": kpi_details,
                "errors":  [f"{ap_id.upper()}: {e}" for e in kpi_errors],
            })

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    for ap_id in ap_ids:
        entry = per_ap.setdefault(ap_id, _empty_ap_entry())
        entry["valid"] = len(entry["errors"]) == 0
        global_errors.extend(entry["errors"])

    return _build_report(strategy, True, per_ap, global_errors)


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────────────────────────────────────

def _get_metric(state: dict, metric: str) -> float | None:
    """从 AP 状态提取指标值；支持 max_neighbor_rssi_dbm 等派生指标。"""
    if metric == "max_neighbor_rssi_dbm":
        nbr = state.get("neighbor_rssi_dbm", {})
        return max(nbr.values()) if nbr else None
    val = state.get(metric)
    return float(val) if val is not None else None


def _check_metric(
    metric: str,
    before: float | None,
    after: float | None,
    rule: dict,
) -> list[str]:
    """
    对单个 KPI 指标执行绝对限值检查 + 改善检查。

    改善检查仅在 before 不为 None 且指标协商前处于"差状态"时触发。
    """
    errors: list[str] = []
    label    = rule["label"]
    unit     = rule.get("unit", "")
    direction = rule["direction"]
    optional  = rule.get("optional", False)

    if after is None:
        if not optional:
            errors.append(f"观测结果缺少 {label}")
        return errors

    # ── 绝对限值 ──────────────────────────────────────────────────────────────
    if direction == "lower":
        abs_max = rule.get("abs_max")
        if abs_max is not None and after > abs_max:
            errors.append(f"{label}={after}{unit} 超过上限 {abs_max}")
    else:  # higher
        abs_min = rule.get("abs_min")
        if abs_min is not None and after < abs_min:
            errors.append(f"{label}={after}{unit} 低于下限 {abs_min}")

    if before is None:
        return errors

    # ── 改善检查（需要 before 值）─────────────────────────────────────────────
    if direction == "lower":
        bad_above = rule.get("bad_above")
        if bad_above is not None and before > bad_above:
            if "min_abs_improvement" in rule:
                # 对数域指标（dBm）：用绝对量衡量
                improvement = before - after
                min_imp = rule["min_abs_improvement"]
                if improvement < min_imp:
                    errors.append(
                        f"{label} 改善不足：{before:.1f}→{after:.1f}{unit}，"
                        f"改善 {improvement:.1f}，期望 ≥{min_imp:.1f}"
                    )
            elif "min_rel_improvement" in rule and before != 0:
                # 比率指标：用相对量衡量
                actual_pct = (before - after) / before
                min_pct    = rule["min_rel_improvement"]
                if actual_pct < min_pct:
                    errors.append(
                        f"{label} 改善不足：{before:.3f}→{after:.3f}，"
                        f"实际改善 {actual_pct * 100:.1f}%，期望 ≥{min_pct * 100:.0f}%"
                    )
    else:  # higher
        if "max_abs_degradation" in rule:
            degradation = before - after
            max_deg = rule["max_abs_degradation"]
            if degradation > max_deg:
                errors.append(
                    f"{label} 下降过多：{before:.1f}→{after:.1f}{unit}，"
                    f"下降 {degradation:.1f}，容忍值 {max_deg}"
                )
        if "max_rel_degradation" in rule and before > 0:
            degradation_pct = (before - after) / before
            max_deg_pct = rule["max_rel_degradation"]
            if degradation_pct > max_deg_pct:
                errors.append(
                    f"{label} 下降过多：{before:.1f}→{after:.1f}{unit}，"
                    f"下降 {degradation_pct * 100:.1f}%，容忍值 {max_deg_pct * 100:.0f}%"
                )

    return errors


def _normalize_decision_keys(decision: dict, ap_ids: list[str]) -> dict:
    """将决策 dict 的 key 统一转为小写，兼容 "AP1" / "ap1" 两种写法。"""
    normalized = {}
    for k, v in decision.items():
        lower_k = k.lower()
        if lower_k in ap_ids:
            normalized[lower_k] = v if isinstance(v, dict) else {}
    return normalized


def _empty_ap_entry() -> dict:
    return {
        "proposed_params": {},
        "observed_params": {},
        "checks": [],
        "valid": False,
        "errors": [],
    }


def _validate_edca_range(params: dict) -> list[str]:
    errors: list[str] = []
    for key, (lo, hi) in EDCA_LIMITS.items():
        val = params.get(key)
        if val is None:
            errors.append(f"{key} 缺失")
        elif not (lo <= val <= hi):
            errors.append(f"{key}={val} 超出范围 [{lo}, {hi}]")
    cwmin = params.get("CWmin", 0)
    cwmax = params.get("CWmax", 0)
    if cwmax <= cwmin:
        errors.append(f"CWmax={cwmax} 必须大于 CWmin={cwmin}")
    return errors


def _observed_edca_params(observed: dict) -> dict:
    return {
        "CWmin": observed.get("cwmin"),
        "CWmax": observed.get("cwmax"),
        "AIFSN": observed.get("aifsn"),
    }


def _fail_report(strategy: str, reason: str) -> dict:
    return {
        "approved":      False,
        "strategy":      strategy,
        "parse_ok":      False,
        "per_ap":        {},
        "global_errors": [reason],
        "summary":       f"验证失败：{reason}",
    }


def _build_report(
    strategy: str,
    parse_ok: bool,
    per_ap: dict,
    global_errors: list[str],
) -> dict:
    approved = len(global_errors) == 0 and all(
        v.get("valid", False) for v in per_ap.values()
    )
    if approved:
        summary = f"验证通过（策略={strategy}，所有 AP 参数合规）"
    else:
        n = len(global_errors)
        summary = (
            f"验证失败（策略={strategy}，{n} 项错误）："
            f"{'; '.join(global_errors[:3])}"
        )
    return {
        "approved":      approved,
        "strategy":      strategy,
        "parse_ok":      parse_ok,
        "per_ap":        per_ap,
        "global_errors": global_errors,
        "summary":       summary,
    }

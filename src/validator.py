"""
决策验证器 — 对 LLM 输出的最终 JSON 决策做确定性下发验收。

验收分三层：
  1. 参数范围检查：proposed 值在合法区间内（始终执行）
  2. 参数生效检查：观测值与 proposed 吻合（仅真实观测时执行）
  3. QoS 检查：真实观测时，SR 要求聚合 QoS 不下降；EDCA 允许低优先级
     业务在受控范围内让路，但要求高优先级业务获得可验证收益

只有最终参数合法、真实观测时确认已正常下发，且 QoS 策略核验通过，才判定通过。
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# 硬性参数约束
# ─────────────────────────────────────────────────────────────────────────────
TX_POWER_MIN_DBM = 8.0
TX_POWER_MAX_DBM = 23.0
TX_POWER_APPLIED_TOLERANCE_DB = 0.5
# Co-SR 功率调整量（相对协商前功率）必须为整数 dB
TX_POWER_DELTA_INTEGER_TOLERANCE_DB = 0.05

EDCA_LIMITS = {
    "CWmin": (3, 1023),
    "CWmax": (7, 1023),
    "AIFSN": (1, 15),
}

# QoS 硬性验收阈值。保留很小容忍度，避免浮点/采样抖动导致等价状态误判。
QOS_THROUGHPUT_DROP_TOLERANCE_RATIO = 0.01   # 聚合吞吐下降超过 1% 即失败
QOS_LATENCY_INCREASE_TOLERANCE_RATIO = 0.05  # 平均时延升高超过 5% 即失败
QOS_PACKET_LOSS_INCREASE_TOLERANCE_PCT = 0.1 # 平均丢包率升高超过 0.1 个百分点即失败

# EDCA 是优先级调度：允许低优先级业务轻微让路，但必须守住整体退化上限，
# 且 high-priority 业务不能受损并至少有一个核心 QoS 指标改善。
EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO = 0.05
EDCA_OVERALL_PACKET_LOSS_TOLERANCE_PCT = 5.0
EDCA_HIGH_THROUGHPUT_DROP_TOLERANCE_RATIO = 0.01
EDCA_HIGH_LATENCY_INCREASE_TOLERANCE_RATIO = 0.05
EDCA_HIGH_PACKET_LOSS_TOLERANCE_PCT = 1.0
EDCA_HIGH_THROUGHPUT_GAIN_RATIO = 0.005
EDCA_HIGH_LATENCY_GAIN_RATIO = 0.01
EDCA_HIGH_PACKET_LOSS_GAIN_PCT = 0.1

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
    对最终决策 JSON 执行确定性下发验收。

    Args:
        ap_state:        各 AP 协商前实测状态
        decision:        LLM 输出的决策字典；None 表示解析失败
        strategy:        "co_sr" | "co_edca"
        observed_state:  观测周期结束后重新采集的 AP 状态；None 时回退为 ap_state
        observed_is_real: True 表示 observed_state 来自真实二次采集，
                          才执行参数生效检查；
                          False（无二次采集）时仅检查参数范围合法性

    Returns:
        标准 ValidationReport dict
    """
    if decision is None:
        return _fail_report(strategy, "LLM 未输出合法 JSON，无法执行验证")
    if strategy not in ("co_sr", "co_edca"):
        return _fail_report(strategy, f"不支持的策略 {strategy!r}；仅允许 co_sr 或 co_edca")

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
    if strategy == "co_sr":
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
                # 功率调整量必须为整数 dB（相对协商前功率）
                current_pwr = ap_state.get(ap_id, {}).get("tx_power_dbm")
                if current_pwr is not None:
                    delta = pwr - float(current_pwr)
                    if abs(delta - round(delta)) > TX_POWER_DELTA_INTEGER_TOLERANCE_DB:
                        ap_errors.append(
                            f"功率调整量 {delta:+.2f} dB 必须为整数 dB"
                            f"（当前 {current_pwr} → 提案 {pwr}）"
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
    if strategy == "co_edca":
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

    # ── 汇总 ─────────────────────────────────────────────────────────────────
    if observed_is_real:
        qos_errors, qos_report = _validate_qos_policy(ap_state, obs, strategy)
        if qos_report:
            per_ap.setdefault("_qos", _empty_ap_entry())
            per_ap["_qos"]["checks"].append(qos_report)
            per_ap["_qos"]["valid"] = len(qos_errors) == 0
            per_ap["_qos"]["errors"].extend(qos_errors)
        global_errors.extend(qos_errors)

    for ap_id in ap_ids:
        entry = per_ap.setdefault(ap_id, _empty_ap_entry())
        entry["valid"] = len(entry["errors"]) == 0
        global_errors.extend(entry["errors"])

    return _build_report(strategy, True, per_ap, global_errors)


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助
# ─────────────────────────────────────────────────────────────────────────────


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


def _num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _ap_throughput_total(state: dict) -> float | None:
    vals = [
        _num(state.get("throughput_mbps_iperf")),
        _num(state.get("throughput_mbps_user")),
    ]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals)


def _avg_metric(ap_states: dict, field: str) -> float | None:
    vals = [
        _num(state.get(field))
        for state in ap_states.values()
        if isinstance(state, dict)
    ]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _sum_throughput(ap_states: dict) -> float | None:
    vals = [
        _ap_throughput_total(state)
        for state in ap_states.values()
        if isinstance(state, dict)
    ]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals)


def _priority_states(ap_states: dict, priority: str) -> dict:
    return {
        ap_id: state
        for ap_id, state in ap_states.items()
        if isinstance(state, dict) and state.get("traffic_priority") == priority
    }


def _qos_non_regression_errors(before: dict, after: dict) -> tuple[list[str], dict]:
    """真实 APPLY 后的 QoS 不得劣于协商前状态。"""
    errors: list[str] = []
    details: dict = {}

    before_tput = _sum_throughput(before)
    after_tput = _sum_throughput(after)
    details["throughput_mbps_total_before"] = before_tput
    details["throughput_mbps_total_after"] = after_tput
    if before_tput is not None and after_tput is not None and before_tput > 0:
        drop_ratio = (before_tput - after_tput) / before_tput
        details["throughput_drop_ratio"] = round(drop_ratio, 6)
        if drop_ratio > QOS_THROUGHPUT_DROP_TOLERANCE_RATIO:
            errors.append(
                "QoS 下降：聚合吞吐从 "
                f"{before_tput:.3f} Mbps 降到 {after_tput:.3f} Mbps "
                f"（下降 {drop_ratio * 100:.2f}%）"
            )

    before_latency = _avg_metric(before, "latency_ms")
    after_latency = _avg_metric(after, "latency_ms")
    details["latency_ms_avg_before"] = before_latency
    details["latency_ms_avg_after"] = after_latency
    if before_latency is not None and after_latency is not None and before_latency > 0:
        increase_ratio = (after_latency - before_latency) / before_latency
        details["latency_increase_ratio"] = round(increase_ratio, 6)
        if increase_ratio > QOS_LATENCY_INCREASE_TOLERANCE_RATIO:
            errors.append(
                "QoS 下降：平均时延从 "
                f"{before_latency:.3f} ms 升到 {after_latency:.3f} ms "
                f"（升高 {increase_ratio * 100:.2f}%）"
            )

    before_loss = _avg_metric(before, "packet_loss_pct")
    after_loss = _avg_metric(after, "packet_loss_pct")
    details["packet_loss_pct_avg_before"] = before_loss
    details["packet_loss_pct_avg_after"] = after_loss
    if before_loss is not None and after_loss is not None:
        increase_pct = after_loss - before_loss
        details["packet_loss_increase_pct_points"] = round(increase_pct, 6)
        if increase_pct > QOS_PACKET_LOSS_INCREASE_TOLERANCE_PCT:
            errors.append(
                "QoS 下降：平均丢包率从 "
                f"{before_loss:.3f}% 升到 {after_loss:.3f}% "
                f"（增加 {increase_pct:.3f} 个百分点）"
            )

    return errors, details


def _validate_qos_policy(before: dict, after: dict, strategy: str) -> tuple[list[str], dict]:
    strict_errors, strict_details = _qos_non_regression_errors(before, after)
    if strategy != "co_edca" or not strict_errors:
        return strict_errors, {
            "check": "QoS non-regression",
            "ok": len(strict_errors) == 0,
            "errors": strict_errors,
            "details": strict_details,
        }

    edca_errors, edca_details = _validate_edca_priority_qos(before, after)
    if not edca_errors:
        return [], {
            "check": "EDCA priority-aware QoS",
            "ok": True,
            "errors": [],
            "details": {
                "strict_non_regression_errors": strict_errors,
                **edca_details,
            },
        }
    return strict_errors + edca_errors, {
        "check": "EDCA priority-aware QoS",
        "ok": False,
        "errors": strict_errors + edca_errors,
        "details": {
            "strict_non_regression_errors": strict_errors,
            "priority_errors": edca_errors,
            **edca_details,
        },
    }


def _validate_edca_priority_qos(before: dict, after: dict) -> tuple[list[str], dict]:
    errors: list[str] = []
    details: dict = {}

    before_tput = _sum_throughput(before)
    after_tput = _sum_throughput(after)
    if before_tput is not None and after_tput is not None and before_tput > 0:
        drop_ratio = (before_tput - after_tput) / before_tput
        details["overall_throughput_drop_ratio"] = round(drop_ratio, 6)
        if drop_ratio > EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO:
            errors.append(
                "EDCA 整体退化超限：聚合吞吐下降 "
                f"{drop_ratio * 100:.2f}% > {EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO * 100:.2f}%"
            )
    else:
        errors.append("EDCA 缺少聚合吞吐指标")

    before_latency = _avg_metric(before, "latency_ms")
    after_latency = _avg_metric(after, "latency_ms")
    if before_latency is not None and after_latency is not None and before_latency > 0:
        increase_ratio = (after_latency - before_latency) / before_latency
        details["overall_latency_increase_ratio"] = round(increase_ratio, 6)
        if increase_ratio > EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO:
            errors.append(
                "EDCA 整体退化超限：平均时延升高 "
                f"{increase_ratio * 100:.2f}% > {EDCA_OVERALL_DEGRADATION_TOLERANCE_RATIO * 100:.2f}%"
            )
    else:
        errors.append("EDCA 缺少平均时延指标")

    before_loss = _avg_metric(before, "packet_loss_pct")
    after_loss = _avg_metric(after, "packet_loss_pct")
    if before_loss is not None and after_loss is not None:
        increase_pct = after_loss - before_loss
        details["overall_packet_loss_increase_pct_points"] = round(increase_pct, 6)
        if increase_pct > EDCA_OVERALL_PACKET_LOSS_TOLERANCE_PCT:
            errors.append(
                "EDCA 整体退化超限：平均丢包率增加 "
                f"{increase_pct:.3f} 个百分点 > {EDCA_OVERALL_PACKET_LOSS_TOLERANCE_PCT:.3f}"
            )
    else:
        errors.append("EDCA 缺少平均丢包指标")

    high_before = _priority_states(before, "high")
    high_after = _priority_states(after, "high")
    if not high_before or not high_after:
        errors.append("EDCA 缺少 high-priority 业务样本")
        details["high_priority_sample_count_before"] = len(high_before)
        details["high_priority_sample_count_after"] = len(high_after)
        return errors, details

    high_tput_gain_ratio = None
    high_before_tput = _sum_throughput(high_before)
    high_after_tput = _sum_throughput(high_after)
    if high_before_tput is not None and high_after_tput is not None and high_before_tput > 0:
        high_tput_gain_ratio = (high_after_tput - high_before_tput) / high_before_tput
        details["high_priority_throughput_gain_ratio"] = round(high_tput_gain_ratio, 6)
        if high_tput_gain_ratio < -EDCA_HIGH_THROUGHPUT_DROP_TOLERANCE_RATIO:
            errors.append(f"EDCA high-priority 吞吐受损：{high_tput_gain_ratio * 100:.2f}%")
    else:
        errors.append("EDCA 缺少 high-priority 吞吐指标")

    high_latency_gain_ratio = None
    high_before_latency = _avg_metric(high_before, "latency_ms")
    high_after_latency = _avg_metric(high_after, "latency_ms")
    if high_before_latency is not None and high_after_latency is not None and high_before_latency > 0:
        high_latency_gain_ratio = (high_before_latency - high_after_latency) / high_before_latency
        details["high_priority_latency_gain_ratio"] = round(high_latency_gain_ratio, 6)
        if high_latency_gain_ratio < -EDCA_HIGH_LATENCY_INCREASE_TOLERANCE_RATIO:
            errors.append(f"EDCA high-priority 时延受损：{-high_latency_gain_ratio * 100:.2f}%")
    else:
        errors.append("EDCA 缺少 high-priority 时延指标")

    high_loss_delta = None
    high_before_loss = _avg_metric(high_before, "packet_loss_pct")
    high_after_loss = _avg_metric(high_after, "packet_loss_pct")
    if high_before_loss is not None and high_after_loss is not None:
        high_loss_delta = high_after_loss - high_before_loss
        details["high_priority_packet_loss_delta_pct_points"] = round(high_loss_delta, 6)
        if high_loss_delta > EDCA_HIGH_PACKET_LOSS_TOLERANCE_PCT:
            errors.append(f"EDCA high-priority 丢包受损：增加 {high_loss_delta:.3f} 个百分点")
    else:
        errors.append("EDCA 缺少 high-priority 丢包指标")

    has_high_gain = (
        (high_tput_gain_ratio is not None and high_tput_gain_ratio >= EDCA_HIGH_THROUGHPUT_GAIN_RATIO)
        or (high_latency_gain_ratio is not None and high_latency_gain_ratio >= EDCA_HIGH_LATENCY_GAIN_RATIO)
        or (high_loss_delta is not None and high_loss_delta <= -EDCA_HIGH_PACKET_LOSS_GAIN_PCT)
    )
    details["high_priority_has_gain"] = has_high_gain
    if not has_high_gain:
        errors.append("EDCA high-priority 未出现足够明确的吞吐、时延或丢包收益")

    return errors, details


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

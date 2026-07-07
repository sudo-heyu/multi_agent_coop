"""
决策验证器 — 对 LLM 输出的最终 JSON 决策做确定性下发验收。

验收分三层：
  1. 参数范围检查：proposed 值在合法区间内（始终执行）
  2. 参数生效检查：观测值与 proposed 吻合（仅真实观测时执行）

KPI 指标不再作为 Validator 的通过条件。
只要最终参数合法且真实观测时确认已正常下发，即判定通过。
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# 硬性参数约束
# ─────────────────────────────────────────────────────────────────────────────
TX_POWER_MIN_DBM = 1.0
TX_POWER_MAX_DBM = 23.0
TX_POWER_APPLIED_TOLERANCE_DB = 0.5
# Co-SR 功率调整量（相对协商前功率）必须为整数 dB
TX_POWER_DELTA_INTEGER_TOLERANCE_DB = 0.05
# 协议级 Co-SR：OBSS_PD 门限的合法 SR 窗口与耦合约束
OBSS_PD_MIN_DBM = -82.0
OBSS_PD_MAX_DBM = -62.0
OBSS_PD_APPLIED_TOLERANCE_DB = 0.5
# 标准耦合 tx ≤ TX_POWER_MAX-(OBSS_PD-OBSS_PD_MIN) 的判定容差
OBSS_PD_COUPLING_TOLERANCE_DB = 0.5

EDCA_LIMITS = {
    "CWmin": (3, 1023),
    "CWmax": (7, 1023),
    "AIFSN": (1, 15),
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
    对最终决策 JSON 执行确定性下发验收。

    Args:
        ap_state:        各 AP 协商前实测状态
        decision:        LLM 输出的决策字典；None 表示解析失败
        strategy:        "co_sr" | "co_edca" | "joint"
        observed_state:  观测周期结束后重新采集的 AP 状态；None 时回退为 ap_state
        observed_is_real: True 表示 observed_state 来自真实二次采集，
                          才执行参数生效检查；
                          False（mock/无采集器）时仅检查参数范围合法性

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

            # 协议级 Co-SR：OBSS_PD 门限（可选字段，携带则校验范围 / 耦合 / 生效）
            obss_pd = entry.get("obss_pd_dbm")
            if obss_pd is not None:
                obss_pd = float(obss_pd)
                report["proposed_params"]["obss_pd_dbm"] = obss_pd
                if not (OBSS_PD_MIN_DBM <= obss_pd <= OBSS_PD_MAX_DBM):
                    ap_errors.append(
                        f"obss_pd_dbm={obss_pd} 超出 SR 合法窗口 "
                        f"[{OBSS_PD_MIN_DBM}, {OBSS_PD_MAX_DBM}] dBm"
                    )
                # 标准耦合：开 SR（门限>-82）时 tx ≤ TX_POWER_MAX-(obss_pd-OBSS_PD_MIN)
                if pwr is not None and obss_pd > OBSS_PD_MIN_DBM:
                    tx_limit = TX_POWER_MAX_DBM - (obss_pd - OBSS_PD_MIN_DBM)
                    if float(pwr) > tx_limit + OBSS_PD_COUPLING_TOLERANCE_DB:
                        ap_errors.append(
                            f"违反 SR 功率耦合：obss_pd={obss_pd} dBm 时 tx 上限 "
                            f"{round(tx_limit, 1)} dBm，提案 tx={pwr} dBm"
                        )
                if observed_is_real:
                    observed_obss = obs.get(ap_id, {}).get("obss_pd_dbm")
                    report["observed_params"]["obss_pd_dbm"] = observed_obss
                    if observed_obss is None:
                        ap_errors.append("观测结果缺少 obss_pd_dbm")
                    elif abs(float(observed_obss) - obss_pd) > OBSS_PD_APPLIED_TOLERANCE_DB:
                        ap_errors.append(
                            f"obss_pd_dbm 未生效：期望 {obss_pd} dBm，观测 {observed_obss} dBm"
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
            edca_groups = _extract_edca_param_groups(params_raw)

            if not edca_groups:
                err = f"{ap_id.upper()}: 缺少 EDCA 参数 ['CWmin', 'CWmax', 'AIFSN']"
                global_errors.append(err)
                report["errors"].append(err)
                report["checks"].append({"check": "Co-EDCA params", "ok": False, "errors": [err]})
                continue

            edca_errors: list[str] = []

            for ac, edca_params in edca_groups.items():
                missing_keys = [k for k, v in edca_params.items() if v is None]
                if missing_keys:
                    edca_errors.append(f"{ac}: 缺少 EDCA 参数 {missing_keys}")
                    continue

                edca_params_int = {k: int(v) for k, v in edca_params.items()}
                report["proposed_params"].update(_format_edca_group(ac, edca_params_int))
                edca_errors.extend(
                    f"{ac}: {e}" for e in _validate_edca_range(edca_params_int)
                )

                if observed_is_real:
                    observed_edca = _observed_edca_params(obs.get(ap_id, {}), ac)
                    has_edca_obs = any(v is not None for v in observed_edca.values())
                    if has_edca_obs:
                        # AP 上报了 EDCA 参数（从 hostapd/ns-3 回读）→ 检查是否生效
                        report["observed_params"].update(
                            _format_edca_group(
                                ac, {k: v for k, v in observed_edca.items() if v is not None}
                            )
                        )
                        for key, expected in edca_params_int.items():
                            actual = observed_edca.get(key)
                            if actual is None:
                                edca_errors.append(f"{ac}: 观测结果缺少 {key}")
                            elif int(actual) != expected:
                                edca_errors.append(
                                    f"{ac}: {key} 未生效：期望 {expected}，观测 {actual}"
                                )
                    # else: AP 未上报该 AC 的 EDCA 观测值，跳过生效检查

            report["errors"].extend([f"{ap_id.upper()}: {e}" for e in edca_errors])
            report["checks"].append({
                "check": "Co-EDCA params",
                "ok": len(edca_errors) == 0,
                "errors": [f"{ap_id.upper()}: {e}" for e in edca_errors],
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


def _first_present(params: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in params and params.get(key) is not None:
            return params.get(key)
    return None


def _extract_edca_param_groups(params: dict) -> dict[str, dict]:
    """提取 legacy/Per-AC EDCA 参数。

    旧字段 CWmin/CWmax/AIFSN 等价于 BE；显式 BE_* 优先于旧字段。
    VI_* 可单独出现，用于只调整 AC_VI。
    """
    specs = {
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
    out: dict[str, dict] = {}
    for ac, fields in specs.items():
        group = {canonical: _first_present(params, aliases)
                 for canonical, aliases in fields.items()}
        if any(v is not None for v in group.values()):
            out[ac] = group
    return out


def _format_edca_group(ac: str, params: dict) -> dict:
    if ac == "BE":
        # 保持旧报告字段兼容；额外 BE_* 字段便于审计 Per-AC 下发。
        return {
            "CWmin": params.get("CWmin"),
            "CWmax": params.get("CWmax"),
            "AIFSN": params.get("AIFSN"),
            "BE_CWmin": params.get("CWmin"),
            "BE_CWmax": params.get("CWmax"),
            "BE_AIFSN": params.get("AIFSN"),
        }
    prefix = f"{ac}_"
    return {
        f"{prefix}CWmin": params.get("CWmin"),
        f"{prefix}CWmax": params.get("CWmax"),
        f"{prefix}AIFSN": params.get("AIFSN"),
    }


def _observed_edca_params(observed: dict, ac: str = "BE") -> dict:
    if ac == "VI":
        return {
            "CWmin": observed.get("vi_cwmin"),
            "CWmax": observed.get("vi_cwmax"),
            "AIFSN": observed.get("vi_aifsn"),
        }
    return {
        "CWmin": observed.get("be_cwmin", observed.get("cwmin")),
        "CWmax": observed.get("be_cwmax", observed.get("cwmax")),
        "AIFSN": observed.get("be_aifsn", observed.get("aifsn")),
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

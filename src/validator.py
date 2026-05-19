"""
决策验证器 — 对 LLM 输出的最终 JSON 决策进行确定性验算。

不依赖 LLM，纯 Python 数学验证：
  Co-SR  → 调用 sr.validate()，检查 TX Power 约束
  Co-EDCA → 调用 edca.validate()，检查参数范围
  joint   → 两者均检查

返回结构化 ValidationReport，可直接写入 JSONL 日志。
"""
from __future__ import annotations

from .tools import sr, edca

# 与 AGENTS.md 中硬性约束保持一致
TX_POWER_MIN_DBM = 1.0
TX_POWER_MAX_DBM = 23.0


def validate_decision(
    ap_state: dict,
    decision: dict | None,
    strategy: str,
) -> dict:
    """
    对最终决策 JSON 执行确定性验证。

    Args:
        ap_state:  各 AP 当前实测状态（与 orchestrator.run() 收到的格式相同）
        decision:  _extract_json() 解析出的决策字典；None 表示 LLM 未能输出合法 JSON
        strategy:  "co_sr" | "co_edca" | "joint"

    Returns:
        {
            "approved":      bool,          # 全部检查通过 → True
            "strategy":      str,
            "parse_ok":      bool,          # decision 是否成功解析
            "per_ap": {
                "ap1": {
                    "proposed_params": {...},
                    "checks":          [...],   # 每项检查结果列表
                    "valid":           bool,
                    "errors":          [...],
                },
                ...
            },
            "global_errors": [...],         # 跨 AP 或解析级别的错误
            "summary":       str,           # 一行人读摘要
        }
    """
    if decision is None:
        return _fail_report(strategy, "LLM 未输出合法 JSON，无法执行验证")

    ap_ids = list(ap_state.keys())
    per_ap: dict[str, dict] = {}
    global_errors: list[str] = []

    # ------------------------------------------------------------------
    # 规范化决策 key（LLM 可能输出 "AP1" 或 "ap1"）
    # ------------------------------------------------------------------
    normalized = _normalize_decision_keys(decision, ap_ids)
    missing = [ap for ap in ap_ids if ap not in normalized]
    if missing:
        global_errors.append(f"决策缺少以下 AP 的参数: {missing}")
        return _build_report(strategy, True, per_ap, global_errors)

    # ------------------------------------------------------------------
    # Co-SR 校验
    # ------------------------------------------------------------------
    if strategy in ("co_sr", "joint"):
        proposed_powers: dict[str, float] = {}
        for ap_id in ap_ids:
            entry = normalized[ap_id]
            pwr = entry.get("tx_power_dbm")
            if pwr is None:
                global_errors.append(f"{ap_id.upper()}: 缺少 tx_power_dbm")
            else:
                proposed_powers[ap_id] = float(pwr)

        if len(proposed_powers) == len(ap_ids):
            # 范围检查
            for ap_id, pwr in proposed_powers.items():
                if not (TX_POWER_MIN_DBM <= pwr <= TX_POWER_MAX_DBM):
                    global_errors.append(
                        f"{ap_id.upper()}: tx_power_dbm={pwr} 超出合法范围 "
                        f"[{TX_POWER_MIN_DBM}, {TX_POWER_MAX_DBM}] dBm"
                    )

            # 物理约束（CCA / SINR / STA RSSI）
            ok, sr_errors = sr.validate(ap_state, proposed_powers)
            global_errors.extend(sr_errors)

            # 按 AP 填充结果
            for ap_id in ap_ids:
                entry = per_ap.setdefault(ap_id, _empty_ap_entry())
                pwr = proposed_powers.get(ap_id)
                entry["proposed_params"]["tx_power_dbm"] = pwr
                ap_sr_errors = [e for e in sr_errors if e.upper().startswith(ap_id.upper())]
                entry["errors"].extend(ap_sr_errors)
                entry["checks"].append({
                    "check": "Co-SR",
                    "ok":    len(ap_sr_errors) == 0,
                    "errors": ap_sr_errors,
                })
        else:
            for ap_id in ap_ids:
                per_ap.setdefault(ap_id, _empty_ap_entry())

    # ------------------------------------------------------------------
    # Co-EDCA 校验
    # ------------------------------------------------------------------
    if strategy in ("co_edca", "joint"):
        for ap_id in ap_ids:
            entry = per_ap.setdefault(ap_id, _empty_ap_entry())
            params_raw = normalized[ap_id]
            edca_params = {
                k: params_raw.get(k)
                for k in ("CWmin", "CWmax", "AIFSN")
            }

            missing_keys = [k for k, v in edca_params.items() if v is None]
            if missing_keys:
                err = f"{ap_id.upper()}: 缺少 EDCA 参数 {missing_keys}"
                global_errors.append(err)
                entry["errors"].append(err)
                entry["checks"].append({"check": "Co-EDCA", "ok": False, "errors": [err]})
                continue

            edca_params_int = {k: int(v) for k, v in edca_params.items()}
            entry["proposed_params"].update(edca_params_int)
            ok, errors = edca.validate(edca_params_int)
            entry["errors"].extend(errors)
            entry["checks"].append({"check": "Co-EDCA", "ok": ok, "errors": errors})
            if not ok:
                global_errors.extend([f"{ap_id.upper()}: {e}" for e in errors])

    # ------------------------------------------------------------------
    # 汇总 per_ap valid 字段
    # ------------------------------------------------------------------
    for ap_id in ap_ids:
        entry = per_ap.setdefault(ap_id, _empty_ap_entry())
        entry["valid"] = len(entry["errors"]) == 0

    return _build_report(strategy, True, per_ap, global_errors)


# ------------------------------------------------------------------
# 内部辅助
# ------------------------------------------------------------------

def _normalize_decision_keys(decision: dict, ap_ids: list[str]) -> dict:
    """将决策 dict 的 key 统一转为小写，兼容 "AP1" / "ap1" 两种写法。"""
    normalized = {}
    for k, v in decision.items():
        lower_k = k.lower()
        if lower_k in ap_ids:
            normalized[lower_k] = v if isinstance(v, dict) else {}
    return normalized


def _empty_ap_entry() -> dict:
    return {"proposed_params": {}, "checks": [], "valid": False, "errors": []}


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
        summary = f"验证失败（策略={strategy}，{n} 项错误）：{'; '.join(global_errors[:3])}"
    return {
        "approved":      approved,
        "strategy":      strategy,
        "parse_ok":      parse_ok,
        "per_ap":        per_ap,
        "global_errors": global_errors,
        "summary":       summary,
    }

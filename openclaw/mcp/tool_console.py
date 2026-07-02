"""
工具调用的富文本控制台摘要 formatter。

从原 src/orchestrator.py 模块级 helper 原样迁移而来（逻辑零改写），
供 run_openclaw.py 的进程内阶段接力（structured_relay）把 AP 的 MCP 工具调用渲染成单行/多行摘要。
复用保留为基础设施的 src/console_style.py。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.console_style import (
    ap_label, dim, status_ok, status_warn, status_fail,
    tool_dur, tool_name, tool_prefix,
)

AP_IDS = ["ap1", "ap2", "ap3"]


def _fmt_num(value: object, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value}{suffix}"
    if isinstance(value, float):
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        return f"{text}{suffix}"
    return f"{value}{suffix}"


def _is_truncated_marker(v: object) -> bool:
    """openclaw trajectory 对超深工具参数会写入 {truncated:true,...} 标记。"""
    return isinstance(v, dict) and v.get("truncated") is True


def _sanitize_truncated(obj):
    """递归把 truncated 标记替换为字面量，便于 json.dumps 展示而非泄漏内部元信息。"""
    if _is_truncated_marker(obj):
        return "…(截断)"
    if isinstance(obj, dict):
        return {k: _sanitize_truncated(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_truncated(v) for v in obj]
    return obj


def _format_tool_args(tname: str, raw_args: dict) -> str:
    if not raw_args:
        return ""
    if tname in ("validate_sr_proposal", "evaluate_sr_candidate"):
        powers = raw_args.get("proposed_powers", {})
        if _is_truncated_marker(powers):
            return " proposed_powers=…(截断)"
        if isinstance(powers, dict):
            parts = [f"{ap}={_fmt_num(power, 'dBm')}" for ap, power in powers.items()]
            group = raw_args.get("concurrent_group")
            if _is_truncated_marker(group):
                return " " + " ".join(parts) + " concurrent_group=…(截断)"
            group_text = f" group={','.join(group)}" if isinstance(group, list) else ""
            return " " + " ".join(parts) + group_text
    # 以下工具参数在结果行显示，header 省略
    if tname in ("validate_edca_proposal", "rank_sr_candidates"):
        return ""
    compact = json.dumps(_sanitize_truncated(raw_args), ensure_ascii=False)
    return " " + (compact[:80] + "..." if len(compact) > 80 else compact)


def _tool_header(tname: str, raw_args: dict, dur_ms: float | None) -> str:
    """Build colored tool header line (without status flag)."""
    dur = f" {tool_dur(dur_ms)}" if dur_ms is not None else ""
    return (
        f"{tool_prefix()} {tool_name(tname)}"
        f"{_format_tool_args(tname, raw_args)}{dur}"
    )


def _format_tool_console(tname: str, raw_args: dict, result: dict, dur_ms: float | None = None) -> str:
    hdr = _tool_header(tname, raw_args, dur_ms)

    # ── get_latest_ap_states ──────────────────────────────────────────
    if tname == "get_latest_ap_states":
        states = result.get("ap_states", {}) if isinstance(result, dict) else {}
        lines  = [hdr]
        if result.get("error"):
            lines.append(f"{status_fail('错误:')} {result['error']}")
        for ap_id in AP_IDS:
            state = states.get(ap_id, {}) if isinstance(states, dict) else {}
            neighbors = state.get("neighbor_rssi_dbm") or {}
            nbr_text  = " ".join(f"{ap}:{int(rssi)}" for ap, rssi in neighbors.items()) or "—"
            svc = state.get("service_name", "—")
            biz = state.get("business_type", "—")
            prio = state.get("traffic_priority", "—")
            lines.append(
                f"{ap_label(ap_id)} {dim(svc + '/' + biz + '/' + prio)}"
                f" TX={_fmt_num(state.get('tx_power_dbm'), 'dBm')}"
                f" STA={_fmt_num(state.get('sta_rssi_dbm'), 'dBm')}"
                f" tput={_fmt_num(state.get('throughput_mbps_user'), 'Mbps')}"
                f" EDCA={state.get('cwmin','—')}/{state.get('cwmax','—')}/{state.get('aifsn','—')}"
                f" {dim('[' + nbr_text + ']')}"
            )
        return "\n".join(lines)

    # ── analyze_sr_interference ───────────────────────────────────────
    if tname == "analyze_sr_interference":
        summary    = result.get("summary", {})
        strong_cnt = summary.get("strong_link_count", 0)
        mod_cnt    = summary.get("moderate_link_count", 0)
        triggered  = result.get("co_sr_triggered")
        sr_flag    = status_ok("触发") if triggered else dim("未触发")

        strong_links = result.get("strong_links", [])
        link_texts   = [
            f"{lnk['source_ap']}→{lnk['victim_ap']}({int(lnk['rssi_dbm'])}dBm)"
            for lnk in strong_links[:3]
        ]

        interferers = result.get("primary_interferers", [])
        victims     = result.get("primary_victims", [])
        top_src = f"{interferers[0]['ap_id']}({interferers[0]['score']}分)" if interferers else "—"
        top_vic = f"{victims[0]['ap_id']}({victims[0]['score']}分)"    if victims     else "—"

        link_summary = status_warn("  ".join(link_texts)) if link_texts else dim("无强干扰链路")
        lines = [
            f"{hdr}  CoSR={sr_flag} strong={strong_cnt} moderate={mod_cnt}",
            f"{link_summary}  干扰源:{top_src}  受害:{top_vic}",
        ]
        return "\n".join(lines)

    # ── compute_sr_feasible_ranges ────────────────────────────────────
    if tname == "compute_sr_feasible_ranges":
        lines  = [hdr]
        ranges = result.get("ranges", {})
        all_feasible = result.get("all_individual_ranges_feasible", True)
        for ap_id in AP_IDS:
            item    = ranges.get(ap_id, {}) if isinstance(ranges, dict) else {}
            cur     = _fmt_num(item.get("current_dbm"), "")
            lo      = _fmt_num(item.get("min_dbm"), "")
            hi      = _fmt_num(item.get("max_dbm"), "")
            feasible = item.get("feasible_individual_range", True)
            reasons = dim(",".join(item.get("upper_reasons", [])) or "—")
            if feasible:
                lines.append(f"{ap_label(ap_id)} {cur}→[{lo},{hi}]dBm  上界:{reasons}")
            else:
                lines.append(
                    f"{ap_label(ap_id)} {cur}→[{status_fail('无可行范围')}]  "
                    f"上界 {hi} < 下界 {lo}（{reasons}）"
                )
        seed = result.get("candidate_hints", {}).get("minimal_necessary_drop")
        if seed and isinstance(seed, dict) and seed:
            seed_text = " ".join(f"{ap}={_fmt_num(p,'dBm')}" for ap, p in seed.items())
            lines.append(f"{dim('可行起点:')} {seed_text}")
        if not all_feasible:
            lines.append(status_warn("警告：存在不可行范围，CCA 约束可能无法满足"))
        return "\n".join(lines)

    # ── select_sr_concurrent_groups ───────────────────────────────────
    if tname == "select_sr_concurrent_groups":
        lines = [hdr]
        best = result.get("best_group") if isinstance(result, dict) else None
        if best:
            group = ",".join(best.get("concurrent_group", []))
            non = ",".join(best.get("non_concurrent_aps", [])) or "—"
            score = best.get("score", {})
            powers = best.get("recommended_powers", {})
            pw_txt = " ".join(f"{ap}={_fmt_num(p,'dBm')}" for ap, p in powers.items())
            lines.append(
                f"{status_ok('最佳并发组')} {group}  非并发:{non}  {pw_txt}"
            )
            lines.append(
                f"{dim('代价:')} 总降功={_fmt_num(score.get('total_power_drop_db'), 'dB')}"
                f" maxCCA={_fmt_num(score.get('max_cca_dbm'), 'dBm')}"
                f" minSINR={_fmt_num(score.get('min_sinr_db'), 'dB')}"
            )
        else:
            lines.append(status_fail("没有找到可行并发组"))
            failed_groups = [
                item for item in (result.get("all_groups") or [])
                if isinstance(item, dict) and not item.get("valid")
            ]
            for item in failed_groups[:6]:
                group = ",".join(item.get("concurrent_group", [])) or "—"
                error = item.get("error") or "未给出具体错误"
                lines.append(f"  {dim('候选组 ' + group + ':')} {error}")
            diagnosis = result.get("diagnosis", {}) if isinstance(result, dict) else {}
            for ap_id in AP_IDS:
                info = diagnosis.get(ap_id, {}) if isinstance(diagnosis, dict) else {}
                reasons = info.get("reasons") or []
                if reasons:
                    lines.append(f"  {dim(ap_id + ':')} " + "; ".join(reasons))
                elif failed_groups:
                    strongest = info.get("strongest_interferer")
                    if isinstance(strongest, dict) and strongest.get("ap"):
                        lines.append(
                            f"  {dim(ap_id + ':')} 结构性 STA/噪声约束未触发；"
                            f"最强邻居 {strongest.get('ap')}={_fmt_num(strongest.get('rssi_dbm'), 'dBm')}"
                        )
        return "\n".join(lines)

    # ── evaluate_sr_candidate / validate_sr_proposal ──────────────────
    if tname in ("validate_sr_proposal", "evaluate_sr_candidate"):
        all_ok = result.get("valid", False)
        flag   = status_ok("全部OK") if all_ok else status_fail("FAIL")
        lines  = [f"{hdr}  {flag}"]
        group = result.get("concurrent_group")
        non_concurrent = result.get("non_concurrent_aps")
        if group:
            group_text = ",".join(group)
            non_text = ",".join(non_concurrent or []) or "—"
            lines.append(f"{dim('并发组:')} {group_text}  {dim('非并发:')} {non_text}")

        if not all_ok:
            per_ap = result.get("per_ap", {})
            for ap_id in AP_IDS:
                if not isinstance(per_ap, dict) or ap_id not in per_ap:
                    continue
                item = per_ap.get(ap_id, {})
                if not item.get("valid"):
                    errors   = item.get("errors") or []
                    err_text = "; ".join(e[:60] for e in errors[:2]) if errors else "未知"
                    lines.append(f"{status_fail(ap_id + ' FAIL:')} {err_text}")

        score = result.get("score") if isinstance(result, dict) else None
        if isinstance(score, dict):
            lines.append(
                f"{dim('代价:')} 总降功={_fmt_num(score.get('total_power_drop_db'), 'dB')}"
                f" 最大单AP={_fmt_num(score.get('max_single_ap_drop_db'), 'dB')}"
                f" STA余量={_fmt_num(score.get('min_sta_rssi_margin_db'), 'dB')}"
                f" minSINR={_fmt_num(score.get('min_sinr_db'), 'dB')}"
            )
        return "\n".join(lines)

    # ── rank_sr_candidates ────────────────────────────────────────────
    if tname == "rank_sr_candidates":
        lines = [f"{hdr}  {dim('objective='+ str(result.get('objective', '')))}"]
        for item in result.get("ranked_candidates", [])[:3]:
            score  = item.get("score", {})
            powers = item.get("proposed_powers", {})
            pw_txt = " ".join(f"{ap}={_fmt_num(p,'dBm')}" for ap, p in powers.items())
            ok     = item.get("valid")
            sflag  = status_ok("OK") if ok else status_fail("FAIL")
            lines.append(
                f"#{item.get('rank')} {item.get('name')} {sflag}: {pw_txt}"
                f" | 降功={_fmt_num(score.get('total_power_drop_db'),'dB')}"
                f" 最大={_fmt_num(score.get('max_single_ap_drop_db'),'dB')}"
            )
        return "\n".join(lines)

    # ── validate_edca_proposal ────────────────────────────────────────
    if tname == "validate_edca_proposal":
        # 检查是否有错误返回（参数缺失）
        if result.get("error"):
            lines = [f"{hdr}  {status_fail('参数缺失')}"]
            lines.append(f"  {dim(result['error'])}")
            return "\n".join(lines)

        ap_entries = [ap_id for ap_id in AP_IDS if isinstance(result.get(ap_id), dict)]
        per_ap_ok = all(
            result.get(ap_id, {}).get("valid", True)
            for ap_id in ap_entries
        )
        effectiveness = result.get("effectiveness") or {}
        has_warn = not effectiveness.get("all_ok", True)

        if not ap_entries:
            flag = status_fail("参数缺失（工具调用格式有误，缺少 proposed_edca 字段）")
        elif not per_ap_ok:
            flag = status_fail("参数违规")
        elif has_warn:
            flag = status_warn("有警告")
        else:
            flag = status_ok("全部合规")

        params_parts = []
        for ap_id in AP_IDS:
            item = result.get(ap_id, {})
            if isinstance(item, dict) and "CWmin" in item:
                params_parts.append(f"{ap_id}={item['CWmin']}/{item['CWmax']}/{item['AIFSN']}")

        lines = [f"{hdr}  {flag}  {dim('  '.join(params_parts))}"]

        for ap_id in ap_entries:
            item = result.get(ap_id, {})
            if not item.get("valid"):
                lines.append(
                    f"{status_fail('[FAIL]')} {ap_id}: {'; '.join(item.get('errors') or [])}"
                )

        for ap_id, eff in (effectiveness.get("per_ap") or {}).items():
            for w in eff.get("warnings") or []:
                lines.append(f"{status_warn('[WARN]')} {ap_id}: {w}")
        for w in (effectiveness.get("fairness") or {}).get("warnings") or []:
            lines.append(f"{status_warn('[WARN]')} 公平性: {w}")

        return "\n".join(lines)

    compact = json.dumps(result, ensure_ascii=False)
    if len(compact) > 200:
        compact = compact[:200] + "..."
    return f"{hdr}\n{compact}"

import itertools
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .agent import APAgent
from .console_style import (
    format_ap_name, strip_md,
    ap_label, congestion_color, dim, status_ok, status_warn, status_fail,
    tool_dur, tool_name, tool_prefix, BOLD, FG, AP_NAME_COLORS, color,
)
from .logger import SessionLogger
from .profile import apply_profile, agent_view
from .tools import sr as _sr
from .tools.edca import encode_params_edca
from .tools.registry import TOOL_DEFINITIONS, make_executor
from .validator import validate_decision

AP_IDS = ["ap1", "ap2", "ap3"]
MAX_VOTE_ROUNDS = 3

# 按工具名预分组，避免重复过滤
_GET_LATEST_STATE = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "get_latest_ap_states"]
_ANALYZE_SR    = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "analyze_sr_interference"]
_SELECT_SR     = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "select_sr_concurrent_groups"]
_VALIDATE_SR   = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "evaluate_sr_candidate"]
_VALIDATE_EDCA = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "validate_edca_proposal"]


def _tools_for_vote(_strategy: str) -> list:
    # 投票者反对时会在同一轮里给出反提案；一旦它选择 Co-SR，
    # 也必须先具备分析/选择可并发组的工具能力。
    return _GET_LATEST_STATE + _ANALYZE_SR + _SELECT_SR + _VALIDATE_SR + _VALIDATE_EDCA


def _normalize_proposal(proposal: dict) -> dict:
    """
    规范化提案为嵌套格式。

    模型有时把 Co-SR 提案写成扁平的 {"ap1": 6.0} 而非
    {"ap1": {"tx_power_dbm": 6.0}}。裸数值统一提升为 tx_power_dbm 嵌套形式，
    使策略推断与投票注入都能识别（否则会被误判为 co_edca）。
    """
    normalized: dict = {}
    for ap_id, value in proposal.items():
        if isinstance(value, bool):
            normalized[ap_id] = value
        elif isinstance(value, (int, float)):
            normalized[ap_id] = {"tx_power_dbm": float(value)}
        else:
            normalized[ap_id] = value
    return normalized


def _infer_strategy_from_proposal(proposal: dict) -> str:
    """Detect negotiation strategy from fields present in the proposal JSON."""
    has_sr = any(
        (isinstance(v, dict) and "tx_power_dbm" in v) or isinstance(v, (int, float))
        for v in proposal.values()
        if not isinstance(v, bool)
    )
    has_edca = any(
        isinstance(v, dict) and any(k in v for k in ("CWmin", "CWmax", "AIFSN"))
        for v in proposal.values()
    )
    if has_sr and has_edca:
        return "joint"
    if has_sr:
        return "co_sr"
    if has_edca:
        return "co_edca"
    return "co_edca"


def _sr_concurrent_group_from_proposal(proposal: dict | None) -> list[str]:
    if not isinstance(proposal, dict):
        return []
    meta = proposal.get("_sr") or proposal.get("sr") or {}
    if isinstance(meta, dict):
        group = meta.get("concurrent_group") or meta.get("concurrent_aps")
        if isinstance(group, list):
            return [str(ap).lower() for ap in group]
    group = proposal.get("concurrent_group")
    if isinstance(group, list):
        return [str(ap).lower() for ap in group]
    return []


def _with_sr_concurrent_group(proposal: dict, ap_state: dict) -> dict:
    """
    Co-SR / joint 提案必须先确定可用并发组。

    若模型已经显式给出 concurrent_group，则保留；若缺失，则使用确定性枚举器
    自动注入 best_group，避免退回“全网 AP 同时并发”的旧缺省语义。
    """
    strategy = _infer_strategy_from_proposal(proposal)
    if strategy not in ("co_sr", "joint"):
        return proposal

    groups = _sr.select_concurrent_groups(ap_state)
    best = groups.get("best_group") if isinstance(groups, dict) else None
    existing_group = _sr_concurrent_group_from_proposal(proposal)
    if existing_group or not best:
        return proposal

    updated = dict(proposal)
    sr_meta = dict(updated.get("_sr") or {})
    sr_meta["concurrent_group"] = list(best.get("concurrent_group", []))
    sr_meta["non_concurrent_aps"] = list(best.get("non_concurrent_aps", []))
    sr_meta["source"] = "orchestrator_auto_select"
    updated["_sr"] = sr_meta
    return updated



def _extract_json(text: str) -> dict | None:
    """从 agent 回复中提取第一个合法 JSON 对象。"""
    for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
        candidate = m.group(1).strip()
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _extract_proposal(text: str) -> dict | None:
    """扫描文本中所有 JSON 块，返回第一个含 ap1/ap2/ap3 键的对象。"""
    def _matches(d: dict) -> dict | None:
        ap_keys = {k.lower() for k in d}
        if set(AP_IDS).issubset(ap_keys):
            return d
        for key in ("proposal", "final_proposal", "decision", "params"):
            nested = d.get(key)
            if isinstance(nested, dict) and set(AP_IDS).issubset({k.lower() for k in nested}):
                return nested
        return None

    for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
        try:
            parsed = json.loads(m.group(1).strip())
            if isinstance(parsed, dict):
                hit = _matches(parsed)
                if hit is not None:
                    return _normalize_proposal(hit)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                hit = _matches(parsed)
                if hit is not None:
                    return _normalize_proposal(hit)
        except json.JSONDecodeError:
            pass
    return None


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


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


def _fmt_pct(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{value * 100:.0f}%"
    return "—"


def _fmt_bool(ok: object) -> str:
    return "ok" if ok else "fail"


def _tool_header(tname: str, raw_args: dict, dur_ms: float) -> str:
    """Build colored tool header line (without status flag)."""
    return (
        f"{tool_prefix()} {tool_name(tname)}"
        f"{_format_tool_args(tname, raw_args)} {tool_dur(dur_ms)}"
    )


def _format_tool_args(tname: str, raw_args: dict) -> str:
    if not raw_args:
        return ""
    if tname in ("validate_sr_proposal", "evaluate_sr_candidate"):
        powers = raw_args.get("proposed_powers", {})
        if isinstance(powers, dict):
            parts = [f"{ap}={_fmt_num(power, 'dBm')}" for ap, power in powers.items()]
            group = raw_args.get("concurrent_group")
            group_text = f" group={','.join(group)}" if isinstance(group, list) else ""
            return " " + " ".join(parts) + group_text
    # 以下工具参数在结果行显示，header 省略
    if tname in ("validate_edca_proposal", "rank_sr_candidates"):
        return ""
    compact = json.dumps(raw_args, ensure_ascii=False)
    return " " + (compact[:80] + "..." if len(compact) > 80 else compact)


def _format_tool_console(tname: str, raw_args: dict, result: dict, dur_ms: float) -> str:
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
            prio = state.get("traffic_priority", "—")
            lines.append(
                f"{ap_label(ap_id)} {dim(svc + '/' + prio)}"
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



class NegotiationOrchestrator:
    def __init__(
        self,
        agents_dir: Path,
        model: str = "qwen3:14b",
        logger: SessionLogger | None = None,
        observation_state_getter: Callable[[], dict] | None = None,
        observation_wait_seconds: float = 30.0,
        executor_endpoints: dict[str, str] | None = None,
    ):
        self.agents: dict[str, APAgent] = {
            ap_id: APAgent(ap_id, agents_dir, model)
            for ap_id in AP_IDS
        }
        self.conversation_log: list[dict] = []
        self.logger = logger
        self._current_ap_states: dict | None = None
        # 观测状态在进入协商前统一应用字段白名单与保守默认值，
        # 确保 get_latest_ap_states / 最终验证观测使用一致的规范化字段。
        if observation_state_getter is not None:
            _raw_getter = observation_state_getter
            self.observation_state_getter: Callable[[], dict] | None = (
                lambda: apply_profile(_raw_getter())
            )
        else:
            self.observation_state_getter = None
        self.observation_wait_seconds = observation_wait_seconds
        # {"ap1": "http://192.168.1.11:5002", ...}；None 表示不推送
        self.executor_endpoints: dict[str, str] | None = executor_endpoints
        # 协商成功后的最终决策与策略（供 mock 喂数器注入曲线，未成功时为 None）
        self.last_decision: dict | None = None
        self.last_strategy: str | None = None

    # ──────────────────────────────────────────────────────────────────────
    # 决策推送
    # ──────────────────────────────────────────────────────────────────────

    def _push_decision(self, decision: dict, strategy: str, session_id: str) -> dict[str, dict]:
        """
        并发向所有香蕉派执行服务推送最终决策。
        每个 AP 只收到属于自己的 params 子集。
        """
        if not self.executor_endpoints:
            return {}

        import requests as _req

        def _send(ap_id: str, url: str) -> tuple[str, bool, str, dict]:
            params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
            # 决策内部用实际 CW 值；香蕉派写 hostapd 需要指数 n，发送前转换。
            # encode_params_edca 返回新字典，不会污染 decision（后续验证仍用 CW 值）。
            params = encode_params_edca(params)
            payload = {
                "session_id": session_id,
                "strategy":   strategy,
                "ap_id":      ap_id,
                "params":     params,
            }
            try:
                r = _req.post(f"{url.rstrip('/')}/apply", json=payload, timeout=8)
                ok = r.status_code == 200
                msg = r.json().get("details", r.text) if ok else r.text
                return ap_id, ok, str(msg), payload
            except Exception as exc:
                return ap_id, False, str(exc), payload

        print("\n[Executor] 开始向香蕉派推送决策...")
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(self.executor_endpoints)) as pool:
            futures = {
                pool.submit(_send, ap_id, url): ap_id
                for ap_id, url in self.executor_endpoints.items()
            }
            for future in as_completed(futures):
                ap_id, ok, msg, payload = future.result()
                status = "✓" if ok else "✗"
                print(f"[{status}] {ap_id.upper()}: {msg}")
                url = self.executor_endpoints[ap_id]
                results[ap_id] = {
                    "ok": ok,
                    "url": url,
                    "payload": payload,
                    "response": msg,
                }
                if self.logger:
                    self.logger.record_executor_apply(
                        ap_id,
                        ok=ok,
                        url=url,
                        payload=payload,
                        response=msg,
                    )
        return results

    # ──────────────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────────────

    def _record(self, speaker: str, content: str) -> None:
        self.conversation_log.append({"speaker": speaker, "content": content})

    def _collect_observed_state(
        self, fallback_state: dict
    ) -> tuple[dict, str | None, bool]:
        if self.observation_state_getter is None:
            if self.logger:
                self.logger.record_state_snapshot(
                    "final_fallback",
                    fallback_state,
                    source="validator_fallback",
                )
            return fallback_state, None, False

        wait_seconds = max(0.0, self.observation_wait_seconds)
        print(f"\n[Validator] 等待观测周期 {wait_seconds:g}s 后读取最终状态...")
        if wait_seconds:
            time.sleep(wait_seconds)

        try:
            observed = self.observation_state_getter()
            if self.logger:
                self.logger.record_state_snapshot(
                    "final_observed",
                    observed,
                    source="validator_observation",
                )
            return observed, None, True
        except Exception as exc:
            return {}, f"观测状态获取失败: {exc}", False

    def _vote_result(self, content: str) -> str:
        """Returns 'agree', 'reject', or 'abstain'."""
        vote = _extract_json(content)
        if isinstance(vote, dict):
            agreed = vote.get("agreed")
            if agreed == "abstain":
                return "abstain"
            if isinstance(agreed, bool):
                return "agree" if agreed else "reject"

        if "弃权" in content:
            return "abstain"
        without_negative = content.replace("不同意", "").replace("反对", "")
        return "agree" if "同意" in without_negative else "reject"

    def _is_negotiation_needed(self, ap_state: dict) -> bool:
        return self._determine_strategy(ap_state) != "noop"

    def _determine_strategy(self, ap_state: dict) -> str:
        """Deterministic fallback used by tests and non-LLM callers."""
        sr_triggered = bool(_sr.analyze_interference(ap_state).get("co_sr_triggered"))
        # Co-EDCA 触发：状态中存在不同业务优先级，说明可能需要差异化 EDCA。
        # priority 来自上报/场景输入；缺省 medium 不会强行制造差异。
        priorities = {
            state.get("traffic_priority", "medium")
            for state in ap_state.values()
        }
        edca_triggered = len(priorities) > 1

        if sr_triggered and edca_triggered:
            return "joint"
        if sr_triggered:
            return "co_sr"
        if edca_triggered:
            return "co_edca"
        return "noop"

    def _speak_and_log(
        self,
        ap_id: str,
        instruction: str,
        phase: int,
        role: str,
        tools: list | None = None,
        speaker: str | None = None,
        proposal: dict | None = None,
    ) -> str:
        """调用 agent.speak_stream()，流式打印、处理工具调用、记录日志。

        proposal：正在被验算的提案。投票阶段传入，验算工具据此校验真实提案，
        无需模型在 tool_args 中重新序列化提案 JSON。
        """
        executor = (
            make_executor(
                self._current_ap_states,
                state_getter=self.observation_state_getter,
                state_setter=self._set_current_ap_states,
                proposal=proposal,
            )
            if tools else None
        )
        speaker = speaker or ap_id.upper()
        line_dirty = False

        def on_tool(tool_name: str, raw_args: dict, result_dict: dict, dur_ms: float) -> None:
            nonlocal line_dirty
            prefix = "\n" if line_dirty else ""
            print(f"{prefix}{_format_tool_console(tool_name, raw_args, result_dict, dur_ms)}", flush=True)
            line_dirty = False

        _CHUNK_BATCH = 40   # 文件日志批次大小（减少 JSONL 条目数）

        t0 = time.time()
        tool_log: list[dict] = []
        content = ""
        active_instruction = instruction

        for attempt in range(2):
            attempt_tool_log: list[dict] = []
            chunks: list[str] = []

            if self.logger:
                self.logger.agent_speak_start(ap_id, phase, role)

            print(f"\n{format_ap_name(speaker)}:")
            line_dirty = False
            _buf: list[str] = []
            _buf_len = 0
            for chunk in self.agents[ap_id].speak_stream(
                self.conversation_log,
                active_instruction,
                tools=tools,
                tool_executor=executor,
                tool_log=attempt_tool_log,
                tool_callback=on_tool,
            ):
                chunks.append(chunk)
                plain_chunk = strip_md(chunk)
                print(plain_chunk, end="", flush=True)
                if plain_chunk:
                    line_dirty = not plain_chunk.endswith("\n")
                # 每个 token 立即推送到 dashboard（不经文件批次，保证流式显示）
                if self.logger:
                    self.logger.push_chunk(ap_id, chunk)
                # 文件日志保持批次化（减少 JSONL 条目数）
                _buf.append(chunk)
                _buf_len += len(chunk)
                if _buf_len >= _CHUNK_BATCH and self.logger:
                    self.logger.agent_speak_chunk(ap_id, "".join(_buf))
                    _buf = []
                    _buf_len = 0
            if _buf and self.logger:
                self.logger.agent_speak_chunk(ap_id, "".join(_buf))
            print()
            line_dirty = False

            content = "".join(chunks).strip()
            tool_log.extend(attempt_tool_log)

            tool_names = [
                t.get("function", {}).get("name", "")
                for t in (tools or [])
            ]
            claimed_without_call = (
                tools
                and not attempt_tool_log
                and any(name and name in content for name in tool_names)
            )

            # 提案方必须在 JSON 前给出文字说明；若没有则重试一次
            missing_preamble = (
                role == "proposer"
                and attempt == 0
                and "```json" in content
                and len(content[:content.index("```json")].strip()) < 30
            )

            has_select_tool = "select_sr_concurrent_groups" in tool_names
            proposal_candidate = _extract_proposal(content) if has_select_tool else None
            missing_sr_group_selection = (
                role in (
                    "proposer",
                    "proposal_json_repair",
                    "voter",
                    "counter_proposal_json_repair",
                )
                and attempt == 0
                and proposal_candidate is not None
                and _infer_strategy_from_proposal(proposal_candidate) in ("co_sr", "joint")
                and not any(tc.get("tool") == "select_sr_concurrent_groups" for tc in attempt_tool_log)
            )

            if (
                not claimed_without_call
                and not missing_preamble
                and not missing_sr_group_selection
            ) or attempt == 1:
                break

            if claimed_without_call:
                retry_hint = (
                    "\n\n[系统提醒] 系统没有收到真实 tool_call。"
                    "如果需要使用工具，必须发起真实 tool_call；"
                    "不能只在文本中声称已调用工具。请重新回答。"
                )
                print(
                    "\n[协调者] 检测到上一轮回复声称使用工具，但未产生真实工具调用；要求重试。",
                    flush=True,
                )
            elif missing_preamble:
                retry_hint = (
                    "\n\n[系统提醒] 你的回复缺少必要的文字说明（在 JSON 之前应说明选择了哪种路径、"
                    "为什么、关键指标是什么、预期改善是什么）。请先给出完整的文字分析，再附上 JSON。"
                )
                print(
                    "\n[协调者] 检测到提案缺少文字说明，要求重新回复并先给出分析。",
                    flush=True,
                )
            else:  # missing_sr_group_selection
                retry_hint = (
                    "\n\n[系统提醒] 你的参数 JSON 属于 Co-SR/联合路径，但本次发言没有先调用 "
                    "select_sr_concurrent_groups。只要选择 Co-SR 方向，第一步必须真实调用 "
                    "get_latest_ap_states → analyze_sr_interference → select_sr_concurrent_groups，"
                    "并在最终 JSON 的 _sr.concurrent_group 写明本轮可并发组。请重新回答。"
                )
                print(
                    "\n[协调者] 检测到 Co-SR 提案未先选择可并发组，要求重新回复。",
                    flush=True,
                )
            active_instruction = instruction + retry_hint

        duration_ms = (time.time() - t0) * 1000

        self._record(speaker, content)

        if self.logger:
            self.logger.agent_speak(
                agent=ap_id,
                phase=phase,
                role=role,
                instruction=instruction,
                response=content,
                duration_ms=duration_ms,
            )
            for tc in tool_log:
                self.logger.tool_call(
                    tc["tool"],
                    tc["input"],
                    tc["output"],
                    tc["duration_ms"],
                )
                if tc["tool"] == "get_latest_ap_states":
                    self.logger.record_state_snapshot(
                        "latest_from_tool",
                        tc["output"].get("ap_states"),
                        source=tc["output"].get("source", "tool"),
                        phase=phase,
                        role=role,
                    )

        return content

    def _set_current_ap_states(self, ap_states: dict) -> None:
        self._current_ap_states = ap_states

    def _finish_session(
        self,
        outcome: str,
        total_rounds: int,
        started_at: float,
    ) -> None:
        duration_s = round(time.time() - started_at, 2)
        print(f"\n[统计] 本次协商总时间：{duration_s:.2f}s")
        if self.logger:
            self.logger.session_end(
                outcome,
                total_rounds,
                negotiation_duration_s=duration_s,
            )

    # ──────────────────────────────────────────────────────────────────────
    # 协商阶段
    # ──────────────────────────────────────────────────────────────────────

    def _phase_broadcast(self, ap_state: dict) -> None:
        if self.logger:
            self.logger.phase_start(1, "广播自身状态")

        visible = agent_view(ap_state)
        for ap_id in AP_IDS:
            state_json = json.dumps(visible[ap_id], ensure_ascii=False, indent=2)
            instruction = (
                f"请广播你（{ap_id.upper()}）的当前状态。\n"
                "发言开头先明确本机 AP 编号，然后用自然语言完整说明你的实测参数，"
                "最后用一两句话简述你当前状态，例如信道是否偏忙、邻居信号是否偏强、"
                "业务质量是否稳定。\n\n"
                "你的实测数据如下，请覆盖所有字段，但不要只复制 JSON，也不要使用固定模板：\n"
                f"{state_json}\n\n"
                "只播报你自己的数据和你本机扫描到的邻居 RSSI，不要引用或分析其他 AP 自己上报的业务指标。"
            )
            self._speak_and_log(
                ap_id,
                instruction,
                phase=1,
                role="broadcast",
                speaker=ap_id.upper(),
            )

    def _phase_propose(
        self,
        proposer_id: str,
        ap_state: dict,
    ) -> dict | None:
        if self.logger:
            self.logger.phase_start(3, f"{proposer_id.upper()} 发起提案（自主选路）")

        state_summary = json.dumps(agent_view(ap_state), ensure_ascii=False, indent=2)

        history_hint = (
            "【重要】请先完整阅读上方的对话记录。\n"
            "如果记录中有 VALIDATOR 发出的验证失败消息，你的新提案必须直接解决其中列出的具体问题。\n"
            "如果记录中已有历史提案和拒绝原因，你的提案必须明确回应各方此前提出的约束顾虑，"
            "而不是重复一个已被否决的方案。协商历史越长，越需要向各方约束的交集靠拢。\n\n"
        )
        instruction = (
            f"你（{proposer_id.upper()}）是本轮的提案方，请发起参数调整提案。\n\n"
            f"{history_hint}"
            f"所有 AP 的初始状态数据（供参考）：\n{state_summary}\n\n"
            "请先调用 get_latest_ap_states 获取最新状态，分析当前网络的核心问题。\n\n"
            "【路径选择规则（基于实时证据，不按 AP 编号或固定业务身份预设）】\n"
            "  · 若邻居 RSSI 偏强、analyze_sr_interference 显示 co_sr_triggered=true，"
            "或 SINR/STA RSSI 约束显示功率调整有必要，可选择 Co-SR。\n"
            "  · 若 traffic_priority、QoS 指标或当前 EDCA 参数显示需要差异化信道竞争机会，"
            "可选择 Co-EDCA。\n"
            "  · 若强干扰和 EDCA 竞争问题同时有证据支持，可选择联合路径。\n"
            "  · 若状态没有足够证据支持调参，应说明暂不调整或提出最小改动方案，"
            "不要为了完成协商强行制造问题。\n\n"
            "你可以根据分析结果，选择以下协商路径：\n\n"
            "【Co-SR】降低各 AP 的 TX Power，减少 OBSS 干扰。\n"
            "  适用场景：邻居 RSSI 偏强、工具判断存在强干扰，或 SINR / STA RSSI 约束显示需要调整功率。\n"
            "  【硬性流程】只要选择 Co-SR 或联合路径，第一步必须先判断可用并发组："
            "调用 get_latest_ap_states → analyze_sr_interference → select_sr_concurrent_groups。\n"
            "  Co-SR 目标是先选择可空间复用的并发组，再在保护 SINR 和 STA RSSI 的前提下最大化降功率；"
            "不是所有 AP 都必须参与同一并发组。若中间 AP 与两侧干扰太强，"
            "可以只让最远、互扰较弱的一组（如 ap1/ap3）并发，强干扰 AP 作为 non_concurrent_aps 保持不并发。\n"
            "  工具链：analyze_sr_interference → select_sr_concurrent_groups → "
            "evaluate_sr_candidate（传入 proposed_powers 和 concurrent_group）。\n"
            "  若确实需要全网三 AP 同时并发，再使用 compute_sr_feasible_ranges / rank_sr_candidates 辅助比较。\n"
            "  功率选择应是最大必要降功率：在 SINR 和 STA RSSI 约束允许的前提下，尽量降至可行区间下界（min_int_dbm）。\n"
            "  提案 JSON 须包含每个 AP 的 tx_power_dbm，并额外包含 "
            "`\"_sr\": {\"concurrent_group\": [\"ap1\", \"ap3\"], "
            "\"non_concurrent_aps\": [\"ap2\"]}` 这样的并发组说明。\n\n"
            "【Co-EDCA】根据当前状态中的 traffic_priority、QoS 指标和 EDCA 参数差异，"
            "调整 CWmin / CWmax / AIFSN。\n"
            "  适用场景：存在业务优先级差异、拥塞/重传/时延/丢包异常，或当前 EDCA 参数与业务需求不匹配。\n"
            "  调整规则：\n"
            "    · high 通常使用更小的 CWmin、CWmax、AIFSN，以缩短退避和 AIFS 等待时间。\n"
            "    · low 通常使用更大的 CWmin、CWmax、AIFSN，以降低自身竞争强度。\n"
            "    · medium 通常介于两者之间。\n"
            "  如果所有 AP 的 traffic_priority 相同或缺省为 medium，不要强行制造优先级梯度；"
            "应主要根据拥塞、重传、时延、丢包和现有 EDCA 参数判断是否需要调整。\n"
            "  硬性排序约束（提案必须满足）：\n"
            "    high.CWmin ≤ medium.CWmin ≤ low.CWmin\n"
            "    high.AIFSN ≤ medium.AIFSN ≤ low.AIFSN\n"
            "  工具链：validate_edca_proposal（校验范围合规 + 优先级排序）\n"
            "  提案 JSON 须包含每个 AP 的 CWmin、CWmax、AIFSN 字段。\n\n"
            "【联合（Co-SR + Co-EDCA）】同时调整 TX Power 和 EDCA：降低 OBSS 干扰的同时，\n"
            "  同时根据实时 QoS / 优先级 / EDCA 参数差异调整 EDCA。提案 JSON 须同时包含 tx_power_dbm 与 EDCA 字段。\n\n"
            "提案须简洁说明：\n"
            "  · 选择了哪种路径，为什么（关键指标数据是什么）\n"
            "  · 每个 AP 的最终参数是多少，为何选择该方案（如有历史争议，说明如何化解）\n"
            "  · 预期改善和主要权衡\n"
            "最终提交前必须调用 evaluate_sr_candidate（Co-SR/联合路径）或 "
            "validate_edca_proposal（Co-EDCA/联合路径）自检相关约束。\n"
            "【关键】提案阶段自检时，必须把你打算提出的参数显式作为工具参数传入"
            "（evaluate_sr_candidate 传 proposed_powers；部分并发时还必须传 concurrent_group；"
            "validate_edca_proposal 传 proposed_edca）；"
            "空参调用只在投票阶段有效，提案阶段空参会因缺参而无法验算。\n"
            "提案末尾必须用 ```json 代码块附上参数摘要，JSON 顶层键必须是 ap1/ap2/ap3。"
        )
        content = self._speak_and_log(
            proposer_id,
            instruction,
            phase=3,
            role="proposer",
            tools=TOOL_DEFINITIONS,
            speaker=proposer_id.upper(),
        )
        proposal = _extract_proposal(content)
        if proposal is not None:
            return _with_sr_concurrent_group(proposal, self._current_ap_states or ap_state)

        repair_instruction = (
            "上一轮提案没有输出可解析的参数 JSON。\n"
            "请只输出一个 ```json 代码块，JSON 顶层键必须是 ap1、ap2、ap3，"
            "每个 AP 内包含本次选定协商路径对应的参数"
            "（Co-SR 用 tx_power_dbm，Co-EDCA 用 CWmin/CWmax/AIFSN）。不要写解释。"
        )
        content = self._speak_and_log(
            proposer_id,
            repair_instruction,
            phase=3,
            role="proposal_json_repair",
            tools=TOOL_DEFINITIONS,
            speaker=proposer_id.upper(),
        )
        proposal = _extract_proposal(content)
        if proposal is None:
            return None
        return _with_sr_concurrent_group(proposal, self._current_ap_states or ap_state)

    def _phase_vote_single(
        self,
        voter_id: str,
        proposer_id: str,
        strategy: str,
        proposal_num: int,
        proposal: dict,
    ) -> tuple[str, str]:
        """单个 AP 对当前提案投票。返回 ('agree'|'reject'|'abstain', content)。"""
        if self.logger:
            self.logger.phase_start(
                4, f"{voter_id.upper()} 投票（提案#{proposal_num}，提案方 {proposer_id.upper()}）"
            )

        verify_hint = {
            "co_sr":   "关注你自己的 TX Power、evaluate_sr_candidate 返回的 valid/errors、STA RSSI/SINR/CCA 余量，以及它对你业务的直接影响",
            "co_edca": "关注你自己的 traffic_priority、QoS 指标、当前 EDCA 参数、"
                       "工具返回的 valid/errors 与优先级排序校验结果，以及参数差异化是否有实时证据支持",
            "joint":   "关注你自己的 TX Power 与 EDCA 建议值、工具返回的 valid/errors，以及组合调整是否可接受",
        }.get(strategy, "关注工具返回的 valid/errors 以及参数对你的影响")

        # 循环风险提示：提案数越多越要告知 agent 有死锁风险
        if proposal_num >= 4:
            stall_hint = (
                f"\n\n【死锁警告：当前已是第 {proposal_num} 个提案，协商很可能已陷入循环】\n"
                "请仔细检查：各方此前的提案是否已经在重复相似的参数？\n"
                "如果是，继续反对只会让协商永远无法终止。此时你应当选择弃权，\n"
                "让当前这个「最接近可行区间的折中方案」通过，而不是提出一个同样无法满足所有约束的新方案。"
            )
        elif proposal_num >= 2:
            stall_hint = (
                f"\n\n【注意：当前已是第 {proposal_num} 个提案】\n"
                "如果协商已经出现重复，请考虑弃权，避免死锁。"
            )
        else:
            stall_hint = ""

        proposal_json = _json(proposal)
        instruction = (
            "【第一步】请完整阅读上方的对话记录，梳理此前所有提案及每次拒绝的具体原因。\n\n"
            f"【第二步】验算 {proposer_id.upper()} 的最新提案（提案#{proposal_num}）"
            f"中针对你自己（{voter_id.upper()}）的参数调整建议。\n\n"
            f"最新提案参数：\n{proposal_json}\n\n"
            "请先调用 get_latest_ap_states 获取最新状态，再调用验算工具。"
            "验算工具会自动校验上方这份当前提案，你无需在工具参数中重新填写提案 JSON，"
            "直接以空参数调用即可。\n"
            "然后用自然语言给出你的判断。"
            f"重点参考：{verify_hint}。\n\n"
            "不要套用固定模板，如果结果很简单可以简短直接。\n\n"
            "你有三种表态选项：\n\n"
            "【同意】提案满足你的约束，或你认为它是可接受的折中。回复末尾附：\n"
            "```json\n{\"agreed\": true, \"reason\": \"...\"}\n```\n\n"
            "【弃权】提案未能完全满足约束，但你也找不到更好的方案，或协商已出现重复迹象。"
            "弃权效果等同于同意，不需要提反提案。回复末尾附：\n"
            "```json\n{\"agreed\": \"abstain\", \"reason\": \"说明为何弃权\"}\n```\n\n"
            "【反对】你有具体的替代方案能改善当前提案。请在同一条回复中直接给出完整反提案。"
            "反提案须满足：\n"
            "  1. 明确说明你对当前提案的具体反对理由（卡在哪个指标或约束）\n"
            "  2. 综合对话记录中其他 AP 之前提出的所有顾虑，方案必须兼顾所有人的约束\n"
            "  3. 若此前已有多次协商失败，给出比之前所有提案更保守的参数，向各方约束交集靠拢\n"
            "  4. 如果认为当前路径不合适，可以切换：\n"
            "     Co-SR → 提案 JSON 中使用 tx_power_dbm 字段\n"
            "     Co-EDCA → 提案 JSON 中使用 CWmin/CWmax/AIFSN 字段\n\n"
            "如果你的反提案选择 Co-SR 或联合路径，必须先真实调用 "
            "get_latest_ap_states → analyze_sr_interference → select_sr_concurrent_groups "
            "判断可用并发组，再在反提案 JSON 中写入 _sr.concurrent_group；"
            "不能在未选组时直接给功率。\n\n"
            "然后附两个 ```json 块：\n"
            "第一块：```json\n{\"agreed\": false, \"reason\": \"...\"}\n```\n"
            "第二块：完整参数反提案，JSON 顶层键必须是 ap1/ap2/ap3。"
            f"{stall_hint}"
        )
        content = self._speak_and_log(
            voter_id,
            instruction,
            phase=4,
            role="voter",
            tools=_tools_for_vote(strategy),
            speaker=voter_id.upper(),
            proposal=proposal,
        )

        vote = self._vote_result(content)
        if self.logger:
            self.logger.vote(voter_id, proposal_num, vote, content)
        return vote, content

    def _phase_counter_propose(
        self,
        voter_id: str,
        vote_content: str,
        proposal_num: int,
        ap_state: dict,
    ) -> dict | None:
        """从投票内容中提取反提案 JSON；提取失败则要求补充一次纯 JSON 回复。"""
        proposal = _extract_proposal(vote_content)
        if proposal is not None:
            return _with_sr_concurrent_group(proposal, self._current_ap_states or ap_state)

        repair_instruction = (
            "你已表示反对，但回复中未找到可解析的参数 JSON。\n"
            "请回顾上方完整协商历史，综合所有 AP 此前提出的约束和顾虑，"
            "给出一个能兼顾所有人需求的反提案。\n"
            "你可以根据实时证据选择协商路径：Co-SR 使用 tx_power_dbm 字段，"
            "Co-EDCA 使用 CWmin/CWmax/AIFSN 字段，联合路径两类字段均出现；"
            "证据不足时不要为了形成反提案强行改变无关参数。\n"
            "如果选择 Co-SR 或联合路径，第一步必须真实调用 get_latest_ap_states、"
            "analyze_sr_interference、select_sr_concurrent_groups，先判断可用并发组，"
            "并在 JSON 中写入 _sr.concurrent_group。\n"
            "请只输出一个 ```json 代码块，JSON 顶层键必须是 ap1、ap2、ap3。不要写解释。"
        )
        content = self._speak_and_log(
            voter_id,
            repair_instruction,
            phase=3,
            role="counter_proposal_json_repair",
            tools=TOOL_DEFINITIONS,
            speaker=voter_id.upper(),
        )
        proposal = _extract_proposal(content)
        if proposal is None:
            return None
        return _with_sr_concurrent_group(proposal, self._current_ap_states or ap_state)

    def _emit_final_decision(self, proposer_id: str, proposal: dict) -> dict | None:
        if self.logger:
            self.logger.phase_start(5, "输出最终决策")

        instruction = (
            "所有 AP 已同意提案。\n"
            f"已通过的提案参数 JSON 如下，请最终决策必须与它保持一致：\n{_json(proposal)}\n\n"
            "请输出最终的 JSON 决策方案（严格遵守格式，JSON 内不得有注释），"
            "JSON 顶层键必须是 ap1、ap2、ap3，"
            "然后在下一行写【协商结束】。"
        )
        content = self._speak_and_log(
            proposer_id,
            instruction,
            phase=5,
            role="decision",
            speaker=proposer_id.upper(),
        )

        decision = _extract_json(content)
        # 保留原始提案里的 _sr 元数据（LLM 被要求只输出 ap1/ap2/ap3 键，
        # 会丢弃 concurrent_group，导致 validator 无法正确过滤非并发 AP）
        if decision is not None and isinstance(proposal, dict):
            for meta_key in ("_sr", "sr", "concurrent_group"):
                if meta_key in proposal and meta_key not in decision:
                    decision[meta_key] = proposal[meta_key]
        if self.logger:
            self.logger.final_decision(decision, content)
            self.logger.record_decision_parameters(
                decision,
                strategy=_infer_strategy_from_proposal(proposal),
            )
        return decision

    # ──────────────────────────────────────────────────────────────────────
    # 公开入口
    # ──────────────────────────────────────────────────────────────────────

    def run(self, ap_state: dict) -> list[dict]:
        """
        执行完整协商流程。

        Returns:
            conversation_log — list of {"speaker": str, "content": str}
        """
        started_at = time.time()
        # 字段白名单 + EDCA 解码：保留协商关心的字段，并对缺失业务字段使用中性默认值。
        ap_state = apply_profile(ap_state)
        self._current_ap_states = ap_state

        # 阶段一：广播（ap1 → ap2 → ap3）
        self._phase_broadcast(ap_state)

        # 阶段二：触发判断（不预定策略，由提案 AP 自主选路）
        if not self._is_negotiation_needed(ap_state):
            print("\n[NOOP] 网络状况良好，无需协商。")
            if self.logger:
                self.logger.phase_start(2, "NOOP：无需协商")
            self._finish_session("noop", 0, started_at)
            return self.conversation_log

        print("\n[协商触发] 首个提案方 AP1 自主选路")
        if self.logger:
            self.logger.phase_start(2, "协商触发，等待 AP1 自主选路")

        MAX_VALIDATION_RETRIES = 3
        MAX_TURNS = 30          # 单轮内最大发言次数（安全上限）
        proposal_num = 0        # 累计提案编号
        strategy = "co_edca"    # 初始占位；实际值在首个提案后由 _infer_strategy_from_proposal 确定

        # 外层循环：Validator 未通过时从 ap1 重新提案
        for retry in range(MAX_VALIDATION_RETRIES):
            if retry > 0:
                print(f"\n[重试] Validator 未通过，AP1 重新发起提案（第 {retry + 1} 次尝试）")

            # 阶段三：首轮提案固定由 ap1 发起（AP1 自主选路）
            current_proposer = "ap1"
            proposal_num += 1
            proposal = self._phase_propose(current_proposer, ap_state)
            if proposal is None:
                print("\n[错误] 提案方未输出可解析的参数 JSON，协商终止。")
                self._finish_session("proposal_parse_error", proposal_num, started_at)
                return self.conversation_log

            strategy = _infer_strategy_from_proposal(proposal)

            agree_set: set[str] = set()

            # 循环迭代器：固定顺序 ap1→ap2→ap3→ap1→…，从 ap1 之后开始
            ap_cycle = itertools.cycle(AP_IDS)
            while next(ap_cycle) != "ap1":
                pass

            # 阶段四：循环投票
            for _ in range(MAX_TURNS):
                voter_id = next(ap_cycle)

                if voter_id == current_proposer:
                    continue  # 提案方跳过自己

                vote_result, content = self._phase_vote_single(
                    voter_id, current_proposer, strategy, proposal_num, proposal
                )

                if vote_result in ("agree", "abstain"):
                    agree_set.add(voter_id)
                    non_proposers = {ap for ap in AP_IDS if ap != current_proposer}
                    if agree_set >= non_proposers:
                        # 全票通过 → 输出最终决策并验证
                        decision = self._emit_final_decision(current_proposer, proposal)

                        precheck = validate_decision(
                            ap_state, decision, strategy,
                            observed_state=ap_state,
                            observed_is_real=False,
                        )
                        print(
                            f"\n[Validator] 参数预检 {'通过' if precheck['approved'] else '未通过'}"
                            f" — {precheck['summary']}"
                        )
                        if self.logger:
                            self.logger.validation_result(precheck)

                        if not precheck["approved"]:
                            err_detail = "；".join(precheck.get("global_errors") or [])
                            self._record(
                                "VALIDATOR",
                                f"[参数预检未通过] {precheck.get('summary', '')}"
                                + (f"\n具体问题：{err_detail}" if err_detail else ""),
                            )
                            break

                        session_id = self.logger.session_id if self.logger else ""
                        self._push_decision(decision, strategy, session_id)

                        observed_state, obs_error, obs_real = (
                            self._collect_observed_state(ap_state)
                        )
                        validation = validate_decision(
                            ap_state, decision, strategy,
                            observed_state=observed_state,
                            observed_is_real=obs_real,
                        )
                        if obs_error:
                            validation["approved"] = False
                            validation["global_errors"].insert(0, obs_error)
                            validation["summary"] = f"验证失败（策略={strategy}）：{obs_error}"

                        print(
                            f"\n[Validator] {'通过' if validation['approved'] else '未通过'}"
                            f" — {validation['summary']}"
                        )
                        if self.logger:
                            self.logger.validation_result(validation)

                        if validation["approved"]:
                            print("\n协商成功完成。")
                            self.last_decision = decision
                            self.last_strategy = strategy
                            self._finish_session("success", proposal_num, started_at)
                            return self.conversation_log

                        # 验证未通过：将原因写入对话记录，让 AP1 重提案时可见
                        err_detail = "；".join(validation.get("global_errors") or [])
                        self._record(
                            "VALIDATOR",
                            f"[验证未通过] {validation.get('summary', '')}"
                            + (f"\n具体问题：{err_detail}" if err_detail else ""),
                        )
                        break
                else:  # reject
                    # 反对者立即成为新提案方
                    new_proposal = self._phase_counter_propose(
                        voter_id, content, proposal_num + 1, ap_state
                    )
                    if new_proposal is not None:
                        proposal_num += 1
                        current_proposer = voter_id
                        strategy = _infer_strategy_from_proposal(new_proposal)
                        proposal = new_proposal
                        agree_set = set()
                        # ap_cycle 已自然推进到 voter_id 之后，无需额外操作
                    else:
                        print(f"\n[警告] {voter_id.upper()} 反提案解析失败，跳过本轮。")
            else:
                # for 循环正常结束（未 break）= 达到 MAX_TURNS
                print(f"\n[超时] 达到最大轮次 {MAX_TURNS}，协商终止。")
                self._finish_session("max_turns_exceeded", proposal_num, started_at)
                return self.conversation_log

        print(f"\n[失败] 达到最大验证重试次数 {MAX_VALIDATION_RETRIES}，协商终止。")
        self._finish_session("max_retries_exceeded", proposal_num, started_at)
        return self.conversation_log

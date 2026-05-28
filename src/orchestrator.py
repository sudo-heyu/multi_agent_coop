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
from .tools.registry import TOOL_DEFINITIONS, make_executor
from .validator import validate_decision

AP_IDS = ["ap1", "ap2", "ap3"]
MAX_VOTE_ROUNDS = 3

# 按工具名预分组，避免重复过滤
_GET_LATEST_STATE = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "get_latest_ap_states"]
_SR_PROPOSE_TOOLS = [
    t for t in TOOL_DEFINITIONS
    if t["function"]["name"] in {
        "analyze_sr_interference",
        "compute_sr_feasible_ranges",
        "evaluate_sr_candidate",
        "rank_sr_candidates",
    }
]
_VALIDATE_SR   = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "evaluate_sr_candidate"]
_VALIDATE_EDCA = [t for t in TOOL_DEFINITIONS if t["function"]["name"] == "validate_edca_proposal"]


def _tools_for_propose(strategy: str) -> list:
    if strategy == "co_sr":
        return _GET_LATEST_STATE + _SR_PROPOSE_TOOLS
    if strategy == "co_edca":
        return _GET_LATEST_STATE + _VALIDATE_EDCA
    return TOOL_DEFINITIONS  # joint


def _tools_for_vote(strategy: str) -> list:
    if strategy == "co_sr":
        return _GET_LATEST_STATE + _VALIDATE_SR
    if strategy == "co_edca":
        return _GET_LATEST_STATE + _VALIDATE_EDCA
    return _GET_LATEST_STATE + _VALIDATE_SR + _VALIDATE_EDCA  # joint



def _strategy_label(strategy: str) -> str:
    return {
        "co_sr":  "Co-SR",
        "co_edca": "Co-EDCA",
        "joint":  "Co-SR + Co-EDCA",
        "noop":   "NOOP",
    }.get(strategy, strategy)


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
                    return hit
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                hit = _matches(parsed)
                if hit is not None:
                    return hit
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
            return " " + " ".join(parts)
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
            lines.append(f"  {status_fail('错误:')} {result['error']}")
        for ap_id in AP_IDS:
            state = states.get(ap_id, {}) if isinstance(states, dict) else {}
            neighbors = state.get("neighbor_rssi_dbm") or {}
            nbr_text  = " ".join(f"{ap}:{int(rssi)}" for ap, rssi in neighbors.items()) or "—"
            lines.append(
                f"  {ap_label(ap_id)}"
                f" TX={_fmt_num(state.get('tx_power_dbm'), 'dBm')}"
                f" busy={_fmt_pct(state.get('channel_busy_ratio'))}"
                f" retry={_fmt_pct(state.get('tx_retries_ratio'))}"
                f" STA={_fmt_num(state.get('sta_rssi_dbm'), 'dBm')}"
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
            f"  {link_summary}  干扰源:{top_src}  受害:{top_vic}",
        ]
        return "\n".join(lines)

    # ── compute_sr_feasible_ranges ────────────────────────────────────
    if tname == "compute_sr_feasible_ranges":
        lines  = [hdr]
        ranges = result.get("ranges", {})
        for ap_id in AP_IDS:
            item    = ranges.get(ap_id, {}) if isinstance(ranges, dict) else {}
            cur     = _fmt_num(item.get("current_dbm"), "")
            lo      = _fmt_num(item.get("min_dbm"), "")
            hi      = _fmt_num(item.get("max_dbm"), "")
            reasons = dim(",".join(item.get("upper_reasons", [])) or "—")
            lines.append(f"  {ap_label(ap_id)} {cur}→[{lo},{hi}]dBm  上界:{reasons}")
        seed = result.get("feasible_seed")
        if seed:
            seed_text = " ".join(f"{ap}={_fmt_num(p,'dBm')}" for ap, p in seed.items())
            lines.append(f"  {dim('可行起点:')} {seed_text}")
        return "\n".join(lines)

    # ── evaluate_sr_candidate / validate_sr_proposal ──────────────────
    if tname in ("validate_sr_proposal", "evaluate_sr_candidate"):
        all_ok = result.get("valid", False)
        flag   = status_ok("全部OK") if all_ok else status_fail("FAIL")
        lines  = [f"{hdr}  {flag}"]

        if not all_ok:
            per_ap = result.get("per_ap", {})
            for ap_id in AP_IDS:
                item = per_ap.get(ap_id, {}) if isinstance(per_ap, dict) else {}
                if not item.get("valid"):
                    errors   = item.get("errors") or []
                    err_text = "; ".join(e[:60] for e in errors[:2]) if errors else "未知"
                    lines.append(f"  {status_fail(ap_id + ' FAIL:')} {err_text}")

        score = result.get("score") if isinstance(result, dict) else None
        if isinstance(score, dict):
            lines.append(
                f"  {dim('代价:')} 总降功={_fmt_num(score.get('total_power_drop_db'), 'dB')}"
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
                f"    #{item.get('rank')} {item.get('name')} {sflag}: {pw_txt}"
                f" | 降功={_fmt_num(score.get('total_power_drop_db'),'dB')}"
                f" 最大={_fmt_num(score.get('max_single_ap_drop_db'),'dB')}"
            )
        return "\n".join(lines)

    # ── validate_edca_proposal ────────────────────────────────────────
    if tname == "validate_edca_proposal":
        per_ap_ok = all(
            result.get(ap_id, {}).get("valid", True)
            for ap_id in AP_IDS
            if isinstance(result.get(ap_id), dict)
        )
        effectiveness = result.get("effectiveness") or {}
        has_warn = not effectiveness.get("all_ok", True)

        if not per_ap_ok:
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

        for ap_id in AP_IDS:
            item = result.get(ap_id, {})
            if isinstance(item, dict) and not item.get("valid"):
                lines.append(
                    f"  {status_fail('[FAIL]')} {ap_id}: {'; '.join(item.get('errors') or [])}"
                )

        for ap_id, eff in (effectiveness.get("per_ap") or {}).items():
            for w in eff.get("warnings") or []:
                lines.append(f"  {status_warn('[WARN]')} {ap_id}: {w}")
        for w in (effectiveness.get("fairness") or {}).get("warnings") or []:
            lines.append(f"  {status_warn('[WARN]')} 公平性: {w}")

        return "\n".join(lines)

    compact = json.dumps(result, ensure_ascii=False)
    if len(compact) > 200:
        compact = compact[:200] + "..."
    return f"{hdr}\n  {compact}"



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
        self.observation_state_getter = observation_state_getter
        self.observation_wait_seconds = observation_wait_seconds
        # {"ap1": "http://192.168.1.11:5002", ...}；None 表示不推送
        self.executor_endpoints: dict[str, str] | None = executor_endpoints

    # ──────────────────────────────────────────────────────────────────────
    # 决策推送
    # ──────────────────────────────────────────────────────────────────────

    def _push_decision(self, decision: dict, strategy: str, session_id: str) -> None:
        """
        并发向所有香蕉派执行服务推送最终决策。
        每个 AP 只收到属于自己的 params 子集。
        """
        if not self.executor_endpoints:
            return

        import requests as _req

        def _send(ap_id: str, url: str) -> tuple[str, bool, str]:
            params = decision.get(ap_id) or decision.get(ap_id.upper()) or {}
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
                return ap_id, ok, str(msg)
            except Exception as exc:
                return ap_id, False, str(exc)

        print("\n[Executor] 开始向香蕉派推送决策...")
        with ThreadPoolExecutor(max_workers=len(self.executor_endpoints)) as pool:
            futures = {
                pool.submit(_send, ap_id, url): ap_id
                for ap_id, url in self.executor_endpoints.items()
            }
            for future in as_completed(futures):
                ap_id, ok, msg = future.result()
                status = "✓" if ok else "✗"
                print(f"  [{status}] {ap_id.upper()}: {msg}")

    # ──────────────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────────────

    def _record(self, speaker: str, content: str) -> None:
        self.conversation_log.append({"speaker": speaker, "content": content})

    def _collect_observed_state(
        self, fallback_state: dict
    ) -> tuple[dict, str | None, bool]:
        if self.observation_state_getter is None:
            return fallback_state, None, False

        wait_seconds = max(0.0, self.observation_wait_seconds)
        print(f"\n[Validator] 等待观测周期 {wait_seconds:g}s 后读取最终状态...")
        if wait_seconds:
            time.sleep(wait_seconds)

        try:
            return self.observation_state_getter(), None, True
        except Exception as exc:
            return {}, f"观测状态获取失败: {exc}", False

    def _agreed(self, content: str) -> bool:
        vote = _extract_json(content)
        if isinstance(vote, dict) and isinstance(vote.get("agreed"), bool):
            return vote["agreed"]

        without_negative = content.replace("不同意", "").replace("反对", "")
        return "同意" in without_negative

    def _determine_strategy(self, ap_state: dict) -> str:
        """Deterministic fallback used by tests and non-LLM callers."""
        sr_triggered = any(
            float(rssi) > -70.0
            for state in ap_state.values()
            for rssi in (state.get("neighbor_rssi_dbm") or {}).values()
        )
        edca_triggered = any(
            float(state.get("channel_busy_ratio", 0.0)) > 0.60
            and float(state.get("tx_retries_ratio", 0.0)) > 0.15
            for state in ap_state.values()
        )

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
    ) -> str:
        """调用 agent.speak_stream()，流式打印、处理工具调用、记录日志。"""
        executor = (
            make_executor(
                self._current_ap_states,
                state_getter=self.observation_state_getter,
                state_setter=self._set_current_ap_states,
            )
            if tools else None
        )
        speaker = speaker or ap_id.upper()

        def on_tool(tool_name: str, raw_args: dict, result_dict: dict, dur_ms: float) -> None:
            print(f"\n{_format_tool_console(tool_name, raw_args, result_dict, dur_ms)}", flush=True)

        _CHUNK_BATCH = 40   # 每积累 40 字符写一次日志，平衡实时性与 I/O

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
                print(strip_md(chunk), end="", flush=True)
                _buf.append(chunk)
                _buf_len += len(chunk)
                if _buf_len >= _CHUNK_BATCH and self.logger:
                    self.logger.agent_speak_chunk(ap_id, "".join(_buf))
                    _buf = []
                    _buf_len = 0
            if _buf and self.logger:
                self.logger.agent_speak_chunk(ap_id, "".join(_buf))
            print()

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
            if not claimed_without_call or attempt == 1:
                break

            retry_hint = (
                "\n\n[系统提醒] 系统没有收到真实 tool_call。"
                "如果需要使用工具，必须发起真实 tool_call；"
                "不能只在文本中声称已调用工具。请重新回答。"
            )
            print(
                "\n[协调者] 检测到上一轮回复声称使用工具，但未产生真实工具调用；要求重试。",
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

        for ap_id in AP_IDS:
            state_json = json.dumps(ap_state[ap_id], ensure_ascii=False, indent=2)
            instruction = (
                f"请广播你（{ap_id.upper()}）的当前状态。\n"
                "发言开头先明确说出你是哪个 AP，然后用自然语言完整说明你的实测参数，"
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
        strategy: str,
    ) -> dict | None:
        if self.logger:
            self.logger.phase_start(3, f"{proposer_id.upper()} 发起提案 | {strategy}")

        strategy_hint = {
            "co_sr":   "Co-SR（降低 TX Power，减少 OBSS 干扰）",
            "co_edca": "Co-EDCA（调整 CWmin / CWmax / AIFSN，缓解信道拥塞）",
            "joint":   "联合（同时调整 TX Power 与 EDCA 参数）",
        }[strategy]

        state_summary = json.dumps(ap_state, ensure_ascii=False, indent=2)
        tools = _tools_for_propose(strategy)

        # 根据策略生成字段约束说明
        if strategy == "co_sr":
            field_constraint = (
                "【重要】Co-SR 路径只调整 TX Power，提案 JSON 中只能包含 tx_power_dbm 字段，"
                "绝对不能包含 CWmin、CWmax、AIFSN 等 EDCA 字段。"
            )
        elif strategy == "co_edca":
            field_constraint = (
                "【重要】Co-EDCA 路径只调整 EDCA 参数，提案 JSON 中只能包含 CWmin、CWmax、AIFSN 字段，"
                "绝对不能包含 tx_power_dbm 字段。"
            )
        else:  # joint
            field_constraint = "联合路径需同时包含 tx_power_dbm 和 CWmin/CWmax/AIFSN 字段。"

        history_hint = (
            "【重要】请先完整阅读上方的对话记录。"
            "如果其中已有历史提案和拒绝原因，你的提案必须明确回应各方此前提出的约束顾虑，"
            "而不是重复一个已被否决的方案。协商历史越长，越需要向各方约束的交集靠拢。\n\n"
        )
        instruction = (
            f"你（{proposer_id.upper()}）是本轮的提案方，请发起参数调整提案。\n\n"
            f"{history_hint}"
            f"协商路径：{strategy_hint}\n\n"
            f"所有 AP 的初始状态数据（供参考）：\n{state_summary}\n\n"
            "请先调用 get_latest_ap_states 获取最新状态。"
            "如果协商路径包含 Co-SR，必须依次使用 analyze_sr_interference、"
            "compute_sr_feasible_ranges，并自行提出至少两个候选 TX Power 方案，"
            "再调用 rank_sr_candidates 比较候选，并用 evaluate_sr_candidate 验证最终候选后选定最终方案。"
            "Co-SR 的主要目标是最小必要降功率；不要选择 1 dBm 这类过度保守的最低功率，"
            "除非靠近可行区间上界的候选无法满足约束。"
            "如果协商路径包含 Co-EDCA，请根据最新状态自行提出 EDCA 候选，"
            "并调用 validate_edca_proposal 验证最终候选。\n"
            f"{field_constraint}\n"
            "然后简洁阐述提案（不要逐步复述工具调用过程）：\n"
            "  · 当前核心问题是什么，关键指标数据是什么\n"
            "  · 为什么走这条协商路径\n"
            "  · 每个 AP 的最终参数是多少，为何选择该方案（如有历史争议，说明如何化解）\n"
            "  · 预期改善和主要权衡\n"
            "最终提交前必须调用 evaluate_sr_candidate 或 validate_edca_proposal 自检相关约束。\n"
            "提案末尾必须用 ```json 代码块附上参数摘要，JSON 顶层键必须是 ap1/ap2/ap3。"
        )
        content = self._speak_and_log(
            proposer_id,
            instruction,
            phase=3,
            role="proposer",
            tools=tools,
            speaker=proposer_id.upper(),
        )
        proposal = _extract_proposal(content)
        if proposal is not None:
            return proposal

        repair_instruction = (
            "上一轮提案没有输出可解析的参数 JSON。\n"
            "请只输出一个 ```json 代码块，JSON 顶层键必须是 ap1、ap2、ap3，"
            "每个 AP 内包含本次协商路径需要调整的具体参数。不要写解释。"
        )
        content = self._speak_and_log(
            proposer_id,
            repair_instruction,
            phase=3,
            role="proposal_json_repair",
            tools=None,
            speaker=proposer_id.upper(),
        )
        return _extract_proposal(content)

    def _phase_vote_single(
        self,
        voter_id: str,
        proposer_id: str,
        strategy: str,
        proposal_num: int,
        proposal: dict,
    ) -> tuple[bool, str]:
        """单个 AP 对当前提案投票。若反对，要求在同一回复中给出反提案 JSON。"""
        if self.logger:
            self.logger.phase_start(
                4, f"{voter_id.upper()} 投票（提案#{proposal_num}，提案方 {proposer_id.upper()}）"
            )

        verify_hint = {
            "co_sr":   "关注你自己的 TX Power、evaluate_sr_candidate 返回的 valid/errors、STA RSSI/SINR/CCA 余量，以及它对你业务的直接影响",
            "co_edca": "关注你自己的 CWmin/CWmax/AIFSN 建议值、工具返回的 valid/errors，以及退避变化是否可接受",
            "joint":   "关注你自己的 TX Power 与 EDCA 建议值、工具返回的 valid/errors，以及组合调整是否可接受",
        }[strategy]

        proposal_json = _json(proposal)
        instruction = (
            "【第一步】请完整阅读上方的对话记录，梳理此前所有提案及每次拒绝的具体原因。\n\n"
            f"【第二步】验算 {proposer_id.upper()} 的最新提案（提案#{proposal_num}）"
            f"中针对你自己（{voter_id.upper()}）的参数调整建议。\n\n"
            f"最新提案参数：\n{proposal_json}\n\n"
            "请先调用 get_latest_ap_states 获取最新状态，再调用验算工具，"
            "然后用自然语言给出你的判断。"
            f"重点参考：{verify_hint}。\n\n"
            "不要套用固定模板，如果结果很简单可以简短直接。\n\n"
            "【同意时】回复末尾附：\n"
            "```json\n{\"agreed\": true, \"reason\": \"...\"}\n```\n\n"
            "【反对时】请在同一条回复中直接给出你的完整反提案。反提案须满足：\n"
            "  1. 明确说明你对当前提案的具体反对理由（卡在哪个指标或约束）\n"
            "  2. 综合对话记录中其他 AP 之前提出的所有顾虑，你的方案必须兼顾所有人的约束，"
            "而不是只满足你自己的需求\n"
            "  3. 若此前已有多次协商失败，请给出比之前所有提案更保守的参数，向各方约束的交集靠拢\n\n"
            "然后附两个 ```json 块：\n"
            "第一块：```json\n{\"agreed\": false, \"reason\": \"...\"}\n```\n"
            "第二块：完整参数反提案，JSON 顶层键必须是 ap1/ap2/ap3。"
        )
        content = self._speak_and_log(
            voter_id,
            instruction,
            phase=4,
            role="voter",
            tools=_tools_for_vote(strategy),
            speaker=voter_id.upper(),
        )

        agreed = self._agreed(content)
        if self.logger:
            self.logger.vote(voter_id, proposal_num, agreed, content)
        return agreed, content

    def _phase_counter_propose(
        self,
        voter_id: str,
        vote_content: str,
        strategy: str,
        proposal_num: int,
    ) -> dict | None:
        """从投票内容中提取反提案 JSON；提取失败则要求补充一次纯 JSON 回复。"""
        proposal = _extract_proposal(vote_content)
        if proposal is not None:
            return proposal

        repair_instruction = (
            "你已表示反对，但回复中未找到可解析的参数 JSON。\n"
            "请回顾上方完整协商历史，综合所有 AP 此前提出的约束和顾虑，"
            "给出一个能兼顾所有人需求的反提案。\n"
            "请只输出一个 ```json 代码块，JSON 顶层键必须是 ap1、ap2、ap3，"
            "每个 AP 内包含本次协商路径需要调整的具体参数。不要写解释。"
        )
        content = self._speak_and_log(
            voter_id,
            repair_instruction,
            phase=3,
            role="counter_proposal_json_repair",
            tools=None,
            speaker=voter_id.upper(),
        )
        return _extract_proposal(content)

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
        if self.logger:
            self.logger.final_decision(decision, content)
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
        self._current_ap_states = ap_state

        # 阶段一：广播（ap1 → ap2 → ap3）
        self._phase_broadcast(ap_state)

        # 阶段二：确定性策略决策（不使用 LLM 协调者）
        strategy = self._determine_strategy(ap_state)
        print(f"\n[策略决策] {_strategy_label(strategy)} | 首个提案方: AP1")
        if self.logger:
            self.logger.phase_start(2, f"策略决策: {_strategy_label(strategy)}")
            self.logger.strategy_decided(strategy, "ap1")

        if strategy == "noop":
            print("网络状况良好，无需协商。")
            self._finish_session("noop", 0, started_at)
            return self.conversation_log

        MAX_VALIDATION_RETRIES = 3
        MAX_TURNS = 30          # 单轮内最大发言次数（安全上限）
        proposal_num = 0        # 累计提案编号

        # 外层循环：Validator 未通过时从 ap1 重新提案
        for retry in range(MAX_VALIDATION_RETRIES):
            if retry > 0:
                print(f"\n[重试] Validator 未通过，AP1 重新发起提案（第 {retry + 1} 次尝试）")

            # 阶段三：首轮提案固定由 ap1 发起
            current_proposer = "ap1"
            proposal_num += 1
            proposal = self._phase_propose(current_proposer, ap_state, strategy)
            if proposal is None:
                print("\n[错误] 提案方未输出可解析的参数 JSON，协商终止。")
                self._finish_session("proposal_parse_error", proposal_num, started_at)
                return self.conversation_log

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

                agreed, content = self._phase_vote_single(
                    voter_id, current_proposer, strategy, proposal_num, proposal
                )

                if agreed:
                    agree_set.add(voter_id)
                    non_proposers = {ap for ap in AP_IDS if ap != current_proposer}
                    if agree_set >= non_proposers:
                        # 全票通过 → 输出最终决策并验证
                        decision = self._emit_final_decision(current_proposer, proposal)

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
                            self._finish_session("success", proposal_num, started_at)
                            session_id = self.logger.session_id if self.logger else ""
                            self._push_decision(decision, strategy, session_id)
                            return self.conversation_log

                        # 验证未通过：退出内层循环，外层重新从 ap1 提案
                        break
                else:
                    # 反对者立即成为新提案方
                    new_proposal = self._phase_counter_propose(
                        voter_id, content, strategy, proposal_num + 1
                    )
                    if new_proposal is not None:
                        proposal_num += 1
                        current_proposer = voter_id
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

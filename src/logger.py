"""
会话日志模块 — 结构化 JSONL + 控制台双轨输出

每次 orchestrator.run() 对应一个 JSONL 文件，按时间序列记录所有事件。

文件路径：logs/session_<YYYYMMDD_HHMMSS>_<session_id>.jsonl
参数时序：logs/state/state_trace_<YYYYMMDD_HHMMSS>_<session_id>.jsonl

━━━ 事件类型一览 ━━━
  session_start     运行开始（模型、场景、AP 初始状态）
  phase_start       协商阶段开始（阶段号 + 标签）
  tool_call         计算工具调用（工具名、输入、完整输出、耗时）
  agent_speak       Agent 发言（角色、指令、回复全文、耗时）
  vote              单个 AP 投票（赞成/反对、回复全文）
  round_result      单轮投票汇总（是否全票通过）
  final_decision    最终 JSON 决策（解析结果 + 原始回复）
  session_end       运行结束（结果、总轮数、总耗时）

━━━ Dashboard 兼容约定 ━━━
每行 JSON 固定携带三个字段：ts / session_id / event
其余字段随事件类型扩展，dashboard 按 event 字段分派处理。
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .console_style import status_label
from .persistence import ActionRecord, EventStore

# 绝对路径（仓库根的 logs/），不受进程工作目录影响：coordinator 路径下 MCP server
# 的 CWD 不一定是仓库根，相对 "logs" 会把日志写散；用绝对路径保证 run_openclaw 能 tail 到。
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
STATE_LOG_DIR = LOG_DIR / "state"
DEFAULT_EVENT_DB = LOG_DIR / "agent_memory.sqlite3"

# 控制台各事件的前缀标签（对齐输出）
_LABELS: dict[str, str] = {
    "session_start":      "会话开始",
    "phase_start":        "阶段",
    "tool_call":          "工具",
    "agent_speak":        "发言",
    "vote":               "投票",
    "round_result":       "轮次",
    "final_decision":     "决策",
    "validation_result":  "验证",
    "session_end":        "会话结束",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ts_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


def _extract_ap_parameters(ap_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields = (
        "tx_power_dbm",
        "obss_pd_dbm",
        "cwmin",
        "cwmax",
        "aifsn",
        "CWmin",
        "CWmax",
        "AIFSN",
        "be_cwmin", "be_cwmax", "be_aifsn",
        "vi_cwmin", "vi_cwmax", "vi_aifsn",
        "BE_CWmin", "BE_CWmax", "BE_AIFSN",
        "VI_CWmin", "VI_CWmax", "VI_AIFSN",
        "Data_rate_to_bandwidth_ratio",
        "tx_retries_ratio",
        "throughput_mbps_iperf",
        "throughput_mbps_user",
        "ac_iperf",
        "ac_user",
        "latency_ms",
        "packet_loss_pct",
        "sta_rssi_dbm",
        "noise_floor_dbm",
        "neighbor_rssi_dbm",
    )
    return {
        ap_id: {
            field: state.get(field)
            for field in fields
            if isinstance(state, dict) and field in state
        }
        for ap_id, state in ap_state.items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# 核心类
# ─────────────────────────────────────────────────────────────────────────────

class SessionLogger:
    """
    单次协商会话的结构化日志记录器。

    用法：
        logger = SessionLogger()
        logger.session_start(model, scene, ap_state)
        ...
        logger.session_end(outcome, rounds)
    """

    def __init__(
        self,
        session_id: str | None = None,
        verbose: bool = True,
        event_sink=None,
        mode: str | None = None,
        event_store: EventStore | None = None,
        resume: bool = False,
    ):
        """
        Args:
            session_id:  可手动指定；默认自动生成 8 位十六进制 ID
            verbose:     True = 同步输出控制台日志；False = 仅写文件
            event_sink:  可选的 Callable[[dict], None]，每次 _write 后同步调用
                         （用于向 dashboard 实时队列推送事件）
        """
        self.session_id: str = session_id or uuid.uuid4().hex[:8]
        self.verbose: bool = verbose
        self._event_sink = event_sink
        self.mode = mode
        self._start_ms: float = _ts_ms()

        LOG_DIR.mkdir(exist_ok=True)
        STATE_LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        suffix = "_resume" if resume else ""
        self.log_path: Path = LOG_DIR / f"session_{ts}_{self.session_id}{suffix}.jsonl"
        self.state_trace_path: Path = STATE_LOG_DIR / f"state_trace_{ts}_{self.session_id}{suffix}.jsonl"
        self._fh = open(self.log_path, "w", encoding="utf-8")
        self._state_fh = open(self.state_trace_path, "w", encoding="utf-8")
        store_enabled = os.environ.get("MULTIAP_EVENT_STORE", "1").lower() not in {
            "0", "false", "no", "off"
        }
        db_path = Path(os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB)))
        self._event_store = event_store or (EventStore(db_path) if store_enabled else None)

    # ──────────────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────────────

    def _write(self, event: str, **kw) -> None:
        row = self._event_row(event, kw)
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self._event_sink is not None:
            try:
                self._event_sink(row)
            except Exception:
                pass

    def _event_row(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        ts = _now()
        event_id = uuid.uuid4().hex
        sequence = None
        if self._event_store is not None:
            event_id, sequence = self._event_store.append_event(
                self.session_id,
                event,
                payload,
                event_id=event_id,
                occurred_at=ts,
            )
        return {
            "ts":         ts,
            "session_id": self.session_id,
            "event":      event,
            "event_id":   event_id,
            "sequence":   sequence,
            **payload,
        }

    def _write_file_only(self, event: str, **kw) -> None:
        """写入 JSONL 文件，不通知 event_sink（避免与高频推送重复）。"""
        row = self._event_row(event, kw)
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()  # sink 错误不中断会话

    def _write_state_trace(self, event: str, **kw) -> None:
        row = {
            "ts":         _now(),
            "session_id": self.session_id,
            "event":      event,
            **kw,
        }
        self._state_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._state_fh.flush()

    def _console(self, event: str, msg: str) -> None:
        if not self.verbose:
            return
        label = _LABELS.get(event, event)
        print(f"{status_label('LOG')} {label} | {msg}")

    # ──────────────────────────────────────────────────────────────────────
    # 事件 API
    # ──────────────────────────────────────────────────────────────────────

    def session_start(self, model: str, scene: str, ap_state: dict) -> None:
        """运行开始，记录完整初始状态（dashboard 用于渲染对比表）。"""
        if self._event_store is not None:
            self._event_store.start_run(
                self.session_id,
                mode=self.mode,
                scene=scene,
                model=model,
                metadata={
                    "log_path": str(self.log_path),
                    "state_trace_path": str(self.state_trace_path),
                },
            )
        self._write("session_start", model=model, scene=scene, ap_state=ap_state)
        self.record_state_snapshot(
            "initial",
            ap_state,
            source="session_start",
            model=model,
            scene=scene,
        )
        self._console("session_start",
                      f"id={self.session_id} model={model} scene={scene} "
                      f"log={self.log_path} state_trace={self.state_trace_path}")

    def session_resume(self, checkpoint: dict[str, Any]) -> None:
        """Attach a new logger process to an existing durable run."""
        if self._event_store is None or self._event_store.get_run(self.session_id) is None:
            raise ValueError(f"cannot resume unknown run: {self.session_id}")
        self._write(
            "session_resumed",
            boundary=checkpoint.get("boundary"),
            projection_version=checkpoint.get("projection_version"),
        )

    def save_negotiation_checkpoint(
        self,
        boundary: str,
        state: dict[str, Any],
        *,
        safe_to_resume: bool = True,
    ) -> None:
        if self._event_store is None:
            return
        self._event_store.save_projection(
            self.session_id,
            boundary=boundary,
            state=state,
            safe_to_resume=safe_to_resume,
        )
        self._write(
            "negotiation_checkpoint",
            boundary=boundary,
            safe_to_resume=safe_to_resume,
        )

    def save_session_memory(
        self,
        memory: dict[str, Any],
        summary_text: str,
        budget_chars: int,
    ) -> None:
        if self._event_store is None:
            return
        self._event_store.save_session_memory(
            self.session_id,
            memory=memory,
            summary_text=summary_text,
            budget_chars=budget_chars,
        )
        self._write(
            "session_memory_updated",
            summarized_turns=memory.get("summarized_turns", 0),
            entry_count=len(memory.get("entries") or []),
            budget_chars=budget_chars,
        )

    def save_agent_session_memory(
        self, agent_id: str, memory: dict[str, Any], summary_text: str,
        budget_chars: int,
    ) -> None:
        if self._event_store is None:
            return
        self._event_store.save_agent_session_memory(
            self.session_id, agent_id, memory=memory,
            summary_text=summary_text, budget_chars=budget_chars,
        )
        self._write(
            "agent_session_memory_updated", agent=agent_id,
            summarized_turns=memory.get("summarized_turns", 0),
            entry_count=len(memory.get("entries") or []), budget_chars=budget_chars,
        )

    def workspace_memory_failed(self, agent_id: str, error: str) -> None:
        self._write("workspace_memory_failed", agent=agent_id, error=error)

    def context_manifest(self, agent_id: str, manifest: dict[str, Any]) -> None:
        self._write("context_manifest", agent=agent_id, **manifest)

    def recall_episodes(
        self,
        state: dict[str, Any],
        *,
        limit: int = 3,
        min_quality: float = 0.5,
        require_evaluation: bool = True,
    ) -> list[dict[str, Any]]:
        if self._event_store is None:
            return []
        from .memory import find_similar_episodes
        episodes = find_similar_episodes(
            self._event_store,
            state,
            limit=limit,
            min_quality=min_quality,
            exclude_run_id=self.session_id,
            require_evaluation=require_evaluation,
        )
        self._write(
            "episodic_memory_recalled",
            matches=[
                {
                    "episode_id": item["episode_id"],
                    "run_id": item["run_id"],
                    "similarity": item["similarity"],
                    "quality_score": item["quality_score"],
                }
                for item in episodes
            ],
        )
        return episodes

    def recall_agent_episodes(
        self, agent_id: str, state: dict[str, Any], *, limit: int = 3,
        min_quality: float = 0.5,
    ) -> list[dict[str, Any]]:
        if self._event_store is None:
            return []
        from .memory import find_agent_episodes
        episodes = find_agent_episodes(
            self._event_store, agent_id, state,
            limit=limit, min_quality=min_quality,
        )
        self._write(
            "agent_episodic_memory_recalled", agent=agent_id,
            run_ids=[item["run_id"] for item in episodes],
        )
        return episodes

    def recall_episode_memory(
        self, state: dict[str, Any], *, positive_limit: int = 3,
        warning_limit: int = 2,
    ) -> dict[str, list[dict[str, Any]]]:
        if self._event_store is None:
            return {"positive": [], "warnings": []}
        from .memory import find_episode_memory
        result = find_episode_memory(
            self._event_store, state, positive_limit=positive_limit,
            warning_limit=warning_limit, exclude_run_id=self.session_id,
        )
        self._write(
            "episodic_memory_channels_recalled",
            positive_run_ids=[e["run_id"] for e in result["positive"]],
            warning_run_ids=[e["run_id"] for e in result["warnings"]],
        )
        return result

    def memory_reliance(
        self, proposer: str, *, episodes=(), warnings=(), rules=(),
        agent_episodes=(), proposal_num: int | None = None,
    ) -> None:
        """R3：把本次提案实际依赖的记忆（含注入时信任分与预测）落入事件流。

        predicted 字段是该记忆的可证伪预测（案例=历史 verdict，规律=主导 verdict），
        供执行后评估比对，判定证实或证伪（矛盾账本）。
        """
        entries = []
        for item in episodes:
            entries.append({
                "memory_kind": "episode", "memory_key": item.get("run_id"),
                "role": "positive", "trust": item.get("trust"),
                "predicted": (item.get("evaluation") or {}).get("final_verdict"),
            })
        for item in warnings:
            entries.append({
                "memory_kind": "episode", "memory_key": item.get("run_id"),
                "role": "warning", "trust": item.get("trust"),
                "predicted": "degraded",
            })
        for rule in rules:
            entries.append({
                "memory_kind": "rule", "memory_key": rule.get("rule_id"),
                "role": "rule", "trust": rule.get("trust"),
                "predicted": rule.get("dominant_verdict"),
            })
        for agent_id, item in agent_episodes:
            entries.append({
                "memory_kind": "agent_episode",
                "memory_key": f"{item.get('run_id')}:{agent_id}",
                "role": "agent_local", "trust": item.get("trust"),
                "predicted": (item.get("evaluation") or {}).get("final_verdict"),
            })
        self._write(
            "memory_reliance", proposer=proposer,
            proposal_num=proposal_num, entries=entries,
        )

    def load_recalled_memory(
        self, episode_ids: list[str], warning_ids: list[str], rule_ids: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if self._event_store is None:
            return {"positive": [], "warnings": [], "rules": []}
        positive = [item for episode_id in episode_ids
                    if (item := self._event_store.get_episode(episode_id=episode_id)) is not None]
        warnings = [item for episode_id in warning_ids
                    if (item := self._event_store.get_episode(episode_id=episode_id)) is not None]
        wanted = set(rule_ids)
        rules = [r for r in self._event_store.list_rules(include_conflicted=False)
                 if r["rule_id"] in wanted]
        return {"positive": positive, "warnings": warnings, "rules": rules}

    def schedule_outcome_evaluations(
        self,
        baseline_state: dict[str, Any],
        windows,
    ) -> list[dict[str, Any]]:
        """决策生效后登记多窗口效果评估（收割由 run_openclaw / memory_admin 完成）。"""
        if self._event_store is None or not windows:
            return []
        from .memory import schedule_outcome_evaluations
        scheduled = schedule_outcome_evaluations(
            self._event_store, self.session_id, baseline_state, tuple(windows)
        )
        self._write(
            "outcome_evaluation_scheduled",
            windows=[
                {"window": item["window_label"], "due_at": item["due_at"]}
                for item in scheduled
            ],
        )
        return scheduled

    def recall_rules(
        self,
        state: dict[str, Any],
        *,
        scene: str | None = None,
        min_confidence: float = 0.5,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        if self._event_store is None:
            return []
        from .memory import find_matching_rules
        rules = find_matching_rules(
            self._event_store, state, scene=scene,
            min_confidence=min_confidence, limit=limit,
        )
        self._write(
            "semantic_rules_recalled",
            matches=[
                {
                    "rule_id": r["rule_id"],
                    "strategy": r["strategy"],
                    "dominant_verdict": r["dominant_verdict"],
                    "support": r["support"],
                    "confidence": r["confidence"],
                }
                for r in rules
            ],
        )
        return rules

    def record_state_snapshot(
        self,
        label: str,
        ap_state: dict[str, Any] | None,
        *,
        source: str,
        phase: int | None = None,
        role: str | None = None,
        model: str | None = None,
        scene: str | None = None,
    ) -> None:
        """
        记录一次 AP 完整状态快照到独立 JSONL 参数时序文件。

        state_trace 文件用于还原一次协商从开始到结束的参数和业务指标变化；
        每行都保留完整 ap_state，同时额外抽取关键参数便于后处理。
        """
        if ap_state is None:
            return
        if self._event_store is not None:
            self._event_store.record_snapshot(
                self.session_id,
                label=label,
                source=source,
                state=ap_state,
            )
        self._write_state_trace(
            "state_snapshot",
            label=label,
            source=source,
            phase=phase,
            role=role,
            model=model,
            scene=scene,
            parameters=_extract_ap_parameters(ap_state),
            ap_state=ap_state,
        )

    def record_proposal(
        self,
        proposal_num: int,
        proposer: str,
        strategy: str | None,
        proposal: dict[str, Any] | None,
        *,
        kind: str = "proposal",
    ) -> None:
        """记录一次解析成功的提案/反提案候选参数（协商中间参数演变）。

        与 agent_speak 的原文互补：这里是结构化参数，供参数时间线直接合并；
        kind: "proposal"=正常提案, "counter"=反对者接管的反提案。
        """
        if proposal is None:
            return
        payload = {
            "proposal_num": proposal_num,
            "proposer": proposer,
            "strategy": strategy,
            "kind": kind,
            "parameters": _extract_ap_parameters(proposal),
            "proposal": proposal,
        }
        self._write("proposal_params", **payload)
        self._write_state_trace("proposal_params", **payload)

    def record_telemetry_trace(self, action: str, path: str | None) -> None:
        """记录 state server 连续遥测 trace 的启停与文件路径（便于按 run 关联）。"""
        self._write("telemetry_trace", action=action, path=path)

    def record_decision_parameters(
        self,
        decision: dict[str, Any] | None,
        *,
        strategy: str | None = None,
        source: str = "final_decision",
    ) -> None:
        """记录最终下发目标参数；它代表决策目标，不代表已经观测生效。"""
        if decision is None:
            return
        self._write_state_trace(
            "decision_parameters",
            source=source,
            strategy=strategy,
            parameters=_extract_ap_parameters(decision),
            decision=decision,
        )

    def record_executor_apply(
        self,
        ap_id: str,
        *,
        ok: bool,
        url: str,
        payload: dict[str, Any],
        response: Any,
    ) -> None:
        """记录向香蕉派执行服务下发参数后的响应。"""
        self._write_state_trace(
            "executor_apply",
            ap_id=ap_id,
            ok=ok,
            url=url,
            payload=payload,
            response=response,
        )
        self._write(
            "executor_apply",
            ap_id=ap_id,
            ok=ok,
            url=url,
            payload=payload,
            response=response,
        )

    def start_step(
        self,
        name: str,
        *,
        retry_budget: int = 0,
        input_data: dict[str, Any] | None = None,
    ) -> str | None:
        if self._event_store is None:
            return None
        step_id = self._event_store.start_step(
            self.session_id,
            name,
            retry_budget=retry_budget,
            input_data=input_data,
        )
        self._write("step_started", step_id=step_id, name=name)
        return step_id

    def finish_step(
        self,
        step_id: str | None,
        *,
        status: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        if self._event_store is None or step_id is None:
            return
        self._event_store.finish_step(
            step_id,
            status=status,
            result=result,
            error=error,
        )
        self._write(
            "step_finished", step_id=step_id, status=status, result=result, error=error
        )

    def prepare_action(
        self,
        *,
        idempotency_key: str,
        action_type: str,
        target: str,
        request: dict[str, Any],
        step_id: str | None = None,
    ) -> tuple[ActionRecord | None, bool]:
        if self._event_store is None:
            return None, True
        action, created = self._event_store.prepare_action(
            self.session_id,
            idempotency_key=idempotency_key,
            action_type=action_type,
            target=target,
            request=request,
            step_id=step_id,
        )
        if created:
            self._write(
                "action_prepared",
                action_id=action.action_id,
                step_id=step_id,
                idempotency_key=idempotency_key,
                action_type=action_type,
                target=target,
                request=request,
            )
        return action, created

    def mark_action_running(self, action_id: str) -> ActionRecord | None:
        if self._event_store is None:
            return None
        action = self._event_store.mark_action_running(action_id)
        self._write(
            "action_started",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            attempts=action.attempts,
        )
        return action

    def finish_action(
        self,
        action_id: str,
        *,
        status: str,
        response: Any = None,
        error: str | None = None,
    ) -> ActionRecord | None:
        if self._event_store is None:
            return None
        action = self._event_store.finish_action(
            action_id,
            status=status,
            response=response,
            error=error,
        )
        self._write(
            "action_finished",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            status=status,
            response=response,
            error=error,
        )
        return action

    def phase_start(self, phase: int, label: str) -> None:
        """协商阶段开始。phase: 1=广播, 2=提案, 3=投票, 4=最终决策"""
        self._write("phase_start", phase=phase, label=label)
        self._console("phase_start", f"[{phase}] {label}")

    def tool_call(
        self,
        tool: str,
        ap_state: dict,
        result: dict,
        duration_ms: float,
    ) -> None:
        """
        计算工具调用记录。

        Fields:
            tool:        "co_sr" | "co_edca"
            ap_state:    传入工具的原始状态（完整保留，dashboard 可还原推荐过程）
            result:      工具返回的完整 dict
            duration_ms: 工具运行耗时（ms）
        """
        self._write(
            "tool_call",
            tool=tool,
            ap_state=ap_state,
            result=result,
            duration_ms=round(duration_ms, 1),
        )
        summary = _tool_summary(tool, result)
        self._console("tool_call", f"{tool} ({duration_ms:.0f}ms) | {summary}")

    def agent_speak_start(self, agent: str, phase: int, role: str) -> None:
        """Agent 开始发言（流式输出前写入，dashboard 据此创建空气泡）。"""
        self._write("agent_speak_start", agent=agent, phase=phase, role=role)

    def push_chunk(self, agent: str, text: str) -> None:
        """每个 Ollama token 直接推送到 dashboard（不写 JSONL 文件），供高频调用。"""
        if self._event_sink is None or not text:
            return
        try:
            self._event_sink({
                "ts":         _now(),
                "session_id": self.session_id,
                "event":      "agent_speak_chunk",
                "agent":      agent,
                "text":       text,
            })
        except Exception:
            pass

    def agent_speak_chunk(self, agent: str, text: str) -> None:
        """批量写入 JSONL 文件供回放；不推送到实时队列（避免与 push_chunk 重复）。"""
        self._write_file_only("agent_speak_chunk", agent=agent, text=text)

    def agent_speak(
        self,
        agent: str,
        phase: int,
        role: str,
        instruction: str,
        response: str,
        duration_ms: float,
    ) -> None:
        """
        Agent 发言记录（含完整指令和回复，dashboard 用于还原对话流）。

        role 取值：
            "broadcast"  — 第一阶段广播
            "proposer"   — 发起提案
            "voter"      — 投票验算
            "revise"     — 修订提案
            "decision"   — 输出最终决策
        """
        self._write(
            "agent_speak",
            agent=agent,
            phase=phase,
            role=role,
            instruction=instruction,
            response=response,
            duration_ms=round(duration_ms, 1),
        )
        self._console("agent_speak",
                      f"{agent.upper()} role={role} phase={phase} "
                      f"({duration_ms:.0f}ms, {len(response)}字)")

    def vote(
        self,
        voter: str,
        round_num: int,
        agreed: bool | str,
        response: str,
    ) -> None:
        """单个 AP 投票结果。agreed 可为 True/'agree'、False/'reject'、'abstain'。"""
        self._write(
            "vote",
            voter=voter,
            round=round_num,
            agreed=agreed,
            response=response,
        )
        mark = {"agree": "同意", True: "同意", "abstain": "弃权", "reject": "不同意", False: "不同意"}.get(agreed, str(agreed))
        self._console("vote", f"{voter.upper()} 第{round_num}轮 → {mark}")

    def round_result(
        self,
        round_num: int,
        all_agreed: bool,
        agree_count: int,
        total_voters: int,
    ) -> None:
        """本轮投票汇总（dashboard 用于渲染进度条）。"""
        self._write(
            "round_result",
            round=round_num,
            all_agreed=all_agreed,
            agree_count=agree_count,
            total_voters=total_voters,
        )
        self._console("round_result",
                      f"第{round_num}轮 {agree_count}/{total_voters} "
                      f"{'全票通过' if all_agreed else '未通过'}")

    def final_decision(self, decision: dict | None, raw_response: str) -> None:
        """
        最终决策。

        Fields:
            decision:     解析出的 JSON dict（None 表示解析失败）
            raw_response: agent 原始回复全文
        """
        self._write("final_decision", decision=decision, raw_response=raw_response)
        if decision:
            self._console("final_decision",
                          json.dumps(decision, ensure_ascii=False))
        else:
            self._console("final_decision", "JSON 解析失败，见 raw_response")

    def validation_result(self, result: dict) -> None:
        """
        决策验证结果。

        Fields:
            approved:      bool — 全部检查通过
            strategy:      str
            parse_ok:      bool — LLM JSON 是否成功解析
            per_ap:        dict — 每个 AP 的详细检查项
            global_errors: list — 跨 AP 或解析级别错误
            summary:       str  — 一行人读摘要
        """
        self._write("validation_result", **result)
        mark = "通过" if result.get("approved") else "未通过"
        self._console("validation_result",
                      f"{mark} | {result.get('summary', '')}")

    def session_end(
        self,
        outcome: str,
        total_rounds: int,
        negotiation_duration_s: float | None = None,
    ) -> None:
        """
        运行结束。

        Args:
            outcome:      "success" | "max_rounds_reached" | "error"
            total_rounds: 实际经历的投票轮数
        """
        duration_s = round((_ts_ms() - self._start_ms) / 1000, 2)
        self._write(
            "session_end",
            outcome=outcome,
            total_rounds=total_rounds,
            duration_s=duration_s,
            negotiation_duration_s=(
                round(negotiation_duration_s, 2)
                if negotiation_duration_s is not None else None
            ),
        )
        self._write_state_trace(
            "trace_end",
            outcome=outcome,
            total_rounds=total_rounds,
            duration_s=duration_s,
            negotiation_duration_s=(
                round(negotiation_duration_s, 2)
                if negotiation_duration_s is not None else None
            ),
        )
        duration_label = (
            f"negotiation_duration={negotiation_duration_s:.2f}s "
            if negotiation_duration_s is not None else ""
        )
        self._console("session_end",
                      f"outcome={outcome} rounds={total_rounds} "
                      f"{duration_label}duration={duration_s}s log={self.log_path} "
                      f"state_trace={self.state_trace_path}")
        if self._event_store is not None:
            try:
                from .memory import materialize_episode
                episode = materialize_episode(self._event_store, self.session_id)
                if episode is not None:
                    self._write(
                        "episodic_memory_created",
                        episode_id=episode["episode_id"],
                        quality_score=episode["quality_score"],
                    )
            except Exception as exc:
                self._write("episodic_memory_failed", error=str(exc))
            self._event_store.complete_run(self.session_id, outcome)
        self._fh.close()
        self._state_fh.close()
        if self._event_store is not None:
            self._event_store.close()
            self._event_store = None

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        if not self._state_fh.closed:
            self._state_fh.close()
        if self._event_store is not None:
            self._event_store.close()
            self._event_store = None

    # ──────────────────────────────────────────────────────────────────────
    # 工具方法（供 orchestrator 使用）
    # ──────────────────────────────────────────────────────────────────────

    def elapsed_ms(self) -> float:
        """返回自会话开始经过的毫秒数。"""
        return _ts_ms() - self._start_ms


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────

def _tool_summary(tool: str, result: dict) -> str:
    """生成工具调用的单行摘要（控制台友好）。"""
    if tool == "co_sr":
        recs = result.get("recommendations", {})
        feasible = result.get("feasible")
        per_ap = "  ".join(
            f"{ap}:{v['current_dbm']}→{v.get('optimal_dbm', v.get('recommended_dbm'))}dBm"
            for ap, v in recs.items()
        ) if recs else "—"
        parts = [f"feasible={feasible}", per_ap]
        matrix = result.get("interference_matrix", {})
        strong = [k for k, v in matrix.items() if v.get("level") == "strong"]
        if strong:
            parts.append(f"强干扰={strong}")
        return " | ".join(parts)

    if tool == "co_edca":
        return "  ".join(
            f"{ap}={v.get('congestion_level')}→"
            f"CWmin={v.get('CWmin')} CWmax={v.get('CWmax')} AIFSN={v.get('AIFSN')}"
            for ap, v in result.items()
        )

    if tool == "validate_sr_proposal":
        return "  ".join(
            f"{ap}:{'OK' if v.get('valid') else 'FAIL'}"
            f"(CCA={'ok' if v.get('cca_ok') else 'fail'}"
            f" SINR={'ok' if v.get('sinr_ok') else 'fail'}"
            f" STA={'ok' if v.get('sta_rssi_ok') else 'fail'})"
            for ap, v in result.items()
            if isinstance(v, dict) and "valid" in v
        ) or str(result)[:120]

    if tool == "validate_edca_proposal":
        return "  ".join(
            f"{ap}:{'OK' if v.get('valid') else 'FAIL'}"
            for ap, v in result.items()
            if isinstance(v, dict)
        ) or str(result)[:120]

    return str(result)[:120]

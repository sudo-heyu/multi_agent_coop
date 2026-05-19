"""
会话日志模块 — 结构化 JSONL + 控制台双轨输出

每次 orchestrator.run() 对应一个 JSONL 文件，按时间序列记录所有事件。

文件路径：logs/session_<YYYYMMDD_HHMMSS>_<session_id>.jsonl

━━━ 事件类型一览 ━━━
  session_start     运行开始（模型、场景、AP 初始状态）
  strategy_decided  策略路由结果（co_sr / co_edca / joint）和提案方
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("logs")

# 控制台各事件的前缀标签（对齐输出）
_LABELS: dict[str, str] = {
    "session_start":    "▶ 会话开始",
    "strategy_decided": "⚙ 策略",
    "phase_start":      "── 阶段",
    "tool_call":        "🔧 工具",
    "agent_speak":      "💬 发言",
    "vote":             "🗳 投票",
    "round_result":     "📊 轮次",
    "final_decision":     "✅ 决策",
    "validation_result":  "🔍 验证",
    "session_end":        "■ 会话结束",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ts_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


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

    def __init__(self, session_id: str | None = None, verbose: bool = True):
        """
        Args:
            session_id: 可手动指定；默认自动生成 8 位十六进制 ID
            verbose:    True = 同步输出控制台日志；False = 仅写文件
        """
        self.session_id: str = session_id or uuid.uuid4().hex[:8]
        self.verbose: bool = verbose
        self._start_ms: float = _ts_ms()

        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.log_path: Path = LOG_DIR / f"session_{ts}_{self.session_id}.jsonl"
        self._fh = open(self.log_path, "w", encoding="utf-8")

    # ──────────────────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────────────────

    def _write(self, event: str, **kw) -> None:
        row = {
            "ts":         _now(),
            "session_id": self.session_id,
            "event":      event,
            **kw,
        }
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def _console(self, event: str, msg: str) -> None:
        if not self.verbose:
            return
        label = _LABELS.get(event, event)
        print(f"[LOG] {label} | {msg}")

    # ──────────────────────────────────────────────────────────────────────
    # 事件 API
    # ──────────────────────────────────────────────────────────────────────

    def session_start(self, model: str, scene: str, ap_state: dict) -> None:
        """运行开始，记录完整初始状态（dashboard 用于渲染对比表）。"""
        self._write("session_start", model=model, scene=scene, ap_state=ap_state)
        self._console("session_start",
                      f"id={self.session_id} model={model} scene={scene} "
                      f"log={self.log_path}")

    def strategy_decided(self, strategy: str, proposer: str) -> None:
        """策略路由结果和提案方已确定。"""
        self._write("strategy_decided", strategy=strategy, proposer=proposer)
        self._console("strategy_decided",
                      f"strategy={strategy.upper()} proposer={proposer.upper()}")

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
        agreed: bool,
        response: str,
    ) -> None:
        """单个 AP 投票结果。"""
        self._write(
            "vote",
            voter=voter,
            round=round_num,
            agreed=agreed,
            response=response,
        )
        mark = "✅ 同意" if agreed else "❌ 不同意"
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
        mark = "✅ 通过" if result.get("approved") else "❌ 未通过"
        self._console("validation_result",
                      f"{mark} | {result.get('summary', '')}")

    def session_end(self, outcome: str, total_rounds: int) -> None:
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
        )
        self._console("session_end",
                      f"outcome={outcome} rounds={total_rounds} "
                      f"duration={duration_s}s log={self.log_path}")
        self._fh.close()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

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
        rec = result.get("recommended_uniform_dbm")
        feasible = result.get("feasible")
        parts = [f"推荐={rec}dBm feasible={feasible}"]
        matrix = result.get("interference_matrix", {})
        strong = [k for k, v in matrix.items() if v.get("level") == "strong"]
        if strong:
            parts.append(f"强干扰链路={strong}")
        return " | ".join(parts)

    if tool == "co_edca":
        return "  ".join(
            f"{ap}={v.get('congestion_level')}→"
            f"CWmin={v.get('CWmin')} CWmax={v.get('CWmax')} AIFSN={v.get('AIFSN')}"
            for ap, v in result.items()
        )

    return str(result)[:120]

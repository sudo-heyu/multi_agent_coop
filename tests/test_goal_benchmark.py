"""I5 实效基准：目标驱动多轮迭代 vs 单轮协商的达成率对比（固定种子，可复现）。

环境是确定性的合成响应模型（CWmin 越小、ap2 重传率越低，敏感度 k 随场景变化）；
"提案方"是一个确定性控制器，它只消费迭代模块的归因分类来决定下一步参数——
这正是 I3 注入给 LLM 的同一信息。基准证明的是迭代脚手架本身的增益：
同样的初始策略，带归因反馈的多轮尝试必须比单轮达成更多场景。

I6 闭环：达标目标链参与规则归纳加成；失败链依赖的记忆走 R4 矛盾账本。
"""

import copy
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from tests.mock_scenes import MOCK_SCENES
from src.memory import materialize_episode
from src.memory.goals import (
    create_goal, refresh_goal_after_evaluation, register_attempt,
    record_attempt_result,
)
from src.memory.reflection import reconcile_memory_reliance
from src.memory.semantic import induce_rules
from src.persistence import EventStore

TARGET = {"ap": "ap2", "metric": "tx_retries_ratio", "op": "<=", "value": 0.15}
BASE_RETRIES = 0.35
INITIAL_CWMIN = 31
SCENARIOS = 30
SEED = 20260707


def env_retries(cwmin: int, k: float) -> float:
    """合成环境：把 CWmin 从 31 每降 1，ap2 重传率降 k；下限 0.02。"""
    return round(max(0.02, BASE_RETRIES - k * (INITIAL_CWMIN - cwmin)), 6)


def next_cwmin(previous: int, classification: str | None) -> int:
    """确定性控制器：只根据迭代模块的归因分类决定下一步参数。"""
    if classification == "worsened":
        return min(INITIAL_CWMIN, previous * 2 + 1)
    if classification == "no_effect":
        return max(1, previous - 4)
    # first_probe / improved_but_insufficient：沿同方向加大幅度。
    return max(1, previous // 2)


class GoalIterationBenchmark(unittest.TestCase):
    """I5：同一环境与初始策略下，迭代达成率必须高于单轮。"""

    def _drive(self, store, goal, k: float) -> str:
        """跑一条目标链直到停机；返回最终目标状态。"""
        cwmin = 15  # 单轮与迭代共用的初始策略
        attempt_index = 0
        while store.get_goal(goal["goal_id"])["status"] == "active":
            attempt_index += 1
            run_id = f"run-{attempt_index}"
            attempt = register_attempt(store, goal["goal_id"], run_id)
            if attempt_index > 1:
                cwmin = next_cwmin(
                    cwmin, (attempt.get("attribution") or {}).get("classification")
                )
            self._episode(store, run_id, {"ap2": {"CWmin": cwmin}})
            record_attempt_result(store, run_id, outcome="success")
            observed = env_retries(cwmin, k)
            for label in ("w1", "w2"):  # 达标须在下一窗口保持
                self._window(store, run_id, label, observed)
            refresh_goal_after_evaluation(store, run_id)
        return store.get_goal(goal["goal_id"])["status"]

    def _episode(self, store, run_id, decision):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        store.append_event(run_id, "session_start",
                           {"model": "openclaw", "scene": "edca", "ap_state": state})
        store.record_snapshot(run_id, label="initial", source="s", state=state)
        store.append_event(run_id, "final_decision",
                           {"decision": decision, "raw_response": "{}"})
        store.append_event(run_id, "validation_result",
                           {"approved": True, "strategy": "co_edca", "summary": "ok"})
        store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        store.complete_run(run_id, "success")
        materialize_episode(store, run_id)

    def _window(self, store, run_id, label, retries):
        due = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        record, _ = store.schedule_evaluation(
            run_id, window_label=label, window_seconds=10.0, due_at=due,
            baseline={"ap2": {"tx_retries_ratio": BASE_RETRIES}},
        )
        store.finish_evaluation(
            record["evaluation_id"], status="collected",
            observed={"ap2": {"tx_retries_ratio": retries}},
            verdict="neutral", confidence=0.8,
        )

    def _achievement_rate(self, budget: int, sensitivities: list[float]) -> float:
        achieved = 0
        for k in sensitivities:
            with tempfile.TemporaryDirectory() as td:
                store = EventStore(Path(td) / "bench.sqlite3")
                try:
                    goal = create_goal(store, target=TARGET, budget_attempts=budget)
                    if self._drive(store, goal, k) == "achieved":
                        achieved += 1
                finally:
                    store.close()
        return achieved / len(sensitivities)

    def test_goal_driven_iteration_beats_single_round(self):
        rng = random.Random(SEED)
        sensitivities = [round(rng.uniform(0.004, 0.02), 6) for _ in range(SCENARIOS)]
        single_rate = self._achievement_rate(1, sensitivities)
        iterated_rate = self._achievement_rate(5, sensitivities)
        # 固定种子 → 两个比率完全可复现；迭代必须显著更高。
        self.assertGreater(iterated_rate, single_rate)
        self.assertGreaterEqual(iterated_rate - single_rate, 0.2)
        # 迭代的失败场景以 blocked（预算耗尽）收场，而不是无限循环——
        # _drive 能返回本身就证明了停机准则生效。


class ReflectionIterationClosureTests(unittest.TestCase):
    """I6：达标链加成规则归纳；失败链依赖的记忆走矛盾账本。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "closure.sqlite3")
        self.bench = GoalIterationBenchmark()

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _evaluated_episode(self, run_id, verdict="improved", confidence=0.9):
        self.bench._episode(self.store, run_id, {"ap2": {"CWmin": 7}})
        self.store.update_episode_evaluation(
            run_id,
            evaluation={"final_verdict": verdict, "final_confidence": confidence,
                        "needs_rollback": False},
            quality_score=0.9,
        )

    def test_achieved_chain_boosts_rule_confidence(self):
        self._evaluated_episode("run-1")
        self._evaluated_episode("run-2")
        baseline_confidence = induce_rules(self.store, min_support=2)[0]["confidence"]

        goal = create_goal(self.store, target=TARGET, budget_attempts=5)
        register_attempt(self.store, goal["goal_id"], "run-1")
        register_attempt(self.store, goal["goal_id"], "run-2")
        self.store.update_goal_status(goal["goal_id"], "achieved", reason="test")
        boosted = induce_rules(self.store, min_support=2)[0]
        self.assertEqual(boosted["goal_supported"], 2)
        self.assertAlmostEqual(
            boosted["confidence"], min(1.0, baseline_confidence + 0.10), places=4
        )

    def test_failed_chain_relied_memory_hits_contradiction_ledger(self):
        self._evaluated_episode("memory-run")
        goal = create_goal(self.store, target=TARGET, budget_attempts=1)
        register_attempt(self.store, goal["goal_id"], "attempt-run")
        self.bench._episode(self.store, "attempt-run", {"ap2": {"CWmin": 15}})
        self.store.append_event("attempt-run", "memory_reliance", {
            "proposer": "ap1", "proposal_num": 1,
            "entries": [{"memory_kind": "episode", "memory_key": "memory-run",
                         "role": "positive", "trust": 0.8, "predicted": "improved"}],
        })
        record_attempt_result(self.store, "attempt-run", outcome="success")
        # 评估定论 degraded：R4 记矛盾；同一收割里目标进度回填并停机。
        self.bench._window(self.store, "attempt-run", "w1", 0.34)
        reconcile_memory_reliance(self.store, "attempt-run", "degraded")
        refresh_goal_after_evaluation(self.store, "attempt-run")
        self.assertEqual(
            self.store.get_episode(run_id="memory-run")["contradictions"], 1
        )
        self.assertEqual(self.store.get_goal(goal["goal_id"])["status"], "blocked")


if __name__ == "__main__":
    unittest.main()

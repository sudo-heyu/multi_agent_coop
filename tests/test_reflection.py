import copy
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

from openclaw.scenes import MOCK_SCENES
from src.memory import (
    find_agent_episodes, find_episode_memory, find_matching_rules,
    find_similar_episodes, materialize_episode, memory_trust, should_quarantine,
    trust_score,
)
from src.memory.reflection import (
    CONTRADICTION_FACTOR, FRESHNESS_FLOOR, QUARANTINE_CONTRADICTIONS,
    QUARANTINE_TRUST_THRESHOLD, TRUST_HALF_LIFE_SECONDS,
    base_confidence, contradiction_penalty, freshness, gate_memories,
    memory_age_seconds,
)
from src.memory.semantic import induce_rules
from src.persistence import EventStore


class TrustFormulaTests(unittest.TestCase):
    """R1：信任分确定性可复算，衰减/惩罚曲线锁定。"""

    def test_freshness_now_is_full(self):
        self.assertEqual(freshness(0.0), 1.0)
        self.assertEqual(freshness(-5.0), 1.0)

    def test_freshness_half_life(self):
        self.assertAlmostEqual(freshness(TRUST_HALF_LIFE_SECONDS), 0.5, places=4)
        self.assertAlmostEqual(freshness(2 * TRUST_HALF_LIFE_SECONDS), 0.25, places=4)

    def test_freshness_never_below_floor(self):
        self.assertEqual(freshness(1000 * TRUST_HALF_LIFE_SECONDS), FRESHNESS_FLOOR)

    def test_contradiction_penalty_curve(self):
        self.assertEqual(contradiction_penalty(0), 1.0)
        self.assertAlmostEqual(contradiction_penalty(1), CONTRADICTION_FACTOR, places=4)
        self.assertAlmostEqual(contradiction_penalty(2), CONTRADICTION_FACTOR ** 2, places=4)

    def test_trust_score_deterministic_and_bounded(self):
        first = trust_score(0.8, age_seconds=3600.0, contradictions=1)
        second = trust_score(0.8, age_seconds=3600.0, contradictions=1)
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0.0)
        self.assertLessEqual(first, 1.0)
        self.assertEqual(trust_score(1.5, age_seconds=0, contradictions=0), 1.0)
        self.assertEqual(trust_score(-1.0, age_seconds=0, contradictions=0), 0.0)

    def test_base_confidence_prefers_evaluation(self):
        self.assertEqual(
            base_confidence({"quality_score": 0.9,
                             "evaluation": {"final_confidence": 0.6}}),
            0.6,
        )
        self.assertEqual(base_confidence({"quality_score": 0.9, "evaluation": None}), 0.9)
        self.assertEqual(base_confidence({"confidence": 0.7}), 0.7)

    def test_memory_age_uses_last_verified_then_created(self):
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        memory = {
            "created_at": (now - timedelta(days=10)).isoformat(),
            "last_verified_at": (now - timedelta(days=2)).isoformat(),
        }
        self.assertAlmostEqual(
            memory_age_seconds(memory, now=now), 2 * 86400.0, places=1
        )
        del memory["last_verified_at"]
        self.assertAlmostEqual(
            memory_age_seconds(memory, now=now), 10 * 86400.0, places=1
        )

    def test_memory_trust_combines_all_terms(self):
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        memory = {
            "quality_score": 0.5,
            "evaluation": {"final_confidence": 0.8},
            "last_verified_at": (now - timedelta(seconds=TRUST_HALF_LIFE_SECONDS)).isoformat(),
            "contradictions": 1,
        }
        expected = round(0.8 * 0.5 * CONTRADICTION_FACTOR, 4)
        self.assertAlmostEqual(memory_trust(memory, now=now), expected, places=3)

    def test_should_quarantine_thresholds(self):
        self.assertTrue(should_quarantine(1.0, QUARANTINE_CONTRADICTIONS))
        self.assertTrue(should_quarantine(QUARANTINE_TRUST_THRESHOLD - 0.01, 0))
        self.assertFalse(should_quarantine(QUARANTINE_TRUST_THRESHOLD + 0.01, 0))


class ContradictionLedgerTests(unittest.TestCase):
    """v16 数据层：矛盾账本、验证时间、隔离区。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self._td.name) / "trust.sqlite3")
        self.baseline = copy.deepcopy(MOCK_SCENES["edca"])
        self._episode("run-a", "improved")

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _episode(self, run_id, verdict, *, confidence=0.9):
        state = copy.deepcopy(self.baseline)
        self.store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        self.store.append_event(
            run_id, "session_start",
            {"model": "openclaw", "scene": "edca", "ap_state": state},
        )
        self.store.record_snapshot(run_id, label="initial", source="s", state=state)
        self.store.append_event(
            run_id, "final_decision",
            {"decision": {"strategy": "co_edca",
                          "edca": {"ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3}}},
             "raw_response": "{}"},
        )
        self.store.append_event(
            run_id, "validation_result",
            {"approved": True, "strategy": "co_edca", "summary": "ok"},
        )
        self.store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
        self.store.complete_run(run_id, "success")
        materialize_episode(self.store, run_id)
        evaluation = {"final_verdict": verdict, "final_confidence": confidence,
                      "needs_rollback": False}
        self.store.update_episode_evaluation(run_id, evaluation=evaluation, quality_score=0.9)
        for agent in ("ap1", "ap2", "ap3"):
            self.store.update_agent_episode_evaluation(
                run_id, agent, evaluation=evaluation, quality_score=0.9,
            )

    def test_record_contradiction_increments_and_is_idempotent(self):
        count = self.store.record_contradiction(
            memory_kind="episode", memory_key="run-a", run_id="run-later",
            expected="improved", observed="degraded",
        )
        self.assertEqual(count, 1)
        # 同一 run 重复记账 → None，计数不再增长。
        self.assertIsNone(self.store.record_contradiction(
            memory_kind="episode", memory_key="run-a", run_id="run-later",
            expected="improved", observed="degraded",
        ))
        episode = self.store.get_episode(run_id="run-a")
        self.assertEqual(episode["contradictions"], 1)
        # 另一个 run 的矛盾正常累计。
        self.assertEqual(self.store.record_contradiction(
            memory_kind="episode", memory_key="run-a", run_id="run-later-2",
            expected="improved", observed="degraded",
        ), 2)
        ledger = self.store.list_contradictions(memory_kind="episode", memory_key="run-a")
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["expected"], "improved")

    def test_mark_verified_and_quarantine_roundtrip(self):
        self.store.mark_memory_verified("episode", "run-a")
        episode = self.store.get_episode(run_id="run-a")
        self.assertIsNotNone(episode["last_verified_at"])
        self.assertFalse(episode["quarantined"])

        self.store.set_memory_quarantined("episode", "run-a", True)
        episode = self.store.get_episode(run_id="run-a")
        self.assertTrue(episode["quarantined"])
        quarantined = self.store.list_quarantined_memories()
        self.assertEqual(
            [(item["memory_kind"], item["memory_key"]) for item in quarantined],
            [("episode", "run-a")],
        )
        # 解除隔离（再验证路径）。
        self.store.set_memory_quarantined("episode", "run-a", False)
        self.assertEqual(self.store.list_quarantined_memories(), [])

    def test_agent_episode_key_and_rule_target(self):
        agents = self.store.list_agent_episodes("ap1")
        self.assertTrue(agents)
        key = f"run-a:{agents[0]['agent_id']}"
        self.assertEqual(self.store.record_contradiction(
            memory_kind="agent_episode", memory_key=key, run_id="run-x",
            expected="improved", observed="degraded",
        ), 1)
        self.assertEqual(self.store.list_agent_episodes("ap1")[0]["contradictions"], 1)

        rules = induce_rules(self.store, min_support=1)
        self.assertTrue(rules)
        rule_id = rules[0]["rule_id"]
        self.assertEqual(self.store.record_contradiction(
            memory_kind="rule", memory_key=rule_id, run_id="run-x",
            expected="improved", observed="degraded",
        ), 1)
        listed = self.store.list_rules()
        self.assertEqual(listed[0]["contradictions"], 1)
        self.assertFalse(listed[0]["quarantined"])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.store.record_contradiction(
                memory_kind="nope", memory_key="x", run_id="y",
                expected="a", observed="b",
            )
        with self.assertRaises(ValueError):
            self.store._memory_where("agent_episode", "missing-agent-part")


class RecallGatingTests(ContradictionLedgerTests):
    """R2：隔离区记忆在所有召回路径上零注入；R6：门控为纯内存计算。"""

    def test_quarantined_episode_not_recalled(self):
        state = copy.deepcopy(self.baseline)
        self.assertTrue(find_similar_episodes(self.store, state, require_evaluation=True))
        self.assertTrue(find_episode_memory(self.store, state)["positive"])

        self.store.set_memory_quarantined("episode", "run-a", True)
        self.assertEqual(
            find_similar_episodes(self.store, state, require_evaluation=True), []
        )
        memory = find_episode_memory(self.store, state)
        self.assertEqual(memory["positive"], [])
        self.assertEqual(memory["warnings"], [])

    def test_quarantined_agent_episode_not_recalled(self):
        state = copy.deepcopy(self.baseline)
        self.assertTrue(find_agent_episodes(self.store, "ap1", state))
        self.store.set_memory_quarantined("agent_episode", "run-a:ap1", True)
        self.assertEqual(find_agent_episodes(self.store, "ap1", state), [])

    def test_quarantined_rule_not_matched(self):
        for index in range(2, 7):
            self._episode(f"run-{index}", "improved")
        rules = induce_rules(self.store, min_support=1)
        state = copy.deepcopy(self.baseline)
        matched = find_matching_rules(self.store, state, min_confidence=0.0)
        self.assertTrue(matched)
        self.store.set_memory_quarantined("rule", rules[0]["rule_id"], True)
        self.assertEqual(find_matching_rules(self.store, state, min_confidence=0.0), [])

    def test_recalled_memories_carry_trust(self):
        state = copy.deepcopy(self.baseline)
        episodes = find_similar_episodes(self.store, state, require_evaluation=True)
        self.assertIn("trust", episodes[0])
        self.assertGreater(episodes[0]["trust"], 0.0)

    def test_stale_memory_gated_out(self):
        stale = {
            "quality_score": 0.9,
            "evaluation": {"final_confidence": 0.9},
            "created_at": "2020-01-01T00:00:00+00:00",
            "contradictions": 0,
        }
        self.assertEqual(gate_memories([stale]), [])

    def test_reflection_switch_restores_legacy_behavior(self):
        self.store.set_memory_quarantined("episode", "run-a", True)
        state = copy.deepcopy(self.baseline)
        with patch.dict(os.environ, {"MULTIAP_REFLECTION": "0"}):
            episodes = find_similar_episodes(self.store, state, require_evaluation=True)
        self.assertEqual([item["run_id"] for item in episodes], ["run-a"])
        self.assertNotIn("trust", episodes[0])

    def test_memory_reliance_event_recorded(self):
        """R3：注入即记账，show <run_id> 可审计依赖了哪些记忆及其信任分。"""
        from src.logger import SessionLogger
        import src.logger as logger_module
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(logger_module, "LOG_DIR", root / "logs"), patch.object(
                logger_module, "STATE_LOG_DIR", root / "logs" / "state"
            ):
                logger = SessionLogger(
                    session_id="reliance-run", verbose=False, mode="mock",
                    event_store=self.store,
                )
                logger.session_start("openclaw", "edca", copy.deepcopy(self.baseline))
                logger.memory_reliance(
                    "ap1",
                    episodes=[{"run_id": "run-a", "trust": 0.8,
                               "evaluation": {"final_verdict": "improved"}}],
                    warnings=[{"run_id": "run-w", "trust": 0.6}],
                    rules=[{"rule_id": "rule-1", "trust": 0.7,
                            "dominant_verdict": "improved"}],
                    agent_episodes=[("ap1", {"run_id": "run-a", "trust": 0.8,
                                             "evaluation": {"final_verdict": "improved"}})],
                    proposal_num=1,
                )
        events = self.store.load_events("reliance-run")
        reliance = [e for e in events if e["event"] == "memory_reliance"]
        self.assertEqual(len(reliance), 1)
        entries = reliance[0]["entries"]
        self.assertEqual(len(entries), 4)
        by_role = {entry["role"]: entry for entry in entries}
        self.assertEqual(by_role["positive"]["memory_key"], "run-a")
        self.assertEqual(by_role["positive"]["predicted"], "improved")
        self.assertEqual(by_role["positive"]["trust"], 0.8)
        self.assertEqual(by_role["warning"]["predicted"], "degraded")
        self.assertEqual(by_role["rule"]["memory_key"], "rule-1")
        self.assertEqual(by_role["agent_local"]["memory_key"], "run-a:ap1")

    def test_gate_thousand_memories_under_latency_budget(self):
        now = datetime.now(timezone.utc).isoformat()
        memories = [{
            "quality_score": 0.8, "evaluation": {"final_confidence": 0.8},
            "created_at": now, "contradictions": index % 3, "quarantined": index % 5 == 0,
        } for index in range(1000)]
        started = time.perf_counter()
        gated = gate_memories(memories)
        elapsed = time.perf_counter() - started
        self.assertTrue(gated)
        self.assertLess(elapsed, 0.1)


class HypothesisInjectionTests(unittest.TestCase):
    """R3：注入格式携带信任分/最近验证，记忆以待检验假设呈现。"""

    def test_agent_message_shows_trust_and_hypothesis_wording(self):
        from openclaw.mcp import orchestration as orch
        memory = {
            "strategy": "co_edca", "decision": {"ap1": {"CWmin": 7}},
            "case_narrative": None, "trust": 0.72,
            "last_verified_at": "2026-07-01T00:00:00+00:00",
            "evaluation": {"final_confidence": 0.9},
        }
        text = orch._build_agent_message(
            "ap1", "", "请提案", shared_positive=[memory], shared_warnings=[memory],
        )
        self.assertIn("共享经验假设", text)
        self.assertIn("信任=0.72", text)
        self.assertIn("最近验证=2026-07-01", text)

    def test_agent_message_without_trust_keeps_legacy_format(self):
        from openclaw.mcp import orchestration as orch
        memory = {"strategy": "co_edca", "decision": {"ap1": {"CWmin": 7}},
                  "case_narrative": None, "evaluation": {"final_confidence": 0.9}}
        text = orch._build_agent_message("ap1", "", "请提案", shared_positive=[memory])
        self.assertNotIn("信任=", text)

    def test_format_rule_includes_trust(self):
        from src.memory import format_rule
        rule = {
            "strategy": "co_edca", "support": 6, "consistency": 0.9,
            "confidence": 0.85, "dominant_verdict": "improved",
            "verdict_counts": {"improved": 6}, "action_summary": {},
            "trust": 0.61, "last_verified_at": None,
        }
        text = format_rule(rule)
        self.assertIn("信任=0.61", text)
        self.assertIn("未再验证", text)

    def test_memory_md_renders_trust_line(self):
        from src.memory.workspace import save_long_term_memory
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"MULTIAP_AGENT_WORKSPACES_ROOT": td}
        ):
            save_long_term_memory("ap1", [{
                "run_id": "r1", "scene": "edca", "strategy": "co_edca",
                "local_state": {}, "local_decision": {"CWmin": 7},
                "outcome": "success", "quality_score": 0.9,
                "evaluation": {"final_verdict": "improved", "final_confidence": 0.9},
                "trust": 0.83, "contradictions": 1,
                "last_verified_at": "2026-07-05T00:00:00+00:00",
                "created_at": "2026-07-01T00:00:00+00:00",
            }])
            content = (Path(td) / "ap1" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("待检验的假设", content)
        self.assertIn("信任：0.83（矛盾 1 笔，最近验证 2026-07-05）", content)


if __name__ == "__main__":
    unittest.main()

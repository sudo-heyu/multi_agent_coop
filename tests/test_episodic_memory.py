import copy
import tempfile
import unittest
from pathlib import Path

from tests.mock_scenes import MOCK_SCENES
from src.memory import (
    encode_features, find_agent_episodes, find_episode_memory,
    find_similar_episodes, materialize_episode,
)
from src.persistence import EventStore


class EpisodicMemoryTests(unittest.TestCase):
    def _run(
        self, store, run_id, state, *, outcome="success", latency_delta=-20,
        qos_acceptance=None, validation_extra=None,
    ):
        store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        store.append_event(run_id, "session_start", {"model": "openclaw", "scene": "edca", "ap_state": state})
        decision = {ap: {"CWmin": 7, "CWmax": 31, "AIFSN": 3} for ap in ("ap1", "ap2", "ap3")}
        store.append_event(run_id, "final_decision", {"decision": decision, "raw_response": "{}"})
        validation = {"approved": outcome == "success", "strategy": "co_edca", "summary": outcome}
        if qos_acceptance is not None:
            validation["qos_acceptance"] = qos_acceptance
        if validation_extra:
            validation.update(validation_extra)
        store.append_event(run_id, "validation_result", validation)
        observed = copy.deepcopy(state)
        for row in observed.values():
            row["latency_ms"] = row.get("latency_ms", 100) + latency_delta
        store.record_snapshot(run_id, label="final_observed", source="test", state=observed)
        store.append_event(run_id, "session_end", {"outcome": outcome, "total_rounds": 1})
        store.complete_run(run_id, outcome)
        return materialize_episode(store, run_id)

    def test_completed_run_materializes_structured_episode(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "episodes.sqlite3")
            episode = self._run(store, "episode-1", copy.deepcopy(MOCK_SCENES["edca"]))
            loaded = store.get_episode(run_id="episode-1")
            store.close()
        self.assertEqual(episode["strategy"], "co_edca")
        self.assertEqual(loaded["decision"], episode["decision"])
        self.assertTrue(loaded["metrics"]["available"])
        self.assertGreaterEqual(loaded["quality_score"], 0.9)

    def test_qos_acceptance_materializes_immediate_degraded_warning(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "qos-inline.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(
                store, "bad-qos", state,
                qos_acceptance={
                    "verdict": "degraded",
                    "confidence": 0.82,
                    "approved": False,
                    "reason": "score below threshold",
                    "deltas": {"score": -0.12},
                },
            )
            loaded = store.get_episode(run_id="bad-qos")
            agent_case = store.list_agent_episodes("ap1", min_quality=0.0)[0]
            recalled = find_episode_memory(store, state)
            store.close()

        self.assertEqual(loaded["evaluation"]["final_verdict"], "degraded")
        self.assertEqual(loaded["evaluation"]["source"], "qos_acceptance")
        self.assertTrue(loaded["evaluation"]["needs_rollback"])
        self.assertEqual(loaded["lifecycle"], "warning")
        self.assertGreater(loaded["quality_vector"]["outcome_confidence"], 0.8)
        self.assertEqual(agent_case["evaluation"]["final_verdict"], "degraded")
        self.assertEqual([item["run_id"] for item in recalled["warnings"]], ["bad-qos"])

    def test_qos_acceptance_materializes_immediate_positive_memory(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "qos-positive.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(
                store, "good-qos", state,
                qos_acceptance={
                    "verdict": "improved",
                    "confidence": 0.9,
                    "approved": True,
                    "deltas": {"score": 0.08},
                },
            )
            recalled = find_episode_memory(store, state)
            loaded = store.get_episode(run_id="good-qos")
            store.close()

        self.assertEqual(loaded["evaluation"]["final_verdict"], "improved")
        self.assertEqual(loaded["lifecycle"], "trusted")
        self.assertEqual([item["run_id"] for item in recalled["positive"]], ["good-qos"])

    def test_failed_neutral_qos_acceptance_is_recalled_as_warning(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "qos-neutral-warning.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(
                store, "neutral-failed-qos", state,
                qos_acceptance={
                    "verdict": "neutral",
                    "confidence": 0.75,
                    "approved": False,
                    "deltas": {"score": -0.03},
                },
            )
            recalled = find_episode_memory(store, state)
            loaded = store.get_episode(run_id="neutral-failed-qos")
            store.close()

        self.assertEqual(
            [item["run_id"] for item in recalled["warnings"]],
            ["neutral-failed-qos"],
        )
        self.assertEqual(loaded["lifecycle"], "warning")
        self.assertLessEqual(loaded["quality_score"], 0.45)

    def test_positive_near_miss_qos_acceptance_is_recalled_as_reference(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "qos-near-miss.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(
                store, "neutral-near-miss", state,
                qos_acceptance={
                    "verdict": "neutral",
                    "confidence": 0.75,
                    "approved": False,
                    "deltas": {"score": 0.04},
                },
            )
            recalled = find_episode_memory(store, state)
            loaded = store.get_episode(run_id="neutral-near-miss")
            store.close()

        self.assertEqual(
            [item["run_id"] for item in recalled["positive"]],
            ["neutral-near-miss"],
        )
        self.assertEqual(recalled["warnings"], [])
        self.assertEqual(loaded["lifecycle"], "evaluated")
        self.assertGreaterEqual(loaded["quality_score"], 0.55)

    def test_observed_sla_validation_failure_materializes_degraded_warning(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "sla-warning.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(
                store, "sla-failed",
                state,
                validation_extra={
                    "approved": False,
                    "summary": "验证失败：决策后引入新的 STA SLA 违规",
                    "global_errors": ["决策后引入新的 STA SLA 违规: ['sta_ap1_user']"],
                    "sta_qoe": {"checked": True, "approved": False},
                    "new_violations": [{"sta_id": "sta_ap1_user"}],
                },
            )
            loaded = store.get_episode(run_id="sla-failed")
            recalled = find_episode_memory(store, state)
            store.close()

        self.assertEqual(loaded["evaluation"]["source"], "validation_result")
        self.assertEqual(loaded["evaluation"]["final_verdict"], "degraded")
        self.assertTrue(loaded["evaluation"]["needs_rollback"])
        self.assertEqual(loaded["lifecycle"], "warning")
        self.assertEqual([item["run_id"] for item in recalled["warnings"]], ["sla-failed"])

    def test_materialization_creates_agent_scoped_long_term_cases(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "agent-episodes.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(store, "local-case", state)
            ap1 = find_agent_episodes(store, "ap1", state, require_evaluation=False)
            ap2 = find_agent_episodes(store, "ap2", state, require_evaluation=False)
            store.close()
        self.assertEqual([item["run_id"] for item in ap1], ["local-case"])
        self.assertEqual(set(ap1[0]["local_state"]), set(state["ap1"]))
        self.assertNotEqual(ap1[0]["local_state"], ap2[0]["local_state"])
        self.assertEqual(ap1[0]["local_decision"], {"CWmin": 7, "CWmax": 31, "AIFSN": 3})

    def test_rematerialization_preserves_existing_evaluation_feedback(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "repeat-materialize.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(store, "repeat-case", state)
            store.update_episode_evaluation(
                "repeat-case",
                evaluation={"final_verdict": "improved", "final_confidence": 0.9},
                quality_score=0.95,
                quality_vector={"outcome_confidence": 0.9},
                lifecycle="trusted",
            )
            store.update_agent_episode_evaluation(
                "repeat-case", "ap1",
                evaluation={"final_verdict": "improved", "final_confidence": 0.8},
                quality_score=0.91,
            )

            materialize_episode(store, "repeat-case")

            episode = store.get_episode(run_id="repeat-case")
            ap1 = store.list_agent_episodes("ap1", min_quality=0.0)[0]
            store.close()

        self.assertEqual(episode["evaluation"]["final_verdict"], "improved")
        self.assertEqual(episode["quality_score"], 0.95)
        self.assertEqual(episode["lifecycle"], "trusted")
        self.assertEqual(episode["quality_vector"]["outcome_confidence"], 0.9)
        self.assertEqual(ap1["evaluation"]["final_verdict"], "improved")
        self.assertEqual(ap1["quality_score"], 0.91)

    def test_opening_store_backfills_legacy_shared_episodes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "backfill.sqlite3"
            store = EventStore(path)
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(store, "legacy", state)
            with store._lock, store._conn:
                store._conn.execute("DELETE FROM agent_episodic_memories")
            store.close()
            reopened = EventStore(path)
            count = len(reopened.list_agent_episodes("ap1", min_quality=0.0))
            reopened.close()
        self.assertEqual(count, 1)

    def test_similarity_ranks_closest_domain_state_first(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "episodes.sqlite3")
            query = copy.deepcopy(MOCK_SCENES["edca"])
            far = copy.deepcopy(query)
            far["ap2"]["Data_rate_to_bandwidth_ratio"] = 0.05
            far["ap2"]["tx_retries_ratio"] = 0.01
            far["ap2"]["tx_power_dbm"] = 2
            self._run(store, "close", copy.deepcopy(query))
            self._run(store, "far", far)
            ranked = find_similar_episodes(store, query, limit=2)
            store.close()
        self.assertEqual([item["run_id"] for item in ranked], ["close", "far"])
        self.assertGreater(ranked[0]["similarity"], ranked[1]["similarity"])

    def test_positive_and_negative_channels_are_separate_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "channels.sqlite3")
            state = copy.deepcopy(MOCK_SCENES["edca"])
            self._run(store, "good-1", state)
            self._run(store, "good-duplicate", copy.deepcopy(state))
            bad_state = copy.deepcopy(state)
            bad_state["ap1"]["Data_rate_to_bandwidth_ratio"] = 0.5
            self._run(store, "bad", bad_state)
            for run_id, verdict, quality in (
                ("good-1", "improved", 0.9),
                ("good-duplicate", "improved", 0.9),
                ("bad", "degraded", 0.2),
            ):
                store.update_episode_evaluation(
                    run_id, evaluation={"final_verdict": verdict, "final_confidence": 0.9},
                    quality_score=quality,
                )
            result = find_episode_memory(store, state)
            store.close()
        self.assertEqual(len(result["positive"]), 1)
        self.assertEqual([item["run_id"] for item in result["warnings"]], ["bad"])

    def test_topology_signature_prevents_cross_topology_recall(self):
        with tempfile.TemporaryDirectory() as td:
            store = EventStore(Path(td) / "episodes.sqlite3")
            query = copy.deepcopy(MOCK_SCENES["edca"])
            changed = copy.deepcopy(query)
            changed["ap1"]["neighbor_rssi_dbm"].pop("ap3")
            self._run(store, "other-topology", changed)
            ranked = find_similar_episodes(store, query, limit=5)
            query_signature, _ = encode_features(query)
            changed_signature, _ = encode_features(changed)
            store.close()
        self.assertNotEqual(query_signature, changed_signature)
        self.assertEqual(ranked, [])

    def test_radio_context_changes_similarity_not_structural_signature(self):
        baseline = copy.deepcopy(MOCK_SCENES["edca"])
        changed = copy.deepcopy(baseline)
        for state, channel in ((baseline, 36), (changed, 149)):
            for row in state.values():
                row["channel"] = channel
                row["bandwidth_mhz"] = 80 if channel == 36 else 20
        left_sig, left = encode_features(baseline)
        right_sig, right = encode_features(changed)
        from src.memory.episodic import feature_similarity
        score, parts = feature_similarity(left, right)
        self.assertEqual(left_sig, right_sig)
        self.assertNotEqual(left["deployment_signature"], right["deployment_signature"])
        self.assertLess(parts["radio"], 1.0)
        self.assertLess(score, 1.0)


if __name__ == "__main__":
    unittest.main()

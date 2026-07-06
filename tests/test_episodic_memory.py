import copy
import tempfile
import unittest
from pathlib import Path

from openclaw.scenes import MOCK_SCENES
from src.memory import (
    encode_features, find_agent_episodes, find_episode_memory,
    find_similar_episodes, materialize_episode,
)
from src.persistence import EventStore


class EpisodicMemoryTests(unittest.TestCase):
    def _run(self, store, run_id, state, *, outcome="success", latency_delta=-20):
        store.start_run(run_id, mode="mock", scene="edca", model="openclaw")
        store.append_event(run_id, "session_start", {"model": "openclaw", "scene": "edca", "ap_state": state})
        decision = {ap: {"CWmin": 7, "CWmax": 31, "AIFSN": 3} for ap in ("ap1", "ap2", "ap3")}
        store.append_event(run_id, "final_decision", {"decision": decision, "raw_response": "{}"})
        store.append_event(run_id, "validation_result", {"approved": outcome == "success", "strategy": "co_edca", "summary": outcome})
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

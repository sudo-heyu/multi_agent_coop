import copy
import tempfile
import unittest
from pathlib import Path

from openclaw.scenes import MOCK_SCENES
from src.memory import encode_features, find_similar_episodes, materialize_episode
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


if __name__ == "__main__":
    unittest.main()

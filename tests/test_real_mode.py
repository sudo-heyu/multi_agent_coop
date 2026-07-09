import unittest
from unittest.mock import patch
import sys
import tempfile
from pathlib import Path

import run_openclaw
from tests.mock_scenes import MOCK_SCENES
from openclaw.mcp import orchestration as orch
import copy


class RealModeTests(unittest.TestCase):
    def test_real_endpoints_require_all_three_aps(self):
        with self.assertRaisesRegex(ValueError, "缺少 ap3"):
            run_openclaw._validate_real_endpoints({
                "ap1": "http://10.0.0.1:5002",
                "ap2": "http://10.0.0.2:5002",
            })

    def test_real_endpoints_accept_exact_ap_set(self):
        run_openclaw._validate_real_endpoints({
            "ap1": "http://10.0.0.1:5002",
            "ap2": "http://10.0.0.2:5002",
            "ap3": "http://10.0.0.3:5002",
        })

    def test_runtime_has_no_mock_feeder_path(self):
        """mock 已降级为测试夹具：运行时入口不得再引用 feeder 或 mock 模式。"""
        self.assertFalse(hasattr(run_openclaw, "MockTelemetryFeeder"))
        self.assertFalse(hasattr(run_openclaw, "_start_telemetry"))
        source = open(run_openclaw.__file__, encoding="utf-8").read()
        self.assertNotIn("MockTelemetryFeeder(", source)

    def test_mode_choices_exclude_mock(self):
        source = open(run_openclaw.__file__, encoding="utf-8").read()
        self.assertIn('choices=["real", "ns3"]', source)
        self.assertNotIn('choices=["mock"', source)

    def test_scene_names_match_test_fixtures(self):
        from openclaw.scenes import SCENE_NAMES
        self.assertEqual(set(SCENE_NAMES), {"sr", "edca"})
        self.assertTrue(set(SCENE_NAMES).issubset(MOCK_SCENES))

    def test_ns3_eval_windows_default_to_short_feedback(self):
        with patch.dict(run_openclaw.os.environ, {"MULTIAP_EVAL_WINDOWS": ""}):
            self.assertEqual(run_openclaw._resolve_eval_windows("", "ns3"), (10.0, 30.0))

    def test_resume_rejects_changed_ap_parameters(self):
        stored = orch.apply_profile(copy.deepcopy(MOCK_SCENES["edca"]))
        latest = {
            ap: {"data": copy.deepcopy(state), "stale": False}
            for ap, state in MOCK_SCENES["edca"].items()
        }
        latest["ap2"]["data"]["tx_power_dbm"] += 1

        compatible, reason = run_openclaw._resume_state_compatible(stored, latest)

        self.assertFalse(compatible)
        self.assertIn("ap2.tx_power_dbm", reason)

    def test_resume_allows_qos_drift_when_parameters_match(self):
        stored = orch.apply_profile(copy.deepcopy(MOCK_SCENES["edca"]))
        latest = {
            ap: {"data": copy.deepcopy(state), "stale": False}
            for ap, state in MOCK_SCENES["edca"].items()
        }
        latest["ap2"]["data"]["latency_ms"] += 25

        compatible, _ = run_openclaw._resume_state_compatible(stored, latest)

        self.assertTrue(compatible)

    def _write_openclaw_profile(self, home, config):
        cfg_dir = home / ".openclaw-multiap"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            run_openclaw.json.dumps(config),
            encoding="utf-8",
        )
        fake_bin = home / "openclaw"
        fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        return fake_bin

    def test_openclaw_config_rejects_ollama_primary_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fake_bin = self._write_openclaw_profile(home, {
                "models": {
                    "providers": {
                        "ollama": {"models": [{"id": "qwen3:8b"}]},
                    },
                },
                "agents": {
                    "defaults": {
                        "models": {"ollama/qwen3:8b": {"alias": "local-qwen"}},
                        "model": {"primary": "ollama/qwen3:8b"},
                    },
                },
            })

            with patch.object(run_openclaw, "OPENCLAW_BIN", str(fake_bin)), \
                 patch.object(run_openclaw.Path, "home", return_value=home), \
                 self.assertRaises(SystemExit):
                run_openclaw._require_openclaw_config()

    def test_openclaw_config_allows_ollama_only_when_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fake_bin = self._write_openclaw_profile(home, {
                "models": {
                    "providers": {
                        "ollama": {"models": [{"id": "qwen3:8b"}]},
                    },
                },
                "agents": {
                    "defaults": {
                        "models": {"ollama/qwen3:8b": {"alias": "local-qwen"}},
                        "model": {"primary": "ollama/qwen3:8b"},
                    },
                },
            })

            with patch.object(run_openclaw, "OPENCLAW_BIN", str(fake_bin)), \
                 patch.object(run_openclaw.Path, "home", return_value=home):
                run_openclaw._require_openclaw_config(allow_ollama=True)

    def test_openclaw_config_accepts_ppio_primary_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            fake_bin = self._write_openclaw_profile(home, {
                "models": {
                    "providers": {
                        "ppio": {
                            "apiKey": "test-key",
                            "models": [{"id": "qwen/qwen3.6-35b-a3b", "name": "qwen80binstruct"}],
                        },
                    },
                },
                "agents": {
                    "defaults": {
                        "models": {
                            "ppio/qwen/qwen3.6-35b-a3b": {"alias": "qwen80binstruct"},
                        },
                        "model": {"primary": "ppio/qwen/qwen3.6-35b-a3b"},
                    },
                },
            })

            with patch.object(run_openclaw, "OPENCLAW_BIN", str(fake_bin)), \
                 patch.object(run_openclaw.Path, "home", return_value=home):
                run_openclaw._require_openclaw_config(require_qwen80b=True)

    def test_main_passes_raw_observation_state_to_structured_relay(self):
        raw_state = copy.deepcopy(MOCK_SCENES["edca"])
        ready = {
            ap: {"data": copy.deepcopy(state), "stale": False}
            for ap, state in raw_state.items()
        }
        captured = {}

        class FakeLogger:
            session_id = "raw-observation-test"

            def __init__(self, *args, **kwargs):
                pass

            def session_start(self, *args, **kwargs):
                pass

            def record_telemetry_trace(self, *args, **kwargs):
                pass

        def fake_structured_relay(*args, observation_state_getter=None, **kwargs):
            captured["observed"] = observation_state_getter()
            return {
                "outcome": "success",
                "transcript_turns": 0,
                "strategy": "co_edca",
                "decision": {},
                "push_results": {},
                "log_path": None,
                "validation": {"approved": True, "summary": "ok"},
            }

        argv = [
            "run_openclaw.py",
            "--mode", "ns3",
            "--scene", "edca",
            "--no-dashboard",
            "--no-academic-plot",
            "--eval-windows", "off",
        ]
        with patch.object(sys, "argv", argv), \
             patch.object(run_openclaw, "_require_state_server", return_value={"ok": True}), \
             patch.object(run_openclaw, "_require_gateway"), \
             patch.object(run_openclaw, "_require_openclaw_config"), \
             patch.object(run_openclaw, "_wait_state_ready", return_value=ready), \
             patch.object(run_openclaw, "_harvest_due_evaluations", return_value=[]), \
             patch.object(run_openclaw, "_start_run_trace", return_value=None), \
             patch.object(run_openclaw, "SessionLogger", FakeLogger), \
             patch.object(run_openclaw.orch, "get_all_states", return_value=raw_state), \
             patch.object(run_openclaw.orch, "structured_relay", side_effect=fake_structured_relay):
            run_openclaw.main()

        self.assertEqual(captured["observed"]["ap1"]["cwmin"], raw_state["ap1"]["cwmin"])
        self.assertNotEqual(
            captured["observed"]["ap1"]["cwmin"],
            orch.apply_profile(raw_state)["ap1"]["cwmin"],
        )


if __name__ == "__main__":
    unittest.main()

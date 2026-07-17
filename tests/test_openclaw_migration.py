import copy
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openclaw.mcp import orchestration as orch
from src.validator import validate_decision


def _with_common_fields(scene: dict) -> dict:
    for ap_id, state in scene.items():
        state.setdefault("source", "ns3")
        state.setdefault("throughput_mbps_user", round(state["throughput_mbps_iperf"] * 0.6, 1))
        state.setdefault("ac_iperf", "BK")
        state.setdefault(
            "ac_user",
            {"high": "VO", "medium": "BE", "low": "BK"}.get(
                state.get("traffic_priority", "medium"),
                "BE",
            ),
        )
    return scene


NS3_FIXTURES = {
    "sr": _with_common_fields({
        "ap1": {
            "service_name": "generic_data",
            "business_type": "后台下载",
            "tx_power_dbm": 20.0,
            "cwmin": 7, "cwmax": 15, "aifsn": 2,
            "traffic_priority": "medium",
            "Data_rate_to_bandwidth_ratio": 0.45,
            "tx_retries_ratio": 0.08,
            "neighbor_rssi_dbm": {"ap2": -74.7, "ap3": -88.0},
            "sta_rssi_dbm": -45.0,
            "noise_floor_dbm": -92.0,
            "throughput_mbps_iperf": 22.1,
            "latency_ms": 210.0,
            "packet_loss_pct": 0.5,
        },
        "ap2": {
            "service_name": "generic_data",
            "business_type": "直播",
            "tx_power_dbm": 14.0,
            "cwmin": 7, "cwmax": 15, "aifsn": 2,
            "traffic_priority": "medium",
            "Data_rate_to_bandwidth_ratio": 0.50,
            "tx_retries_ratio": 0.10,
            "neighbor_rssi_dbm": {"ap1": -68.6, "ap3": -81.4},
            "sta_rssi_dbm": -48.0,
            "noise_floor_dbm": -91.0,
            "throughput_mbps_iperf": 20.3,
            "latency_ms": 195.0,
            "packet_loss_pct": 0.3,
        },
        "ap3": {
            "service_name": "generic_data",
            "business_type": "后台下载",
            "tx_power_dbm": 8.0,
            "cwmin": 7, "cwmax": 15, "aifsn": 2,
            "traffic_priority": "medium",
            "Data_rate_to_bandwidth_ratio": 0.38,
            "tx_retries_ratio": 0.06,
            "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -76.0},
            "sta_rssi_dbm": -50.0,
            "noise_floor_dbm": -90.0,
            "throughput_mbps_iperf": 28.5,
            "latency_ms": 120.0,
            "packet_loss_pct": 0.1,
        },
    }),
    "edca": _with_common_fields({
        "ap1": {
            "service_name": "background_download",
            "business_type": "后台下载",
            "tx_power_dbm": 10.0,
            "cwmin": 7, "cwmax": 15, "aifsn": 2,
            "traffic_priority": "low",
            "Data_rate_to_bandwidth_ratio": 0.42,
            "tx_retries_ratio": 0.06,
            "neighbor_rssi_dbm": {"ap2": -85.0, "ap3": -88.0},
            "sta_rssi_dbm": -55.0,
            "noise_floor_dbm": -92.0,
            "throughput_mbps_iperf": 30.2,
            "latency_ms": 130.0,
            "packet_loss_pct": 0.2,
        },
        "ap2": {
            "service_name": "live_streaming",
            "business_type": "直播",
            "tx_power_dbm": 10.0,
            "cwmin": 7, "cwmax": 15, "aifsn": 2,
            "traffic_priority": "high",
            "Data_rate_to_bandwidth_ratio": 0.72,
            "tx_retries_ratio": 0.18,
            "neighbor_rssi_dbm": {"ap1": -85.0, "ap3": -87.0},
            "sta_rssi_dbm": -61.0,
            "noise_floor_dbm": -91.0,
            "throughput_mbps_iperf": 18.4,
            "latency_ms": 312.0,
            "packet_loss_pct": 1.2,
        },
        "ap3": {
            "service_name": "background_download",
            "business_type": "后台下载",
            "tx_power_dbm": 10.0,
            "cwmin": 7, "cwmax": 15, "aifsn": 2,
            "traffic_priority": "low",
            "Data_rate_to_bandwidth_ratio": 0.38,
            "tx_retries_ratio": 0.05,
            "neighbor_rssi_dbm": {"ap1": -88.0, "ap2": -87.0},
            "sta_rssi_dbm": -58.0,
            "noise_floor_dbm": -90.0,
            "throughput_mbps_iperf": 34.1,
            "latency_ms": 98.0,
            "packet_loss_pct": 0.1,
        },
    }),
}


EDCA_DECISION = {
    "ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
    "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
    "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
}


def decision_for(scene_name: str) -> dict:
    state = orch.apply_profile(copy.deepcopy(NS3_FIXTURES[scene_name]))
    if scene_name == "edca":
        return copy.deepcopy(EDCA_DECISION)

    best = orch._sr.select_concurrent_groups(state)["best_group"]
    decision = {
        ap_id: {"tx_power_dbm": power}
        for ap_id, power in best["recommended_powers"].items()
    }
    decision["_sr"] = {
        "concurrent_group": best["concurrent_group"],
        "non_concurrent_aps": best["non_concurrent_aps"],
    }
    return decision


class FakeOpenClawAP:
    def __init__(self, decision: dict):
        self.decision = decision
        self.calls = []
        self.vote_envs = []

    def __call__(self, ap_id, instruction, thinking="off", extra_env=None):
        self.calls.append((ap_id, instruction, extra_env or {}))
        if "当前提案" in instruction or "最新提案参数" in instruction:
            self.vote_envs.append(extra_env or {})
            return '同意。\n```json\n{"agreed": true, "reason": "验算通过"}\n```'
        if "所有 AP 已同意" in instruction:
            return "```json\n" + json.dumps(self.decision, ensure_ascii=False) + "\n```\n协商结束"
        if "提案方" in instruction:
            return "建议采用当前可行方案。\n```json\n" + json.dumps(self.decision, ensure_ascii=False) + "\n```"
        return f"{ap_id.upper()} 广播自身状态。"


class OpenClawMigrationTests(unittest.TestCase):
    def test_business_type_defaults_and_ns3_fixture_values(self):
        profiled = orch.apply_profile({"apx": {}})

        self.assertEqual(profiled["apx"]["business_type"], "未声明业务类型")
        for scene in NS3_FIXTURES.values():
            self.assertEqual(scene["ap1"]["business_type"], "后台下载")
            self.assertEqual(scene["ap2"]["business_type"], "直播")
            self.assertEqual(scene["ap3"]["business_type"], "后台下载")

    def test_edca_scene_prioritizes_ap2_live_stream(self):
        scene = NS3_FIXTURES["edca"]

        self.assertEqual(scene["ap1"]["traffic_priority"], "low")
        self.assertEqual(scene["ap2"]["traffic_priority"], "high")
        self.assertEqual(scene["ap3"]["traffic_priority"], "low")
        self.assertEqual(
            {(s["cwmin"], s["cwmax"], s["aifsn"]) for s in scene.values()},
            {(7, 15, 2)},
        )
        self.assertLess(
            EDCA_DECISION["ap2"]["CWmin"],
            EDCA_DECISION["ap1"]["CWmin"],
        )
        self.assertLess(
            EDCA_DECISION["ap2"]["AIFSN"],
            EDCA_DECISION["ap3"]["AIFSN"],
        )

    def test_noop_state_stops_after_broadcast(self):
        state = copy.deepcopy(NS3_FIXTURES["edca"])
        for ap_state in state.values():
            ap_state["traffic_priority"] = "medium"
            ap_state["neighbor_rssi_dbm"] = {
                ap: -90.0 for ap in ap_state["neighbor_rssi_dbm"]
            }
        fake = FakeOpenClawAP({})

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake):
            result = orch.structured_relay()

        self.assertEqual(result["outcome"], "noop")
        self.assertEqual(result["strategy"], "noop")
        self.assertEqual(len(fake.calls), 3)

    def test_structured_relay_streams_agent_chunks(self):
        state = copy.deepcopy(NS3_FIXTURES["edca"])
        for ap_state in state.values():
            ap_state["traffic_priority"] = "medium"
            ap_state["neighbor_rssi_dbm"] = {
                ap: -90.0 for ap in ap_state["neighbor_rssi_dbm"]
            }
        events = []

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            text = f"{ap_id.upper()} 流式广播。"
            if on_text_delta is not None:
                on_text_delta(text)
            return text

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(
                on_event_start=lambda role, ap: events.append(("start", role, ap)),
                on_event_chunk=lambda role, ap, text: events.append(("chunk", role, ap, text)),
            )

        self.assertEqual(result["outcome"], "noop")
        self.assertEqual(len([e for e in events if e[0] == "start"]), 3)
        self.assertEqual(len([e for e in events if e[0] == "chunk"]), 3)
        self.assertTrue(all(e[3].endswith("流式广播。") for e in events if e[0] == "chunk"))
        self.assertEqual(
            [e[2] for e in events if e[0] == "start"],
            ["ap1", "ap2", "ap3"],
        )

    def test_structured_relay_streaming_broadcast_runs_in_order(self):
        state = copy.deepcopy(NS3_FIXTURES["edca"])
        for ap_state in state.values():
            ap_state["traffic_priority"] = "medium"
            ap_state["neighbor_rssi_dbm"] = {
                ap: -90.0 for ap in ap_state["neighbor_rssi_dbm"]
            }
        calls = []
        events = []

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            calls.append(ap_id)
            if on_text_delta is not None:
                on_text_delta(f"{ap_id.upper()} 广播")
            return f"{ap_id.upper()} 广播"

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(
                on_event_start=lambda role, ap: events.append(("start", ap)),
                on_event_chunk=lambda role, ap, text: events.append(("chunk", ap, text)),
        )

        self.assertEqual(result["outcome"], "noop")
        self.assertCountEqual(calls, ["ap1", "ap2", "ap3"])
        self.assertEqual([e[1] for e in events if e[0] == "start"], ["ap1", "ap2", "ap3"])

    def test_raw_stream_tail_emits_text_delta_without_session_duplicate(self):
        class FakeProc:
            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls < 4 else 0

        sid = "ap1-rawstream-test"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"OPENCLAW_HOME": td}):
            profile_dir = os.path.join(td, f".openclaw-{orch.PROFILE}")
            raw_path = os.path.join(profile_dir, "logs", "raw-stream.jsonl")
            session_dir = os.path.join(profile_dir, "agents", "ap1", "sessions")
            session_path = os.path.join(session_dir, f"{sid}.jsonl")
            trajectory_path = os.path.join(session_dir, f"{sid}.trajectory.jsonl")
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            os.makedirs(session_dir, exist_ok=True)

            def writer():
                time.sleep(0.05)
                with open(trajectory_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "type": "session.started",
                        "sessionId": sid,
                        "runId": "run-1",
                    }, ensure_ascii=False) + "\n")
                with open(raw_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "event": "assistant_text_stream",
                        "runId": "other-run",
                        "evtType": "text_delta",
                        "delta": "忽略",
                    }, ensure_ascii=False) + "\n")
                    fh.write(json.dumps({
                        "event": "assistant_text_stream",
                        "runId": "run-1",
                        "evtType": "text_delta",
                        "delta": "流",
                    }, ensure_ascii=False) + "\n")
                    fh.write(json.dumps({
                        "event": "assistant_text_stream",
                        "runId": "run-1",
                        "evtType": "text_delta",
                        "delta": "式",
                    }, ensure_ascii=False) + "\n")
                time.sleep(0.05)
                with open(session_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "timestamp": 1000,
                            "content": [{"type": "text", "text": "完整文本不应重复"}],
                        },
                    }, ensure_ascii=False) + "\n")

            chunks = []
            t = threading.Thread(target=writer)
            t.start()
            try:
                orch._stream_agent_session(
                    "ap1",
                    sid,
                    FakeProc(),
                    chunks.append,
                    {"OPENCLAW_RAW_STREAM_PATH": raw_path},
                )
            finally:
                t.join(timeout=2)

        self.assertEqual(chunks, ["流", "式"])

    def test_raw_stream_tail_does_not_replay_after_session_text(self):
        class FakeProc:
            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls < 5 else 0

        sid = "ap1-session-first-test"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"OPENCLAW_HOME": td}):
            profile_dir = os.path.join(td, f".openclaw-{orch.PROFILE}")
            raw_path = os.path.join(profile_dir, "logs", "raw-stream.jsonl")
            session_dir = os.path.join(profile_dir, "agents", "ap1", "sessions")
            session_path = os.path.join(session_dir, f"{sid}.jsonl")
            trajectory_path = os.path.join(session_dir, f"{sid}.trajectory.jsonl")
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            os.makedirs(session_dir, exist_ok=True)

            def writer():
                time.sleep(0.05)
                with open(session_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "timestamp": 1000,
                            "content": [{"type": "text", "text": "完整"}],
                        },
                    }, ensure_ascii=False) + "\n")
                time.sleep(0.05)
                with open(trajectory_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "type": "session.started",
                        "sessionId": sid,
                        "runId": "run-2",
                    }, ensure_ascii=False) + "\n")
                with open(raw_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "event": "assistant_text_stream",
                        "runId": "run-2",
                        "evtType": "text_delta",
                        "delta": "完整",
                    }, ensure_ascii=False) + "\n")

            chunks = []
            t = threading.Thread(target=writer)
            t.start()
            try:
                orch._stream_agent_session(
                    "ap1",
                    sid,
                    FakeProc(),
                    chunks.append,
                    {"OPENCLAW_RAW_STREAM_PATH": raw_path},
                )
            finally:
                t.join(timeout=2)

        self.assertEqual(chunks, ["完整"])

    def test_non_broadcast_stream_start_is_emitted_before_drive_ap(self):
        state = copy.deepcopy(NS3_FIXTURES["sr"])
        events = []
        decision = decision_for("sr")

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            if "所有 AP 已同意" in instruction:
                role = "decision"
                response = "```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```\n协商结束"
            elif "当前提案" in instruction or "最新提案参数" in instruction:
                role = "voter"
                response = '同意。\n```json\n{"agreed": true, "reason": "验算通过"}\n```'
            elif "提案方" in instruction:
                role = "proposer"
                response = "建议采用当前可行方案。\n```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```"
            else:
                role = "broadcast"
                response = f"{ap_id.upper()} 广播"
            events.append(("drive", role, ap_id))
            return response

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(
                on_event_start=lambda role, ap: events.append(("start", role, ap)),
                on_event_chunk=lambda role, ap, text: events.append(("chunk", ap, text)),
            )

        self.assertEqual(result["outcome"], "success")
        proposer_start = events.index(("start", "proposer", "ap1"))
        proposer_drive = events.index(("drive", "proposer", "ap1"))
        self.assertLess(proposer_start, proposer_drive)

    def test_final_decision_skips_llm_turn(self):
        state = copy.deepcopy(NS3_FIXTURES["sr"])
        decision = decision_for("sr")
        calls = []

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            calls.append(instruction)
            if "所有 AP 已同意" in instruction:
                self.fail("final decision should be emitted deterministically")
            if "当前提案" in instruction or "最新提案参数" in instruction:
                return '同意。\n```json\n{"agreed": true, "reason": "验算通过"}\n```'
            if "提案方" in instruction:
                return "建议采用当前可行方案。\n```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```"
            return f"{ap_id.upper()} 广播"

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(max_turns=8)

        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["decision"], decision)
        self.assertFalse(any("所有 AP 已同意" in c for c in calls))

    def test_structured_relay_accepts_sr_and_edca_only(self):
        for scene_name, expected_strategy in (
            ("sr", "co_sr"),
            ("edca", "co_edca"),
        ):
            with self.subTest(scene=scene_name):
                state = copy.deepcopy(NS3_FIXTURES[scene_name])
                fake = FakeOpenClawAP(decision_for(scene_name))

                with patch.object(orch, "get_all_states", return_value=state), \
                     patch.object(orch, "drive_ap", fake):
                    result = orch.structured_relay(max_turns=8)

                self.assertEqual(result["outcome"], "success")
                self.assertEqual(result["strategy"], expected_strategy)
                self.assertTrue(result["validation"]["approved"], result["validation"])
                self.assertGreaterEqual(len(fake.vote_envs), 2)
                for env in fake.vote_envs:
                    self.assertIn("MULTIAP_CURRENT_PROPOSAL", env)
                    self.assertIn("MULTIAP_CURRENT_STRATEGY", env)

    def test_mixed_sr_edca_proposal_is_rejected(self):
        state = orch.apply_profile(copy.deepcopy(NS3_FIXTURES["edca"]))
        mixed = copy.deepcopy(EDCA_DECISION)
        for ap_id in ("ap1", "ap2", "ap3"):
            mixed[ap_id]["tx_power_dbm"] = state[ap_id]["tx_power_dbm"]

        self.assertEqual(orch.resolve_strategy(mixed), "invalid_mixed")
        report = validate_decision(state, mixed, "invalid_mixed")
        self.assertFalse(report["approved"])
        self.assertIn("仅允许 co_sr 或 co_edca", report["summary"])

    def test_structured_relay_does_not_require_real_observation_when_executor_fails(self):
        state = copy.deepcopy(NS3_FIXTURES["edca"])
        fake = FakeOpenClawAP(copy.deepcopy(EDCA_DECISION))
        failed_push = {
            "ap1": {
                "ok": False,
                "url": "http://192.168.1.1:5002",
                "payload": {},
                "response": "connection refused",
            }
        }

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake), \
             patch.object(orch, "_push_decision", return_value=failed_push):
            result = orch.structured_relay(
                max_turns=8,
                observation_state_getter=lambda: orch.apply_profile(state),
                executor_endpoints={"ap1": "http://192.168.1.1:5002"},
            )

        self.assertEqual(result["outcome"], "success")
        self.assertTrue(result["validation"]["approved"], result["validation"])
        self.assertFalse(result["observed_is_real"])
        self.assertEqual(result["push_results"], failed_push)

    def test_counter_proposal_repair_turn_recovers_unparseable_reject(self):
        """反对者首次未给出可解析反提案 → 修复轮再驱动一次补纯 JSON，被接管为新提案。"""
        state = copy.deepcopy(NS3_FIXTURES["edca"])
        counter = copy.deepcopy(EDCA_DECISION)

        class RepairFakeAP:
            def __init__(self):
                self.rejected_once = False
                self.repair_called = False

            def __call__(self, ap_id, instruction, thinking="off", extra_env=None):
                if "未找到可解析的参数 JSON" in instruction:        # 修复轮
                    self.repair_called = True
                    return "修订反提案。\n```json\n" + json.dumps(counter, ensure_ascii=False) + "\n```"
                if "最新提案参数" in instruction:                    # 投票
                    if ap_id == "ap2" and not self.rejected_once:
                        self.rejected_once = True
                        return "我反对当前提案，对我不利。"           # 无 JSON → 触发修复轮
                    return '同意。\n```json\n{"agreed": true, "reason": "ok"}\n```'
                if "所有 AP 已同意" in instruction:                  # 最终决策
                    return "```json\n" + json.dumps(counter, ensure_ascii=False) + "\n```\n协商结束"
                if "提案方" in instruction:                          # 提案
                    return "提案。\n```json\n" + json.dumps(counter, ensure_ascii=False) + "\n```"
                return f"{ap_id.upper()} 广播自身状态。"

        fake = RepairFakeAP()
        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake):
            result = orch.structured_relay(max_turns=12)

        self.assertTrue(fake.repair_called, "未触发修复轮")
        self.assertEqual(result["outcome"], "success", result)
        self.assertTrue(result["validation"]["approved"], result["validation"])

    def test_mcp_tools_backfill_current_edca_proposal_from_environment(self):
        try:
            from openclaw.mcp import multiap_mcp
        except ModuleNotFoundError as exc:
            self.skipTest(f"mcp package is not installed: {exc}")

        state = copy.deepcopy(NS3_FIXTURES["edca"])
        proposal = copy.deepcopy(EDCA_DECISION)
        old_env = os.environ.get("MULTIAP_CURRENT_PROPOSAL")
        os.environ["MULTIAP_CURRENT_PROPOSAL"] = json.dumps(proposal)
        try:
            with patch.object(multiap_mcp, "get_all_states", return_value=state):
                result = multiap_mcp.validate_edca_proposal()
        finally:
            if old_env is None:
                os.environ.pop("MULTIAP_CURRENT_PROPOSAL", None)
            else:
                os.environ["MULTIAP_CURRENT_PROPOSAL"] = old_env

        self.assertTrue(result["effectiveness"]["all_ok"], result)
        self.assertTrue(result["ap1"]["valid"], result)

    def test_mcp_tools_backfill_current_sr_proposal_from_environment(self):
        try:
            from openclaw.mcp import multiap_mcp
        except ModuleNotFoundError as exc:
            self.skipTest(f"mcp package is not installed: {exc}")

        state = copy.deepcopy(NS3_FIXTURES["sr"])
        proposal = decision_for("sr")
        old_env = os.environ.get("MULTIAP_CURRENT_PROPOSAL")
        os.environ["MULTIAP_CURRENT_PROPOSAL"] = json.dumps(proposal)
        try:
            with patch.object(multiap_mcp, "get_all_states", return_value=state):
                result = multiap_mcp.evaluate_sr_candidate()
        finally:
            if old_env is None:
                os.environ.pop("MULTIAP_CURRENT_PROPOSAL", None)
            else:
                os.environ["MULTIAP_CURRENT_PROPOSAL"] = old_env

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["concurrent_group"], proposal["_sr"]["concurrent_group"])


if __name__ == "__main__":
    unittest.main()

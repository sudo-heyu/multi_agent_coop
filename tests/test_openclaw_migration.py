import copy
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openclaw.scenes import MOCK_SCENES
from openclaw.mcp import orchestration as orch
from src.tools.edca import encode_params_edca


EDCA_DECISION = {
    "ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
    "ap2": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
    "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
}


def decision_for(scene_name: str) -> dict:
    state = orch.apply_profile(copy.deepcopy(MOCK_SCENES[scene_name]))
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
    if scene_name == "joint":
        for ap_id, params in EDCA_DECISION.items():
            decision[ap_id].update(params)
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
    def setUp(self):
        self._workspace_td = tempfile.TemporaryDirectory()
        self._workspace_patch = patch.dict(
            os.environ, {"MULTIAP_AGENT_WORKSPACES_ROOT": self._workspace_td.name}
        )
        self._workspace_patch.start()

    def tearDown(self):
        self._workspace_patch.stop()
        self._workspace_td.cleanup()

    def test_business_type_defaults_and_mock_scene_values(self):
        profiled = orch.apply_profile({"apx": {}})

        self.assertEqual(profiled["apx"]["business_type"], "未声明业务类型")
        for name in ("sr", "edca", "joint"):
            scene = MOCK_SCENES[name]
            self.assertEqual(scene["ap1"]["business_type"], "后台下载")
            self.assertEqual(scene["ap2"]["business_type"], "直播")
            self.assertEqual(scene["ap3"]["business_type"], "后台下载")

    def test_edca_scene_prioritizes_ap2_live_stream(self):
        scene = MOCK_SCENES["edca"]

        self.assertEqual(scene["ap1"]["traffic_priority"], "low")
        self.assertEqual(scene["ap2"]["traffic_priority"], "high")
        self.assertEqual(scene["ap3"]["traffic_priority"], "low")
        self.assertEqual(
            {(s["cwmin"], s["cwmax"], s["aifsn"]) for s in scene.values()},
            {(3, 4, 2)},
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
        state = copy.deepcopy(MOCK_SCENES["edca"])
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
        state = copy.deepcopy(MOCK_SCENES["edca"])
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
        state = copy.deepcopy(MOCK_SCENES["edca"])
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
                    {
                        "MULTIAP_OPENCLAW_RAW_STREAM": "1",
                        "MULTIAP_OPENCLAW_SESSION_TAIL": "1",
                        "OPENCLAW_RAW_STREAM_PATH": raw_path,
                    },
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
                    {
                        "MULTIAP_OPENCLAW_RAW_STREAM": "1",
                        "MULTIAP_OPENCLAW_SESSION_TAIL": "1",
                        "OPENCLAW_RAW_STREAM_PATH": raw_path,
                    },
                )
            finally:
                t.join(timeout=2)

        self.assertEqual(chunks, ["完整"])

    def test_stream_agent_session_default_skips_text_tails(self):
        class FakeProc:
            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls < 4 else 0

        sid = "ap1-default-no-text-tail"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"OPENCLAW_HOME": td}):
            profile_dir = os.path.join(td, f".openclaw-{orch.PROFILE}")
            raw_path = os.path.join(profile_dir, "logs", "raw-stream.jsonl")
            session_dir = os.path.join(profile_dir, "agents", "ap1", "sessions")
            session_path = os.path.join(session_dir, f"{sid}.jsonl")
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            os.makedirs(session_dir, exist_ok=True)

            def writer():
                time.sleep(0.05)
                with open(session_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "session文本"}],
                        },
                    }, ensure_ascii=False) + "\n")
                with open(raw_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "event": "assistant_text_stream",
                        "sessionId": sid,
                        "evtType": "text_delta",
                        "delta": "raw文本",
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

        self.assertEqual(chunks, [])

    def test_stream_agent_session_consumes_source_tool_events(self):
        class FakeProc:
            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls < 4 else 0

        sid = "ap1-tool-event-test"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"OPENCLAW_HOME": td}):
            tool_path = os.path.join(td, "tool-events.jsonl")

            def writer():
                time.sleep(0.05)
                with open(tool_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "event": "mcp_tool_call",
                        "tool": "validate_edca_proposal",
                        "args": {"proposed_edca": {"ap1": {"CWmin": 7}}},
                        "result": {"valid": True},
                        "dur_ms": 12.5,
                    }, ensure_ascii=False) + "\n")

            captured = []
            old_callback = orch._tool_callback
            orch._tool_callback = lambda name, args, result, dur_ms: captured.append(
                (name, args, result, dur_ms)
            )
            t = threading.Thread(target=writer)
            t.start()
            try:
                count = orch._stream_agent_session(
                    "ap1",
                    sid,
                    FakeProc(),
                    None,
                    {"MULTIAP_TOOL_EVENT_PATH": tool_path},
                )
            finally:
                orch._tool_callback = old_callback
                t.join(timeout=2)

        self.assertEqual(count, 1)
        self.assertEqual(captured[0][0], "validate_edca_proposal")
        self.assertEqual(captured[0][1]["proposed_edca"]["ap1"]["CWmin"], 7)
        self.assertTrue(captured[0][2]["valid"])

    def test_non_broadcast_stream_start_is_emitted_before_drive_ap(self):
        state = copy.deepcopy(MOCK_SCENES["sr"])
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
        state = copy.deepcopy(MOCK_SCENES["sr"])
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

    def test_proposal_precheck_blocks_invalid_proposal_before_vote(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        valid = copy.deepcopy(EDCA_DECISION)
        invalid = {
            "ap1": {"CWmin": 1023, "CWmax": 1023, "AIFSN": 2},
            "ap2": {"CWmin": 7, "CWmax": 15, "AIFSN": 2},
            "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 6},
        }
        proposer_calls = 0
        vote_after_invalid = False

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            nonlocal proposer_calls, vote_after_invalid
            if "最新提案参数" in instruction:
                if proposer_calls == 1:
                    vote_after_invalid = True
                return '同意。\n```json\n{"agreed": true, "reason": "验算通过"}\n```'
            if "提案方" in instruction:
                proposer_calls += 1
                proposal = invalid if proposer_calls == 1 else valid
                return "提案。\n```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"
            return f"{ap_id.upper()} 广播"

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(max_validation_retries=3, max_turns=8)

        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(proposer_calls, 2)
        self.assertFalse(vote_after_invalid)
        self.assertTrue(
            any(
                item.get("speaker") == "VALIDATOR"
                and "[提案预检未通过]" in item.get("content", "")
                for item in orch._SESSION.transcript
            )
        )

    def test_mechanical_edca_repair_allows_trivial_cwmax_relation_error(self):
        state = copy.deepcopy(MOCK_SCENES["joint"])
        proposal = {
            "ap1": {"tx_power_dbm": 15, "cwmin": 7, "cwmax": 15, "aifsn": 2},
            "ap2": {"tx_power_dbm": 15, "cwmin": 10, "cwmax": 15, "aifsn": 3},
            "ap3": {"tx_power_dbm": 15, "cwmin": 15, "cwmax": 15, "aifsn": 4},
        }
        vote_count = 0

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            nonlocal vote_count
            if "最新提案参数" in instruction:
                vote_count += 1
                return '同意。\n```json\n{"agreed": true, "reason": "硬约束通过"}\n```'
            if "提案方" in instruction:
                return "联合提案。\n```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"
            return f"{ap_id.upper()} 广播"

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(max_validation_retries=1, max_turns=8)

        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(result["strategy"], "joint")
        self.assertGreaterEqual(vote_count, 2)
        self.assertEqual(result["decision"]["ap3"]["cwmax"], 31)
        self.assertTrue(result["validation"]["approved"], result["validation"])
        self.assertTrue(
            any(
                item.get("speaker") == "VALIDATOR"
                and "[提案机械修复]" in item.get("content", "")
                for item in orch._SESSION.transcript
            )
        )

    def test_nested_edca_proposal_is_normalized_before_validation(self):
        state = copy.deepcopy(MOCK_SCENES["joint"])
        proposal = {
            "ap1": {
                "tx_power_dbm": 18.0,
                "edca": {"cwmin": 7, "cwmax": 15, "aifsn": 2},
            },
            "ap2": {
                "tx_power_dbm": 17.0,
                "edca": {"cwmin": 9, "cwmax": 20, "aifsn": 3},
            },
            "ap3": {
                "tx_power_dbm": 16.0,
                "edca": {"cwmin": 14, "cwmax": 35, "aifsn": 4},
            },
        }

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            if "最新提案参数" in instruction:
                return '同意。\n```json\n{"agreed": true, "reason": "验算通过"}\n```'
            if "提案方" in instruction:
                return "联合提案。\n```json\n" + json.dumps(proposal, ensure_ascii=False) + "\n```"
            return f"{ap_id.upper()} 广播"

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(max_validation_retries=1, max_turns=8)

        self.assertEqual(result["outcome"], "success", result)
        self.assertEqual(result["strategy"], "joint")
        self.assertNotIn("edca", result["decision"]["ap2"])
        self.assertEqual(result["decision"]["ap2"]["cwmin"], 9)
        self.assertEqual(
            result["validation"]["per_ap"]["ap2"]["proposed_params"]["CWmin"], 9
        )

    def test_proposal_prompt_uses_non_mandatory_tool_language(self):
        instruction = orch.propose_instruction("ap1", "joint")
        vote = orch.vote_instruction("ap2", "ap1", "joint", decision_for("joint"), 1)

        self.assertNotIn("必须调用", instruction)
        self.assertNotIn("不得", instruction)
        self.assertIn("编排层会在投票前执行确定性预检", instruction)
        self.assertIn("get_sta_feedback", instruction)
        self.assertIn("未实际调用工具时", vote)
        self.assertIn("get_sta_feedback", vote)
        self.assertIn("如有真实工具结果", vote)

    def test_resume_projection_skips_completed_broadcast_proposal_and_vote(self):
        state = orch.apply_profile(copy.deepcopy(MOCK_SCENES["edca"]))
        decision = copy.deepcopy(EDCA_DECISION)
        calls = []

        def fake_drive(ap_id, instruction, thinking="off", extra_env=None, on_text_delta=None):
            calls.append(ap_id)
            return '同意。\n```json\n{"agreed": true, "reason": "恢复后验算通过"}\n```'

        projection = {
            "boundary": "vote_progress",
            "ap_state": state,
            "transcript": [
                {"speaker": "AP1", "content": "广播"},
                {"speaker": "AP2", "content": "广播"},
                {"speaker": "AP3", "content": "广播"},
                {"speaker": "AP1", "content": "已提出持久化提案"},
                {"speaker": "AP2", "content": "同意"},
            ],
            "proposer": "ap1",
            "proposal": decision,
            "strategy": "co_edca",
            "proposal_num": 1,
            "retry": 0,
            "agree": ["ap2"],
            "vote_cursor": 2,
        }

        with patch.object(orch, "drive_ap", fake_drive):
            result = orch.structured_relay(resume_projection=projection)

        self.assertEqual(result["outcome"], "success")
        self.assertEqual(result["decision"], decision)
        self.assertEqual(calls, ["ap3"])

    def test_proposal_instruction_injects_bounded_episode_references(self):
        episodes = [
            {
                "episode_id": f"ep-{index}",
                "similarity": 0.9 - index * 0.1,
                "quality_score": 0.95,
                "strategy": "co_edca",
                "outcome": "success",
                "decision": {"ap1": {"CWmin": 7 + index}},
                "metrics": {"available": index == 0},
            }
            for index in range(5)
        ]

        instruction = orch.propose_instruction("ap1", "co_edca", episodes)
        text = orch._build_agent_message(
            "ap1", "", instruction, shared_positive=episodes
        )

        self.assertNotIn("历史案例", instruction)
        self.assertIn("共享经验", text)
        self.assertIn("CWmin", text)
        self.assertEqual(text.count("- 正例："), 3)

    def test_structured_relay_accepts_sr_edca_joint(self):
        for scene_name, expected_strategy in (
            ("sr", "co_sr"),
            ("edca", "co_edca"),
            ("joint", "joint"),
        ):
            with self.subTest(scene=scene_name):
                state = copy.deepcopy(MOCK_SCENES[scene_name])
                fake = FakeOpenClawAP(decision_for(scene_name))

                with patch.object(orch, "get_all_states", return_value=state), \
                     patch.object(orch, "drive_ap", fake):
                    result = orch.structured_relay(max_turns=8)

                self.assertEqual(result["outcome"], "success")
                self.assertEqual(result["strategy"], expected_strategy)
                self.assertTrue(result["validation"]["approved"], result["validation"])
                self.assertGreaterEqual(len(fake.vote_envs), 2)
                for env in fake.vote_envs:
                    self.assertNotIn("MULTIAP_CURRENT_PROPOSAL", env)
                    self.assertNotIn("MULTIAP_CURRENT_STRATEGY", env)

    def test_structured_relay_does_not_require_real_observation_when_executor_fails(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
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

    def test_failed_real_observation_rolls_back_applied_candidate(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        state["ap3"]["stas"] = [{
            "sta_id": "sta_ap3",
            "associated_ap": "ap3",
            "flow_type": "bulk_download",
            "sla": {"min_throughput_mbps": 1.0},
            "measurements": {"throughput_mbps": 1.2},
        }]
        observed = copy.deepcopy(state)
        for ap_id, params in EDCA_DECISION.items():
            encoded = encode_params_edca(params)
            observed[ap_id].update({
                "cwmin": encoded["CWmin"],
                "cwmax": encoded["CWmax"],
                "aifsn": encoded["AIFSN"],
            })
        observed["ap3"]["stas"][0]["measurements"]["throughput_mbps"] = 0.2

        fake = FakeOpenClawAP(copy.deepcopy(EDCA_DECISION))
        endpoints = {ap: f"http://{ap}.local:5002" for ap in ("ap1", "ap2", "ap3")}
        push_calls = []

        def fake_push(decision, strategy, endpoints_arg, session_id="", logger=None,
                      action_type="executor_apply"):
            push_calls.append((copy.deepcopy(decision), strategy, action_type))
            return {
                ap: {"ok": True, "url": url, "payload": {}, "response": "ok"}
                for ap, url in endpoints_arg.items()
            }

        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake), \
             patch.object(orch, "_push_decision", side_effect=fake_push):
            result = orch.structured_relay(
                max_validation_retries=1,
                max_turns=8,
                observation_state_getter=lambda: observed,
                executor_endpoints=endpoints,
            )

        self.assertEqual(result["outcome"], "max_retries_exceeded")
        self.assertEqual([call[2] for call in push_calls],
                         ["executor_apply", "executor_rollback"])
        rollback = push_calls[1][0]
        self.assertEqual(
            rollback,
            {
                "ap1": {"cwmin": 7, "cwmax": 15, "aifsn": 2},
                "ap2": {"cwmin": 7, "cwmax": 15, "aifsn": 2},
                "ap3": {"cwmin": 7, "cwmax": 15, "aifsn": 2},
            },
        )

    def test_vote_model_failure_falls_back_to_validator_vote(self):
        state = copy.deepcopy(MOCK_SCENES["edca"])
        decision = copy.deepcopy(EDCA_DECISION)

        class VoteFailureFakeAP:
            def __init__(self):
                self.vote_failures = 0

            def __call__(
                self,
                ap_id,
                instruction,
                thinking="off",
                extra_env=None,
                on_text_delta=None,
            ):
                if "最新提案参数" in instruction:
                    self.vote_failures += 1
                    raise RuntimeError("incomplete terminal response")
                if "所有 AP 已同意" in instruction:
                    return "```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```\n协商结束"
                if "提案方" in instruction:
                    return "建议采用 EDCA 差异化。\n```json\n" + json.dumps(decision, ensure_ascii=False) + "\n```"
                return f"{ap_id.upper()} 广播自身状态。"

        fake = VoteFailureFakeAP()
        with patch.object(orch, "get_all_states", return_value=state), \
             patch.object(orch, "drive_ap", fake):
            result = orch.structured_relay(max_turns=8)

        self.assertEqual(result["outcome"], "success", result)
        self.assertGreaterEqual(fake.vote_failures, 2)
        self.assertTrue(result["validation"]["approved"], result["validation"])

    def test_counter_proposal_repair_turn_recovers_unparseable_reject(self):
        """反对者首次未给出可解析反提案 → 修复轮再驱动一次补纯 JSON，被接管为新提案。"""
        state = copy.deepcopy(MOCK_SCENES["edca"])
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

    def test_mcp_tools_accept_full_edca_proposal_argument(self):
        try:
            from openclaw.mcp import multiap_mcp
        except ModuleNotFoundError as exc:
            self.skipTest(f"mcp package is not installed: {exc}")

        state = copy.deepcopy(MOCK_SCENES["edca"])
        proposal = copy.deepcopy(EDCA_DECISION)
        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.validate_edca_proposal(proposal)

        self.assertTrue(result["effectiveness"]["all_ok"], result)
        self.assertTrue(result["ap1"]["valid"], result)

    def test_mcp_tools_accept_full_sr_proposal_argument(self):
        try:
            from openclaw.mcp import multiap_mcp
        except ModuleNotFoundError as exc:
            self.skipTest(f"mcp package is not installed: {exc}")

        state = copy.deepcopy(MOCK_SCENES["sr"])
        proposal = decision_for("sr")
        with patch.object(multiap_mcp, "get_all_states", return_value=state):
            result = multiap_mcp.evaluate_sr_candidate(proposal)

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["concurrent_group"], proposal["_sr"]["concurrent_group"])


if __name__ == "__main__":
    unittest.main()

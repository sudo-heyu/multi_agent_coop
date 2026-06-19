import copy
import json
import os
import unittest
from unittest.mock import patch

from openclaw.scenes import MOCK_SCENES
from openclaw.mcp import orchestration as orch


EDCA_DECISION = {
    "ap1": {"CWmin": 3, "CWmax": 15, "AIFSN": 2},
    "ap2": {"CWmin": 7, "CWmax": 31, "AIFSN": 3},
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
                    self.assertIn("MULTIAP_CURRENT_PROPOSAL", env)
                    self.assertIn("MULTIAP_CURRENT_STRATEGY", env)

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

    def test_mcp_tools_backfill_current_edca_proposal_from_environment(self):
        try:
            from openclaw.mcp import multiap_mcp
        except ModuleNotFoundError as exc:
            self.skipTest(f"mcp package is not installed: {exc}")

        state = copy.deepcopy(MOCK_SCENES["edca"])
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

        state = copy.deepcopy(MOCK_SCENES["sr"])
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

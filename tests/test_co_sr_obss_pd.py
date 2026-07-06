"""协议级 Co-SR（OBSS_PD/SRP）端到端字段的确定性单测。

覆盖三处接缝：
  1. src/tools/sr.py —— 工具输出携带 OBSS_PD 门限（合法窗口内）；
  2. src/validator.py —— co_sr 分支对 obss_pd 的范围 / 耦合 / 缺省容忍；
  3. openclaw/mcp/proposal_utils.py —— 仅带 obss_pd 时仍判为 co_sr；
  4. state_server/ns3_bridge.py —— co_sr 提案映射为 `APPLY ... tx= obss_pd=`。
"""
import io
import threading
import unittest

from src.tools import sr
from src import validator
from openclaw.mcp.proposal_utils import _infer_strategy_from_proposal


def _states():
    """三 AP 同信道、含 dict 形式邻居 RSSI 的实测状态。"""
    return {
        "ap1": {"tx_power_dbm": 20, "sta_rssi_dbm": -55, "noise_floor_dbm": -94,
                "neighbor_rssi_dbm": {"ap2": -66}},
        "ap2": {"tx_power_dbm": 20, "sta_rssi_dbm": -55, "noise_floor_dbm": -94,
                "neighbor_rssi_dbm": {"ap1": -66}},
        "ap3": {"tx_power_dbm": 20, "sta_rssi_dbm": -55, "noise_floor_dbm": -94,
                "neighbor_rssi_dbm": {}},
    }


class SrObssPdToolTests(unittest.TestCase):
    def test_feasible_ranges_carry_obss_pd_in_window(self):
        ranges = sr.compute_feasible_ranges(_states())["ranges"]
        for ap_id, r in ranges.items():
            obss = r.get("recommended_obss_pd_dbm")
            self.assertIsNotNone(obss, f"{ap_id} 缺少 recommended_obss_pd_dbm")
            self.assertLessEqual(sr.OBSS_PD_MIN_DBM, obss)
            self.assertLessEqual(obss, sr.OBSS_PD_MAX_DBM)
        # ap1/ap2 互为最强干扰 -66 → 门限应取 -66；ap3 无邻居 → 回落默认 -82
        self.assertEqual(ranges["ap1"]["recommended_obss_pd_dbm"], -66.0)
        self.assertEqual(ranges["ap3"]["recommended_obss_pd_dbm"], sr.OBSS_PD_MIN_DBM)

    def test_evaluate_candidate_exposes_obss_pd_alias(self):
        states = _states()
        proposed = {"ap1": 6.0, "ap2": 6.0, "ap3": 20.0}
        per_ap = sr.evaluate_candidate_for_group(states, proposed, ["ap1", "ap2"])["per_ap"]
        for ap_id in ("ap1", "ap2"):
            self.assertIn("obss_pd_dbm", per_ap[ap_id])
            self.assertLessEqual(per_ap[ap_id]["obss_pd_dbm"], sr.OBSS_PD_MAX_DBM)


class ValidatorObssPdTests(unittest.TestCase):
    def test_valid_proposal_with_coupled_power_is_approved(self):
        # obss_pd=-66 → tx 上限 23-(−66+82)=7；tx=6 满足耦合
        decision = {"ap1": {"tx_power_dbm": 6, "obss_pd_dbm": -66},
                    "ap2": {"tx_power_dbm": 6, "obss_pd_dbm": -66},
                    "ap3": {"tx_power_dbm": 20, "obss_pd_dbm": -82}}
        rep = validator.validate_decision(_states(), decision, "co_sr")
        self.assertTrue(rep["approved"], rep["summary"])
        self.assertEqual(rep["per_ap"]["ap1"]["proposed_params"]["obss_pd_dbm"], -66.0)

    def test_out_of_window_obss_pd_is_rejected(self):
        decision = {"ap1": {"tx_power_dbm": 6, "obss_pd_dbm": -50},
                    "ap2": {"tx_power_dbm": 6, "obss_pd_dbm": -66},
                    "ap3": {"tx_power_dbm": 20, "obss_pd_dbm": -82}}
        rep = validator.validate_decision(_states(), decision, "co_sr")
        self.assertFalse(rep["approved"])
        self.assertTrue(any("SR 合法窗口" in e for e in rep["per_ap"]["ap1"]["errors"]))

    def test_coupling_violation_is_rejected(self):
        # obss_pd=-66（上限 7）但 tx=15 → 违反耦合
        decision = {"ap1": {"tx_power_dbm": 15, "obss_pd_dbm": -66},
                    "ap2": {"tx_power_dbm": 6, "obss_pd_dbm": -66},
                    "ap3": {"tx_power_dbm": 20, "obss_pd_dbm": -82}}
        rep = validator.validate_decision(_states(), decision, "co_sr")
        self.assertFalse(rep["approved"])
        self.assertTrue(any("功率耦合" in e for e in rep["per_ap"]["ap1"]["errors"]))

    def test_missing_obss_pd_is_tolerated_backward_compatible(self):
        # 旧式仅带 tx_power_dbm 的 co_sr 提案不应因缺 obss_pd 被拒
        decision = {"ap1": {"tx_power_dbm": 10},
                    "ap2": {"tx_power_dbm": 10},
                    "ap3": {"tx_power_dbm": 10}}
        rep = validator.validate_decision(_states(), decision, "co_sr")
        self.assertTrue(rep["approved"], rep["summary"])


class StrategyInferenceTests(unittest.TestCase):
    def test_obss_pd_only_infers_co_sr(self):
        proposal = {"ap1": {"obss_pd_dbm": -70}, "ap2": {"obss_pd_dbm": -70}}
        self.assertEqual(_infer_strategy_from_proposal(proposal), "co_sr")


class BridgeApplyTableTests(unittest.TestCase):
    def _bridge(self):
        from state_server import ns3_bridge as br
        b = br.Ns3Bridge.__new__(br.Ns3Bridge)
        b._stdin_lock = threading.Lock()

        class _Proc:
            def __init__(self):
                self.stdin = io.StringIO()

        b.proc = _Proc()
        return b

    def test_co_sr_maps_tx_and_obss_pd(self):
        cmd = self._bridge().apply("ap1", "co_sr",
                                   {"tx_power_dbm": 6, "obss_pd_dbm": -66})["command"]
        self.assertEqual(cmd, "APPLY ap1 tx=6.0 obss_pd=-66.0")

    def test_joint_maps_both_families(self):
        cmd = self._bridge().apply("ap3", "joint",
                                   {"tx_power_dbm": 8, "obss_pd_dbm": -70,
                                    "cwmin": 4, "cwmax": 10, "aifsn": 2})["command"]
        # 指数 n=4 → 实际 CW 15；n=10 → 1023
        self.assertEqual(cmd, "APPLY ap3 tx=8.0 obss_pd=-70.0 cwmin=15 cwmax=1023 aifsn=2")


if __name__ == "__main__":
    unittest.main()

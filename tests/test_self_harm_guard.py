"""Validator 第三层自伤门的确定性单测。

背景：一次真实 ns-3 协商（2026-07-09）里，Co-EDCA 提案把 low 优先级 AP 的
CWmin/AIFSN 拉到 [32, 7]，跟其他 AP 的 [7, 2] 相比排序完全合规（low 应该更
保守），但幅度太极端，把自己的信道抢占能力压到几乎为零——实测 iperf 吞吐从
11.3Mbps 崩到 0。当时的 Validator 只做参数区间 + 优先级排序检查，看不出幅度
问题，径直放行。这里补的是幅度门：Co-EDCA 用预测信道抢占份额跌幅，
Co-SR 用预测 STA RSSI 安全下界，都是闭式估算、不依赖真实观测。
"""
import unittest

from src import validator
from src.tools import edca, sr


def _edca_states(overrides=None):
    """三 AP 同信道，初始 EDCA 参数一致（默认场景基线）。"""
    base = {
        "cwmin": 15, "cwmax": 1023, "aifsn": 3,
        "traffic_priority": "medium", "sla_violations": [],
        "sta_feedback_summary": {"status": "satisfied"},
    }
    states = {ap: dict(base) for ap in ("ap1", "ap2", "ap3")}
    for ap, patch in (overrides or {}).items():
        states[ap].update(patch)
    return states


class EdcaAccessWeightTests(unittest.TestCase):
    def test_larger_cwmin_and_aifsn_lower_weight(self):
        aggressive = edca.access_weight(cwmin=7, aifsn=2)
        conservative = edca.access_weight(cwmin=32, aifsn=7)
        self.assertGreater(aggressive, conservative)

    def test_predict_access_share_reproduces_incident(self):
        """复现事故现场的三组参数，验证份额预测方向正确、幅度足够大。"""
        states = _edca_states()
        proposed = {
            "ap1": {"CWmin": 32, "CWmax": 1023, "AIFSN": 7},
            "ap2": {"CWmin": 7, "CWmax": 15, "AIFSN": 2},
            "ap3": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},
        }
        shares = edca.predict_access_share(states, proposed, ac="BE")
        self.assertLess(shares["ap1"]["share_ratio"], 0.5)
        self.assertGreater(shares["ap2"]["share_ratio"], 1.0)
        # ap3 参数未变，但邻居此消彼长仍会拉低它的相对份额（份额是零和的）。
        self.assertLess(shares["ap3"]["share_ratio"], 1.0)

    def test_detect_self_harm_flags_only_the_touched_ap(self):
        states = _edca_states()
        proposed = {
            "ap1": {"CWmin": 32, "CWmax": 1023, "AIFSN": 7},
            "ap2": {"CWmin": 7, "CWmax": 15, "AIFSN": 2},
            "ap3": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},  # 未改动
        }
        flagged = edca.detect_self_harm(states, proposed, ac="BE")
        flagged_ids = {item["ap_id"] for item in flagged}
        self.assertIn("ap1", flagged_ids)
        self.assertNotIn("ap3", flagged_ids)  # ap3 参数没变，被动份额下降不算自伤

    def test_moderate_priority_gradient_not_flagged(self):
        """合理的优先级梯度（差距不极端）不应被自伤门误伤。"""
        states = _edca_states()
        proposed = {
            "ap1": {"CWmin": 15, "CWmax": 1023, "AIFSN": 4},   # low
            "ap2": {"CWmin": 7, "CWmax": 255, "AIFSN": 2},     # high
            "ap3": {"CWmin": 10, "CWmax": 511, "AIFSN": 3},    # medium
        }
        flagged = edca.detect_self_harm(states, proposed, ac="BE", share_ratio_floor=0.5)
        self.assertEqual(flagged, [])


class ValidatorEdcaSelfHarmTests(unittest.TestCase):
    def _incident_decision(self):
        return {
            "ap1": {"CWmin": 32, "CWmax": 1023, "AIFSN": 7},
            "ap2": {"CWmin": 7, "CWmax": 15, "AIFSN": 2},
            "ap3": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},
        }

    def test_self_harm_decision_rejected_without_justification(self):
        states = _edca_states()
        report = validator.validate_decision(states, self._incident_decision(), "co_edca")
        self.assertFalse(report["approved"], report["summary"])
        self.assertTrue(
            any("自伤" in e for e in report["per_ap"]["ap1"]["errors"]),
            report["per_ap"]["ap1"]["errors"],
        )

    def test_self_harm_decision_approved_when_neighbor_sla_violated(self):
        """ap2 正处于 SLA 违规，牺牲 ap1 抢占权去救它，属于正当理由，应放行。"""
        states = _edca_states({
            "ap2": {"sta_feedback_summary": {"status": "violated"}},
        })
        report = validator.validate_decision(states, self._incident_decision(), "co_edca")
        self.assertTrue(report["approved"], report["summary"])

    def test_ordering_compliant_but_extreme_gradient_still_rejected(self):
        """priority 排序完全合规（low 参数更大），幅度门仍要拦——这正是排序检查
        看不出来、这次事故实际发生的模式。"""
        states = _edca_states({
            "ap1": {"traffic_priority": "low"},
            "ap2": {"traffic_priority": "high"},
            "ap3": {"traffic_priority": "medium"},
        })
        decision = self._incident_decision()
        effectiveness = edca.evaluate_edca_effectiveness(states, decision)
        self.assertTrue(effectiveness["all_ok"], effectiveness["priority_ordering"]["warnings"])
        report = validator.validate_decision(states, decision, "co_edca")
        self.assertFalse(report["approved"])

    def test_reasonable_proposal_still_approved(self):
        states = _edca_states()
        decision = {
            "ap1": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},
            "ap2": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},
            "ap3": {"CWmin": 15, "CWmax": 1023, "AIFSN": 3},
        }
        report = validator.validate_decision(states, decision, "co_edca")
        self.assertTrue(report["approved"], report["summary"])


def _sr_states():
    # sta_rssi_dbm=-60 @ tx=20dBm → 路径损耗 80dB；砍到法定下限 1dBm（delta=-19dB）
    # 会把 STA RSSI 打到 -79dBm，跌破 STA_RSSI_MIN_DBM=-75dBm 的安全下界。
    return {
        "ap1": {"tx_power_dbm": 20, "sta_rssi_dbm": -60, "noise_floor_dbm": -94,
                "neighbor_rssi_dbm": {"ap2": -70}, "sla_violations": []},
        "ap2": {"tx_power_dbm": 20, "sta_rssi_dbm": -60, "noise_floor_dbm": -94,
                "neighbor_rssi_dbm": {"ap1": -70}, "sla_violations": []},
    }


class SrSelfHarmTests(unittest.TestCase):
    def test_detect_self_harm_flags_extreme_power_cut(self):
        states = _sr_states()
        proposed = {"ap1": 1.0}  # 从 20dBm 砍到 1dBm，STA RSSI 必然跌破安全下界
        flagged = sr.detect_self_harm(states, proposed)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["ap_id"], "ap1")
        self.assertLess(flagged[0]["sta_rssi_after"], sr.STA_RSSI_MIN_DBM)

    def test_untouched_ap_not_flagged(self):
        states = _sr_states()
        flagged = sr.detect_self_harm(states, {"ap2": 20.0})  # ap2 功率不变
        self.assertEqual(flagged, [])

    def test_validator_rejects_extreme_power_cut(self):
        states = _sr_states()
        decision = {"ap1": {"tx_power_dbm": 1}, "ap2": {"tx_power_dbm": 20}}
        report = validator.validate_decision(states, decision, "co_sr")
        self.assertFalse(report["approved"], report["summary"])
        self.assertTrue(any("自伤" in e for e in report["per_ap"]["ap1"]["errors"]))

    def test_validator_approves_modest_power_change(self):
        states = _sr_states()
        decision = {"ap1": {"tx_power_dbm": 18}, "ap2": {"tx_power_dbm": 20}}
        report = validator.validate_decision(states, decision, "co_sr")
        self.assertTrue(report["approved"], report["summary"])


if __name__ == "__main__":
    unittest.main()

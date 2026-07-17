import unittest

from src.profile import agent_view, apply_profile


class ProfileEdcaSourceTest(unittest.TestCase):
    def test_ns3_cw_values_are_already_actual(self):
        profiled = apply_profile({
            "ap1": {
                "source": "ns3",
                "service_name": "best_effort",
                "traffic_priority": "medium",
                "cwmin": 15,
                "cwmax": 1023,
                "aifsn": 3,
            }
        })
        self.assertEqual(profiled["ap1"]["cwmin"], 15)
        self.assertEqual(profiled["ap1"]["cwmax"], 1023)

    def test_real_ap_ecw_values_are_decoded(self):
        profiled = apply_profile({
            "ap1": {
                "source": "ap",
                "service_name": "best_effort",
                "traffic_priority": "medium",
                "cwmin": 4,
                "cwmax": 10,
                "aifsn": 3,
            }
        })
        self.assertEqual(profiled["ap1"]["cwmin"], 15)
        self.assertEqual(profiled["ap1"]["cwmax"], 1023)

    def test_source_is_internal_not_agent_visible(self):
        profiled = apply_profile({"ap1": {"source": "ns3"}})
        self.assertIn("source", profiled["ap1"])
        self.assertNotIn("source", agent_view(profiled)["ap1"])

    def test_ns3_qos_fields_are_retained(self):
        profiled = apply_profile({
            "ap1": {
                "source": "ns3",
                "throughput_mbps_iperf": 10.0,
                "throughput_mbps_user": 5.0,
                "latency_ms": 7.5,
                "packet_loss_pct": 0.2,
            }
        })
        self.assertEqual(profiled["ap1"]["throughput_mbps_iperf"], 10.0)
        self.assertEqual(profiled["ap1"]["throughput_mbps_user"], 5.0)
        self.assertEqual(profiled["ap1"]["latency_ms"], 7.5)
        self.assertEqual(profiled["ap1"]["packet_loss_pct"], 0.2)


if __name__ == "__main__":
    unittest.main()

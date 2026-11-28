"""Check configured credit conversion independently of external TSN dependencies."""
import unittest
from fractions import Fraction
from math import ceil

from algorithm.cbs_configuration import (
    CBSResponseConversionError, PortConfiguration, admin_idle_slope,
)


def group(rank, *, port="P5", frame=4336, blocking=12336):
    return {"node": "SW1", "port": port, "srClass": f"client-{8-rank}",
            "zhaoPriorityRank": rank, "maxFrameBits": frame,
            "blockingFrameBits": blocking,
            "sharedCBS": {"sendSlopeBps": -1, "creditMinBits": -1, "creditMaxBits": 1}}


class TestCBSConfiguration(unittest.TestCase):
    def test_tc2_tc3_tc4_m2_credit(self):
        for m1, m2, frame, expected in [
            (9892317, 10579002, 4336, 239),
            (19784633, 21602932, 4336, 533),
            (7296001, 7296001, 2736, 147),
        ]:
            with self.subTest(m1=m1):
                config = PortConfiguration()
                config.registers(group(1, frame=frame), m1, 100_000_000, "M1")
                self.assertEqual(config.registers(group(2, frame=frame), m2, 100_000_000, "M2")["hiCreditBytes"], expected)

    def test_all_seven_classes_with_quantized_higher_slopes(self):
        speed = 100_000_000
        config = PortConfiguration()
        higher = []
        for rank in range(1, 8):
            slope = admin_idle_slope(2_000_001 + rank, speed,
                                     {"idleSlopeGranularityBps": 1_000_000}, str(rank))
            frame = 800 + rank * 80
            expected = ceil(Fraction(slope, speed - sum(s for s, _ in higher)) *
                            (12336 + sum(Fraction((speed-s)*f, speed) for s, f in higher)) / 8)
            result = config.registers(group(rank, frame=frame), slope, speed, str(rank))
            self.assertEqual(result["hiCreditBytes"], expected)
            self.assertEqual(result["sendSlopeBps"], speed-slope)
            self.assertEqual(result["loCreditBytes"], ceil(Fraction((speed-slope)*frame, speed*8)))
            higher.append((slope, frame))

    def test_ports_independent(self):
        config = PortConfiguration()
        config.registers(group(1), 20_000_000, 100_000_000, "M1")
        result = config.registers(group(2, port="P6"), 10_000_000, 100_000_000, "M2")
        self.assertEqual(result["hiCreditBytes"], 155)

    def test_rejects_out_of_order_and_capacity(self):
        config = PortConfiguration()
        config.registers(group(2), 60_000_000, 100_000_000, "M2")
        with self.assertRaisesRegex(CBSResponseConversionError, "order"):
            config.registers(group(1), 1_000_000, 100_000_000, "M1")
        with self.assertRaisesRegex(CBSResponseConversionError, "capacity"):
            config.registers(group(3), 40_000_000, 100_000_000, "M3")


if __name__ == "__main__":
    unittest.main()

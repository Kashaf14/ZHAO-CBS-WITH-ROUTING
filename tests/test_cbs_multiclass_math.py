"""Unit tests for isolated Zhao CBS per-class math (not an M2 end-to-end regression)."""
import math
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "optimizer-manager"))

from optimizers.cbs_multiclass_math import (  # noqa: E402
    HigherPriorityClass,
    calculate_class_cbs,
)


class TestZhaoPerClassCBS(unittest.TestCase):
    def test_m1_reduces_to_existing_theorem2_expression(self):
        # Synthetic M1 local case: no higher priority, BE/lower frame is 800 bits.
        c = 100_000_000.0
        x_ns, y_bits, d_ns, l_lower = 10_000.0, 800.0, 100_000.0, 800.0
        value = calculate_class_cbs(
            x_ns=x_ns, y_bits=y_bits, local_deadline_ns=d_ns,
            own_max_frame_bits=1600, lower_priority_max_frame_bits=l_lower,
            link_speed_bps=c,
        )
        original = y_bits / ((d_ns + x_ns) / 1e9 - l_lower / c)
        self.assertAlmostEqual(value.idle_slope_bps, original, places=7)
        self.assertAlmostEqual(value.credit_max_bits, original * l_lower / c, places=10)
        self.assertAlmostEqual(value.credit_min_bits, (original - c) * 1600 / c, places=10)
        self.assertAlmostEqual(value.worst_case_delay_ns, d_ns, places=7)

    def test_m2_uses_m1_credit_min_and_residual_capacity(self):
        # Synthetic local M2 point; M1 slope is taken from user-reported TC1 M1 regression.
        c = 100_000_000.0
        m1_slope = 12_769_329.56648479
        m1_max_frame_bits = 1600.0
        l_lower = 800.0
        x_ns, y_bits, d_ns = 10_000.0, 1600.0, 250_000.0
        result = calculate_class_cbs(
            x_ns=x_ns, y_bits=y_bits, local_deadline_ns=d_ns,
            own_max_frame_bits=1600, lower_priority_max_frame_bits=l_lower,
            link_speed_bps=c,
            higher_priority_classes=(HigherPriorityClass(m1_slope, m1_max_frame_bits),),
        )
        hp_credit_min = (m1_slope - c) * m1_max_frame_bits / c
        expected_service_latency = (l_lower - hp_credit_min) / (c - m1_slope)
        expected_idle = y_bits / ((d_ns + x_ns) / 1e9 - expected_service_latency)
        self.assertAlmostEqual(result.idle_slope_bps, expected_idle, places=7)
        self.assertAlmostEqual(result.service_latency_s, expected_service_latency, places=14)
        self.assertAlmostEqual(result.credit_max_bits, expected_idle * expected_service_latency, places=10)
        self.assertAlmostEqual(result.worst_case_delay_ns, d_ns, places=7)
        self.assertLess(m1_slope + result.idle_slope_bps, c)

    def test_third_class_sums_both_higher_priority_credits(self):
        c = 100_000_000.0
        higher = (HigherPriorityClass(12_000_000, 1200),
                  HigherPriorityClass(18_000_000, 900))
        result = calculate_class_cbs(
            x_ns=0, y_bits=1600, local_deadline_ns=500_000,
            own_max_frame_bits=1000, lower_priority_max_frame_bits=800,
            link_speed_bps=c, higher_priority_classes=higher,
        )
        self.assertAlmostEqual(result.worst_case_delay_ns, 500_000, places=7)
        self.assertLess(sum(x.idle_slope_bps for x in higher) + result.idle_slope_bps, c)
        self.assertLess(result.credit_min_bits, 0)
        self.assertGreater(result.credit_max_bits, 0)

    def test_rejects_exhausted_higher_priority_capacity(self):
        with self.assertRaisesRegex(ValueError, "exhaust link capacity"):
            calculate_class_cbs(
                x_ns=0, y_bits=800, local_deadline_ns=200_000,
                own_max_frame_bits=800, lower_priority_max_frame_bits=800,
                link_speed_bps=100_000_000,
                higher_priority_classes=(HigherPriorityClass(55_000_000, 1000),
                                         HigherPriorityClass(45_000_000, 1000)),
            )

    def test_rejects_impossible_local_deadline(self):
        with self.assertRaisesRegex(ValueError, "insufficient"):
            calculate_class_cbs(
                x_ns=0, y_bits=800, local_deadline_ns=100,
                own_max_frame_bits=800, lower_priority_max_frame_bits=800,
                link_speed_bps=100_000_000,
            )

    def test_rejects_current_class_exhausting_residual_capacity(self):
        with self.assertRaisesRegex(ValueError, "reaches link capacity"):
            calculate_class_cbs(
                x_ns=0, y_bits=100_000_000, local_deadline_ns=100_000,
                own_max_frame_bits=800, lower_priority_max_frame_bits=800,
                link_speed_bps=100_000_000,
            )

    def test_rejects_invalid_units_and_nan(self):
        for case in (float("nan"), float("inf"), -1, True):
            with self.subTest(case=repr(case)), self.assertRaises(ValueError):
                calculate_class_cbs(
                    x_ns=0, y_bits=800, local_deadline_ns=200_000,
                    own_max_frame_bits=800, lower_priority_max_frame_bits=800,
                    link_speed_bps=case,
                )


if __name__ == "__main__":
    unittest.main()

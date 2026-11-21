"""N-class CBS prerequisite tests (synthetic values, not paper measurements)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algorithm.cbs_priority_dependencies import (
    higher_priority_for_port, priority_index,
)


def group(cls, slope=None, *, node="SW1", port="P4", speed=100_000_000, frame=800):
    result = {
        "node": node, "port": port, "srClass": cls,
        "linkSpeedBps": speed, "maxFrameBits": frame,
    }
    if slope is not None:
        result["sharedCBS"] = {"idleSlopeBps": slope}
    return result


class TestNClassHigherPriorityDependencies(unittest.TestCase):
    def test_explicit_priority_order(self):
        self.assertEqual([priority_index(f"M{i}") for i in range(1, 8)], list(range(1, 8)))

    def test_m1_has_no_higher_priority(self):
        self.assertEqual(
            higher_priority_for_port(group("M1", 12_000_000),
                [group("M1", 12_000_000), group("M2", 8_000_000)]),
            (),
        )

    def test_m2_uses_m1_only(self):
        hp = higher_priority_for_port(group("M2", 8_000_000), [
            group("M2", 8_000_000), group("M1", 12_000_000, frame=1600)])
        self.assertEqual(len(hp), 1)
        self.assertEqual(hp[0].idle_slope_bps, 12_000_000)
        self.assertEqual(hp[0].max_frame_bits, 1600)

    def test_m3_uses_m1_and_m2_even_if_supplied_unsorted(self):
        hp = higher_priority_for_port(group("M3", 4_000_000), [
            group("M3", 4_000_000),
            group("M2", 8_000_000, frame=1600),
            group("M1", 12_000_000, frame=800),
        ])
        self.assertEqual(
            [(x.idle_slope_bps, x.max_frame_bits) for x in hp],
            [(12_000_000, 800), (8_000_000, 1600)],
        )

    def test_gaps_are_allowed_when_no_actual_m2_traffic_at_port(self):
        hp = higher_priority_for_port(group("M3", 4_000_000), [
            group("M1", 12_000_000), group("M3", 4_000_000)])
        self.assertEqual(len(hp), 1)
        self.assertEqual(hp[0].idle_slope_bps, 12_000_000)

    def test_other_ports_do_not_affect_current_port(self):
        hp = higher_priority_for_port(group("M3", 4_000_000), [
            group("M3", 4_000_000),
            group("M1", 12_000_000, port="P5"),
            group("M2", 8_000_000, node="ES1"),
        ])
        self.assertEqual(hp, ())

    def test_rejects_uncomputed_active_higher_class(self):
        with self.assertRaisesRegex(ValueError, "Uncomputed"):
            higher_priority_for_port(group("M3", 4_000_000), [
                group("M1", 12_000_000), group("M2"), group("M3", 4_000_000)])

    def test_rejects_duplicate_class_and_link_speed_conflict(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            higher_priority_for_port(group("M2"), [
                group("M1", 1_000_000), group("M1", 1_000_000), group("M2")])
        with self.assertRaisesRegex(ValueError, "Inconsistent link speed"):
            higher_priority_for_port(group("M2"), [
                group("M1", 1_000_000, speed=10_000_000), group("M2")])

    def test_rejects_unknown_class_and_reservation_over_capacity(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            priority_index("M8")
        with self.assertRaisesRegex(ValueError, "exhaust"):
            higher_priority_for_port(group("M3", 4_000_000), [
                group("M1", 60_000_000),
                group("M2", 50_000_000),
                group("M3", 4_000_000),
            ])

    def test_rejects_missing_current_group(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            higher_priority_for_port(group("M3"), [group("M1", 12_000_000)])


if __name__ == "__main__":
    unittest.main()

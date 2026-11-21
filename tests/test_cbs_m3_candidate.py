"""Step 9: BOUNDED synthetic M3 candidate, not Zhao TC1 M3 or CNC validation.

Requires Step 8 pipeline and previous helpers. Runs M3 DIRECTLY; never exposes
M3 through Manager capabilities or edits the TC1 contract fixture on disk.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "optimizer-manager"))

from optimizers.cbs import CBSOptimizer
from optimizers import cbs_m3_candidate
from usecase_adapter import build_usecase_from_payload

TC1 = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))


def m3_request_from_tc1(original):
    """Clone TC1 in memory and add *synthetic* M3 f9; no published M3 target."""
    request = deepcopy(original)
    assert request["schemaVersion"] == "1.4"
    assert request["operation"] == "configuration"
    assert request["optimizationTask"]["scope"]["trafficClasses"] == ["M1"]
    source = {s["definition"]["id"]: s for s in request["forwardingPlane"]["streams"]}
    assert set(source) == {f"f{i}" for i in range(1, 9)}
    assert all(s["definition"]["trafficClassId"] in {"M1", "M2"} for s in source.values())

    new = deepcopy(source["f8"])
    new["definition"]["id"] = "f9"
    new["definition"]["streamId"]["macAddress"] = "02:00:00:00:00:09"
    new["definition"]["streamId"]["uniqueId"] = 9
    new["definition"]["trafficClassId"] = "M3"
    new["definition"]["trafficSpec"]["maxFrameSize"] = 100
    new["definition"]["trafficSpec"]["maxIntervalFrames"] = 1
    new["definition"]["trafficSpec"]["intervalNs"] = 10_000_000
    new["definition"]["userToNetworkRequirements"]["maxLatencyNs"] = 5_000_000
    new["configuration"]["pcp"] = 3  # only a stream PCP; no fabricated physical queue mapping
    request["forwardingPlane"]["streams"].append(new)
    request["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2", "M3"]
    return request


def by_group(result):
    index = {}
    for row in result["portClassGroups"]:
        key = (row["srClass"], row["node"], row["port"])
        assert key not in index, key
        index[key] = row
    return index


class TestBoundedSyntheticM3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.source_bytes)
        cls.synthetic = m3_request_from_tc1(cls.original)
        cls.optimizer = CBSOptimizer()
        base = deepcopy(cls.original)
        base["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2"]
        cls.m1_m2 = cls.optimizer.optimize(build_usecase_from_payload(base))
        cls.candidate = cbs_m3_candidate.optimize_m1_m2_m3_candidate(
            cls.optimizer, build_usecase_from_payload(cls.synthetic)
        )
        cls.base_index = by_group(cls.m1_m2)
        cls.index = by_group(cls.candidate)

    def test_m1_m2_port_slope_and_credit_results_unchanged(self):
        for key, before in self.base_index.items():
            after = self.index[key]
            for k in ("idleSlopeBps", "sendSlopeBps", "creditMinBits", "creditMaxBits"):
                self.assertAlmostEqual(before["sharedCBS"][k], after["sharedCBS"][k], places=6)
            self.assertAlmostEqual(before["sharedDelayNs"], after["sharedDelayNs"], places=4)
        self.assertEqual(set(self.index) - set(self.base_index), {
            ("M3", "ES3", "Port1"), ("M3", "SW1", "P5")
        })

    def test_m3_at_two_ports_sees_both_higher_classes(self):
        original = cbs_m3_candidate.higher_priority_for_port
        with patch.object(cbs_m3_candidate, "higher_priority_for_port", wraps=original) as spy:
            result = cbs_m3_candidate.optimize_m1_m2_m3_candidate(
                self.optimizer, build_usecase_from_payload(self.synthetic)
            )
        self.assertEqual(len([r for r in result["sharedStreamResults"] if r["stream"] == "f9"]), 1)
        self.assertEqual(spy.call_count, 2)
        for call in spy.call_args_list:
            current, all_groups = call.args
            self.assertEqual(current["srClass"], "M3")
            higher = original(current, all_groups)
            self.assertEqual(len(higher), 2)
            expected = [
                self.index[(cls, current["node"], current["port"])]
                for cls in ("M1", "M2")
            ]
            self.assertEqual([x.idle_slope_bps for x in higher], [
                g["sharedCBS"]["idleSlopeBps"] for g in expected
            ])

    def test_m3_equations_and_three_class_port_capacity(self):
        for (cls, node, port), row in self.index.items():
            if cls != "M3":
                continue
            higher = [self.index[(c, node, port)] for c in ("M1", "M2")]
            speed = row["linkSpeedBps"]
            hi_slope = sum(g["sharedCBS"]["idleSlopeBps"] for g in higher)
            hi_credit_min = sum(g["sharedCBS"]["creditMinBits"] for g in higher)
            latency_s = (row["blockingFrameBits"] - hi_credit_min) / (speed - hi_slope)
            x_s = row["sharedXYPoint"]["xNs"] / 1_000_000_000
            y = row["sharedXYPoint"]["yBits"]
            deadline_s = row["requiredDeadlineNs"] / 1_000_000_000
            expected_slope = y / (deadline_s + x_s - latency_s)
            actual = row["sharedCBS"]
            self.assertTrue(math.isclose(actual["idleSlopeBps"], expected_slope,
                                         rel_tol=1e-10), (node, port))
            self.assertTrue(math.isclose(actual["creditMaxBits"],
                                         actual["idleSlopeBps"] * latency_s,
                                         rel_tol=1e-10))
            self.assertTrue(math.isclose(actual["creditMinBits"],
                                         (actual["idleSlopeBps"]-speed)
                                         * row["maxFrameBits"]/speed,
                                         rel_tol=1e-10))
            combined = actual["idleSlopeBps"] + hi_slope
            self.assertLess(combined, speed)
            self.assertGreaterEqual(actual["idleSlopeBps"] + 1e-6, row["aggregateRhoBps"])
            self.assertLessEqual(row["sharedDelayNs"], row["requiredDeadlineNs"]+1e-4)

    def test_nine_analytical_path_deadlines(self):
        self.assertEqual(len(self.candidate["sharedStreamResults"]), 9)
        self.assertTrue(self.candidate["allDeadlinesSatisfied"])
        self.assertEqual({r["stream"] for r in self.candidate["sharedStreamResults"]},
                         {f"f{i}" for i in range(1, 10)})
        for row in self.candidate["sharedStreamResults"]:
            self.assertLessEqual(row["sharedPathDelayNs"], row["deadlineNs"]+1e-4)

    def test_larger_m3_frames_recalculate_higher_class_blocking(self):
        larger = deepcopy(self.synthetic)
        s = next(s for s in larger["forwardingPlane"]["streams"]
                 if s["definition"]["id"] == "f9")
        s["definition"]["trafficSpec"]["maxFrameSize"] = 300
        result = cbs_m3_candidate.optimize_m1_m2_m3_candidate(
            self.optimizer, build_usecase_from_payload(larger)
        )
        self.assertTrue(result["allDeadlinesSatisfied"])
        changed = by_group(result)
        # f9: ES3/Port1 -> SW1/P5. At those ports M3's 2400-bit frame
        # must be considered as a blocker by both M1 and M2.
        for cls in ("M1", "M2"):
            for node, port in (("ES3", "Port1"), ("SW1", "P5")):
                key = (cls, node, port)
                self.assertEqual(changed[key]["blockingFrameBits"], 2400)
                self.assertGreater(
                    changed[key]["blockingFrameBits"],
                    self.index[key]["blockingFrameBits"], key,
                )
                self.assertGreater(
                    changed[key]["sharedCBS"]["idleSlopeBps"],
                    self.index[key]["sharedCBS"]["idleSlopeBps"], key,
                )

    def test_unrelated_port_blocking_remains_unchanged(self):
        larger = deepcopy(self.synthetic)
        s = next(s for s in larger["forwardingPlane"]["streams"]
                 if s["definition"]["id"] == "f9")
        s["definition"]["trafficSpec"]["maxFrameSize"] = 300
        changed = by_group(cbs_m3_candidate.optimize_m1_m2_m3_candidate(
            self.optimizer, build_usecase_from_payload(larger)
        ))
        # The M3 stream does not traverse SW1/P4 or ES1/Port1.
        for cls in ("M1", "M2"):
            for node, port in (("ES1", "Port1"), ("SW1", "P4")):
                key = (cls, node, port)
                self.assertEqual(
                    changed[key]["blockingFrameBits"],
                    self.index[key]["blockingFrameBits"], key,
                )

    def test_multiframe_m3_rejected(self):
        invalid = deepcopy(self.synthetic)
        s = next(s for s in invalid["forwardingPlane"]["streams"]
                 if s["definition"]["id"] == "f9")
        s["definition"]["trafficSpec"]["maxIntervalFrames"] = 2
        with self.assertRaisesRegex(ValueError, "one frame per interval"):
            cbs_m3_candidate.optimize_m1_m2_m3_candidate(
                self.optimizer, build_usecase_from_payload(invalid)
            )

    def test_wrong_target_classes_rejected(self):
        invalid = deepcopy(self.synthetic)
        invalid["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M3"]
        with self.assertRaisesRegex(ValueError, "M1, M2, M3 target classes"):
            cbs_m3_candidate.optimize_m1_m2_m3_candidate(
                self.optimizer, build_usecase_from_payload(invalid)
            )

    def test_reconfiguration_m3_rejected(self):
        invalid = deepcopy(self.synthetic)
        invalid["operation"] = "reconfiguration"
        with self.assertRaisesRegex(ValueError, "configuration only"):
            cbs_m3_candidate.optimize_m1_m2_m3_candidate(
                self.optimizer, build_usecase_from_payload(invalid)
            )

    def test_cbs_capability_advertises_client_class_support(self):
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)

    def test_original_tc1_fixture_untouched(self):
        self.assertEqual(TC1.read_bytes(), self.source_bytes)
        self.assertEqual(len(self.original["forwardingPlane"]["streams"]), 8)
        self.assertEqual(self.original["optimizationTask"]["scope"]["trafficClasses"], ["M1"])


if __name__ == "__main__":
    unittest.main()

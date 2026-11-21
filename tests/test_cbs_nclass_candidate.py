"""Direct-call variable-N CBS tests. Numerical M1/M2 references: Zhao TC1.

M3..M7 are SYNTHETIC stress cases, not Zhao published measurements.
No physical queue mappings, Manager call, CNC run, or independent NC proof.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algorithm.cbs import CBSOptimizer
from algorithm.cbs_nclass_candidate import optimize_nclass_candidate
from algorithm.cbs_priority_dependencies import priority_index
from usecase_adapter import build_usecase_from_payload

TC1 = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))

# Publication Table II; percentage of a 100 Mbps link.
PAPER = {
    ("M1", "ES1", "Port1"): 12.77,
    ("M1", "ES2", "Port1"): 4.94,
    ("M1", "ES3", "Port1"): 2.47,
    ("M1", "SW1", "P4"): 9.84,
    ("M1", "SW1", "P5"): 9.56,
    ("M2", "ES1", "Port1"): 7.93,
    ("M2", "ES2", "Port1"): 0.72,
    ("M2", "ES3", "Port1"): 5.11,
    ("M2", "SW1", "P4"): 8.74,
    ("M2", "SW1", "P5"): 4.42,
}


def synth_request(original, classes=2, *, m4_frame=100):
    """In-memory clone of TC1; extra streams are invented synthetic cases."""
    req = deepcopy(original)
    if classes < 2 or classes > 7:
        raise ValueError("2..7 classes supported by this fixture constructor")
    f8 = next(s for s in req["forwardingPlane"]["streams"]
              if s["definition"]["id"] == "f8")
    for i in range(3, classes + 1):
        new = deepcopy(f8)
        number = i + 6
        new["definition"]["id"] = f"f{number}"
        new["definition"]["streamId"]["macAddress"] = f"02:00:00:00:00:{number:02x}"
        new["definition"]["streamId"]["uniqueId"] = number
        new["definition"]["trafficClassId"] = f"M{i}"
        new["definition"]["trafficSpec"]["maxFrameSize"] = (
            m4_frame if i == 4 else 100
        )
        new["definition"]["trafficSpec"]["maxIntervalFrames"] = 1
        new["definition"]["trafficSpec"]["intervalNs"] = 100_000_000
        new["definition"]["userToNetworkRequirements"]["maxLatencyNs"] = 20_000_000
        # PCP here is a synthetically assigned tag, NOT a physical queue mapping.
        new["configuration"]["pcp"] = max(0, 6 - i)
        req["forwardingPlane"]["streams"].append(new)
    req["optimizationTask"]["scope"]["trafficClasses"] = [
        f"M{i}" for i in range(1, classes + 1)
    ]
    return req


def index(result):
    groups = {}
    for g in result["portClassGroups"]:
        key = (g["srClass"], g["node"], g["port"])
        assert key not in groups, key
        groups[key] = g
    return groups


class TestGenericNClassCandidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.original_bytes)
        cls.optimizer = CBSOptimizer()
        cls.request2 = synth_request(cls.original, classes=2)
        cls.result2 = optimize_nclass_candidate(
            cls.optimizer, build_usecase_from_payload(cls.request2)
        )

    def test_two_classes_reproduce_all_ten_zhao_tc1_bandwidth_values(self):
        groups = index(self.result2)
        self.assertEqual(set(groups), set(PAPER))
        for key, published_pct in PAPER.items():
            g = groups[key]
            actual_pct = 100 * g["sharedCBS"]["idleSlopeBps"] / g["linkSpeedBps"]
            self.assertLessEqual(
                abs(actual_pct - published_pct), 0.01, (key, actual_pct, published_pct)
            )
        self.assertTrue(self.result2["allDeadlinesSatisfied"])
        self.assertEqual(len(self.result2["sharedStreamResults"]), 8)

    def test_three_class_synthetic_routes_and_deadlines(self):
        req = synth_request(self.original, classes=3)
        r = optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))
        groups = index(r)
        self.assertEqual(len(r["sharedStreamResults"]), 9)
        self.assertTrue(r["allDeadlinesSatisfied"])
        self.assertEqual({k for k in groups if k[0] == "M3"}, {
            ("M3", "ES3", "Port1"), ("M3", "SW1", "P5")
        })
        for key in PAPER:
            a = groups[key]["sharedCBS"]["idleSlopeBps"]
            b = index(self.result2)[key]["sharedCBS"]["idleSlopeBps"]
            self.assertTrue(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-5), key)

    def test_four_classes_larger_frame_recomputes_blocking(self):
        req = synth_request(self.original, classes=4, m4_frame=300)
        r = optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))
        groups = index(r)
        self.assertEqual(len(r["sharedStreamResults"]), 10)
        self.assertTrue(r["allDeadlinesSatisfied"])
        for cls in ("M1", "M2", "M3"):
            for node, port in (("ES3", "Port1"), ("SW1", "P5")):
                self.assertEqual(groups[(cls, node, port)]["blockingFrameBits"], 2400)
        for cls in ("M1", "M2"):
            for node, port in (("ES1", "Port1"), ("SW1", "P4")):
                key = (cls, node, port)
                self.assertAlmostEqual(
                    groups[key]["sharedCBS"]["idleSlopeBps"],
                    index(self.result2)[key]["sharedCBS"]["idleSlopeBps"],
                    delta=1e-5,
                )

    def test_seven_classes_processed_without_separate_class_branches(self):
        req = synth_request(self.original, classes=7)
        r = optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))
        groups = index(r)
        self.assertEqual(len(r["sharedStreamResults"]), 13)
        self.assertTrue(r["allDeadlinesSatisfied"])
        self.assertEqual({key[0] for key in groups}, {f"M{i}" for i in range(1, 8)})
        for g in groups.values():
            self.assertLess(g["sharedCBS"]["idleSlopeBps"], g["linkSpeedBps"])
        ports = {(g["node"], g["port"]) for g in groups.values()}
        for node, port in ports:
            values = [g for g in groups.values() if g["node"] == node and g["port"] == port]
            self.assertLess(
                sum(g["sharedCBS"]["idleSlopeBps"] for g in values),
                values[0]["linkSpeedBps"],
            )

    def test_invalid_domains_fail_closed(self):
        req = synth_request(self.original, classes=3)
        req["forwardingPlane"]["streams"][-1]["definition"]["trafficSpec"]["maxIntervalFrames"] = 2
        with self.assertRaisesRegex(ValueError, "one frame per interval"):
            optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))
        req = synth_request(self.original, classes=3)
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M2", "M3"]
        with self.assertRaisesRegex(ValueError, "higher-priority"):
            optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))

    def test_reconfiguration_numerical_parity(self):
        """Same numerical result; changePolicy/application are NOT being tested."""
        request = synth_request(self.original, classes=3)
        baseline = optimize_nclass_candidate(
            self.optimizer, build_usecase_from_payload(deepcopy(request))
        )
        request["operation"] = "reconfiguration"
        request["optimizationTask"]["changePolicy"] = {"allowQueueChanges": True}
        result = optimize_nclass_candidate(
            self.optimizer, build_usecase_from_payload(request)
        )
        actual, expected = index(result), index(baseline)
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(result["sharedStreamResults"]), len(baseline["sharedStreamResults"]))
        for key in expected:
            self.assertAlmostEqual(
                actual[key]["sharedCBS"]["idleSlopeBps"],
                expected[key]["sharedCBS"]["idleSlopeBps"], places=6,
            )

    def test_lower_priority_m3_can_remain_blocking_background(self):
        req = synth_request(self.original, classes=3)
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2"]
        result = optimize_nclass_candidate(
            self.optimizer, build_usecase_from_payload(req)
        )
        self.assertEqual(len(result["sharedStreamResults"]), 8)
        self.assertEqual({g["srClass"] for g in result["portClassGroups"]}, {"M1", "M2"})
        self.assertTrue(result["allDeadlinesSatisfied"])

    def test_source_fixture_and_advertised_capabilities_unchanged(self):
        self.assertEqual(TC1.read_bytes(), self.original_bytes)

        capabilities = self.optimizer.get_capabilities()

        self.assertNotIn("trafficClasses", capabilities)
        self.assertTrue(capabilities["inputRequirements"]["requiresPcp"])
        self.assertTrue(capabilities["inputRequirements"]["acceptsClientTrafficClassIds"])

        self.assertEqual(self.original["optimizationTask"]["scope"]["trafficClasses"], ["M1"])
        self.assertEqual(priority_index("M7"), 7)


if __name__ == "__main__":
    unittest.main()

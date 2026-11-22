"""Numerical CBS N-class regression against five frozen legacy scenarios.

The reference contains recorded pre-migration CBS outputs. This test verifies
its checksum and compares current calculations with those historical values.
It does not regenerate the reference or provide independent network-calculus
or hardware validation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from algorithm.cbs import CBSOptimizer
import algorithm.cbs_nclass_candidate as generic_module
from usecase_adapter import build_usecase_from_payload
from scripts.cbs_legacy_baseline_capture import (
    compact_result, canonical_json, make_request, append_m3,
)

SNAPSHOT = ROOT / "tests/regression/evidence/cbs_step17_pre_dispatch_baseline.json"
SNAPSHOT_SHA256 = "d10dd2c8e17ccd55ea0cb9f0e85d54538313cacc4cd88c759b58c37964f11f21"
FIXTURE = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts/tests/request/valid/"
    "cbs-paper-tc1-full-8flow-contract.json"
))
FIELDS = ("linkSpeedBps", "requiredDeadlineNs", "sharedDelayNs",
          "blockingFrameBits", "maxFrameBits", "aggregateRhoBps")
CBS_FIELDS = ("idleSlopeBps", "sendSlopeBps", "creditMinBits", "creditMaxBits")
STREAM_FIELDS = ("sharedPathDelayNs", "deadlineNs")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def equal_float(test, actual, expected, context, *, abs_tol=1e-4):
    test.assertTrue(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=abs_tol),
                    f"{context}: frozen={expected!r}; current={actual!r}")


class TestGenericDispatcherFrozenReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reference_bytes = SNAPSHOT.read_bytes()
        if sha(reference_bytes) != SNAPSHOT_SHA256:
            raise AssertionError("STOP: frozen Step 17 reference hash changed; do not regenerate")
        cls.reference = json.loads(reference_bytes)
        if cls.reference.get("managerAdvertisedTrafficClasses") != ["M1"]:
            raise AssertionError("Unexpected saved Manager capabilities")
        cls.fixture_bytes = FIXTURE.read_bytes()
        if sha(cls.fixture_bytes) != cls.reference["sourceFixtureSHA256"]:
            raise AssertionError("TC1 input fixture changed since Step 17")
        cls.original = json.loads(cls.fixture_bytes)
        cls.optimizer = CBSOptimizer()
        cls.cases = {
            "tc1_m1_with_m2_background": deepcopy(cls.original),
            "tc1_m1_m2": make_request(cls.original),
            "tc1_m1_m2_m3_100B": make_request(cls.original, with_m3=True, frame_size=100),
            "tc1_m1_m2_m3_300B": make_request(cls.original, with_m3=True, frame_size=300),
            "tc1_m1_with_m3_300B_background": append_m3(deepcopy(cls.original), frame_size=300),
        }
        if set(cls.cases) != set(cls.reference["cases"]):
            raise AssertionError("Frozen case inventory differs from expected five scenarios")

    def compare(self, case):
        payload = self.cases[case]
        unchanged = canonical_json(payload)
        expected = self.reference["cases"][case]
        self.assertEqual(sha(unchanged), expected["inputSHA256"])
        with patch.object(generic_module, "optimize_nclass_candidate",
                          wraps=generic_module.optimize_nclass_candidate) as spy:
            result = self.optimizer.optimize(build_usecase_from_payload(deepcopy(payload)))
            self.assertEqual(spy.call_count, 1, "Normal dispatch must invoke generic solver once")
        self.assertEqual(canonical_json(payload), unchanged)
        actual = compact_result(result)
        old = expected["result"]
        self.assertEqual(actual["allDeadlinesSatisfied"], old["allDeadlinesSatisfied"])
        equal_float(self, actual["overallWorstCaseDelayNs"],
                    old["overallWorstCaseDelayNs"], case + " max delay")

        def keyed(groups):
            index = {(g["srClass"], g["node"], g["port"]): g for g in groups}
            self.assertEqual(len(index), len(groups), "Duplicate port/class")
            return index

        old_groups, new_groups = keyed(old["portClassGroups"]), keyed(actual["portClassGroups"])
        self.assertEqual(set(old_groups), set(new_groups), case + " port/class coverage")
        for key in old_groups:
            for field in FIELDS:
                equal_float(self, new_groups[key][field], old_groups[key][field],
                            f"{case} {key} {field}")
            for field in CBS_FIELDS:
                equal_float(self, new_groups[key]["sharedCBS"][field],
                            old_groups[key]["sharedCBS"][field],
                            f"{case} {key} {field}", abs_tol=1e-5)

        old_streams = {s["stream"]: s for s in old["sharedStreamResults"]}
        new_streams = {s["stream"]: s for s in actual["sharedStreamResults"]}
        self.assertEqual(len(old_streams), len(old["sharedStreamResults"]))
        self.assertEqual(len(new_streams), len(actual["sharedStreamResults"]))
        self.assertEqual(set(old_streams), set(new_streams), case + " stream coverage")
        for name in old_streams:
            self.assertEqual(new_streams[name]["deadlineSatisfied"],
                             old_streams[name]["deadlineSatisfied"])
            for field in STREAM_FIELDS:
                equal_float(self, new_streams[name][field], old_streams[name][field],
                            f"{case} stream {name} {field}")

    def test_tc1_m1_with_m2_background(self):
        self.compare("tc1_m1_with_m2_background")

    def test_tc1_m1_m2(self):
        self.compare("tc1_m1_m2")

    def test_tc1_m1_m2_m3_100B(self):
        self.compare("tc1_m1_m2_m3_100B")

    def test_tc1_m1_m2_m3_300B(self):
        self.compare("tc1_m1_m2_m3_300B")

    def test_tc1_m1_with_m3_300B_background(self):
        self.compare("tc1_m1_with_m3_300B_background")

    def test_normal_dispatch_supports_seven_synthetic_classes(self):
        # Dispatch behavior for N=7, not independent validation or real queue fit.
        from test_cbs_nclass_candidate import synth_request
        request = synth_request(self.original, classes=7)
        result = self.optimizer.optimize(
            build_usecase_from_payload(deepcopy(request))
        )
        self.assertEqual(len(result["sharedStreamResults"]), 13)
        self.assertTrue(result["allDeadlinesSatisfied"])
        self.assertEqual(
            {g["srClass"] for g in result["portClassGroups"]},
            {f"M{i}" for i in range(1, 8)}
        )

    def test_normal_dispatch_rejects_eighth_class(self):
        # Contract/model labels beyond M7 are unsupported, not silently mapped.
        from test_cbs_nclass_candidate import synth_request
        request = synth_request(self.original, classes=7)
        request["optimizationTask"]["scope"]["trafficClasses"][-1] = "M8"
        request["forwardingPlane"]["streams"][-1]["definition"]["trafficClassId"] = "M8"
        with self.assertRaisesRegex(ValueError, "Unsupported or unverified"):
            self.optimizer.optimize(build_usecase_from_payload(request))

    def test_capabilities_match_supported_operation(self):
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)
        self.assertEqual(caps["tasks"], ["configuration","reconfiguration"])
        self.assertIs(caps["supportsReconfiguration"], True)
        self.assertEqual(FIXTURE.read_bytes(), self.fixture_bytes)
        self.assertEqual(sha(SNAPSHOT.read_bytes()), SNAPSHOT_SHA256)


if __name__ == "__main__":
    unittest.main()

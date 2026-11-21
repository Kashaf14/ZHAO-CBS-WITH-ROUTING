"""Step 16 checks retained after generic dispatch migration.

Checks generic dispatch versus direct-call behavior, blocking with untargeted
lower classes, and validation of unsafe selections. These tests are NOT old
solver parity after Step 18; the immutable numerical reference is checked in
``test_cbs_step18_frozen_baseline.py``. Not independent NC validation.
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
from usecase_adapter import build_usecase_from_payload

TC1 = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))
FIELDS = ("idleSlopeBps", "sendSlopeBps", "creditMinBits", "creditMaxBits")


def groups(result):
    return {
        (g["srClass"], g["node"], g["port"]): g
        for g in result["portClassGroups"]
    }


def streams(result):
    return {r["stream"]: r for r in result["sharedStreamResults"]}


def append_m3(req, *, frame_size=300):
    source = next(s for s in req["forwardingPlane"]["streams"]
                  if s["definition"]["id"] == "f8")
    extra = deepcopy(source)
    extra["definition"]["id"] = "f9"
    extra["definition"]["streamId"]["macAddress"] = "02:00:00:00:00:09"
    extra["definition"]["streamId"]["uniqueId"] = 9
    extra["definition"]["trafficClassId"] = "M3"
    extra["definition"]["trafficSpec"]["maxFrameSize"] = frame_size
    extra["definition"]["trafficSpec"]["maxIntervalFrames"] = 1
    extra["definition"]["trafficSpec"]["intervalNs"] = 10_000_000
    extra["definition"]["userToNetworkRequirements"]["maxLatencyNs"] = 5_000_000
    extra["configuration"]["pcp"] = 3  # TEST TAG, not physical queue mapping.
    req["forwardingPlane"]["streams"].append(extra)
    return req


class TestGenericNClassBackground(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.fixture_bytes)
        cls.optimizer = CBSOptimizer()

    def compare(self, req, *, targeted_classes):
        baseline = self.optimizer.optimize(build_usecase_from_payload(deepcopy(req)))
        generic = optimize_nclass_candidate(
            self.optimizer, build_usecase_from_payload(deepcopy(req))
        )
        self.assertEqual(set(groups(baseline)), set(groups(generic)))
        self.assertEqual(set(streams(baseline)), set(streams(generic)))
        self.assertEqual({g["srClass"] for g in generic["portClassGroups"]}, set(targeted_classes))
        self.assertTrue(baseline["allDeadlinesSatisfied"])
        self.assertTrue(generic["allDeadlinesSatisfied"])
        for key in sorted(groups(baseline)):
            a, b = groups(baseline)[key], groups(generic)[key]
            for field in FIELDS:
                with self.subTest(group=key, field=field):
                    self.assertTrue(math.isclose(
                        a["sharedCBS"][field], b["sharedCBS"][field],
                        rel_tol=1e-9, abs_tol=1e-5
                    ), f"{key} {field}: old={a['sharedCBS'][field]}, "
                       f"generic={b['sharedCBS'][field]}")
            self.assertEqual(a["blockingFrameBits"], b["blockingFrameBits"])
        for sid in sorted(streams(baseline)):
            a, b = streams(baseline)[sid], streams(generic)[sid]
            self.assertEqual(a["deadlineSatisfied"], b["deadlineSatisfied"])
            self.assertTrue(math.isclose(
                a["sharedPathDelayNs"], b["sharedPathDelayNs"],
                rel_tol=1e-9, abs_tol=1e-4
            ), f"{sid}: {a['sharedPathDelayNs']} vs {b['sharedPathDelayNs']}")
        return baseline, generic

    def test_m1_only_parity_with_m2_background(self):
        req = deepcopy(self.original)  # The actual published TC1 fixture targets M1.
        self.assertEqual(req["optimizationTask"]["scope"]["trafficClasses"], ["M1"])
        base, candidate = self.compare(req, targeted_classes=["M1"])
        self.assertEqual(len(streams(candidate)), 4)

    def test_m1_only_parity_with_larger_m3_background(self):
        req = append_m3(deepcopy(self.original), frame_size=300)
        self.compare(req, targeted_classes=["M1"])

    def test_m1_m2_parity_with_larger_m3_background(self):
        req = append_m3(deepcopy(self.original), frame_size=300)
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2"]

        # The old M1+M2 dispatcher rejects any request containing M3,
        # including an untargeted background stream. Instead, use its
        # already-tested FULL three-class dispatch on the IDENTICAL nine
        # streams as the reference for the M1/M2 results. M3 reservations
        # should not alter higher-class CBS calculations, but its frame can
        # block them regardless of whether M3 is itself optimized.
        reference_request = deepcopy(req)
        reference_request["optimizationTask"]["scope"]["trafficClasses"] = [
            "M1", "M2", "M3"
        ]
        reference = self.optimizer.optimize(
            build_usecase_from_payload(reference_request)
        )
        candidate = optimize_nclass_candidate(
            self.optimizer, build_usecase_from_payload(deepcopy(req))
        )
        self.assertTrue(reference["allDeadlinesSatisfied"])
        self.assertTrue(candidate["allDeadlinesSatisfied"])
        self.assertEqual(
            {g["srClass"] for g in candidate["portClassGroups"]},
            {"M1", "M2"},
        )

        expected_groups = {
            key: group for key, group in groups(reference).items()
            if key[0] in ("M1", "M2")
        }
        candidate_groups = groups(candidate)
        self.assertEqual(set(expected_groups), set(candidate_groups))
        for key, a in sorted(expected_groups.items()):
            b = candidate_groups[key]
            for field in FIELDS:
                with self.subTest(group=key, field=field):
                    self.assertTrue(math.isclose(
                        a["sharedCBS"][field], b["sharedCBS"][field],
                        rel_tol=1e-9, abs_tol=1e-5
                    ), f"{key} {field}: full={a['sharedCBS'][field]}, "
                       f"background={b['sharedCBS'][field]}")
            for field in ("blockingFrameBits", "requiredDeadlineNs", "sharedDelayNs"):
                with self.subTest(group=key, field=field):
                    self.assertTrue(math.isclose(
                        a[field], b[field], rel_tol=1e-9, abs_tol=1e-4
                    ), f"{key} {field}: full={a[field]}, background={b[field]}")

        expected_ids = {
            s["definition"]["id"]
            for s in req["forwardingPlane"]["streams"]
            if s["definition"]["trafficClassId"] in ("M1", "M2")
        }
        self.assertEqual(set(streams(candidate)), expected_ids)
        for stream_id in sorted(expected_ids):
            full, partial = streams(reference)[stream_id], streams(candidate)[stream_id]
            with self.subTest(stream=stream_id):
                self.assertEqual(full["deadlineSatisfied"], partial["deadlineSatisfied"])
                self.assertTrue(math.isclose(
                    full["sharedPathDelayNs"], partial["sharedPathDelayNs"],
                    rel_tol=1e-9, abs_tol=1e-4
                ), f"{stream_id}: full={full['sharedPathDelayNs']}, "
                   f"background={partial['sharedPathDelayNs']}")

        # A larger M3 frame blocks M1/M2 on these shared egress ports.
        for cls in ("M1", "M2"):
            for node, port in (("ES3", "Port1"), ("SW1", "P5")):
                self.assertEqual(
                    candidate_groups[(cls, node, port)]["blockingFrameBits"], 2400
                )
        # No mutation of the requested M1+M2-only scope.
        self.assertEqual(
            req["optimizationTask"]["scope"]["trafficClasses"], ["M1", "M2"]
        )

    def test_skipping_active_higher_class_is_rejected(self):
        req = deepcopy(self.original)
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M2"]
        with self.assertRaisesRegex(ValueError, "higher-priority"):
            optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))
        req = append_m3(deepcopy(self.original))
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M3"]
        with self.assertRaisesRegex(ValueError, "higher-priority"):
            optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))

    def test_unknown_background_class_and_missing_target_fail_closed(self):
        req = append_m3(deepcopy(self.original))
        req["forwardingPlane"]["streams"][-1]["definition"]["trafficClassId"] = "M8"
        with self.assertRaisesRegex(ValueError, "Unsupported or unverified"):
            optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))
        req = deepcopy(self.original)
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M7"]
        with self.assertRaisesRegex(ValueError, "at least one stream"):
            optimize_nclass_candidate(self.optimizer, build_usecase_from_payload(req))

    def test_original_fixture_and_manager_advertisement_unchanged(self):
        self.assertEqual(TC1.read_bytes(), self.fixture_bytes)
        capabilities = self.optimizer.get_capabilities()

        self.assertNotIn("trafficClasses", capabilities)
        self.assertTrue(capabilities["inputRequirements"]["requiresPcp"])
        self.assertTrue(capabilities["inputRequirements"]["acceptsClientTrafficClassIds"])

if __name__ == "__main__":
    unittest.main()

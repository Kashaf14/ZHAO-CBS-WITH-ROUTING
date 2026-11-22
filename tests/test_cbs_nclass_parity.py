"""Compare retained class-specific CBS candidates with the generic N-class solver.

The legacy M1/M2 and M1/M2/M3 candidate functions are called directly and
compared with optimize_nclass_candidate() on cloned Zhao TC1 requests:
M1+M2, synthetic M3 at 100 bytes, and synthetic M3 at 300 bytes.

This is numerical implementation parity, not an independent schedulability
proof. The original input fixture is never edited.

Run:
  python -m unittest discover -s tests -p 'test_cbs_nclass_parity.py' -v
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algorithm.cbs import CBSOptimizer
from algorithm.cbs_nclass_candidate import optimize_nclass_candidate
from usecase_adapter import build_usecase_from_payload

TC1 = ROOT / "tests" / "fixtures" / "cbs-paper-tc1-step17-original.json"

CBS_FIELDS = ("idleSlopeBps", "sendSlopeBps", "creditMinBits", "creditMaxBits")
GROUP_FIELDS = (
    "linkSpeedBps", "requiredDeadlineNs", "sharedDelayNs",
    "blockingFrameBits", "maxFrameBits", "aggregateRhoBps",
)


def make_request(original: dict, *, with_m3: bool = False, frame_size: int = 100) -> dict:
    request = deepcopy(original)
    assert request["schemaVersion"] == "1.4"
    assert request["operation"] == "configuration"
    assert request["optimizationTask"]["scope"]["trafficClasses"] == ["M1"]
    assert {s["definition"]["id"] for s in request["forwardingPlane"]["streams"]} == {
        f"f{i}" for i in range(1, 9)
    }
    request["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2"]
    if with_m3:
        source = next(s for s in request["forwardingPlane"]["streams"]
                      if s["definition"]["id"] == "f8")
        f9 = deepcopy(source)
        f9["definition"]["id"] = "f9"
        f9["definition"]["streamId"]["macAddress"] = "02:00:00:00:00:09"
        f9["definition"]["streamId"]["uniqueId"] = 9
        f9["definition"]["trafficClassId"] = "M3"
        f9["definition"]["trafficSpec"]["maxFrameSize"] = frame_size
        f9["definition"]["trafficSpec"]["maxIntervalFrames"] = 1
        f9["definition"]["trafficSpec"]["intervalNs"] = 10_000_000
        f9["definition"]["userToNetworkRequirements"]["maxLatencyNs"] = 5_000_000
        # Synthetic stream tag only. NO device or physical queue mapping.
        f9["configuration"]["pcp"] = 3
        request["forwardingPlane"]["streams"].append(f9)
        request["optimizationTask"]["scope"]["trafficClasses"].append("M3")
    return request


def group_index(result: dict) -> dict:
    index = {}
    for group in result["portClassGroups"]:
        key = (group["srClass"], group["node"], group["port"])
        if key in index:
            raise AssertionError(f"Duplicate port-class result: {key}")
        index[key] = group
    return index


def stream_index(result: dict) -> dict:
    index = {}
    for row in result["sharedStreamResults"]:
        if row["stream"] in index:
            raise AssertionError(f"Duplicate stream: {row['stream']}")
        index[row["stream"]] = row
    return index


class TestNClassNumericalParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.source_bytes)
        cls.optimizer = CBSOptimizer()

    def check_case(self, case: str, request: dict, expected_streams: int):
        original_input = deepcopy(request)
        # Use the old M2/M3 implementations directly, not the migrated
        # optimize() dispatcher (which now calls the generic solver).
        selected = request["optimizationTask"]["scope"]["trafficClasses"]
        if selected == ["M1", "M2"]:
            from algorithm.cbs_multiclass_pipeline import optimize_m1_m2_candidate
            old = optimize_m1_m2_candidate(
                self.optimizer, build_usecase_from_payload(deepcopy(request))
            )
        elif selected == ["M1", "M2", "M3"]:
            from algorithm.cbs_m3_candidate import optimize_m1_m2_m3_candidate
            old = optimize_m1_m2_m3_candidate(
                self.optimizer, build_usecase_from_payload(deepcopy(request))
            )
        else:
            raise AssertionError(f"No pre-migration reference for {selected}")
        new = optimize_nclass_candidate(
            self.optimizer, build_usecase_from_payload(deepcopy(request))
        )

        self.assertEqual(request, original_input, f"{case}: request mutated")
        self.assertEqual(
            old["allDeadlinesSatisfied"], new["allDeadlinesSatisfied"],
            f"{case}: deadline flag differs"
        )
        self.assertTrue(old["allDeadlinesSatisfied"], f"{case}: old solver not feasible")
        self.assertTrue(new["allDeadlinesSatisfied"], f"{case}: generic solver not feasible")

        old_groups, new_groups = group_index(old), group_index(new)
        self.assertEqual(set(old_groups), set(new_groups),
                         f"{case}: port-class coverage differs")
        old_streams, new_streams = stream_index(old), stream_index(new)
        self.assertEqual(set(old_streams), set(new_streams),
                         f"{case}: stream coverage differs")
        self.assertEqual(len(new_streams), expected_streams)

        # Tight implementation parity. A failure is evidence to examine,
        # not permission to change tolerances or baseline data.
        for key in sorted(old_groups):
            a, b = old_groups[key], new_groups[key]
            for field in GROUP_FIELDS:
                with self.subTest(case=case, group=key, field=field):
                    self.assertTrue(math.isclose(
                        a[field], b[field], rel_tol=1e-9, abs_tol=1e-4),
                        f"{case} {key} {field}: OLD={a[field]:.12g}, "
                        f"GENERIC={b[field]:.12g}, difference={b[field] - a[field]:.12g}"
                    )
            for field in CBS_FIELDS:
                with self.subTest(case=case, group=key, field=field):
                    x, y = a["sharedCBS"][field], b["sharedCBS"][field]
                    self.assertTrue(math.isclose(
                        x, y, rel_tol=1e-9, abs_tol=1e-5),
                        f"{case} {key} {field}: OLD={x:.12g}, "
                        f"GENERIC={y:.12g}, difference={y-x:.12g}"
                    )

        for stream_id in sorted(old_streams):
            a, b = old_streams[stream_id], new_streams[stream_id]
            with self.subTest(case=case, stream=stream_id):
                self.assertEqual(a["deadlineSatisfied"], b["deadlineSatisfied"])
                self.assertTrue(math.isclose(
                    a["sharedPathDelayNs"], b["sharedPathDelayNs"],
                    rel_tol=1e-9, abs_tol=1e-4
                ), f"{case} {stream_id} path delay differs: "
                   f"OLD={a['sharedPathDelayNs']}, GENERIC={b['sharedPathDelayNs']}")

    def test_zhao_tc1_two_class_parity(self):
        self.check_case(
            "Zhao TC1 M1+M2", make_request(self.original), 8
        )

    def test_synthetic_m3_100_byte_parity(self):
        self.check_case(
            "Synthetic M3 100B",
            make_request(self.original, with_m3=True, frame_size=100), 9
        )

    def test_synthetic_m3_300_byte_blocking_parity(self):
        self.check_case(
            "Synthetic M3 300B",
            make_request(self.original, with_m3=True, frame_size=300), 9
        )

    def test_original_fixture_and_discovery_unchanged(self):
        self.assertEqual(TC1.read_bytes(), self.source_bytes)
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)


if __name__ == "__main__":
    unittest.main()

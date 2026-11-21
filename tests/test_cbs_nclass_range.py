"""Step 19/20: generic CBS execution across supported SR-class counts.

Test-only file. Uses the existing Zhao TC1 fixture and SYNTHETIC additional
M3..M7 streams from test_cbs_nclass_candidate.synth_request.

These checks verify consistent output, numerical invariants, capacity and
fail-closed behavior for N=1..7. They DO NOT prove independent schedulability,
real PCP-to-queue feasibility, or live OaaS Manager/CNC interoperability.

Usage (file placed inside tests/):
  python -m unittest discover -s tests -p 'test_cbs_nclass_range.py' -v
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "optimizer-manager"))
sys.path.insert(0, str(ROOT / "tests"))

from optimizers.cbs import CBSOptimizer
from optimizers.cbs_priority_dependencies import priority_index
from test_cbs_nclass_candidate import synth_request
from usecase_adapter import build_usecase_from_payload

FIXTURE = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))


def close(a, b, *, rel_tol=1e-9, abs_tol=1e-5):
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


class TestCBSGenericNClassRange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_bytes = FIXTURE.read_bytes()
        cls.original = json.loads(cls.fixture_bytes)
        cls.optimizer = CBSOptimizer()

    def request_for_n(self, n):
        if n == 1:
            return deepcopy(self.original)  # M2 remains blocking background.
        return synth_request(self.original, classes=n)

    def run_case(self, request):
        pristine = deepcopy(request)
        result = self.optimizer.optimize(
            build_usecase_from_payload(deepcopy(request))
        )
        self.assertEqual(request, pristine, "The request must remain unchanged")
        return result

    def test_every_class_count_one_through_seven_has_complete_results(self):
        for n in range(1, 8):
            with self.subTest(classes=n):
                request = self.request_for_n(n)
                expected_classes = {f"M{i}" for i in range(1, n + 1)}
                expected_names = {
                    stream["definition"]["id"]
                    for stream in request["forwardingPlane"]["streams"]
                    if stream["definition"]["trafficClassId"] in expected_classes
                }
                result = self.run_case(request)
                groups = result["portClassGroups"]
                rows = result["sharedStreamResults"]
                self.assertEqual({g["srClass"] for g in groups}, expected_classes)
                self.assertEqual({r["stream"] for r in rows}, expected_names)
                self.assertEqual(len(rows), len(expected_names))
                self.assertEqual(len({(g["node"], g["port"], g["srClass"]) for g in groups}), len(groups))
                self.assertTrue(result["allDeadlinesSatisfied"])
                for row in rows:
                    self.assertTrue(row["deadlineSatisfied"], (n, row["stream"]))
                    self.assertLessEqual(row["sharedPathDelayNs"], row["deadlineNs"] + 1e-4)

    def test_credit_equations_and_total_port_capacity_for_each_n(self):
        for n in range(1, 8):
            with self.subTest(classes=n):
                result = self.run_case(self.request_for_n(n))
                per_port = defaultdict(list)
                for g in result["portClassGroups"]:
                    per_port[(g["node"], g["port"])].append(g)
                for port, port_groups in per_port.items():
                    ordered = sorted(port_groups, key=lambda g: priority_index(g["srClass"]))
                    speed = ordered[0]["linkSpeedBps"]
                    self.assertTrue(all(g["linkSpeedBps"] == speed for g in ordered))
                    used = 0.0
                    hp_min_total = 0.0
                    for g in ordered:
                        with self.subTest(classes=n, port=port, cls=g["srClass"]):
                            data = g["sharedCBS"]
                            idle = data["idleSlopeBps"]
                            send = data["sendSlopeBps"]
                            cmin = data["creditMinBits"]
                            cmax = data["creditMaxBits"]
                            own_frame = g["maxFrameBits"]
                            blocker = g["blockingFrameBits"]
                            self.assertTrue(all(math.isfinite(v) for v in
                                (idle, send, cmin, cmax, own_frame, blocker, speed)))
                            self.assertGreater(idle, 0)
                            self.assertGreaterEqual(cmax, 0)
                            self.assertLessEqual(cmin, 0)
                            self.assertLess(idle, speed - used)
                            self.assertTrue(close(send, idle - speed))
                            self.assertTrue(close(cmin, send * own_frame / speed))
                            # Zhao Eq.(4): service latency = (blocking frame -
                            # higher-class creditMin sum) / residual capacity.
                            expected_cmax = idle * (blocker - hp_min_total) / (speed - used)
                            self.assertTrue(close(cmax, expected_cmax),
                                (n, port, g["srClass"], cmax, expected_cmax))
                            self.assertGreaterEqual(idle + 1e-5, g["aggregateRhoBps"])
                            used += idle
                            hp_min_total += cmin
                    self.assertLess(used, speed, (n, port, used, speed))

    def test_invalid_eighth_class_rejected(self):
        req = self.request_for_n(7)
        req["optimizationTask"]["scope"]["trafficClasses"].append("M8")
        with self.assertRaises(ValueError):
            self.run_case(req)

    def test_skipped_active_higher_class_rejected(self):
        req = self.request_for_n(3)
        req["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M3"]
        with self.assertRaisesRegex(ValueError, "higher-priority"):
            self.run_case(req)

    def test_multi_frame_rejected(self):
        req = self.request_for_n(4)
        req["forwardingPlane"]["streams"][-1]["definition"]["trafficSpec"]["maxIntervalFrames"] = 2
        with self.assertRaisesRegex(ValueError, "one frame per interval"):
            self.run_case(req)

    def test_reconfiguration_accepted_with_complete_results(self):
        req = self.request_for_n(4)
        req["operation"] = "reconfiguration"
        result = self.run_case(req)

        self.assertEqual(
            {g["srClass"] for g in result["portClassGroups"]},
            {"M1", "M2", "M3", "M4"},
        )
        self.assertTrue(result["allDeadlinesSatisfied"])

    def test_infeasible_extremely_short_deadline_rejected(self):
        req = self.request_for_n(2)
        for stream in req["forwardingPlane"]["streams"]:
            stream["definition"]["userToNetworkRequirements"]["maxLatencyNs"] = 1
        with self.assertRaises(ValueError):
            self.run_case(req)

    def test_original_fixture_and_manager_claim_unchanged(self):
        self.assertEqual(FIXTURE.read_bytes(), self.fixture_bytes)
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)
        self.assertEqual(caps["tasks"], ["configuration", "reconfiguration"])
        self.assertIs(caps["supportsReconfiguration"], True)


if __name__ == "__main__":
    unittest.main()

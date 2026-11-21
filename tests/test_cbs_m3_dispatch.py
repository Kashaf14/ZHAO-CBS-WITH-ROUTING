"""Step 12: constrained M3 dispatch regression, NOT Manager/CNC validation.

Uses the existing synthetic M3 f9 built from Zhao TC1 (which has only M1/M2).
No edits to the original request, queue mapping, contract or Manager capabilities.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "optimizer-manager"))
sys.path.insert(0, str(ROOT / "tests"))

from optimizers.cbs import CBSOptimizer
from optimizers.cbs_m3_candidate import optimize_m1_m2_m3_candidate
from usecase_adapter import build_usecase_from_payload
from test_cbs_m3_candidate import m3_request_from_tc1
from test_cbs_m3_response_boundary import with_test_only_mappings
from cbs_response_adapter import build_cbs_response_v13
from tsn_contract_validator.response_validator import validate_optimizer_response

TC1 = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))


def index(result):
    groups = {(g["srClass"], g["node"], g["port"]): g for g in result["portClassGroups"]}
    assert len(groups) == len(result["portClassGroups"])
    return groups


class TestCBSM3Dispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tc1_bytes = TC1.read_bytes()
        cls.tc1 = json.loads(cls.tc1_bytes)
        cls.request = m3_request_from_tc1(cls.tc1)
        cls.optimizer = CBSOptimizer()

    def test_dispatch_invokes_generic_nclass_solver(self):
        # After migration, optimize() must call the same N-class path for M3.
        import optimizers.cbs_nclass_candidate as candidate
        with patch.object(candidate, "optimize_nclass_candidate",
                          wraps=candidate.optimize_nclass_candidate) as call:
            result = self.optimizer.optimize(build_usecase_from_payload(deepcopy(self.request)))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(result["sharedStreamResults"]), 9)
        self.assertTrue(result["allDeadlinesSatisfied"])
        self.assertEqual({g["srClass"] for g in result["portClassGroups"]}, {"M1", "M2", "M3"})

    def test_dispatch_matches_direct_result_for_synthetic_case(self):
        u1 = build_usecase_from_payload(deepcopy(self.request))
        u2 = build_usecase_from_payload(deepcopy(self.request))
        direct = optimize_m1_m2_m3_candidate(self.optimizer, u1)
        dispatched = self.optimizer.optimize(u2)
        self.assertEqual(set(index(direct)), set(index(dispatched)))
        for key, a in index(direct).items():
            b = index(dispatched)[key]
            for field in ("idleSlopeBps", "sendSlopeBps", "creditMinBits", "creditMaxBits"):
                self.assertAlmostEqual(a["sharedCBS"][field], b["sharedCBS"][field], places=6, msg=str(key))
            self.assertAlmostEqual(a["sharedDelayNs"], b["sharedDelayNs"], places=4)

    def test_larger_m3_frame_recomputes_higher_blocking_through_dispatch(self):
        req = deepcopy(self.request)
        new = next(s for s in req["forwardingPlane"]["streams"] if s["definition"]["id"] == "f9")
        new["definition"]["trafficSpec"]["maxFrameSize"] = 300
        result = self.optimizer.optimize(build_usecase_from_payload(req))
        self.assertTrue(result["allDeadlinesSatisfied"])
        for cls in ("M1", "M2"):
            for node, port in (("ES3", "Port1"), ("SW1", "P5")):
                self.assertEqual(index(result)[(cls, node, port)]["blockingFrameBits"], 2400)

    def test_dispatched_result_passes_response_contract_with_synthetic_mapping(self):
        # Simulated physical queues and fake matching job ids only: not Manager validation.
        req = with_test_only_mappings(self.request, m3=True)
        saved = deepcopy(req["forwardingPlane"])
        result = self.optimizer.optimize(build_usecase_from_payload(req))
        response = build_cbs_response_v13(req, result)
        report = validate_optimizer_response(
            response, request=req, baseline=saved,
            expected_job_id="offline-step12", result_job_id="offline-step12",
        )
        self.assertTrue(report.applicable)
        self.assertTrue(report.not_verified)
        self.assertEqual(req["forwardingPlane"], saved)

    def test_invalid_multiframe_fails_closed(self):
        multi = deepcopy(self.request)
        f9 = next(s for s in multi["forwardingPlane"]["streams"] if s["definition"]["id"] == "f9")
        f9["definition"]["trafficSpec"]["maxIntervalFrames"] = 2
        with self.assertRaisesRegex(ValueError, "one frame per interval"):
            self.optimizer.optimize(build_usecase_from_payload(multi))


    def test_reconfiguration_numerical_dispatch(self):
        """Active N-class solver computes; CNC reconfiguration is a separate check."""
        baseline = self.optimizer.optimize(build_usecase_from_payload(deepcopy(self.request)))
        reconfig = deepcopy(self.request)
        reconfig["operation"] = "reconfiguration"
        reconfig["optimizationTask"]["changePolicy"] = {"allowQueueChanges": True}
        result = self.optimizer.optimize(build_usecase_from_payload(reconfig))
        actual, expected = index(result), index(baseline)
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(len(result["sharedStreamResults"]), len(baseline["sharedStreamResults"]))
        for key in expected:
            self.assertAlmostEqual(
                actual[key]["sharedCBS"]["idleSlopeBps"],
                expected[key]["sharedCBS"]["idleSlopeBps"], places=6,
            )

    def test_capabilities_and_original_fixture_unchanged(self):
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)
        self.assertEqual(TC1.read_bytes(), self.tc1_bytes)
        self.assertEqual(self.tc1["optimizationTask"]["scope"]["trafficClasses"], ["M1"])
        self.assertEqual(len(self.tc1["forwardingPlane"]["streams"]), 8)


if __name__ == "__main__":
    unittest.main()

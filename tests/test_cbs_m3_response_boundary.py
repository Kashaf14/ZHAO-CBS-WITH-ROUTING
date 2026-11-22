"""Step 11: Offline, synthetic M1/M2/M3 response-contract boundary regression.

Requires Step 10 M3 candidate. All PCP-to-queue mappings introduced here are
TEST-ONLY assumptions, not real OpenCNC/device configurations or team approval.
The original Zhao TC1 fixture remains unchanged on disk. No Manager job runs.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cbs_response_adapter import CBSResponseConversionError, build_cbs_response_v13
from algorithm.cbs import CBSOptimizer
from algorithm.cbs_m3_candidate import optimize_m1_m2_m3_candidate
from tsn_contract_validator.response_validator import validate_optimizer_response
from usecase_adapter import build_usecase_from_payload
from test_cbs_m3_candidate import m3_request_from_tc1

TC1 = ROOT / "tests" / "fixtures" / "cbs-paper-tc1-step17-original.json"
M3_PORTS = {("ES3", "Port1"), ("SW1", "P5")}
TC1_PORTS = {
    ("ES1", "Port1"), ("ES2", "Port1"), ("ES3", "Port1"),
    ("SW1", "P4"), ("SW1", "P5"),
}


def with_test_only_mappings(request, *, m3: bool):
    """Clone and add explicitly synthetic PCP-to-physical-queue assignments."""
    result = deepcopy(request)
    encountered = set()
    for node in result["forwardingPlane"]["configuration"]["nodeConfigs"]:
        for port in node["portConfigs"]:
            key = (node["nodeId"], port["portId"])
            if key not in TC1_PORTS:
                raise AssertionError(f"Unexpected original TC1 config port: {key}")
            encountered.add(key)
            if port.get("pcpMappingEnabled") is not True:
                raise AssertionError(f"PCP mapping not enabled: {key}")
            table = port["pcpConfig"]["trafficClassTable"]
            if table != [{"priority": 5, "egressQueueId": 5}]:
                raise AssertionError(f"Expected original PCP 5 mapping at {key}: {table!r}")
            table.append({"priority": 4, "egressQueueId": 4})
            if m3 and key in M3_PORTS:
                table.append({"priority": 3, "egressQueueId": 3})
    if encountered != TC1_PORTS:
        raise AssertionError(f"Incomplete TC1 configured port coverage: {encountered}")
    return result


class TestSyntheticM3ResponseBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.original_bytes)
        cls.m3_request = m3_request_from_tc1(cls.original)
        cls.optimizer = CBSOptimizer()
        cls.legacy = optimize_m1_m2_m3_candidate(
            cls.optimizer, build_usecase_from_payload(cls.m3_request)
        )

    def test_without_m3_physical_queue_mapping_response_is_rejected(self):
        missing_m3 = with_test_only_mappings(self.m3_request, m3=False)
        with self.assertRaisesRegex(CBSResponseConversionError, "PCP 3"):
            build_cbs_response_v13(missing_m3, self.legacy)

    def test_synthetic_m3_queue_mapping_produces_distinct_cbs_updates(self):
        request = with_test_only_mappings(self.m3_request, m3=True)
        before = deepcopy(request)
        response = build_cbs_response_v13(request, self.legacy)
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["schemaVersion"], "1.3")
        solution = response["solution"]
        self.assertEqual(solution["type"], "configuration")
        self.assertEqual(solution["representation"], "partial")
        self.assertEqual(set(solution["changes"]), {"configuration"})

        by_port = {}
        for node in solution["changes"]["configuration"]["nodeConfigs"]:
            for port in node["portConfigs"]:
                key = (node["nodeId"], port["portId"])
                self.assertNotIn(key, by_port)
                by_port[key] = {}
                for queue in port["queueConfigs"]:
                    qid = queue["queueId"]
                    self.assertNotIn(qid, by_port[key])
                    by_port[key][qid] = queue
                    self.assertEqual(queue["scheduling"], "CREDIT_BASED_SHAPER")
                    self.assertEqual(len(queue["cbs"]), 1)
                    self.assertEqual(queue["cbs"][0]["egressQueueId"], qid)
                    self.assertGreater(queue["cbs"][0]["adminIdleSlopeBps"], 0)
        self.assertEqual(set(by_port), TC1_PORTS)
        for key in TC1_PORTS:
            self.assertEqual(set(by_port[key]), {4, 5, 3} if key in M3_PORTS else {4, 5})
        self.assertEqual(request, before, "Response conversion mutated its input")

    def test_shared_v13_semantics_with_synthetic_m3_mapping(self):
        request = with_test_only_mappings(self.m3_request, m3=True)
        preserved_baseline = deepcopy(request["forwardingPlane"])
        response = build_cbs_response_v13(request, self.legacy)
        # These equal IDs are fabricated ONLY to exercise the boundary validator.
        # They do not constitute actual Manager job-correlation verification.
        report = validate_optimizer_response(
            response,
            request=request,
            baseline=preserved_baseline,
            expected_job_id="synthetic-test-no-real-manager",
            result_job_id="synthetic-test-no-real-manager",
        )
        self.assertTrue(report.applicable)
        self.assertEqual(request["forwardingPlane"], preserved_baseline)
        # Shared validation reports unresolved real-world checks separately.
        self.assertTrue(report.not_verified)

    def test_explicit_cbs_unsupported_device_is_rejected(self):
        request = with_test_only_mappings(self.m3_request, m3=True)
        device = next(d for d in request["capabilities"]["devices"] if d["nodeId"] == "ES3")
        device["mechanisms"]["CBS"]["supported"] = False
        with self.assertRaisesRegex(CBSResponseConversionError, "unsupported"):
            build_cbs_response_v13(request, self.legacy)

    def test_original_tc1_request_and_capabilities(self):
        self.assertEqual(TC1.read_bytes(), self.original_bytes)
        self.assertEqual(self.original["optimizationTask"]["scope"]["trafficClasses"], ["M1"])
        self.assertEqual(len(self.original["forwardingPlane"]["streams"]), 8)
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)
        for nc in self.original["forwardingPlane"]["configuration"]["nodeConfigs"]:
            for pc in nc["portConfigs"]:
                self.assertEqual(pc["pcpConfig"]["trafficClassTable"],
                                 [{"priority": 5, "egressQueueId": 5}])


if __name__ == "__main__":
    unittest.main()

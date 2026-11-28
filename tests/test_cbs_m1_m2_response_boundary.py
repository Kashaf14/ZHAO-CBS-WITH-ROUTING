"""Offline CBS M1+M2 response-boundary test: synthetic PCP mapping, NO hardware claim.

Preconditions:
  - Step 4 M1+M2 candidate dispatch already applied locally.
  - Shared contracts v1.0.0 installed for offline schema tests.
  - The frozen local TC1 Step 17 fixture in tests/fixtures/.
  - Hamza's existing Usecase package importable as in previous tests.

This test uses a SYNTHETIC, TEST-ONLY PCP 4 -> queue 4 mapping.
It is not an OpenCNC device mapping, deployment instruction or team-approved
configuration. The production request fixture is NEVER edited.
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

from algorithm.cbs import CBSOptimizer
from usecase_adapter import build_usecase_from_payload
from cbs_response_adapter import build_cbs_response_v13 as _map_response
from algorithm.cbs_configuration import prepare_cbs_configuration
from tsn_contract_validator.response import validate_common_response

TC1 = ROOT / "tests" / "fixtures" / "cbs-paper-tc1-step17-original.json"
EXPECTED_PORTS = {
    ("ES1", "Port1"), ("ES2", "Port1"), ("ES3", "Port1"),
    ("SW1", "P4"), ("SW1", "P5"),
}


def make_test_only_request(original: dict) -> dict:
    """Never mutate the original contract fixture; add an explicit *synthetic* mapping."""
    request = deepcopy(original)
    assert request["schemaVersion"] == "1.4"
    assert request["operation"] == "configuration"
    assert request["optimizationTask"]["scope"]["trafficClasses"] == ["M1"]
    assert {s["definition"]["trafficClassId"] for s in
            request["forwardingPlane"]["streams"]} == {"M1", "M2"}
    assert {s["configuration"]["pcp"] for s in
            request["forwardingPlane"]["streams"]} == {4, 5}

    request["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2"]
    seen = set()
    for nc in request["forwardingPlane"]["configuration"]["nodeConfigs"]:
        for pc in nc["portConfigs"]:
            key = (nc["nodeId"], pc["portId"])
            assert key in EXPECTED_PORTS, f"Unexpected configured port: {key}"
            seen.add(key)
            assert pc["pcpMappingEnabled"] is True
            table = pc["pcpConfig"]["trafficClassTable"]
            assert table == [{"priority": 5, "egressQueueId": 5}], (
                f"Original PCP map changed at {key}: {table}"
            )
            # Synthetic unit-test mapping only; do NOT copy into real CNC config.
            table.append({"priority": 4, "egressQueueId": 4})
    assert seen == EXPECTED_PORTS, f"Missing/extra configured ports: {seen}"
    return request


class TestCBSM1M2ResponseBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TC1.is_file():
            raise FileNotFoundError(f"Missing TC1 fixture: {TC1}; restore the local frozen fixture")
        cls.original_bytes = TC1.read_bytes()
        cls.original = json.loads(cls.original_bytes)
        cls.request = make_test_only_request(cls.original)
        cls.unchanged_request = deepcopy(cls.request)
        cls.preserved_baseline = deepcopy(cls.request["forwardingPlane"])
        cls.legacy = CBSOptimizer().optimize(build_usecase_from_payload(cls.request))
        cls.response = build_cbs_response_v13(cls.request, cls.legacy)

    def test_sparse_queue_changes_are_explicit_and_noncolliding(self):
        self.assertEqual(self.response["schemaVersion"], "1.3")
        self.assertEqual(self.response["status"], "success")
        solution = self.response["solution"]
        self.assertEqual(solution["type"], "configuration")
        self.assertEqual(solution["representation"], "partial")
        self.assertEqual(set(solution["changes"]), {"configuration"})
        config = solution["changes"]["configuration"]

        seen = {}
        for node in config["nodeConfigs"]:
            for port in node["portConfigs"]:
                key = (node["nodeId"], port["portId"])
                self.assertNotIn(key, seen)
                seen[key] = {}
                for queue in port["queueConfigs"]:
                    queue_id = queue["queueId"]
                    self.assertNotIn(queue_id, seen[key])
                    seen[key][queue_id] = queue
                    self.assertEqual(queue["scheduling"], "CREDIT_BASED_SHAPER")
                    self.assertEqual(len(queue["cbs"]), 1)
                    rec = queue["cbs"][0]
                    self.assertEqual(rec["egressQueueId"], queue_id)
                    self.assertEqual(rec["adminState"], "ENABLED")
                    self.assertGreater(rec["adminIdleSlopeBps"], 0)
                    self.assertGreaterEqual(rec["hiCreditBytes"], 0)
                    self.assertGreaterEqual(rec["loCreditBytes"], 0)
                    self.assertGreaterEqual(rec["sendSlopeBps"], 0)
        self.assertEqual(set(seen), EXPECTED_PORTS)
        for port, queues in seen.items():
            self.assertEqual(set(queues), {4, 5}, port)

    def test_shared_response_schema_compatibility(self):
        validate_common_response(self.response)

    def test_original_request_and_input_are_not_modified(self):
        self.assertEqual(TC1.read_bytes(), self.original_bytes)
        self.assertEqual(self.request, self.unchanged_request)
        self.assertEqual(self.request["forwardingPlane"], self.preserved_baseline)
        for nc in self.original["forwardingPlane"]["configuration"]["nodeConfigs"]:
            for pc in nc["portConfigs"]:
                self.assertNotIn(4, [
                    item["priority"] for item in pc["pcpConfig"]["trafficClassTable"]
                ])



def build_cbs_response_v13(request, legacy):
    """Exercise the explicit configuration-to-response boundary."""
    return _map_response(request, legacy, prepared=prepare_cbs_configuration(request, legacy))


if __name__ == "__main__":
    unittest.main()

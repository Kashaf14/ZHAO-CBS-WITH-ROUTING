"""Framework-side generated CBS route serialization regression coverage."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cbs_route_response import generated_route_changes
from cbs_response_adapter import build_cbs_response_v13


HOPS = [{"hopNumber": 0, "bridgeId": "SW1", "ingressPortId": "P1", "egressPortId": "P2"}]


def request(*, policy="optimize", path=None, change=None):
    model = {
        "definition": {
            "id": "logical-f1", "streamId": {"macAddress": "02:00:00:00:00:01", "uniqueId": 8},
            "talkerNodeId": "ES1", "listenerNodeIds": ["ES2"], "trafficClassId": "critical",
        },
        "configuration": {"pcp": 5},
        "decisionVariables": {"routing": policy},
    }
    if path is not None:
        model["configuration"]["path"] = deepcopy(path)
    if change is not None:
        model["change"] = change
    return {"schemaVersion": "1.4", "mechanisms": [{"name": "CBS"}],
            "forwardingPlane": {"streams": [model], "metadata": {}}}


def fake_numerical():
    return {"optimizer": "CBS", "allDeadlinesSatisfied": True,
            "sharedStreamResults": [{"stream": "logical-f1", "deadlineSatisfied": True,
                                      "sharedPathDelayNs": 150.0, "deadlineNs": 200}],
            "kpis": {}, "portClassGroups": []}


class TestCBSRouteResponse(unittest.TestCase):
    def test_return_composite_identifier_and_no_request_mutation(self):
        payload = request()
        before = deepcopy(payload)
        changes = generated_route_changes(payload, {"logical-f1": HOPS})
        self.assertEqual(changes, [{
            "streamId": {"macAddress": "02:00:00:00:00:01", "uniqueId": 8},
            "configuration": {"path": HOPS},
        }])
        changes[0]["configuration"]["path"][0]["egressPortId"] = "changed"
        self.assertEqual(payload, before)
        self.assertEqual(HOPS[0]["egressPortId"], "P2")

    def test_response_contains_sparse_path_delta_and_application_unverified_warning(self):
        payload = request()
        numerical = fake_numerical()
        with patch("cbs_response_adapter._partial_changes_from_result", return_value={}) as calc:
            result = build_cbs_response_v13(payload, numerical,
                                            generated_routes={"logical-f1": HOPS})
        calc.assert_called_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["schemaVersion"], "1.3")
        self.assertEqual(result["solution"]["changes"]["streams"][0]["configuration"]["path"], HOPS)
        self.assertEqual(result["solution"]["representation"], "partial")
        self.assertTrue(result["outcome"]["configurationChanged"])
        self.assertTrue(any(
            w["code"] == "CNC_PATH_APPLICATION_NOT_VERIFIED"
            and "not verified by the CBS service" in w["message"]
            for w in result["warnings"]
        ))
        self.assertFalse(any(w["code"] == "CNC_PATH_APPLICATION_PENDING"
                             for w in result["warnings"]))

    def test_supplied_fixed_route_generates_no_path_change(self):
        payload = request(policy="fixed", path=HOPS)
        self.assertEqual(generated_route_changes(payload, {}), [])

    def test_missing_route_cannot_be_omitted(self):
        with self.assertRaisesRegex(ValueError, "Missing generated"):
            generated_route_changes(request(), {})

    def test_add_stream_is_not_claimed_as_existing_stream_update(self):
        with self.assertRaisesRegex(ValueError, "Stream addition"):
            generated_route_changes(request(change="add"), {"logical-f1": HOPS})

    def test_wrong_routing_policy_rejects_generated_route(self):
        with self.assertRaisesRegex(ValueError, "Unexpected generated"):
            generated_route_changes(request(policy="preserve", path=HOPS), {"logical-f1": HOPS})

    def test_extra_route_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown/removed"):
            generated_route_changes(request(policy="fixed", path=HOPS), {"other-stream": HOPS})

if __name__ == "__main__":
    unittest.main()

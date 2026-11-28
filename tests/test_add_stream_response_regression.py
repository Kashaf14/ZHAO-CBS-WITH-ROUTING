"""PROPOSED red tests for CBS ADD_STREAM serialization.

This describes desired behavior, not implemented functionality or an approved
shared-contract amendment. The tests should FAIL against the current serializer:
`generated_route_changes` rejects change='add'.

Keep these tests in the framework's tests/ directory alongside tests/fixtures/.
Do not interpret a successful serializer test as validated CNC application.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cbs_response_adapter import build_cbs_response_v13

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "add_stream_captured_request.json"


def generated_routes_for_fixture(request):
    # Proposed single-bridge test routes derived from the directed topology.
    # NOT actual optimizer measurements or proof of live feasibility.
    first, second = request["forwardingPlane"]["streams"]
    return {
        first["definition"]["id"]: [{"hopNumber": 0, "bridgeId": "RELY-TSN4",
                                      "ingressPortId": "P1", "egressPortId": "P2"}],
        second["definition"]["id"]: [{"hopNumber": 0, "bridgeId": "RELY-TSN4",
                                       "ingressPortId": "P2", "egressPortId": "P1"}],
    }


def fake_numerical(request):
    return {
        "optimizer": "CBS", "allDeadlinesSatisfied": True,
        "sharedStreamResults": [
            {"stream": s["definition"]["id"], "deadlineSatisfied": True,
             "sharedPathDelayNs": 150.0, "deadlineNs": 200}
            for s in request["forwardingPlane"]["streams"]
        ],
        "kpis": {}, "portClassGroups": [],
    }


class TestProposedAddStreamSerialization(unittest.TestCase):
    def setUp(self):
        self.request = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.original = deepcopy(self.request)
        self.routes = generated_routes_for_fixture(self.request)
        self.assertEqual(self.request["optimizationTask"]["intent"], "ADD_STREAM")
        self.assertEqual([s.get("change") for s in self.request["forwardingPlane"]["streams"]],
                         ["add", "add"])

    def test_returns_both_full_additions_without_existing_stream_updates(self):
        with patch("cbs_response_adapter._partial_changes_from_result", return_value={}):
            response = build_cbs_response_v13(
                self.request, fake_numerical(self.request), prepared=[], generated_routes=self.routes
            )

        changes = response["solution"]["changes"]
        self.assertNotIn("streams", changes)
        self.assertEqual(len(changes["streamAdditions"]), 2)
        for added, requested in zip(changes["streamAdditions"],
                                    self.request["forwardingPlane"]["streams"]):
            name = requested["definition"]["id"]
            self.assertEqual(set(added), {"definition", "configuration"})
            self.assertEqual(added["definition"], requested["definition"])
            self.assertEqual(added["configuration"], {
                **requested["configuration"], "path": self.routes[name]
            })
        self.assertEqual(self.request, self.original)

    def test_cbs_queue_changes_are_not_lost_when_routes_are_added(self):
        # Placeholder configuration to test preservation, NOT measured CBS results.
        cbs_sentinel = {"configuration": {"nodeConfigs": [{"nodeId": "RELY-TSN4",
                         "portConfigs": [{"portId": "P1", "queueConfigs": [
                             {"queueId": 7, "scheduling": "CREDIT_BASED_SHAPER",
                              "cbs": [{"egressQueueId": 7, "adminIdleSlopeBps": 12000000}]}
                         ]}]}]}}
        with patch("cbs_response_adapter._partial_changes_from_result",
                   return_value=deepcopy(cbs_sentinel)):
            response = build_cbs_response_v13(
                self.request, fake_numerical(self.request), prepared=[], generated_routes=self.routes
            )
        changes = response["solution"]["changes"]
        self.assertEqual(changes["configuration"], cbs_sentinel["configuration"])
        self.assertEqual(len(changes["streamAdditions"]), 2)
        self.assertNotIn("streams", changes)
        self.assertEqual(self.request, self.original)


if __name__ == "__main__":
    unittest.main()

"""Proposed CBS Add Stream serialization tests. Not shared/CNC validation."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "optimizer-manager"))
from cbs_route_response import generated_route_change_sets

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "add_stream_captured_request.json"


def example_routes(request):
    # Synthetic directed single-bridge routes; not solver or hardware evidence.
    s1, s2 = request["forwardingPlane"]["streams"]
    return {
        s1["definition"]["id"]: [
            {"hopNumber": 0, "bridgeId": "RELY-TSN4", "ingressPortId": "P1", "egressPortId": "P2"}
        ],
        s2["definition"]["id"]: [
            {"hopNumber": 0, "bridgeId": "RELY-TSN4", "ingressPortId": "P2", "egressPortId": "P1"}
        ],
    }


class TestCBSAddStreamChangeSets(unittest.TestCase):
    def setUp(self):
        self.request = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.routes = example_routes(self.request)
        self.before = deepcopy(self.request)

    def test_two_full_stream_additions_with_paths(self):
        changes = generated_route_change_sets(self.request, self.routes)
        self.assertNotIn("streams", changes)
        self.assertEqual(len(changes["streamAdditions"]), 2)
        for candidate, original in zip(changes["streamAdditions"], self.request["forwardingPlane"]["streams"]):
            self.assertEqual(set(candidate), {"definition", "configuration"})
            self.assertEqual(candidate["definition"], original["definition"])
            self.assertEqual(candidate["configuration"], {
                **original["configuration"],
                "path": self.routes[original["definition"]["id"]],
            })
        self.assertEqual(self.request, self.before)

    def test_missing_route_fails_closed(self):
        partial = deepcopy(self.routes)
        partial.pop(next(iter(partial)))
        with self.assertRaisesRegex(ValueError, "Missing generated CBS route"):
            generated_route_change_sets(self.request, partial)

    def test_unexpected_route_fails_closed(self):
        routes = deepcopy(self.routes)
        routes["unknown"] = [{"hopNumber": 0, "bridgeId": "SW", "ingressPortId": "A", "egressPortId": "B"}]
        with self.assertRaisesRegex(ValueError, "Unknown/removed"):
            generated_route_change_sets(self.request, routes)

    def test_not_add_stream_rejected(self):
        altered = deepcopy(self.request)
        altered["optimizationTask"]["intent"] = "CONFIGURE"
        with self.assertRaisesRegex(ValueError, "ADD_STREAM intent"):
            generated_route_change_sets(altered, self.routes)

    def test_outside_scope_rejected(self):
        altered = deepcopy(self.request)
        altered["optimizationTask"]["scope"]["streamIds"] = ["not-in-request"]
        with self.assertRaisesRegex(ValueError, "ADD_STREAM scope must match"):
            generated_route_change_sets(altered, self.routes)

    def test_existing_stream_is_not_returned_as_addition(self):
        # Present an existing stream that needs a generated route (no change=add).
        altered = deepcopy(self.request)
        altered["forwardingPlane"]["streams"][0]["change"] = "none"
        altered["optimizationTask"]["scope"]["streamIds"] = [
            altered["forwardingPlane"]["streams"][1]["definition"]["id"]
        ]
        changes = generated_route_change_sets(altered, self.routes)
        self.assertEqual(len(changes["streams"]), 1)
        self.assertEqual(len(changes["streamAdditions"]), 1)

    def test_duplicate_composite_stream_id_rejected(self):
        altered = deepcopy(self.request)
        altered["forwardingPlane"]["streams"][1]["definition"]["streamId"] = deepcopy(
            altered["forwardingPlane"]["streams"][0]["definition"]["streamId"]
        )
        with self.assertRaisesRegex(ValueError, "Duplicate CBS composite streamId"):
            generated_route_change_sets(altered, self.routes)

    def test_no_aliasing_between_output_and_input(self):
        changes = generated_route_change_sets(self.request, self.routes)
        changes["streamAdditions"][0]["configuration"]["path"][0]["egressPortId"] = "CHANGED"
        self.assertEqual(self.request, self.before)
        self.assertEqual(self.routes[next(iter(self.routes))][0]["egressPortId"], "P2")


if __name__ == "__main__":
    unittest.main()

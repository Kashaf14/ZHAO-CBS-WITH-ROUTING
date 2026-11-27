"""Check that the expanded TC1 fixture differs only in its explicit PCP 4 mappings and queue priorities.

Historical numerical comparisons must use the original Step 17 input.
This test does not establish hardware correctness or live CNC integration.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class TestTC1FixtureVariants(unittest.TestCase):
    def test_expanded_mapping_and_queue_priorities_are_only_differences(self):
        original = json.loads(
            (FIXTURES / "cbs-paper-tc1-step17-original.json").read_text(encoding="utf-8")
        )
        extended = json.loads(
            (FIXTURES / "cbs-paper-tc1-full-8flow-contract.json").read_text(encoding="utf-8")
        )
        expected = deepcopy(original)
        configured_ports = 0
        for node in expected["forwardingPlane"]["configuration"]["nodeConfigs"]:
            for port in node["portConfigs"]:
                table = port["pcpConfig"]["trafficClassTable"]
                self.assertEqual(table, [{"priority": 5, "egressQueueId": 5}])
                table.append({"priority": 4, "egressQueueId": 4})
                # Same experimental eight-queue ordering as the CNC request adapter.
                self.assertNotIn("queuePriorityTable", port)
                port["queuePriorityTable"] = [
                    {"queueId": queue, "schedulingPriority": 8 - queue}
                    for queue in range(8)
                ]
                configured_ports += 1
        self.assertEqual(configured_ports, 5)
        self.assertEqual(extended, expected)


if __name__ == "__main__":
    unittest.main()

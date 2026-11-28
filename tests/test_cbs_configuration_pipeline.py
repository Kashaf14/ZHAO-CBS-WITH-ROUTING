"""Exercise the configuration stage and response adapter as separate boundaries."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from algorithm.cbs_configuration import (
    CBSResponseConversionError, prepare_cbs_configuration,
)
from cbs_response_adapter import build_cbs_response_v13


def fixture():
    request = {
        "schemaVersion": "1.4",
        "mechanisms": [{"name": "CBS"}],
        "forwardingPlane": {
            "topology": {"nodes": [{"name": "SW1", "ports": [
                {"id": "P5", "number_of_queues": 8, "capabilities": {"port_speed": 100}}
            ]}]},
            "streams": [
                {"definition": {"id": "f1", "trafficClassId": "critical"},
                 "configuration": {"pcp": 5}},
                {"definition": {"id": "f2", "trafficClassId": "regular"},
                 "configuration": {"pcp": 4}},
            ],
            "configuration": {"nodeConfigs": [{"nodeId": "SW1", "portConfigs": [
                {"portId": "P5", "pcpMappingEnabled": True, "pcpConfig": {
                    "trafficClassTable": [{"priority": 5, "egressQueueId": 5},
                                          {"priority": 4, "egressQueueId": 4}]
                }}
            ]}]},
        },
    }
    groups = []
    for rank, cls, stream, slope in [
        (1, "critical", "f1", 9892316.116079576),
        (2, "regular", "f2", 10579001.023720473),
    ]:
        groups.append({
            "node": "SW1", "port": "P5", "srClass": cls, "streams": [stream],
            "zhaoPriorityRank": rank, "maxFrameBits": 4336, "blockingFrameBits": 12336,
            "sharedCBS": {"idleSlopeBps": slope, "sendSlopeBps": slope - 100_000_000,
                          "creditMinBits": -100, "creditMaxBits": 100},
        })
    legacy = {"optimizer": "CBS", "portClassGroups": groups,
              "sharedStreamResults": [
                  {"stream": stream, "deadlineSatisfied": True,
                   "sharedPathDelayNs": 1000, "deadlineNs": 1000}
                  for stream in ("f1", "f2")
              ], "allDeadlinesSatisfied": True}
    return request, legacy


class TestConfigurationPipeline(unittest.TestCase):
    def test_prepare_then_map_without_recalculation(self):
        request, legacy = fixture()
        before = deepcopy((request, legacy))
        prepared = prepare_cbs_configuration(request, legacy)
        self.assertEqual([q.hi_credit_bytes for q in prepared], [153, 239])
        # Once prepared, mapping must not invoke either mathematical routine.
        with patch("algorithm.cbs_configuration.admin_idle_slope", side_effect=AssertionError("recalculated slope")), \
             patch("algorithm.cbs_configuration.configured_registers", side_effect=AssertionError("recalculated credit")):
            response = build_cbs_response_v13(request, legacy, prepared=prepared)
        queues = response["solution"]["changes"]["configuration"]["nodeConfigs"][0]["portConfigs"][0]["queueConfigs"]
        self.assertEqual([q["cbs"][0]["hiCreditBytes"] for q in queues], [153, 239])
        self.assertEqual(response["outcome"]["details"]["cbsDiagnostics"], legacy)
        self.assertEqual((request, legacy), before)

    def test_reject_missing_prepared_configuration(self):
        request, legacy = fixture()
        with self.assertRaisesRegex(CBSResponseConversionError, "Missing or mismatched"):
            build_cbs_response_v13(request, legacy, prepared=[])

    def test_no_solution_does_not_require_configuration(self):
        request, legacy = fixture()
        legacy["allDeadlinesSatisfied"] = False
        prepared = prepare_cbs_configuration(request, legacy)
        self.assertEqual(prepared, [])
        response = build_cbs_response_v13(request, legacy, prepared=prepared)
        self.assertEqual(response["status"], "noSolutionFound")
        self.assertIsNone(response["solution"])


if __name__ == "__main__":
    unittest.main()

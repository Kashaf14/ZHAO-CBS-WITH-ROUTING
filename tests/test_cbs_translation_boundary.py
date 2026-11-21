"""Isolated integration-boundary checks; no live Manager/HTTP claims."""
import copy
import importlib.util
from unittest.mock import patch
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Supply the same egress helper interface used by the real N-class solver.
# This isolates the adapter wiring without substituting a numerical solve.
package = types.ModuleType("optimizers")
package.__path__ = []
solver = types.ModuleType("optimizers.cbs_nclass_candidate")

def resolve_egress(optimizer, stream, usecase):
    return {"egressPorts": [
        {"node": node, "port": port}
        for node, port in usecase.routes[stream["name"]]
    ]}

solver._cbs_egress_context = resolve_egress
# Import the adapter under a scoped stub without polluting other test modules.
with patch.dict(sys.modules, {
    "optimizers": package,
    "optimizers.cbs_nclass_candidate": solver,
}):
    source = ROOT / "optimizer-manager" / "cbs_translation_boundary.py"
    spec = importlib.util.spec_from_file_location("cbs_translation_boundary_isolated", source)
    boundary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boundary)
CBSClassTranslationError = boundary.CBSClassTranslationError
prepare_cbs_solver_usecase = boundary.prepare_cbs_solver_usecase
restore_client_classes_in_cbs_result = boundary.restore_client_classes_in_cbs_result


def request():
    return {
        "schemaVersion": "1.4", "mechanisms": [{"name": "CBS"}],
        "optimizationTask": {"scope": {"trafficClasses": ["critical", "standard"]}},
        "forwardingPlane": {
            "streams": [
                {"definition": {"id": "s1", "trafficClassId": "critical"}, "configuration": {"pcp": 7}},
                {"definition": {"id": "s2", "trafficClassId": "standard"}, "configuration": {"pcp": 5}},
            ],
            "configuration": {"nodeConfigs": [{"nodeId": "SW1", "portConfigs": [
                {"portId": "P1", "pcpMappingEnabled": True, "pcpConfig": {"trafficClassTable": [
                    {"priority": 7, "egressQueueId": 0},
                    {"priority": 5, "egressQueueId": 1}
                ]}}
            ]}]}
        }
    }


def usecase():
    u = types.SimpleNamespace()
    u.streams = [{"name": "s1", "srClass": "critical"}, {"name": "s2", "srClass": "standard"}]
    u.target_traffic_classes = ["critical", "standard"]
    u.routes = {"s1": [("SW1", "P1")], "s2": [("SW1", "P1")]}
    return u


class TestCBSBoundary(unittest.TestCase):
    def test_translate_internal_but_keep_request(self):
        req, u = request(), usecase()
        before = copy.deepcopy(req)
        mapping = prepare_cbs_solver_usecase(req, u, object())
        self.assertEqual(mapping, {"standard": "M1", "critical": "M2"})
        self.assertEqual(u.target_traffic_classes, ["M2", "M1"])
        self.assertEqual([s["srClass"] for s in u.streams], ["M2", "M1"])
        self.assertEqual(req, before)

    def test_restore_all_srclass_values_for_response_adapter(self):
        mapping = {"critical": "M2", "standard": "M1"}
        result = {"portClassGroups": [
            {"node": "SW1", "port": "P1", "srClass": "M1", "sharedCBS": {"idleSlopeBps": 123}},
            {"node": "SW1", "port": "P1", "srClass": "M2", "sharedCBS": {"idleSlopeBps": 456}},
        ], "streams": [{"srClass": "M2"}], "sharedStreamResults": [{"stream": "s1", "deadlineSatisfied": True}]}
        before = copy.deepcopy(result)
        restored = restore_client_classes_in_cbs_result(result, mapping)
        self.assertEqual([g["srClass"] for g in restored["portClassGroups"]], ["standard", "critical"])
        self.assertEqual(restored["streams"][0]["srClass"], "critical")
        self.assertEqual(restored["portClassGroups"][0]["sharedCBS"]["idleSlopeBps"], 123)
        self.assertEqual(result, before)

    def test_fail_closed_does_not_mutate_usecase(self):
        req, u = request(), usecase()
        req["forwardingPlane"]["configuration"]["nodeConfigs"][0]["portConfigs"][0]["pcpMappingEnabled"] = False
        original = copy.deepcopy(u.streams)
        with self.assertRaisesRegex(CBSClassTranslationError, "Explicit, enabled"):
            prepare_cbs_solver_usecase(req, u, object())
        self.assertEqual(u.streams, original)
        self.assertEqual(u.target_traffic_classes, ["critical", "standard"])

    def test_unmatched_target_does_not_mutate_usecase(self):
        req, u = request(), usecase()
        u.target_traffic_classes = ["unknown"]
        original = copy.deepcopy(u.streams)
        with self.assertRaisesRegex(CBSClassTranslationError, "Target class"):
            prepare_cbs_solver_usecase(req, u, object())
        self.assertEqual(u.streams, original)

    def test_unknown_solver_label_response_rejected(self):
        with self.assertRaisesRegex(CBSClassTranslationError, "Unexpected Zhao class"):
            restore_client_classes_in_cbs_result({"portClassGroups": [{"srClass": "M7"}]}, {"critical": "M1"})

if __name__ == "__main__":
    unittest.main()

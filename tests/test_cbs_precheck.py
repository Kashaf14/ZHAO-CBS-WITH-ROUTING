"""Focused CBS pre-optimization hardware tests (synthetic, not real CNC proof).

Place in tests/test_cbs_precheck.py and run from the framework repository root:
    python -m unittest discover -s tests -p 'test_cbs_precheck.py' -v

The paper numerical regressions remain in the existing CBS test files.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cbs_precheck import validate_cbs_precheck
from cbs_response_adapter import CBSResponseConversionError
from algorithm.cbs import CBSOptimizer


def scenario(*, target_classes=("M1",), include_m2=False, benchmark=None):
    links = [
        {"sourceNode": "ES1", "sourcePort": "Port1", "destNode": "SW1",
         "destPort": "P1", "linkBw": 100_000_000},
        {"sourceNode": "SW1", "sourcePort": "P2", "destNode": "ES2",
         "destPort": "Port1", "linkBw": 100_000_000},
    ]
    streams = [{
        "name": "f1", "srClass": "M1", "pcp": 5, "maxFrameSize": 100,
        "tlkrPssblPlcmnt": ["ES1"], "lstnsrPssblPlcmnt": [["ES2"]],
    }]
    if include_m2:
        streams.append({
            "name": "f2", "srClass": "M2", "pcp": 3, "maxFrameSize": 100,
            "tlkrPssblPlcmnt": ["ES1"], "lstnsrPssblPlcmnt": [["ES2"]],
        })
    usecase = SimpleNamespace(
        streams=streams,
        links=links,
        fixed_routes={s["name"]: {"path": [{
            "hopNumber": 0, "bridgeId": "SW1", "ingressPortId": "P1",
            "egressPortId": "P2",
        }]} for s in streams},
        target_traffic_classes=list(target_classes),
    )
    if benchmark:
        usecase.cbs_benchmark_output_port = benchmark

    def port(port_id):
        return {"id": port_id, "number_of_queues": 8}

    def port_config(port_id):
        return {"portId": port_id, "pcpMappingEnabled": True, "pcpConfig": {
            "trafficClassTable": [
                {"priority": 5, "egressQueueId": 4},
                {"priority": 3, "egressQueueId": 3},
            ]
        }}

    # Synthetic blocking-frame bound, not a measured hardware limit.
    request = {"schemaVersion": "1.4","mechanisms": [
        {"name": "CBS", "constraints": {"maxBestEffortFrameSizeBytes": 1500}},
    ], "forwardingPlane": {
        
            "topology": {"nodes": [
                {"name": "ES1", "type": "END_STATION", "ports": [port("Port1")]},
                {"name": "SW1", "type": "BRIDGE", "ports": [port("P1"), port("P2"), port("P99")]},
                {"name": "ES2", "type": "END_STATION", "ports": [port("Port1")]},
            ]},
            "streams": [
                {
                    "definition": {
                        "id": s["name"],
                        "talkerNodeId": "ES1",
                        "listenerNodeIds": ["ES2"],
                    },
                    "decisionVariables": {"routing": "fixed"},
                    "configuration": {
                        "path": deepcopy(usecase.fixed_routes[s["name"]]["path"])
                    },
                }
                for s in streams
            ],

        "configuration": {"nodeConfigs": [
            {"nodeId": "ES1", "portConfigs": [port_config("Port1")]},
            {"nodeId": "SW1", "portConfigs": [port_config("P2")]},
        ]},
    }, "capabilities": {"devices": [
        {"nodeId": "ES1", "mechanisms": {"CBS": {"supported": True}}},
        {"nodeId": "SW1", "mechanisms": {"CBS": {"supported": True}}},
    ]}}
    return request, usecase


def capability(request, node):
    return next(d for d in request["capabilities"]["devices"] if d["nodeId"] == node)


def port_config(request, node, port):
    return next(p for n in request["forwardingPlane"]["configuration"]["nodeConfigs"]
                if n["nodeId"] == node for p in n["portConfigs"] if p["portId"] == port)


class CBSPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.optimizer = CBSOptimizer()

    def check(self, request, usecase):
        return validate_cbs_precheck(request, usecase, self.optimizer)

    def test_supported_targeted_ports_pass(self):
        self.check(*scenario())

    def test_missing_best_effort_bound_rejected(self):
        req, uc = scenario()
        del req["mechanisms"][0]["constraints"]
        with self.assertRaisesRegex(CBSResponseConversionError, "maxBestEffortFrameSizeBytes"):
            self.check(req, uc)

    def test_invalid_best_effort_bound_rejected(self):
        for bad in (0, -1, True, 1.5, "1500", None):
            with self.subTest(value=bad):
                req, uc = scenario()
                req["mechanisms"][0]["constraints"]["maxBestEffortFrameSizeBytes"] = bad
                with self.assertRaisesRegex(CBSResponseConversionError, "maxBestEffortFrameSizeBytes"):
                    self.check(req, uc)

    def test_service_missing_best_effort_bound_rejects_before_optimizer(self):
        req, uc = scenario()
        del req["mechanisms"][0]["constraints"]
        adapter = ModuleType("usecase_adapter")
        adapter.build_usecase_from_payload = Mock(return_value=uc)
        path = ROOT / "optimizer-manager" / "cbs_service.py"
        spec = importlib.util.spec_from_file_location("cbs_be_bound_service_test", path)
        service = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"usecase_adapter": adapter}):
            assert spec.loader is not None
            spec.loader.exec_module(service)
        service.optimizer.optimize = Mock(side_effect=AssertionError("solver was called"))
        with patch.dict(os.environ, {"CBS_BENCHMARK_EGRESS_PORT": ""}):
            try:
                service.optimize(deepcopy(req))
            except Exception as exc:
                self.assertEqual(getattr(exc, "status_code", None), 422)
                self.assertEqual(exc.detail["code"], "CBS_PRECHECK_FAILED")
                self.assertIn("maxBestEffortFrameSizeBytes", exc.detail["message"])
            else:
                self.fail("Expected early HTTP 422 rejection")
        service.optimizer.optimize.assert_not_called()

    def test_explicit_false_rejects(self):
        req, uc = scenario()
        capability(req, "ES1")["mechanisms"]["CBS"]["supported"] = False
        with self.assertRaisesRegex(CBSResponseConversionError, "explicitly unsupported"):
            self.check(req, uc)

    def test_missing_support_rejects(self):
        req, uc = scenario()
        del capability(req, "ES1")["mechanisms"]["CBS"]["supported"]
        with self.assertRaisesRegex(CBSResponseConversionError, "Missing explicit CBS support"):
            self.check(req, uc)

    def test_port_override_takes_precedence(self):
        req, uc = scenario()
        capability(req, "SW1")["ports"] = {"P2": {
            "mechanisms": {"CBS": {"supported": False}}
        }}
        with self.assertRaisesRegex(CBSResponseConversionError, "explicitly unsupported.*SW1/P2"):
            self.check(req, uc)
        capability(req, "SW1")["mechanisms"]["CBS"]["supported"] = False
        capability(req, "SW1")["ports"]["P2"]["mechanisms"]["CBS"]["supported"] = True
        self.check(req, uc)

    def test_missing_disabled_and_ambiguous_pcp_mappings_reject(self):
        for defect in ("missing", "disabled", "ambiguous"):
            with self.subTest(defect=defect):
                req, uc = scenario()
                pc = port_config(req, "ES1", "Port1")
                if defect == "missing":
                    pc["pcpConfig"]["trafficClassTable"] = []
                elif defect == "disabled":
                    pc["pcpMappingEnabled"] = False
                else:
                    pc["pcpConfig"]["trafficClassTable"].append({
                        "priority": 5, "egressQueueId": 6
                    })
                with self.assertRaises(CBSResponseConversionError):
                    self.check(req, uc)

    def test_invalid_physical_queue_rejects(self):
        req, uc = scenario()
        port_config(req, "ES1", "Port1")["pcpConfig"]["trafficClassTable"][0]["egressQueueId"] = 9
        with self.assertRaisesRegex(CBSResponseConversionError, "Invalid physical queue"):
            self.check(req, uc)

    def test_unsupported_queue_rejects(self):
        req, uc = scenario()
        capability(req, "ES1")["mechanisms"]["CBS"]["supportedQueues"] = [3]
        with self.assertRaisesRegex(CBSResponseConversionError, "does not support CBS"):
            self.check(req, uc)

    def test_unrelated_port_without_cbs_does_not_reject(self):
        req, uc = scenario()
        # P99 exists on SW1 but is not traversed by a targeted CBS stream.
        capability(req, "SW1")["ports"] = {"P99": {
            "mechanisms": {"CBS": {"supported": False}}
        }}
        self.check(req, uc)

    def test_lower_priority_background_does_not_require_own_cbs_queue(self):
        req, uc = scenario(include_m2=True, target_classes=("M1",))
        for node in ("ES1", "SW1"):
            capability(req, node)["mechanisms"]["CBS"]["supportedQueues"] = [4]
        self.check(req, uc)

    def test_two_targeted_classes_use_distinct_physical_queues(self):
        req, uc = scenario(include_m2=True, target_classes=("M1", "M2"))
        self.check(req, uc)  # Both classes at BOTH output stages.
        port_config(req, "SW1", "P2")["pcpConfig"]["trafficClassTable"][1]["egressQueueId"] = 4
        with self.assertRaisesRegex(CBSResponseConversionError, "More than one CBS class"):
            self.check(req, uc)

    def test_multihop_includes_both_egress_stages(self):
        req, uc = scenario()
        capability(req, "SW1")["mechanisms"]["CBS"]["supported"] = False
        with self.assertRaisesRegex(CBSResponseConversionError, "SW1/P2"):
            self.check(req, uc)

    def test_benchmark_mode_checks_selected_egress_only(self):
        req, uc = scenario(benchmark=("SW1", "P2"))
        capability(req, "ES1")["mechanisms"]["CBS"]["supported"] = False
        self.check(req, uc)  # Solver's opt-in benchmark excludes ES1/Port1.
        capability(req, "SW1")["mechanisms"]["CBS"]["supported"] = False
        with self.assertRaisesRegex(CBSResponseConversionError, "SW1/P2"):
            self.check(req, uc)

    def test_max_cbs_queues_lower_bound(self):
        req, uc = scenario(include_m2=True, target_classes=("M1", "M2"))
        capability(req, "SW1")["mechanisms"]["CBS"]["maxCbsQueues"] = 1
        with self.assertRaisesRegex(CBSResponseConversionError, "maxCbsQueues"):
            self.check(req, uc)

    def test_service_rejection_does_not_call_optimizer(self):
        # Load the actual HTTP handler with only the Usecase constructor stubbed.
        # No Manager, live CNC, external TSN library, or mathematical solver runs.
        req, uc = scenario()
        capability(req, "ES1")["mechanisms"]["CBS"]["supported"] = False
        adapter = ModuleType("usecase_adapter")
        adapter.build_usecase_from_payload = Mock(return_value=uc)
        path = ROOT / "optimizer-manager" / "cbs_service.py"
        spec = importlib.util.spec_from_file_location("cbs_precheck_service_test", path)
        service = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"usecase_adapter": adapter}):
            assert spec.loader is not None
            spec.loader.exec_module(service)
        service.optimizer.optimize = Mock(side_effect=AssertionError("solver was called"))
        with patch.dict(os.environ, {"CBS_BENCHMARK_EGRESS_PORT": ""}):
            try:
                service.optimize(deepcopy(req))
            except Exception as exc:
                self.assertEqual(getattr(exc, "status_code", None), 422)
                self.assertEqual(exc.detail["code"], "CBS_PRECHECK_FAILED")
            else:
                self.fail("Expected early HTTP 422 rejection")
        service.optimizer.optimize.assert_not_called()


if __name__ == "__main__":
    unittest.main()

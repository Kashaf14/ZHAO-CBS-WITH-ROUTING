"""Verify that the generic N-class CBS solver uses per-port priority inputs.

This replaces the Step 8 test that watched the retired M1/M2-only dispatch.
Uses the original Zhao TC1 request with two targeted classes. It checks
actual selector invocation AND delivery of higher-priority inputs to the
CBS mathematical function. It is not independent schedulability validation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "optimizer-manager"))

from optimizers.cbs import CBSOptimizer
from optimizers import cbs_nclass_candidate
from usecase_adapter import build_usecase_from_payload

TC1 = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))

EXPECTED_PORTS = {
    ("ES1", "Port1"), ("ES2", "Port1"), ("ES3", "Port1"),
    ("SW1", "P4"), ("SW1", "P5"),
}


class TestPriorityWiring(unittest.TestCase):
    def test_generic_nclass_uses_per_port_priority_for_two_classes(self):
        request = json.loads(TC1.read_text(encoding="utf-8"))
        self.assertEqual(request["optimizationTask"]["scope"]["trafficClasses"], ["M1"])
        request["optimizationTask"]["scope"]["trafficClasses"] = ["M1", "M2"]

        selector = cbs_nclass_candidate.higher_priority_for_port
        math_solver = cbs_nclass_candidate.calculate_class_cbs
        with patch.object(
            cbs_nclass_candidate, "higher_priority_for_port", wraps=selector
        ) as selector_spy, patch.object(
            cbs_nclass_candidate, "calculate_class_cbs", wraps=math_solver
        ) as math_spy:
            result = CBSOptimizer().optimize(build_usecase_from_payload(request))

        group_map = {
            (g["srClass"], g["node"], g["port"]): g
            for g in result["portClassGroups"]
        }
        self.assertEqual(len(group_map), 10)
        self.assertEqual(len(result["sharedStreamResults"]), 8)
        self.assertTrue(result["allDeadlinesSatisfied"])
        self.assertEqual(selector_spy.call_count, 10)

        # One priority lookup for each M1 group and each M2 group; no
        # hard-coded M2-only execution branch.
        seen = {"M1": set(), "M2": set()}
        for call in selector_spy.call_args_list:
            group, active = call.args
            cls = group["srClass"]
            port = (group["node"], group["port"])
            self.assertIn(cls, seen)
            self.assertNotIn(port, seen[cls])
            seen[cls].add(port)
            self.assertIs(group, group_map[(cls, *port)])

            same_port = {
                g["srClass"]: g for g in active
                if (g["node"], g["port"]) == port
            }
            expected_classes = {"M1"} if cls == "M1" else {"M1", "M2"}
            self.assertEqual(set(same_port), expected_classes)
            dependencies = selector(group, active)
            if cls == "M1":
                self.assertEqual(dependencies, ())
            else:
                self.assertEqual(len(dependencies), 1)
                m1 = group_map[("M1", *port)]
                self.assertEqual(
                    dependencies[0].idle_slope_bps,
                    m1["sharedCBS"]["idleSlopeBps"],
                )
                self.assertEqual(
                    dependencies[0].max_frame_bits,
                    m1["maxFrameBits"],
                )

        self.assertEqual(seen, {"M1": EXPECTED_PORTS, "M2": EXPECTED_PORTS})

        # Theorem-2 calculations must receive real higher-priority data,
        # not merely call the selector without consuming its result.
        self.assertGreater(math_spy.call_count, 0)
        sizes = {len(call.kwargs["higher_priority_classes"])
                 for call in math_spy.call_args_list}
        self.assertEqual(sizes, {0, 1})


if __name__ == "__main__":
    unittest.main()

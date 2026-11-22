"""Local regression of single-port analysis and configured M2 credits.

These tests do NOT establish reproduction of Zhao Fig. 6 or live CNC validity.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algorithm.cbs import CBSOptimizer
from algorithm.cbs_nclass_candidate import optimize_nclass_candidate


def one_stream_usecase(*, benchmark):
    """Synthetic one-flow regression, not a paper TC4 dataset."""
    usecase = SimpleNamespace(
        target_traffic_classes=["M1"],
        optimization_task="configuration",
        streams=[{
            "name": "f1",
            "srClass": "M1",
            "maxFramesPerInterval": 1,
            "maxFrameSize": 342,
            "interval": 1_500_000,
            "maxLatency": 2_800_000,
            "beMaxFrameSize": 1542,
            "tlkrPssblPlcmnt": ["ES1"],
            "lstnsrPssblPlcmnt": [["ES5"]],
        }],
        fixed_routes={
            "f1": {"path": [{
                "hopNumber": 0, "bridgeId": "SW1",
                "ingressPortId": "P1", "egressPortId": "P5",
            }]}
        },
        links=[
            {"sourceNode": "ES1", "sourcePort": "Port1",
             "destNode": "SW1", "destPort": "P1", "linkBw": 100_000_000},
            {"sourceNode": "SW1", "sourcePort": "P5",
             "destNode": "ES5", "destPort": "Port1", "linkBw": 100_000_000},
        ],
    )
    if benchmark:
        usecase.cbs_benchmark_output_port = ("SW1", "P5")
    return usecase


class SinglePortBenchmarkTests(unittest.TestCase):
    def test_opt_in_limits_calculation_to_selected_egress(self):
        result = optimize_nclass_candidate(
            CBSOptimizer(), one_stream_usecase(benchmark=True)
        )
        self.assertEqual(
            [(g["node"], g["port"]) for g in result["portClassGroups"]],
            [("SW1", "P5")],
        )
        self.assertEqual(
            result["streams"][0]["egressPorts"][0]["h"], 0
        )
        self.assertTrue(result["allDeadlinesSatisfied"])

    def test_rate_floor_for_long_deadline(self):
        result = optimize_nclass_candidate(
            CBSOptimizer(), one_stream_usecase(benchmark=True)
        )
        group = result["portClassGroups"][0]
        self.assertGreater(
            group["sharedCBS"]["idleSlopeBps"], group["aggregateRhoBps"]
        )
        self.assertLess(group["sharedDelayNs"], 2_800_000)

    def test_default_keeps_both_egress_stages(self):
        result = optimize_nclass_candidate(
            CBSOptimizer(), one_stream_usecase(benchmark=False)
        )
        self.assertEqual(len(result["streams"][0]["egressPorts"]), 2)
        self.assertEqual(len(result["portClassGroups"]), 2)



if __name__ == "__main__":
    unittest.main()

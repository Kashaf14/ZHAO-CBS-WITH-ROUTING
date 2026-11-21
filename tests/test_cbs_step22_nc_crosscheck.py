"""Step 22: separate, post-hoc NC verification of Zhao-based CBS results.

This test does not call any optimizer function to calculate worst-case delays.
It reconstructs each port's arrival envelope independently from the input
streams, ordered per-stream port memberships, Theorem-1 local deadlines, and
previous CBS parameters. Then it enumerates the piecewise-linear envelope's
breakpoints and evaluates the horizontal deviation from the CBS rate-latency
service curve (Zhao Eq. 3, 9, 11 and NC horizontal-deviation definition).

Scope: Same-model, independently coded numerical cross-check. NOT reproduction
of the separate deterministic NC analysis tool used for Zhao Table III, NOT
hardware testing, and NOT a proof that every TSN deployment is schedulable.

Place this file in tests/ and run:
  python -m unittest discover -s tests -p 'test_cbs_step22_nc_crosscheck.py' -v
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from algorithm.cbs import CBSOptimizer
from test_cbs_nclass_candidate import synth_request
from usecase_adapter import build_usecase_from_payload

FIXTURE = Path(os.environ.get("CBS_TC1_REQUEST") or (
    ROOT.parent / "tsn-optimization-contracts" / "tests" / "request"
    / "valid" / "cbs-paper-tc1-full-8flow-contract.json"
))
NS = 1_000_000_000.0

# Independently published Zhao et al. (2024), Table II, TC1; percentages.
# This is an external published comparison for M1/M2 only, not for M3..M7.
ZHAO_TC1_TABLE_II = {
    ("M1", "ES1", "Port1"): 12.77,
    ("M1", "ES2", "Port1"): 4.94,
    ("M1", "ES3", "Port1"): 2.47,
    ("M1", "SW1", "P4"): 9.84,
    ("M1", "SW1", "P5"): 9.56,
    ("M2", "ES1", "Port1"): 7.93,
    ("M2", "ES2", "Port1"): 0.72,
    ("M2", "ES3", "Port1"): 5.11,
    ("M2", "SW1", "P4"): 8.74,
    ("M2", "SW1", "P5"): 4.42,
}


def _key(group):
    return (group["srClass"], group["node"], group["port"])


def _close(actual, expected, label):
    if not math.isclose(actual, expected, rel_tol=2e-9, abs_tol=2e-3):
        raise AssertionError(f"{label}: recomputed={actual:.12g}, reported={expected:.12g}")


def _source_inputs(request):
    found = {}
    for item in request["forwardingPlane"]["streams"]:
        definition = item["definition"]
        sid = definition["id"]
        if sid in found:
            raise AssertionError(f"Duplicate input stream {sid}")
        traffic = definition["trafficSpec"]
        burst = traffic["maxFrameSize"] * 8.0
        rate = burst * NS / traffic["intervalNs"]
        found[sid] = {
            "class": definition["trafficClassId"],
            "burst": burst,
            "rate": rate,
            "deadline": definition["userToNetworkRequirements"]["maxLatencyNs"],
        }
    return found


def _curve_extrema(lines_by_predecessor, idle):
    """Evaluate alpha(t)/idle - t at all vertices of the min-of-lines envelope.

    Each predecessor contributes a minimum of its flow, link and CBS shaping
    affine bounds. Their sum is piecewise affine and concave; the maximum of
    alpha(t)/idle - t occurs at t=0+ or an intersection, if the final slope
    is below idle. Times are seconds; intercepts are bits.
    """
    times = {0.0}
    tail_rate = 0.0
    for triples in lines_by_predecessor:
        tail_rate += min(slope for slope, _ in triples)
        for i in range(len(triples)):
            for j in range(i + 1, len(triples)):
                m1, b1 = triples[i]
                m2, b2 = triples[j]
                if m1 == m2:
                    continue
                t = (b2 - b1) / (m1 - m2)
                if math.isfinite(t) and t >= 0:
                    times.add(t)
    if tail_rate > idle + 1e-6:
        raise AssertionError(f"Unbounded arrival-rate tail {tail_rate} > idle {idle}")

    def alpha(t):
        return sum(min(m * t + b for m, b in triples)
                   for triples in lines_by_predecessor)

    return max((alpha(t) / idle - t) * NS for t in times)


def independent_nc_postcheck(request, result):
    """Recompute post-hoc horizontal-deviation delays without optimizer math APIs.

    The routing/group membership and local-deadline *inputs* are those emitted
    by the solver, so this is not a completely separate end-to-end NC model.
    """
    source = _source_inputs(request)
    groups = {_key(group): group for group in result["portClassGroups"]}
    if len(groups) != len(result["portClassGroups"]):
        raise AssertionError("Duplicate class-port groups")

    route_membership = defaultdict(list)
    for key, group in groups.items():
        for pos in group["streamPositions"]:
            sid = pos["stream"]
            if sid not in source or source[sid]["class"] != key[0]:
                raise AssertionError(f"Unknown or mismatched stream {sid} on {key}")
            route_membership[sid].append((pos["h"], key, pos))

    # Independently propagate each source traffic envelope using Eq. (9).
    # Never take bursts from result[*].streamBursts or the optimized arrival
    # curve. This catches incorrect burst propagation at intermediate ports.
    propagated = {}
    routes = {}
    for sid, hops in route_membership.items():
        hops = sorted(hops, key=lambda item: item[0])
        if [h for h, _, _ in hops] != list(range(len(hops))):
            raise AssertionError(f"Noncontiguous route positions: {sid}")
        routes[sid] = [key for _, key, _ in hops]
        prior_ns = 0.0
        for h, key, pos in hops:
            expected_prev = None if h == 0 else {
                "node": hops[h - 1][1][1],
                "port": hops[h - 1][1][2],
                "srClass": hops[h - 1][1][0],
            }
            if pos.get("predecessor") != expected_prev:
                raise AssertionError(f"Inconsistent predecessor: {sid} at {key}")
            propagated[(sid, key)] = (
                source[sid]["burst"] + source[sid]["rate"] * prior_ns / NS
            )
            prior_ns += groups[key]["requiredDeadlineNs"]

    recomputed_port_delays = {}
    for key, group in groups.items():
        idle = group["sharedCBS"]["idleSlopeBps"]
        cmax = group["sharedCBS"]["creditMaxBits"]
        if not (0 < idle < group["linkSpeedBps"] and cmax >= 0):
            raise AssertionError(f"Invalid CBS rate/credit: {key}")

        source_positions = [p for p in group["streamPositions"]
                            if p.get("predecessor") is None]
        if source_positions:
            if len(source_positions) != len(group["streamPositions"]):
                raise AssertionError(f"Unsupported mixed source/transit group: {key}")
            burst = sum(source[p["stream"]]["burst"] for p in source_positions)
            rate = sum(source[p["stream"]]["rate"] for p in source_positions)
            if rate > idle + 1e-6:
                raise AssertionError(f"Unstable source aggregate: {key}")
            delay_ns = (cmax + burst) / idle * NS
        else:
            subsets = defaultdict(list)
            for pos in group["streamPositions"]:
                pred = pos["predecessor"]
                pkey = (pred["srClass"], pred["node"], pred["port"])
                if pkey not in groups:
                    raise AssertionError(f"Unknown predecessor group: {pkey}")
                subsets[pkey].append(pos["stream"])
            arc_lines = []
            for pkey, flow_ids in subsets.items():
                prev = groups[pkey]
                frame_max = max(source[s]["burst"] for s in flow_ids)
                rho = sum(source[s]["rate"] for s in flow_ids)
                intercept = sum(propagated[(s, key)] for s in flow_ids)
                prev_cbs = prev["sharedCBS"]
                arc_lines.append([
                    (rho, intercept),
                    (prev["linkSpeedBps"], frame_max),
                    (prev_cbs["idleSlopeBps"],
                     prev_cbs["creditMaxBits"] - prev_cbs["creditMinBits"] + frame_max),
                ])
            deviation_ns = _curve_extrema(arc_lines, idle)
            delay_ns = cmax / idle * NS + deviation_ns

        if not math.isfinite(delay_ns) or delay_ns < 0:
            raise AssertionError(f"Invalid NC delay for {key}: {delay_ns}")
        _close(delay_ns, group["sharedDelayNs"], f"Port NC delay {key}")
        if delay_ns > group["requiredDeadlineNs"] + 2e-3:
            raise AssertionError(f"Port local deadline exceeded: {key}")
        recomputed_port_delays[key] = delay_ns

    rows = {row["stream"]: row for row in result["sharedStreamResults"]}
    if set(rows) != set(routes):
        raise AssertionError("Missing/duplicate result streams")
    for sid, hops in routes.items():
        path_delay_ns = sum(recomputed_port_delays[key] for key in hops)
        row = rows[sid]
        _close(path_delay_ns, row["sharedPathDelayNs"], f"Path NC delay {sid}")
        if path_delay_ns > source[sid]["deadline"] + 2e-3:
            raise AssertionError(f"End-to-end deadline exceeded: {sid}")
    return len(recomputed_port_delays), len(rows)


class TestStep22IndependentNCCrosscheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_bytes = FIXTURE.read_bytes()
        cls.original = json.loads(cls.original_bytes)
        cls.optimizer = CBSOptimizer()

    def run_case(self, n):
        request = deepcopy(self.original) if n == 1 else synth_request(self.original, classes=n)
        result = self.optimizer.optimize(build_usecase_from_payload(deepcopy(request)))
        self.assertTrue(result["allDeadlinesSatisfied"])
        count = independent_nc_postcheck(request, result)
        self.assertGreater(count[0], 0)
        self.assertGreater(count[1], 0)
        return request, result

    def test_independent_horizontal_deviation_n1_n2_n3_n7(self):
        for n in (1, 2, 3, 7):
            with self.subTest(n=n):
                self.run_case(n)

    def test_independent_horizontal_deviation_larger_m4_blocker(self):
        req = synth_request(self.original, classes=4, m4_frame=300)
        res = self.optimizer.optimize(build_usecase_from_payload(deepcopy(req)))
        independent_nc_postcheck(req, res)

    def test_zhao_published_tc1_ten_idle_slopes(self):
        _, result = self.run_case(2)
        reported = {_key(g): g for g in result["portClassGroups"]}
        self.assertEqual(set(reported), set(ZHAO_TC1_TABLE_II))
        for key, reference in ZHAO_TC1_TABLE_II.items():
            with self.subTest(port_class=key):
                g = reported[key]
                percentage = 100.0 * g["sharedCBS"]["idleSlopeBps"] / g["linkSpeedBps"]
                self.assertLessEqual(abs(percentage - reference), 0.01)

    def test_falsified_reported_delay_is_detected(self):
        req = synth_request(self.original, classes=3)
        res = self.optimizer.optimize(build_usecase_from_payload(deepcopy(req)))
        res["portClassGroups"][0]["sharedDelayNs"] += 10_000
        with self.assertRaisesRegex(AssertionError, "Port NC delay"):
            independent_nc_postcheck(req, res)

    def test_input_fixture_unchanged(self):
        self.assertEqual(FIXTURE.read_bytes(), self.original_bytes)
        caps = self.optimizer.get_capabilities()
        self.assertNotIn("trafficClasses", caps)
        self.assertIs(caps["inputRequirements"]["requiresPcp"], True)
        self.assertIs(caps["inputRequirements"]["acceptsClientTrafficClassIds"], True)


if __name__ == "__main__":
    unittest.main()

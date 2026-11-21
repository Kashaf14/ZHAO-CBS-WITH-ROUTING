"""Isolated CBS route-preparation regression tests (no Manager/CNC/network).

Install alongside optimizer-manager/cbs_route_preparation.py and run:
  python -m unittest discover -s tests -p 'test_cbs_route_preparation.py' -v
"""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "optimizer-manager"))
from cbs_route_preparation import (  # noqa: E402
    CBSRoutePreparationError, prepare_cbs_routes,
)


def link(name, src, src_port, dest, dest_port):
    return dict(id=name, sourceNode=src, sourcePort=src_port,
                destNode=dest, destPort=dest_port, linkBw=100_000_000)


LINKS = [
    link("1", "ES1", "e1", "SW1", "s1"),
    link("2", "SW1", "s2", "ES2", "e2"),
]


def payload(*, routing="optimize", hops=None, listener_paths=None, model_id="f1"):
    config = {"pcp": 5}
    if hops is not None:
        config["path"] = copy.deepcopy(hops)
    if listener_paths is not None:
        config["listenerPaths"] = copy.deepcopy(listener_paths)
    return {
        "schemaVersion": "1.4", "mechanisms": [{"name": "CBS"}],
        "forwardingPlane": {
            "topology": {
                "nodes": [
                    {"name": "ES1", "type": "END_STATION"},
                    {"name": "SW1", "type": "BRIDGE"},
                    {"name": "ES2", "type": "END_STATION"},
                ]
            },
            "streams": [{
                "definition": {
                    "id": model_id, "talkerNodeId": "ES1",
                    "listenerNodeIds": ["ES2"],
                },
                "decisionVariables": {"routing": routing},
                "configuration": config,
            }],
        },
    }


HOPS = [{
    "hopNumber": 0, "bridgeId": "SW1",
    "ingressPortId": "s1", "egressPortId": "s2",
}]


class StrictCBSResolver:
    """A small deterministic fake for optimizer.resolve_stream_path()."""

    def resolve_stream_path(self, stream, links, routes=None):
        hops = routes[stream["name"]]["path"]
        if not hops:
            raise ValueError("empty path")
        chain = []
        source = stream["tlkrPssblPlcmnt"][0]
        for hop in hops:
            choices = [l for l in links if l["sourceNode"] == source
                       and l["destNode"] == hop["bridgeId"]
                       and l["destPort"] == hop["ingressPortId"]]
            if len(choices) != 1:
                raise ValueError("invalid directed ingress")
            chain.append(choices[0])
            source = hop["bridgeId"]
        listener = stream["lstnsrPssblPlcmnt"][0][0]
        last = hops[-1]
        choices = [l for l in links if l["sourceNode"] == source
                   and l["sourcePort"] == last["egressPortId"]
                   and l["destNode"] == listener]
        if len(choices) != 1:
            raise ValueError("invalid directed egress")
        chain.append(choices[0])
        return [chain[0]["sourceNode"]] + [l["destNode"] for l in chain], chain


def usecase(p, *, links=None):
    models = p["forwardingPlane"]["streams"]
    return SimpleNamespace(
        streams=[{"name": x["definition"]["id"],
                  "tlkrPssblPlcmnt": ["ES1"],
                  "lstnsrPssblPlcmnt": [["ES2"]]} for x in models],
        links=copy.deepcopy(LINKS if links is None else links),
        fixed_routes={
            x["definition"]["id"]: copy.deepcopy(x.get("configuration") or {})
            for x in models if x["decisionVariables"].get("routing") in ("fixed", "preserve")
        },
    )


class TestCBSRoutePreparation(unittest.TestCase):
    solver = StrictCBSResolver()

    def run_case(self, req, *, links=None):
        uc = usecase(req, links=links)
        unchanged = copy.deepcopy(req)
        generated = prepare_cbs_routes(req, uc, self.solver)
        self.assertEqual(req, unchanged)
        self.assertEqual(set(uc.fixed_routes), {"f1"})
        return uc, generated

    def test_optimize_without_path_generates_one_bridge_route(self):
        uc, generated = self.run_case(payload())
        self.assertEqual(generated, {"f1": HOPS})
        self.assertEqual(uc.fixed_routes["f1"]["path"], HOPS)

    def test_fixed_with_supplied_path_unchanged(self):
        uc, generated = self.run_case(payload(routing="fixed", hops=HOPS))
        self.assertEqual(generated, {})
        self.assertEqual(uc.fixed_routes["f1"]["path"], HOPS)

    def test_preserve_with_supplied_path_unchanged(self):
        _, generated = self.run_case(payload(routing="preserve", hops=HOPS))
        self.assertEqual(generated, {})

    def test_optimize_with_supplied_path_reuses_it(self):
        uc, generated = self.run_case(payload(hops=HOPS))
        self.assertEqual(generated, {})
        self.assertEqual(uc.fixed_routes["f1"]["path"], HOPS)

    def test_fixed_missing_path_fails_atomically(self):
        req = payload(routing="fixed")
        uc = usecase(req)
        before = copy.deepcopy(uc.fixed_routes)
        with self.assertRaisesRegex(CBSRoutePreparationError, "needs a supplied"):
            prepare_cbs_routes(req, uc, self.solver)
        self.assertEqual(uc.fixed_routes, before)

    def test_preserve_missing_path_fails(self):
        with self.assertRaisesRegex(CBSRoutePreparationError, "needs a supplied"):
            self.run_case(payload(routing="preserve"))

    def test_no_routing_field_or_path_fails(self):
        req = payload()
        req["forwardingPlane"]["streams"][0]["decisionVariables"] = {}
        with self.assertRaisesRegex(CBSRoutePreparationError, "unspecified routing"):
            self.run_case(req)

    def test_fixed_single_listenerpaths_supported(self):
        uc, generated = self.run_case(payload(
            routing="fixed", listener_paths=[{"listenerId": "ES2", "path": HOPS}]))
        self.assertEqual(generated, {})
        self.assertEqual(uc.fixed_routes["f1"]["path"], HOPS)

    def test_optimize_single_listenerpaths_supported(self):
        _, generated = self.run_case(payload(
            listener_paths=[{"listenerId": "ES2", "path": HOPS}]))
        self.assertEqual(generated, {})

    def test_reject_ambiguous_two_path_forms(self):
        with self.assertRaisesRegex(CBSRoutePreparationError, "both path and listenerPaths"):
            self.run_case(payload(hops=HOPS, listener_paths=[{"listenerId": "ES2", "path": HOPS}]))

    def test_reject_no_directed_route(self):
        with self.assertRaisesRegex(CBSRoutePreparationError, "no directed bridge route"):
            self.run_case(payload(), links=LINKS[:1])

    def test_no_intermediate_end_station_as_router(self):
        links = [link("a", "ES1", "e1", "ES3", "p1"),
                 link("b", "ES3", "p2", "ES2", "e2")]
        req = payload()
        req["forwardingPlane"]["topology"]["nodes"].append({"name":"ES3","type":"END_STATION"})
        with self.assertRaisesRegex(CBSRoutePreparationError, "no directed bridge route"):
            self.run_case(req, links=links)

    def test_reject_duplicate_physical_link(self):
        with self.assertRaisesRegex(CBSRoutePreparationError, "ambiguous duplicate"):
            self.run_case(payload(), links=LINKS + [dict(LINKS[0], id="duplicated")])

    def test_reject_multicast(self):
        req = payload()
        req["forwardingPlane"]["streams"][0]["definition"]["listenerNodeIds"] = ["ES2", "ES1"]
        with self.assertRaisesRegex(CBSRoutePreparationError, "one physical end-station pair"):
            self.run_case(req)

    def test_reject_direct_end_station_path(self):
        with self.assertRaisesRegex(CBSRoutePreparationError, "no directed bridge route"):
            self.run_case(payload(), links=[link("direct", "ES1", "e1", "ES2", "e2")])

    def test_reject_request_usecase_stream_mismatch(self):
        req = payload()
        uc = usecase(req)
        uc.streams.append({"name": "surprise"})
        with self.assertRaisesRegex(CBSRoutePreparationError, "stream sets differ"):
            prepare_cbs_routes(req, uc, self.solver)

    def test_usecase_endpoints_must_match_request(self):
        req = payload()
        uc = usecase(req)
        uc.streams[0]["tlkrPssblPlcmnt"] = ["OTHER"]
        with self.assertRaisesRegex(CBSRoutePreparationError, "endpoints disagree"):
            prepare_cbs_routes(req, uc, self.solver)

    def test_generated_route_can_be_parsed_by_actual_cbs_optimizer(self):
        try:
            from optimizers.cbs import CBSOptimizer
        except ImportError:
            self.skipTest("Full CBS optimizer package unavailable in isolated patch workspace")
        req = payload()
        uc = usecase(req)
        generated = prepare_cbs_routes(req, uc, CBSOptimizer())
        path, links = CBSOptimizer().resolve_stream_path(
            uc.streams[0], uc.links, uc.fixed_routes
        )
        self.assertEqual(path, ["ES1", "SW1", "ES2"])
        self.assertEqual(len(links), 2)
        self.assertEqual(generated["f1"], HOPS)

    def test_reject_invalid_fixed_hop_not_auto_rerouted(self):
        wrong = [{**HOPS[0], "ingressPortId": "wrong"}]
        req = payload(routing="fixed", hops=wrong)
        with self.assertRaisesRegex(CBSRoutePreparationError, "cannot be consumed"):
            self.run_case(req)

    def test_two_streams_fail_without_partial_mutation(self):
        req = payload()
        bad = copy.deepcopy(req["forwardingPlane"]["streams"][0])
        bad["definition"]["id"] = "f2"
        bad["decisionVariables"]["routing"] = "fixed"
        req["forwardingPlane"]["streams"].append(bad)
        uc = usecase(req)
        before = copy.deepcopy(uc.fixed_routes)
        with self.assertRaisesRegex(CBSRoutePreparationError, "f2"):
            prepare_cbs_routes(req, uc, self.solver)
        self.assertEqual(uc.fixed_routes, before)

    def test_stable_shortest_hop_route_among_alternatives(self):
        req = payload()
        req["forwardingPlane"]["topology"]["nodes"].append({"name": "SW2", "type": "BRIDGE"})
        alt = [link("3", "ES1", "e3", "SW2", "a1"),
               link("4", "SW2", "a2", "ES2", "e4")]
        uc, generated = self.run_case(req, links=alt + LINKS)
        self.assertEqual(generated["f1"], HOPS)  # SW1 < SW2
        self.assertEqual(uc.fixed_routes["f1"]["path"], HOPS)

    def test_direct_edge_does_not_override_supported_bridge_route(self):
        uc, generated = self.run_case(payload(), links=[link("direct","ES1","e1","ES2","e2")] + LINKS)
        self.assertEqual(generated["f1"], HOPS)


if __name__ == "__main__":
    unittest.main()

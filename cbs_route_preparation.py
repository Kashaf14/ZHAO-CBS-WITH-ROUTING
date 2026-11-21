"""CBS-specific route preparation before precheck and Zhao's numerical solver.

Resolves only explicit optimize-routing requests with missing paths. Uses a
stable shortest-hop directed path for a single physical talker/listener and
bridge-only transit. This is route *preparation*, not joint routing/CBS
optimization. No client request or Zhao equations are modified.

The Phase 1 shared response validator currently rejects stream path changes.
Callers MUST reject a successful CBS-only response when generated routes still
need to be returned/applied; a usecase-local route is not a CNC route update.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy


class CBSRoutePreparationError(ValueError):
    """Unsupported, ambiguous, or unavailable route for this CBS adapter."""


def _provided_hops(stream_model: dict, listener: str, stream_id: str):
    config = stream_model.get("configuration") or {}
    path = config.get("path") or []
    listener_paths = config.get("listenerPaths") or []

    if path and listener_paths:
        raise CBSRoutePreparationError(
            f"{stream_id}: both path and listenerPaths supplied; CBS requires one unambiguous route"
        )
    if listener_paths:
        if (len(listener_paths) != 1 or
                listener_paths[0].get("listenerId") != listener or
                not listener_paths[0].get("path")):
            raise CBSRoutePreparationError(
                f"{stream_id}: CBS supports exactly one path for its single physical listener"
            )
        path = listener_paths[0]["path"]
    return deepcopy(path) if path else None


def _shortest_bridge_route(links: list[dict], node_types: dict[str, str],
                           talker: str, listener: str, stream_id: str) -> list[dict]:
    """Stable shortest-hop route using *directed port-level* links only."""
    adjacency: dict[str, list[dict]] = {}
    physical_links = set()
    for link in links:
        try:
            source, dest = link["sourceNode"], link["destNode"]
            source_port, dest_port = link["sourcePort"], link["destPort"]
        except (KeyError, TypeError) as exc:
            raise CBSRoutePreparationError(
                f"{stream_id}: topology has incomplete directed link"
            ) from exc
        edge = (source, source_port, dest, dest_port)
        if edge in physical_links:
            raise CBSRoutePreparationError(
                f"{stream_id}: ambiguous duplicate physical link {edge!r}"
            )
        physical_links.add(edge)
        if source not in node_types or dest not in node_types:
            raise CBSRoutePreparationError(f"{stream_id}: topology link references unknown node")
        adjacency.setdefault(source, []).append(link)

    for edges in adjacency.values():
        edges.sort(key=lambda link: (
            link["destNode"], link["sourcePort"], link["destPort"],
            str(link.get("id", "")),
        ))

    queue = deque([(talker, [])])
    visited = {talker}
    while queue:
        node, route_links = queue.popleft()
        if node == listener:
            if len(route_links) < 2:
                raise CBSRoutePreparationError(
                    f"{stream_id}: direct end-station path is outside current CBS adapter scope"
                )
            return [
                {
                    "hopNumber": i,
                    "bridgeId": outgoing["sourceNode"],
                    "ingressPortId": incoming["destPort"],
                    "egressPortId": outgoing["sourcePort"],
                }
                for i, (incoming, outgoing) in enumerate(zip(route_links, route_links[1:]))
            ]

        for link in adjacency.get(node, []):
            nxt = link["destNode"]
            if nxt in visited:
                continue
            # A direct end-station hop cannot be represented as a CBS StreamHop.
            # Keep searching for a supported path through one or more bridges.
            if node == talker and nxt == listener:
                continue
            if nxt != listener and node_types[nxt] != "BRIDGE":
                continue
            visited.add(nxt)
            queue.append((nxt, route_links + [link]))

    raise CBSRoutePreparationError(
        f"{stream_id}: no directed bridge route from {talker} to {listener}"
    )


def prepare_cbs_routes(request: dict, usecase, optimizer) -> dict[str, list[dict]]:
    """Prepare routes on the internal usecase; return ONLY newly generated paths.

    Existing fixed/preserve routes and supplied optimize-route paths are kept
    unchanged. If any stream fails, usecase.fixed_routes is not modified.
    A generated route must be reflected in an eventual OaaS response; the
    caller must fail closed until response validation/application supports it.
    """
    if request.get("schemaVersion") != "1.4":
        raise CBSRoutePreparationError("Expected OptimizerRequest v1.4")
    if [x.get("name") for x in request.get("mechanisms", [])] != ["CBS"]:
        raise CBSRoutePreparationError("CBS route preparation requires a CBS-only request")

    plane = request["forwardingPlane"]
    node_types = {
        node["name"]: node["type"] for node in plane["topology"]["nodes"]
    }
    models = {}
    for stream in plane["streams"]:
        if stream.get("change") == "remove":
            continue
        stream_id = stream["definition"]["id"]
        if stream_id in models:
            raise CBSRoutePreparationError(f"Duplicate stream ID: {stream_id}")
        models[stream_id] = stream

    solver_streams = {}
    for stream in usecase.streams:
        stream_id = stream["name"]
        if stream_id in solver_streams:
            raise CBSRoutePreparationError(f"Duplicate solver stream: {stream_id}")
        solver_streams[stream_id] = stream
    if set(models) != set(solver_streams):
        raise CBSRoutePreparationError("Request and CBS Usecase stream sets differ")

    prepared = deepcopy(getattr(usecase, "fixed_routes", {}) or {})
    generated = {}
    for stream_id, model in models.items():
        definition = model["definition"]
        talker = definition.get("talkerNodeId")
        listeners = definition.get("listenerNodeIds") or []
        if (not isinstance(talker, str) or not talker or
                len(listeners) != 1 or not isinstance(listeners[0], str) or
                talker not in node_types or listeners[0] not in node_types or
                node_types[talker] != "END_STATION" or
                node_types[listeners[0]] != "END_STATION"):
            raise CBSRoutePreparationError(
                f"{stream_id}: current Zhao CBS adapter requires one physical end-station pair"
            )
        listener = listeners[0]
        solver_stream = solver_streams[stream_id]
        if (solver_stream.get("tlkrPssblPlcmnt") != [talker]
                or solver_stream.get("lstnsrPssblPlcmnt") != [[listener]]):
            raise CBSRoutePreparationError(
                f"{stream_id}: CBS Usecase endpoints disagree with the request"
            )
        routing = (model.get("decisionVariables") or {}).get("routing")
        hops = _provided_hops(model, listener, stream_id)
        if routing not in ("fixed", "preserve", "optimize", None):
            raise CBSRoutePreparationError(f"{stream_id}: unsupported routing policy {routing!r}")

        if hops is None:
            if routing != "optimize":
                raise CBSRoutePreparationError(
                    f"{stream_id}: fixed/preserve/unspecified routing needs a supplied nonempty path"
                )
            hops = _shortest_bridge_route(
                usecase.links, node_types, talker, listener, stream_id
            )
            generated[stream_id] = deepcopy(hops)

        # Validate exactly the route representation that the CBS solver will read.
        # The optimizer's existing resolver checks directed physical links and
        # endpoint/port continuity; this does not run Zhao's equations.
        try:
            optimizer.resolve_stream_path(
                solver_streams[stream_id], usecase.links,
                {stream_id: {"path": hops}},
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise CBSRoutePreparationError(
                f"{stream_id}: route cannot be consumed by CBS: {exc}"
            ) from exc
        prepared[stream_id] = {"path": deepcopy(hops)}

    usecase.fixed_routes = prepared
    return generated

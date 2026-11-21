"""Serialize CBS-generated routes as sparse OptimizerResponse v1.3 stream deltas.

Only missing, existing, routing=optimize unicast routes are represented. The
result does not authorize CNC application: that is a separate adapter stage.
"""
from __future__ import annotations
from copy import deepcopy


def generated_route_changes(request: dict, generated_routes: dict[str, list[dict]]):
    """Return stream updates keyed by original OpenCNC composite StreamId.

    Deliberately never copies client string `definition.id` into response
    `streamId`. Rejects a missing/extra generated route or unsupported stream
    add/remove, rather than silently dropping an internally resolved path.
    """
    if not isinstance(generated_routes, dict):
        raise ValueError("CBS generated routes must be a stream-id map")
    output = []
    covered = set()
    ids = set()
    for model in request["forwardingPlane"]["streams"]:
        definition = model["definition"]
        name = definition["id"]
        if name in ids:
            raise ValueError(f"Duplicate CBS stream {name!r}")
        ids.add(name)
        if model.get("change") == "remove":
            continue
        cfg = model.get("configuration") or {}
        route_policy = (model.get("decisionVariables") or {}).get("routing")
        needs_route = (route_policy == "optimize" and
                       not cfg.get("path") and not cfg.get("listenerPaths"))
        if not needs_route:
            if name in generated_routes:
                raise ValueError(f"Unexpected generated route for {name!r}")
            continue
        if name not in generated_routes:
            raise ValueError(f"Missing generated CBS route for {name!r}")
        if model.get("change") == "add":
            raise ValueError(f"Stream addition route mapping is not implemented: {name!r}")
        hops = generated_routes[name]
        if not isinstance(hops, list) or not hops:
            raise ValueError(f"Generated CBS path missing or empty: {name!r}")
        if not isinstance(definition.get("streamId"), dict):
            raise ValueError(f"Missing OpenCNC composite streamId: {name!r}")
        output.append({
            "streamId": deepcopy(definition["streamId"]),
            "configuration": {"path": deepcopy(hops)},
        })
        covered.add(name)
    if covered != set(generated_routes):
        raise ValueError(f"Unknown/removed generated routes: {sorted(set(generated_routes)-covered)!r}")
    return output


def generated_route_change_sets(request: dict, generated_routes: dict[str, list[dict]]) -> dict:
    """Classify generated paths into stream updates vs complete ADD_STREAM models.

    This is framework serialization ONLY, not shared contract validation or
    authorization to apply changes in CNC. Keep new streams distinct even when
    their definitions are staged in the preserved input ForwardingPlaneModel.
    """
    if not isinstance(generated_routes, dict):
        raise ValueError("CBS generated routes must be a stream-id map")

    models = request["forwardingPlane"]["streams"]
    names = [m["definition"]["id"] for m in models]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate CBS stream identity")
    composite_ids = []
    for model in models:
        stream_id = model["definition"].get("streamId")
        if (not isinstance(stream_id, dict) or
                not isinstance(stream_id.get("macAddress"), str) or
                not isinstance(stream_id.get("uniqueId"), int) or
                isinstance(stream_id.get("uniqueId"), bool)):
            raise ValueError("CBS stream requires a complete composite streamId")
        composite_ids.append((stream_id["macAddress"], stream_id["uniqueId"]))
    if len(composite_ids) != len(set(composite_ids)):
        raise ValueError("Duplicate CBS composite streamId")

    # The original helper remains unchanged for existing-stream route deltas.
    # Do not relabel 'add' as an existing stream merely because CNC stages its
    # definition in the input model.
    existing_models = [m for m in models if m.get("change") != "add"]
    added_models = [m for m in models if m.get("change") == "add"]
    added_names = {m["definition"]["id"] for m in added_models}
    existing_routes = {k: v for k, v in generated_routes.items() if k not in added_names}
    existing_updates = generated_route_changes(
        {"forwardingPlane": {"streams": existing_models}}, existing_routes
    )

    additions = []
    if added_models:
        task = request.get("optimizationTask") or {}
        scope_ids = set((task.get("scope") or {}).get("streamIds") or [])
        if task.get("intent") != "ADD_STREAM" or not scope_ids:
            raise ValueError("Stream additions require ADD_STREAM intent and explicit stream scope")
        if added_names != scope_ids:
            raise ValueError("ADD_STREAM scope must match the new streams exactly")
        for model in added_models:
            definition = model["definition"]
            name = definition["id"]
            if name not in scope_ids:
                raise ValueError(f"Stream addition outside ADD_STREAM scope: {name!r}")
            cfg = model.get("configuration") or {}
            if ((model.get("decisionVariables") or {}).get("routing") != "optimize" or
                    cfg.get("path") or cfg.get("listenerPaths")):
                raise ValueError(f"Stream addition must have a missing optimize-route: {name!r}")
            if name not in generated_routes:
                raise ValueError(f"Missing generated CBS route for stream addition {name!r}")
            hops = generated_routes[name]
            if not isinstance(hops, list) or not hops:
                raise ValueError(f"Generated CBS path missing or empty: {name!r}")
            if not isinstance(definition.get("streamId"), dict):
                raise ValueError(f"Missing OpenCNC composite streamId: {name!r}")

            # Only native-shaped StreamModel fields are serialized. 'change'
            # and 'decisionVariables' are OaaS request directives, not fields
            # in the v1.3 response streamModel schema.
            addition_cfg = deepcopy(cfg)
            addition_cfg["path"] = deepcopy(hops)
            additions.append({
                "definition": deepcopy(definition),
                "configuration": addition_cfg,
            })

    changes = {}
    if existing_updates:
        changes["streams"] = existing_updates
    if additions:
        changes["streamAdditions"] = additions
    return changes

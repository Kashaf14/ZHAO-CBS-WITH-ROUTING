"""Convert CBS optimization results into standardized OaaS responses.

Supports response versions v1.2 and v1.3. The CBS service currently uses
v1.3, which returns only the required configuration changes.

Maps already-prepared physical queue configurations to response fields.
Does not initiate or modify CBS calculations.
"""

from __future__ import annotations

import os
from copy import deepcopy

from cbs_route_response import generated_route_change_sets
from typing import Any
from optimizer_metadata import OPTIMIZER_ID, NAME, VERSION

from algorithm.cbs_configuration import (
    CBSResponseConversionError, PreparedCBSQueue,
)


def _config_port(config: dict[str, Any], node_id: str, port_id: str) -> dict[str, Any]:
    """Return or create the output port configuration without replacing others."""
    nodes = [n for n in config["nodeConfigs"] if n["nodeId"] == node_id]
    if len(nodes) > 1:
        raise CBSResponseConversionError(f"Duplicate config node: {node_id}")
    if not nodes:
        nodes = [{"nodeId": node_id, "portConfigs": []}]
        config["nodeConfigs"].append(nodes[0])
    node = nodes[0]
    ports = [p for p in node.setdefault("portConfigs", []) if p["portId"] == port_id]
    if len(ports) > 1:
        raise CBSResponseConversionError(f"Duplicate config port: {node_id}/{port_id}")
    if not ports:
        ports = [{"portId": port_id}]
        node["portConfigs"].append(ports[0])
    return ports[0]


def _desired_cbs(queue: PreparedCBSQueue) -> dict[str, Any]:
    """Map finished device-compatible values to contract field names."""
    return {
        "adminState": "ENABLED",
        "adminIdleSlopeBps": queue.idle_slope_bps,
        "sendSlopeBps": queue.send_slope_magnitude_bps,
        "hiCreditBytes": queue.hi_credit_bytes,
        "loCreditBytes": queue.lo_credit_magnitude_bytes,
    }


def _require_prepared_groups(legacy: dict[str, Any], prepared: list[PreparedCBSQueue]) -> None:
    """Reject incomplete or mismatched configuration results without calculating."""
    expected = [(g["node"], g["port"], g["srClass"]) for g in legacy["portClassGroups"]]
    actual = [(q.node_id, q.port_id, q.traffic_class) for q in prepared]
    if not expected or len(expected) != len(set(expected)) or sorted(expected) != sorted(actual):
        raise CBSResponseConversionError("Missing or mismatched prepared CBS configuration")


def _full_plane_from_result(
    request: dict[str, Any], legacy: dict[str, Any], prepared: list[PreparedCBSQueue],
) -> dict[str, Any]:
    original = request["forwardingPlane"]
    plane = deepcopy(original)
    task_id = request["optimizationTask"]["taskId"]

    original_meta = original.get("metadata") or {}
    plane["metadata"] = {
        **deepcopy(original_meta),
        "modelId": original_meta.get("modelId", f"{task_id}-cbs-candidate"),
        "revision": original_meta.get("revision", 0) + 1,
        "description": "CBS-generated full desired state; independent validation and CNC deployability not established",
    }
    plane["configuration"] = deepcopy(original.get("configuration") or {"nodeConfigs": []})
    plane["configuration"].setdefault("nodeConfigs", [])
    plane["configuration"].setdefault("configId", f"{task_id}-cbs-proposed")

    # Response StreamModel is not request StreamModel: decisionVariables/change
    # remain input-only. Keep every stream, including untargeted M2 flows.
    plane["streams"] = []
    for incoming in original["streams"]:
        definition = deepcopy(incoming["definition"])
        configuration = deepcopy(incoming.get("configuration") or {})
        configuration["streamId"] = deepcopy(definition["streamId"])
        plane["streams"].append({"definition": definition, "configuration": configuration})

    _require_prepared_groups(legacy, prepared)
    for queue in prepared:
        node_id, port_id, traffic_class = queue.node_id, queue.port_id, queue.traffic_class
        queue_id = queue.queue_id
        label = f"{node_id}/{port_id}/{traffic_class}"
        cfg = _config_port(plane["configuration"], node_id, port_id)
        queue_cfgs = cfg.setdefault("queueConfigs", [])
        same_queue = [q for q in queue_cfgs if q["queueId"] == queue_id]
        if len(same_queue) > 1:
            raise CBSResponseConversionError(f"Duplicate queue config for {label} queue {queue_id}")
        if same_queue:
            queue_cfg = same_queue[0]
        else:
            queue_cfg = {"queueId": queue_id}
            queue_cfgs.append(queue_cfg)
        cbs_items = queue_cfg.setdefault("cbs", [])
        cbs_items[:] = [x for x in cbs_items if x["egressQueueId"] != queue_id]
        cbs_items.append({
            "egressQueueId": queue_id,
            **_desired_cbs(queue),
            "description": (
                f"{traffic_class} result; idleSlope rounded up to hardware granularity; "
                "hi/lo credits rounded outward to bytes; sendSlope/loCredit stored "
                "as unsigned magnitudes (provisional CNC encoding)"
            ),
        })
        queue_cfg["scheduling"] = "CREDIT_BASED_SHAPER"

    return plane


def build_cbs_response_v12(
    request: dict[str, Any], legacy: dict[str, Any],
    *, prepared: list[PreparedCBSQueue],
) -> dict[str, Any]:
    """Convert actual CBS service output to strict Optimizer Response v1.2."""
    if request.get("schemaVersion") != "1.4":
        raise CBSResponseConversionError("Expected standardized CBS Request Contract v1.4")
    if any(x.get("name") == "CBS" for x in request.get("mechanisms", [])) is False:
        raise CBSResponseConversionError("CBS response adapter requires mechanism CBS")
    if legacy.get("optimizer") != "CBS":
        raise CBSResponseConversionError("Legacy result does not identify CBS optimizer")

    meta = request["forwardingPlane"].get("metadata") or {}
    baseline = ({"modelId": meta["modelId"], "revision": meta["revision"]}
                if "modelId" in meta and "revision" in meta else None)
    rows = legacy.get("sharedStreamResults") or []
    all_met = bool(rows) and all(row.get("deadlineSatisfied") is True for row in rows)
    # If no solution found, do not assert *infeasibility* without proof.
    status = "success" if all_met and legacy.get("allDeadlinesSatisfied") is True else "noSolutionFound"
    details = {"cbsDiagnostics": deepcopy(legacy)}
    constraints = [{
        "name": f"deadline:{row['stream']}",
        "status": "satisfied" if row["deadlineSatisfied"] else "violated",
        "affectedResourceIds": [row["stream"]],
        "message": f"CBS analytical path delay {row['sharedPathDelayNs']:.9f} ns; deadline {row['deadlineNs']} ns",
    } for row in rows]
    outcome = {
        "feasibility": "feasible" if status == "success" else "unknown",
        "optimality": "notProven",
        "terminationReason": "completed",
        "deadlineStatus": "satisfied" if status == "success" else "violated",
        "configurationChanged": status == "success",
        "constraints": constraints,
        "details": details,
    }
    warnings = [{
        "code": "NOT_INDEPENDENTLY_VALIDATED",
        "message": "Deadlines and feasibility are CBS optimizer-reported; no independent CNC/hardware validation has run.",
    }]
    if status == "success":
        plane = _full_plane_from_result(request, legacy, prepared)
        solution = {"type": "configuration", "representation": "full", "forwardingPlane": plane}
        warnings.append({
            "code": "CBS_SIGNED_MAGNITUDES_PROVISIONAL",
            "message": "sendSlopeBps/loCreditBytes are nonnegative magnitudes of mathematically nonpositive values in CNC uint64 fields. Confirm end-to-end CNC interpretation before deployment.",
        })
    else:
        solution = None
        warnings.append({
            "code": "NO_FEASIBLE_CBS_CANDIDATE",
            "message": "Optimizer did not report all deadlines met; infeasibility is not proven.",
        })

    return {
        "schemaVersion": "1.2",
        "optimizer": {
            "id": "CBS",
            "name": "CBS",
            "version": os.environ.get("CBS_OPTIMIZER_VERSION", "unversioned"),
            "type": "analytical",
        },
        "optimizedAgainst": baseline,
        "status": status,
        "solution": solution,
        "outcome": outcome,
        "kpis": deepcopy(legacy.get("kpis") or {}),
        "warnings": warnings,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Contract v1.3: new entry point. The v1.2 path above remains available until
# the CBS service and Manager can be switched to v1.3 together.
# ---------------------------------------------------------------------------


def _partial_changes_from_result(
    request: dict[str, Any], legacy: dict[str, Any], prepared: list[PreparedCBSQueue],
) -> dict[str, Any]:
    """Map finished configurations to identifier-addressed changes only."""
    _require_prepared_groups(legacy, prepared)
    baseline_ports = {
        (node["nodeId"], port["portId"]): port
        for node in (request["forwardingPlane"].get("configuration") or {}).get("nodeConfigs", [])
        for port in node.get("portConfigs", [])
    }
    delta_config = {"nodeConfigs": []}
    for queue in prepared:
        node_id, port_id, queue_id = queue.node_id, queue.port_id, queue.queue_id
        label = f"{node_id}/{port_id}/{queue.traffic_class}"
        port_cfg = baseline_ports[(node_id, port_id)]
        desired_cbs = _desired_cbs(queue)
        baseline_queues = [q for q in port_cfg.get("queueConfigs", []) if q["queueId"] == queue_id]
        if len(baseline_queues) > 1:
            raise CBSResponseConversionError(f"Duplicate input queue config for {label} queue {queue_id}")
        existing_queue = baseline_queues[0] if baseline_queues else {}
        baseline_cbs = [c for c in existing_queue.get("cbs", []) if c["egressQueueId"] == queue_id]
        if len(baseline_cbs) > 1:
            raise CBSResponseConversionError(f"Duplicate input CBS config for {label} queue {queue_id}")
        previous_cbs = baseline_cbs[0] if baseline_cbs else {}
        changed_fields = {k: v for k, v in desired_cbs.items() if previous_cbs.get(k) != v}

        queue_change: dict[str, Any] = {"queueId": queue_id}
        if changed_fields:
            queue_change["cbs"] = [{"egressQueueId": queue_id, **changed_fields}]
        if existing_queue.get("scheduling") != "CREDIT_BASED_SHAPER":
            queue_change["scheduling"] = "CREDIT_BASED_SHAPER"
        if len(queue_change) == 1:
            continue  # Fully identical, hence a successful no-change candidate.

        port_change = _config_port(delta_config, node_id, port_id)
        port_change.setdefault("queueConfigs", []).append(queue_change)

    return {"configuration": delta_config} if delta_config["nodeConfigs"] else {}


def build_cbs_response_v13(
    request: dict[str, Any], legacy: dict[str, Any],
    *, prepared: list[PreparedCBSQueue],
    generated_routes: dict[str, list[dict]] | None = None,
) -> dict[str, Any]:
    """Convert the CBS optimizer's legacy result to sparse Response Contract v1.3."""
    if request.get("schemaVersion") != "1.4":
        raise CBSResponseConversionError("Expected standardized CBS Request Contract v1.4")
    if not any(x.get("name") == "CBS" for x in request.get("mechanisms", [])):
        raise CBSResponseConversionError("CBS response adapter requires mechanism CBS")
    if legacy.get("optimizer") != "CBS":
        raise CBSResponseConversionError("Legacy result does not identify CBS optimizer")

    meta = request["forwardingPlane"].get("metadata") or {}
    baseline = ({"modelId": meta["modelId"], "revision": meta["revision"]}
                if "modelId" in meta and "revision" in meta else None)
    rows = legacy.get("sharedStreamResults") or []
    all_met = bool(rows) and all(row.get("deadlineSatisfied") is True for row in rows)
    status = "success" if all_met and legacy.get("allDeadlinesSatisfied") is True else "noSolutionFound"
    details = {"cbsDiagnostics": deepcopy(legacy)}
    constraints = [{
        "name": f"deadline:{row['stream']}",
        "status": "satisfied" if row["deadlineSatisfied"] else "violated",
        "affectedResourceIds": [row["stream"]],
        "message": f"CBS analytical path delay {row['sharedPathDelayNs']:.9f} ns; deadline {row['deadlineNs']} ns",
    } for row in rows]
    outcome: dict[str, Any] = {
        "feasibility": "feasible" if status == "success" else "unknown",
        "optimality": "notProven",
        "terminationReason": "completed",
        "deadlineStatus": "satisfied" if status == "success" else "violated",
        "configurationChanged": False,
        "constraints": constraints,
        "details": details,
    }
    warnings = [{
        "code": "NOT_INDEPENDENTLY_VALIDATED",
        "message": "Deadlines and feasibility are CBS optimizer-reported; no independent CNC/hardware validation has run.",
    }]
    if status == "success":
        changes = _partial_changes_from_result(request, legacy, prepared)
        if generated_routes is not None:
            route_changes = generated_route_change_sets(request, generated_routes)
            if route_changes:
                changes.update(route_changes)
                warnings.append({
                    "code": "CNC_PATH_APPLICATION_NOT_VERIFIED",
                    "message": "Generated routes are included in the optimizer result. Application by the CNC consumer is not verified by the CBS service.",
                })
        outcome["configurationChanged"] = bool(changes)
        solution = {"type": "configuration", "representation": "partial", "changes": changes}
    else:
        solution = None
        warnings.append({
            "code": "NO_FEASIBLE_CBS_CANDIDATE",
            "message": "Optimizer did not report all deadlines met; infeasibility is not proven.",
        })

    return {
        "schemaVersion": "1.3",
        "optimizer": {
            "id": OPTIMIZER_ID,
            "name": NAME,
            "version": VERSION,
            "type": "analytical",
        },
        "optimizedAgainst": baseline,
        "status": status,
        "solution": solution,
        "outcome": outcome,
        "kpis": deepcopy(legacy.get("kpis") or {}),
        "warnings": warnings,
        "errors": []
    }

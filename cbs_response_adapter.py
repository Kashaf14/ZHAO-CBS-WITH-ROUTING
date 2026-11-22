"""Convert CBS optimization results into standardized OaaS responses.

Supports response versions v1.2 and v1.3. The CBS service currently uses
v1.3, which returns only the required configuration changes.

Maps traffic priorities (PCP) to physical queues using the request's
port configuration. Does not modify CBS calculations.
"""

from __future__ import annotations

import math
import os
from copy import deepcopy

from cbs_route_response import generated_route_change_sets
from typing import Any
from optimizer_metadata import OPTIMIZER_ID, NAME, VERSION

class CBSResponseConversionError(ValueError):
    """CBS calculations exist but cannot be converted to a valid configuration."""


def _port_index(topology: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index = {}
    for node in topology["nodes"]:
        for port in node["ports"]:
            key = (node["name"], port["id"])
            if key in index:
                raise CBSResponseConversionError(f"Duplicate topology port {key}")
            index[key] = port
    return index


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


def _mapped_egress_queue(port_cfg: dict[str, Any], pcp: int, port_label: str) -> int:
    if port_cfg.get("pcpMappingEnabled") is not True:
        raise CBSResponseConversionError(
            f"Missing enabled PCP-to-queue mapping for {port_label}. "
            f"Provide forwardingPlane.configuration.nodeConfigs[].portConfigs[] "
            f"with pcpMappingEnabled=true and pcpConfig.trafficClassTable "
            f"(priority={pcp}, egressQueueId=<physical queue>)."
        )
    table = (port_cfg.get("pcpConfig") or {}).get("trafficClassTable", [])
    queues = {entry["egressQueueId"] for entry in table if entry.get("priority") == pcp}
    if len(queues) != 1:
        raise CBSResponseConversionError(
            f"Expected one physical queue mapping for PCP {pcp} at {port_label}; "
            f"found {len(queues)} distinct queues"
        )
    return next(iter(queues))


def _get_device_cbs_capabilities(
    request: dict[str, Any], node_id: str, port_id: str,
) -> dict[str, Any]:
    for device in (request.get("capabilities") or {}).get("devices", []):
        if device.get("nodeId") != node_id:
            continue
        result = dict((device.get("mechanisms") or {}).get("CBS") or {})
        override = ((device.get("ports") or {}).get(port_id) or {})
        result.update((override.get("mechanisms") or {}).get("CBS") or {})
        return result
    return {}


def _admin_idle_slope(raw_slope: Any, speed_bps: int, caps: dict[str, Any], label: str) -> int:
    if not isinstance(raw_slope, (int, float)) or not math.isfinite(raw_slope) or raw_slope < 0:
        raise CBSResponseConversionError(f"Invalid CBS idleSlope at {label}: {raw_slope!r}")
    if caps.get("supported") is False:
        raise CBSResponseConversionError(f"CBS is marked unsupported at {label}")
    step = caps.get("idleSlopeGranularityBps", 1)
    if not isinstance(step, (int, float)) or step <= 0 or not math.isfinite(step):
        raise CBSResponseConversionError(f"Invalid CBS slope granularity at {label}")
    # Ceil rather than truncate: don't under-reserve vs. paper's bound.
    rounded = int(math.ceil(math.ceil(raw_slope / step - 1e-12) * step))
    if rounded >= speed_bps:
        raise CBSResponseConversionError(f"CBS idleSlope {rounded} >= link speed at {label}")
    limit = caps.get("maxIdleSlopeBps")
    if limit is not None and rounded > limit:
        raise CBSResponseConversionError(f"CBS idleSlope {rounded} exceeds hardware limit at {label}")
    return rounded


def _cbs_configured_registers(
    group: dict[str, Any], idle_slope_bps: int, speed_bps: int, label: str,
) -> dict[str, int]:
    """Convert computed CBS results to unsigned OpenCNC register magnitudes.

    A hardware-granularity adjustment to idleSlope also changes sendSlope and
    the credit bounds, so derive all *configured* fields from the rounded
    idleSlope rather than combining raw credits with a different configured
    slope. The original (unrounded) calculations remain in cbsDiagnostics.

    Rounding policy (outwards, in bytes):
       hiCreditBytes = ceil(configured_hi_credit_bits / 8)
       loCreditBytes = ceil(abs(configured_lo_credit_bits) / 8)

    sendSlope and loCredit are mathematically <= 0.  OpenCNC's pinned
    qav.proto uses uint64; encode their NONNEGATIVE MAGNITUDES in JSON. This
    convention has been confirmed by Hamza for the CNC protobuf encoding;
    independent deployability validation remains separate.
    """
    raw = group["sharedCBS"]
    for name in ("sendSlopeBps", "creditMinBits", "creditMaxBits"):
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CBSResponseConversionError(f"Invalid CBS {name} at {label}: {value!r}")
    if raw["sendSlopeBps"] > 0 or raw["creditMinBits"] > 0 or raw["creditMaxBits"] < 0:
        raise CBSResponseConversionError(f"Inconsistent CBS credit/slope signs at {label}")

    # Both are frame lengths in BITS from the original CBS optimizer.
    frame_bits = {}
    for key in ("maxFrameBits", "blockingFrameBits"):
        value = group[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or int(value) != value):
            raise CBSResponseConversionError(f"Invalid CBS {key} at {label}: {value!r}")
        frame_bits[key] = int(value)

    # All operands are integers. Use exact ceiling division (no floating-point
    # rounding errors near byte boundaries).
    denominator = speed_bps * 8
    send_slope_magnitude_bps = speed_bps - idle_slope_bps
    hi_credit_bytes = (
        idle_slope_bps * frame_bits["blockingFrameBits"] + denominator - 1
    ) // denominator
    lo_credit_magnitude_bytes = (
        send_slope_magnitude_bps * frame_bits["maxFrameBits"] + denominator - 1
    ) // denominator

    return {
        "sendSlopeBps": send_slope_magnitude_bps,
        "hiCreditBytes": hi_credit_bytes,
        "loCreditBytes": lo_credit_magnitude_bytes,
    }


def _full_plane_from_result(request: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
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

    streams_by_id = {s["definition"]["id"]: s for s in original["streams"]}
    ports = _port_index(original["topology"])
    seen_groups = set()

    for group in legacy["portClassGroups"]:
        node_id, port_id, traffic_class = group["node"], group["port"], group["srClass"]
        label = f"{node_id}/{port_id}/{traffic_class}"
        key = (node_id, port_id, traffic_class)
        if key in seen_groups:
            raise CBSResponseConversionError(f"Duplicate CBS group {label}")
        seen_groups.add(key)
        if (node_id, port_id) not in ports:
            raise CBSResponseConversionError(f"CBS result references nonexistent port {label}")
        if not group.get("streams"):
            raise CBSResponseConversionError(f"Empty CBS port/class group {label}")

        pcps = set()
        for stream_id in group["streams"]:
            if stream_id not in streams_by_id:
                raise CBSResponseConversionError(f"Unknown stream {stream_id} in {label}")
            stream = streams_by_id[stream_id]
            if stream["definition"].get("trafficClassId") != traffic_class:
                raise CBSResponseConversionError(f"Traffic class mismatch for {stream_id} in {label}")
            pcp = (stream.get("configuration") or {}).get("pcp")
            if not isinstance(pcp, int):
                raise CBSResponseConversionError(f"Stream {stream_id} has no PCP")
            pcps.add(pcp)
        if len(pcps) != 1:
            raise CBSResponseConversionError(f"Multiple PCPs on shared CBS group {label}: {sorted(pcps)}")
        pcp = next(iter(pcps))

        # Require an EXPLICIT physical queue assignment supplied by CNC/test
        # input; never infer queueId from PCP numerical equality.
        original_node_cfgs = (original.get("configuration") or {}).get("nodeConfigs", [])
        matches = [p for n in original_node_cfgs if n["nodeId"] == node_id
                   for p in n.get("portConfigs", []) if p["portId"] == port_id]
        if len(matches) != 1:
            raise CBSResponseConversionError(f"Missing/ambiguous input port config {label}")
        queue_id = _mapped_egress_queue(matches[0], pcp, label)
        max_queues = ports[(node_id, port_id)]["number_of_queues"]
        if not 0 <= queue_id < max_queues:
            raise CBSResponseConversionError(f"Queue {queue_id} outside [0,{max_queues - 1}] at {label}")

        caps = _get_device_cbs_capabilities(request, node_id, port_id)
        supported_queues = caps.get("supportedQueues")
        if supported_queues is not None and queue_id not in supported_queues and str(queue_id) not in supported_queues:
            raise CBSResponseConversionError(f"Queue {queue_id} does not support CBS at {label}")

        # Preserve the raw CBS mathematical values in diagnostics; produce
        # hardware-compatible rounded/unsigned configuration fields here.
        raw_slope = group["sharedCBS"]["idleSlopeBps"]
        speed_bps = int(ports[(node_id, port_id)]["capabilities"]["port_speed"] * 1_000_000)
        slope = _admin_idle_slope(raw_slope, speed_bps, caps, label)
        registers = _cbs_configured_registers(group, slope, speed_bps, label)
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
            "adminState": "ENABLED",
            "adminIdleSlopeBps": slope,
            **registers,
            "description": (
                f"{traffic_class} result; idleSlope rounded up to hardware granularity; "
                "hi/lo credits rounded outward to bytes; sendSlope/loCredit stored "
                "as unsigned magnitudes (provisional CNC encoding)"
            ),
        })
        queue_cfg["scheduling"] = "CREDIT_BASED_SHAPER"

    if not seen_groups:
        raise CBSResponseConversionError("CBS optimizer returned no port-class groups")
    return plane


def build_cbs_response_v12(request: dict[str, Any], legacy: dict[str, Any]) -> dict[str, Any]:
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
        plane = _full_plane_from_result(request, legacy)
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
    request: dict[str, Any], legacy: dict[str, Any]
) -> dict[str, Any]:
    """Build identifier-addressed CBS changes without cloning the input model.

    Only configuration values that differ from the baseline are emitted.
    All other topology, stream definitions, port settings, PCP tables and
    queue configuration remain the CNC adapter's responsibility to preserve.
    """
    original = request["forwardingPlane"]
    topology_ports = _port_index(original["topology"])
    original_node_cfgs = (original.get("configuration") or {}).get("nodeConfigs", [])

    # Index all existing configurations, explicitly rejecting ambiguous keys.
    baseline_ports: dict[tuple[str, str], dict[str, Any]] = {}
    known_nodes: set[str] = set()
    for node in original_node_cfgs:
        node_id = node["nodeId"]
        if node_id in known_nodes:
            raise CBSResponseConversionError(f"Duplicate input config node: {node_id}")
        known_nodes.add(node_id)
        for port in node.get("portConfigs", []):
            key = (node_id, port["portId"])
            if key in baseline_ports:
                raise CBSResponseConversionError(f"Duplicate input port config: {key}")
            baseline_ports[key] = port

    streams_by_id = {}
    for stream in original["streams"]:
        stream_id = stream["definition"]["id"]
        if stream_id in streams_by_id:
            raise CBSResponseConversionError(f"Duplicate input stream: {stream_id}")
        streams_by_id[stream_id] = stream

    delta_config = {"nodeConfigs": []}
    seen_groups: set[tuple[str, str, str]] = set()
    seen_queue_targets: set[tuple[str, str, int]] = set()
    # Proposed slope after hardware rounding, indexed by physical queue per port.
    configured_slopes_by_port: dict[tuple[str, str], dict[int, int]] = {}
    configured_port_speeds: dict[tuple[str, str], int] = {}
    for group in legacy["portClassGroups"]:
        node_id, port_id, traffic_class = group["node"], group["port"], group["srClass"]
        label = f"{node_id}/{port_id}/{traffic_class}"
        group_key = (node_id, port_id, traffic_class)
        if group_key in seen_groups:
            raise CBSResponseConversionError(f"Duplicate CBS group {label}")
        seen_groups.add(group_key)
        if (node_id, port_id) not in topology_ports:
            raise CBSResponseConversionError(f"CBS result references nonexistent port {label}")
        if not group.get("streams"):
            raise CBSResponseConversionError(f"Empty CBS port/class group {label}")

        pcps: set[int] = set()
        for stream_id in group["streams"]:
            stream = streams_by_id.get(stream_id)
            if stream is None:
                raise CBSResponseConversionError(f"Unknown stream {stream_id} in {label}")
            if stream["definition"].get("trafficClassId") != traffic_class:
                raise CBSResponseConversionError(f"Traffic class mismatch for {stream_id} in {label}")
            pcp = (stream.get("configuration") or {}).get("pcp")
            if isinstance(pcp, bool) or not isinstance(pcp, int) or not 0 <= pcp <= 7:
                raise CBSResponseConversionError(f"Stream {stream_id} has no valid PCP")
            pcps.add(pcp)
        if len(pcps) != 1:
            raise CBSResponseConversionError(
                f"Multiple PCPs on shared CBS group {label}: {sorted(pcps)}"
            )
        port_cfg = baseline_ports.get((node_id, port_id))
        if port_cfg is None:
            raise CBSResponseConversionError(f"Missing input port config {label}")
        queue_id = _mapped_egress_queue(port_cfg, next(iter(pcps)), label)
        max_queues = topology_ports[(node_id, port_id)]["number_of_queues"]
        if isinstance(queue_id, bool) or not isinstance(queue_id, int) or not 0 <= queue_id < max_queues:
            raise CBSResponseConversionError(
                f"Queue {queue_id} outside [0,{max_queues - 1}] at {label}"
            )
        queue_target = (node_id, port_id, queue_id)
        if queue_target in seen_queue_targets:
            raise CBSResponseConversionError(
                f"More than one CBS class targets physical queue {node_id}/{port_id}/{queue_id}; "
                "a combined queue-level CBS solution is required"
            )
        seen_queue_targets.add(queue_target)

        caps = _get_device_cbs_capabilities(request, node_id, port_id)
        supported_queues = caps.get("supportedQueues")
        if (supported_queues is not None and queue_id not in supported_queues
                and str(queue_id) not in supported_queues):
            raise CBSResponseConversionError(f"Queue {queue_id} does not support CBS at {label}")
        speed_mbps = topology_ports[(node_id, port_id)]["capabilities"]["port_speed"]
        if (isinstance(speed_mbps, bool) or not isinstance(speed_mbps, (int, float))
                or not math.isfinite(speed_mbps) or speed_mbps <= 0):
            raise CBSResponseConversionError(f"Invalid link speed at {label}: {speed_mbps!r}")
        speed_bps = int(speed_mbps * 1_000_000)
        slope = _admin_idle_slope(group["sharedCBS"]["idleSlopeBps"], speed_bps, caps, label)
        port_key = (node_id, port_id)
        prior_speed = configured_port_speeds.get(port_key)
        if prior_speed is not None and prior_speed != speed_bps:
            raise CBSResponseConversionError(f"Conflicting link speeds at {node_id}/{port_id}")
        configured_port_speeds[port_key] = speed_bps
        # seen_queue_targets above already prevents two optimized classes sharing a queue.
        configured_slopes_by_port.setdefault(port_key, {})[queue_id] = slope
        registers = _cbs_configured_registers(group, slope, speed_bps, label)
        desired_cbs = {
            "adminState": "ENABLED",
            "adminIdleSlopeBps": slope,
            **registers,
        }

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

    # The physical link carries all enabled CBS queues, including unchanged
    # non-target queues retained by this *partial* response. Checking one
    # idleSlope at a time is insufficient after hardware-granularity rounding.
    for (node_id, port_id), proposed in configured_slopes_by_port.items():
        effective = dict(proposed)
        configured_queues = baseline_ports[(node_id, port_id)].get("queueConfigs", [])
        seen_existing_queues: set[int] = set()
        for queue in configured_queues:
            existing_queue_id = queue.get("queueId")
            if (isinstance(existing_queue_id, bool) or not isinstance(existing_queue_id, int)
                    or existing_queue_id in seen_existing_queues):
                raise CBSResponseConversionError(
                    f"Invalid/duplicate existing queue at {node_id}/{port_id}"
                )
            seen_existing_queues.add(existing_queue_id)
            if existing_queue_id in proposed:
                continue  # Replaced by this solution; never double-count old slope.

            matching = [entry for entry in queue.get("cbs", [])
                        if entry.get("egressQueueId") == existing_queue_id]
            enabled = [entry for entry in matching
                       if entry.get("adminState") == "ENABLED"]
            if not enabled and queue.get("scheduling") != "CREDIT_BASED_SHAPER":
                continue
            if not enabled and matching and all(
                entry.get("adminState") == "DISABLED" for entry in matching
            ):
                continue  # Explicitly disabled CBS has no active reservation.
            if len(enabled) != 1:
                raise CBSResponseConversionError(
                    f"Cannot verify existing CBS reservation at "
                    f"{node_id}/{port_id}/queue {existing_queue_id}"
                )
            existing_slope = enabled[0].get("adminIdleSlopeBps")
            if (isinstance(existing_slope, bool) or not isinstance(existing_slope, int)
                    or existing_slope < 0):
                raise CBSResponseConversionError(
                    f"Invalid existing CBS idleSlope at "
                    f"{node_id}/{port_id}/queue {existing_queue_id}"
                )
            effective[existing_queue_id] = existing_slope

        total_slope = sum(effective.values())
        link_speed = configured_port_speeds[(node_id, port_id)]
        # Strictly below link speed preserves capacity for lower-priority BE.
        if total_slope >= link_speed:
            raise CBSResponseConversionError(
                f"Aggregate configured CBS idleSlope {total_slope} bps at "
                f"{node_id}/{port_id} meets/exceeds link speed {link_speed} bps "
                f"after hardware rounding (queues: {sorted(effective)})"
            )

    if not seen_groups:
        raise CBSResponseConversionError("CBS optimizer returned no port-class groups")
    return {"configuration": delta_config} if delta_config["nodeConfigs"] else {}


def build_cbs_response_v13(
    request: dict[str, Any], legacy: dict[str, Any],
    *, generated_routes: dict[str, list[dict]] | None = None,
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
        changes = _partial_changes_from_result(request, legacy)
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

"""Adapt CBS optimization requests to Hamza's external Usecase format.

Supports standardized OptimizerRequest v1.4 and a legacy internal input path
retained for historical tests. The standalone CBS service uses this adapter
before invoking the solver; it does not select or manage optimizers.
"""
from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import networkx as nx

LIBRARY_PATH = Path(
    os.environ.get(
        "TSN_LIBRARY_PATH",
        "/Users/kashaf/Desktop/THESIS/TSN_optimizer_platform-Hamza",
    )
)

if str(LIBRARY_PATH) not in sys.path:
    sys.path.insert(0, str(LIBRARY_PATH))

from usecase_generator import Usecase


def _mechanism_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in payload.get("mechanisms", []):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _applications_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in payload.get("applications", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _application_candidates(app_id: str, applications: dict[str, dict[str, Any]]) -> list[str]:
    try:
        placement = applications[app_id]["placement"]
    except KeyError as exc:
        raise ValueError(f"Unknown application endpoint: {app_id}") from exc

    mode = placement.get("mode")
    if mode == "fixed":
        node = placement.get("node")
        if not node:
            raise ValueError(f"Application '{app_id}' has fixed placement without node")
        return [node]
    if mode == "optimizer":
        candidates = placement.get("candidates") or []
        if not candidates:
            raise ValueError(f"Application '{app_id}' has optimizer placement without candidates")
        return list(candidates)
    raise ValueError(f"Unsupported placement mode for application '{app_id}': {mode}")


def _pcp_mapping_by_port(forwarding_plane: dict[str, Any]) -> dict[tuple[str, str], dict[str, int]]:
    result: dict[tuple[str, str], dict[str, int]] = {}
    config = forwarding_plane.get("configuration") or {}
    for node_cfg in config.get("nodeConfigs", []):
        node_id = node_cfg.get("nodeId")
        for port_cfg in node_cfg.get("portConfigs", []):
            port_id = port_cfg.get("portId")
            table = (port_cfg.get("pcpConfig") or {}).get("trafficClassTable", [])
            if node_id and port_id and table:
                result[(node_id, port_id)] = {
                    str(entry["priority"]): entry["egressQueueId"]
                    for entry in table
                }
    return result


def _device_transit_time(payload: dict[str, Any], node_name: str) -> int:
    for device in (payload.get("capabilities") or {}).get("devices", []):
        if device.get("node") != node_name:
            continue
        transit = (device.get("forwarding") or {}).get("transitTime") or {}
        if "fixedNs" in transit:
            return int(transit["fixedNs"])
        # For a bounded device delay use the conservative upper bound internally.
        if "maxNs" in transit:
            return int(transit["maxNs"])
    return 0


def _port_to_legacy(
    node_name: str,
    port: dict[str, Any],
    pcp_mapping: dict[tuple[str, str], dict[str, int]],
) -> dict[str, Any]:
    capabilities = port.get("capabilities") or {}
    if "port_speed" not in capabilities:
        raise ValueError(f"Port {node_name}/{port.get('id')} is missing capabilities.port_speed")

    legacy = {
        "name": port["id"],
        "speed": capabilities["port_speed"],  # Hamza stores Mbps.
        "queueCount": port["number_of_queues"],
    }

    mapping = pcp_mapping.get((node_name, port["id"]))
    if mapping:
        legacy["pcpToQueue"] = deepcopy(mapping)
    return legacy


def _node_to_legacy(
    payload: dict[str, Any],
    node: dict[str, Any],
    pcp_mapping: dict[tuple[str, str], dict[str, int]],
) -> dict[str, Any]:
    role = node["type"]
    if role == "END_STATION":
        legacy_type = "host"
    elif role in ("BRIDGE", "BRIDGED_END_STATION"):
        legacy_type = "switch"
    else:
        raise ValueError(f"Unsupported OpenCNC node type for current optimizers: {role}")

    legacy = {
        "type": legacy_type,
        "name": node["name"],
        "ports": {
            port["id"]: _port_to_legacy(node["name"], port, pcp_mapping)
            for port in node.get("ports", [])
        },
    }

    if legacy_type == "switch":
        properties = node.get("properties") or {}
        legacy["procDelay"] = int(
            properties.get(
                "processing_delay_ns",
                _device_transit_time(payload, node["name"]),
            )
        )
    return legacy


def _link_to_legacy(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": link["id"],
        "sourceNode": link["source_node"],
        "sourcePort": link["source_port"],
        "destNode": link["destination_node"],
        "destPort": link["destination_port"],
        "propDelay": link["propagation_delay_ns"],
        # Contract v1.2 follows OpenCNC bandwidth_mbps; current Hamza CBS uses Bps.
        "linkBw": link["bandwidth_mbps"] * 1_000_000,
    }


def _stream_endpoints(
    definition: dict[str, Any],
    applications: dict[str, dict[str, Any]],
) -> tuple[list[str], list[list[str]]]:
    if "talkerNodeId" in definition:
        talkers = [definition["talkerNodeId"]]
        listeners = [[x] for x in definition["listenerNodeIds"]]
        return talkers, listeners

    talkers = _application_candidates(definition["talkerApplicationId"], applications)
    listeners = [
        _application_candidates(app_id, applications)
        for app_id in definition["listenerApplicationIds"]
    ]
    return talkers, listeners


def _stream_model_to_legacy(
    stream_model: dict[str, Any],
    applications: dict[str, dict[str, Any]],
    require_pcp: bool,
) -> dict[str, Any]:
    definition = stream_model["definition"]
    traffic = definition["trafficSpec"]
    requirements = definition["userToNetworkRequirements"]
    configuration = stream_model.get("configuration") or {}

    pcp = configuration.get("pcp")
    if require_pcp and pcp is None and stream_model.get("change") != "remove":
        raise ValueError(
            f"Stream '{definition['id']}' has no PCP, but the selected/current CBS implementation requires one."
        )

    talkers, listeners = _stream_endpoints(definition, applications)
    legacy: dict[str, Any] = {
        "name": definition["id"],
        "interval": traffic["intervalNs"],
        "maxFramesPerInterval": traffic["maxIntervalFrames"],
        "maxFrameSize": traffic["maxFrameSize"],
        "maxLatency": requirements["maxLatencyNs"],
        "srClass": definition.get("trafficClassId"),
        "tlkrPssblPlcmnt": talkers,
        "lstnsrPssblPlcmnt": listeners,
        "pcp": pcp,
        "intraClassPriority": definition.get("intraClassPriority"),
    }

    offset = requirements.get("transmitOffsetRange")
    if offset:
        legacy["earliest-transmit-offset"] = offset.get("minNs", 0)
        legacy["latest-transmit-offset"] = offset.get("maxNs", 0)
    if "maxJitterNs" in requirements:
        legacy["jitter"] = requirements["maxJitterNs"]
    return legacy


def _build_usecase(internal_payload: dict[str, Any]) -> Usecase:
    graph = nx.DiGraph()
    graph.add_nodes_from((node["name"], deepcopy(node)) for node in internal_payload["nodes"])
    graph.add_edges_from(
        (link["sourceNode"], link["destNode"], deepcopy(link))
        for link in internal_payload["links"]
    )

    usecase = Usecase(
        configFile=None,
        index=internal_payload.get("index", 0),
        topology=graph,
        streams=deepcopy(internal_payload["streams"]),
    )
    usecase.config = Usecase.guess_config(internal_payload)
    return usecase


def _extract_legacy_tas_configuration(forwarding_plane: dict[str, Any]) -> dict[str, Any] | None:
    config = forwarding_plane.get("configuration") or {}
    ports: list[dict[str, Any]] = []
    for node_cfg in config.get("nodeConfigs", []):
        for port_cfg in node_cfg.get("portConfigs", []):
            gcl = port_cfg.get("gcl")
            if not gcl:
                continue
            entries = []
            elapsed = 0
            for entry in gcl.get("entries", []):
                start = entry.get("startTimeNs", elapsed)
                duration = entry["timeInterval"]
                converted = {
                    "startTimeNs": start,
                    "durationNs": duration,
                }
                if "openTrafficClasses" in entry:
                    converted["openTrafficClasses"] = deepcopy(entry["openTrafficClasses"])
                if "streamIds" in entry:
                    converted["streamIds"] = deepcopy(entry["streamIds"])
                entries.append(converted)
                elapsed = start + duration
            ports.append({
                "node": node_cfg["nodeId"],
                "port": port_cfg["portId"],
                "gcl": {
                    "cycleTimeNs": gcl["cycleTime"],
                    "entries": entries,
                },
            })
    return {"ports": ports} if ports else None


def _build_contract_v1_2_usecase(payload: dict[str, Any]) -> Usecase:
    forwarding_plane = payload["forwardingPlane"]
    topology = forwarding_plane["topology"]
    applications = _applications_by_id(payload)
    pcp_mapping = _pcp_mapping_by_port(forwarding_plane)
    mechanisms = _mechanism_names(payload)
    require_pcp = "CBS" in mechanisms # TODO: make PCP enforcement configurable without hardcoding CBS.

    internal_payload = {
        "index": 0,
        "nodes": [
            _node_to_legacy(payload, node, pcp_mapping)
            for node in topology["nodes"]
        ],
        "links": [_link_to_legacy(link) for link in topology["links"]],
        "streams": [
            _stream_model_to_legacy(sm, applications, require_pcp=require_pcp)
            for sm in forwarding_plane["streams"]
            if sm.get("change") != "remove"
        ],
    }

    # Forward CBS best-effort blocking-frame constraint
    cbs_constraints = next(
        (
            mechanism.get("constraints", {})
            for mechanism in payload.get("mechanisms", [])
            if isinstance(mechanism, dict)
            and mechanism.get("name") == "CBS"
        ),
        {},
    )

    be_frame_size = cbs_constraints.get(
        "maxBestEffortFrameSizeBytes"
    )

    if be_frame_size is not None:
        for stream in internal_payload["streams"]:
            stream["beMaxFrameSize"] = be_frame_size

    usecase = _build_usecase(internal_payload)
    
    task = payload["optimizationTask"]
    scope = task.get("scope") or {}

    if scope.get("trafficClasses") is not None:
        target_classes = list(scope["trafficClasses"])
    else:
        target_classes = sorted({
            sm["definition"]["trafficClassId"]
            for sm in forwarding_plane["streams"]
            if sm.get("change") != "remove"
            and (sm.get("definition") or {}).get("trafficClassId") is not None
        })

    usecase.target_traffic_classes = target_classes


    usecase.target_traffic_classes = target_classes
    usecase.schema_version = payload["schemaVersion"]
    usecase.optimization_task = payload["operation"]
    usecase.optimization_task_details = deepcopy(task)
    usecase.mechanisms = mechanisms
    usecase.objectives = deepcopy(task.get("objectives", []))
    usecase.constraints = deepcopy(task.get("constraints", []))
    usecase.metadata = deepcopy(payload.get("metadata", {}))
    usecase.current_configuration = deepcopy(forwarding_plane.get("configuration"))
    usecase.existing_tas_configuration = _extract_legacy_tas_configuration(forwarding_plane)

    # Keep supplied route/path information available to optimizer-specific adapters.
    usecase.fixed_routes = {
        sm["definition"]["id"]: deepcopy(sm.get("configuration", {}))
        for sm in forwarding_plane["streams"]
        if (sm.get("decisionVariables") or {}).get("routing") in ("fixed", "preserve")
    }

    usecase.new_streams = [
        _stream_model_to_legacy(sm, applications, require_pcp=False)
        for sm in forwarding_plane["streams"]
        if sm.get("change") == "add"
    ]
    usecase.modified_streams = [
        _stream_model_to_legacy(sm, applications, require_pcp=False)
        for sm in forwarding_plane["streams"]
        if sm.get("change") == "modify"
    ]
    usecase.removed_stream_ids = [
        sm["definition"]["id"]
        for sm in forwarding_plane["streams"]
        if sm.get("change") == "remove"
    ]
    return usecase


def _build_legacy_usecase(payload: dict[str, Any]) -> Usecase:
    """Temporary fallback for old local optimizer fixtures during migration."""
    graph = nx.DiGraph()
    graph.add_nodes_from((node["name"], node) for node in payload.get("nodes", []))
    graph.add_edges_from(
        (link["sourceNode"], link["destNode"], dict(link))
        for link in payload.get("links", [])
    )
    usecase = Usecase(
        configFile=None,
        index=payload.get("index", 0),
        topology=graph,
        streams=payload.get("streams", []),
    )
    usecase.config = Usecase.guess_config(payload)
    traffic_classes = payload.get("targetTrafficClasses") or sorted({
        stream.get("srClass") for stream in usecase.streams if stream.get("srClass") is not None
    })
    usecase.target_traffic_classes = traffic_classes
    usecase.existing_tas_configuration = deepcopy(payload.get("existingTasConfiguration"))
    usecase.new_streams = deepcopy(payload.get("newStreams", []))
    return usecase


def build_usecase_from_payload(payload: dict[str, Any]) -> Usecase:
    if not isinstance(payload, dict):
        raise TypeError("Optimizer payload must be a JSON object.")

    if payload.get("schemaVersion") == "1.4" and "forwardingPlane" in payload:
        return _build_contract_v1_2_usecase(payload)

    return _build_legacy_usecase(payload)

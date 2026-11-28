"""CBS configuration quantization, separate from the analytical solver and JSON mapping.

Zhao Eqs. (4), (5) are evaluated with configured integer idle slopes.
This does not re-optimize or independently validate rounded deadline bounds.
"""
from __future__ import annotations

import math
from fractions import Fraction
from dataclasses import dataclass
from typing import Any


class CBSResponseConversionError(ValueError):
    """CBS calculations exist but cannot be converted to a valid configuration."""


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def admin_idle_slope(raw_slope, speed_bps, caps, label):
    if (isinstance(raw_slope, bool) or not isinstance(raw_slope, (int, float))
            or not math.isfinite(raw_slope) or raw_slope < 0):
        raise CBSResponseConversionError(f"Invalid CBS idleSlope at {label}: {raw_slope!r}")
    if caps.get("supported") is False:
        raise CBSResponseConversionError(f"CBS is marked unsupported at {label}")
    step = caps.get("idleSlopeGranularityBps", 1)
    if (isinstance(step, bool) or not isinstance(step, (int, float))
            or not math.isfinite(step) or step <= 0):
        raise CBSResponseConversionError(f"Invalid CBS slope granularity at {label}")
    # Retain the established upward hardware quantization policy.
    rounded = int(math.ceil(math.ceil(raw_slope / step - 1e-12) * step))
    if rounded >= speed_bps:
        raise CBSResponseConversionError(f"CBS idleSlope {rounded} >= link speed at {label}")
    limit = caps.get("maxIdleSlopeBps")
    if limit is not None and rounded > limit:
        raise CBSResponseConversionError(f"CBS idleSlope {rounded} exceeds hardware limit at {label}")
    return rounded


def configured_registers(group, idle_slope_bps, speed_bps, label, higher):
    """Return unsigned register values using all higher-priority classes on this port.

    `higher` contains (configured idleSlope bps, individual max frame bits).
    Integer/Fraction arithmetic avoids floating-point credit rounding errors.
    """
    raw = group["sharedCBS"]
    for name in ("sendSlopeBps", "creditMinBits", "creditMaxBits"):
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CBSResponseConversionError(f"Invalid CBS {name} at {label}: {value!r}")
    if raw["sendSlopeBps"] > 0 or raw["creditMinBits"] > 0 or raw["creditMaxBits"] < 0:
        raise CBSResponseConversionError(f"Inconsistent CBS credit/slope signs at {label}")
    frame_bits = {}
    for key in ("maxFrameBits", "blockingFrameBits"):
        value = group[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or int(value) != value):
            raise CBSResponseConversionError(f"Invalid CBS {key} at {label}: {value!r}")
        frame_bits[key] = int(value)

    residual = speed_bps - sum(slope for slope, _ in higher)
    if not 0 <= idle_slope_bps < residual:
        raise CBSResponseConversionError(f"Aggregate CBS reservations exhaust link capacity at {label}")
    # Zhao Eq. (5) for all higher classes, using their configured slopes.
    higher_credit_min = sum(
        (Fraction((slope - speed_bps) * frame, speed_bps) for slope, frame in higher),
        Fraction(0),
    )
    # Zhao Eq. (4); for M1 the higher-priority sums are empty.
    credit_max_bits = idle_slope_bps * (frame_bits["blockingFrameBits"] - higher_credit_min) / residual
    send_slope_magnitude = speed_bps - idle_slope_bps
    credit_min_magnitude_bits = Fraction(send_slope_magnitude * frame_bits["maxFrameBits"], speed_bps)
    return {
        "sendSlopeBps": send_slope_magnitude,
        "hiCreditBytes": _ceil(credit_max_bits / 8),
        "loCreditBytes": _ceil(credit_min_magnitude_bits / 8),
    }


class PortConfiguration:
    """Accumulate quantized higher-priority groups, independently per port.

    The N-class solver emits groups in increasing Zhao rank; the rank survives
    client-class restoration. Never derive order from PCP or physical queue IDs.
    """
    def __init__(self):
        self._ports = {}

    def registers(self, group, slope, speed_bps, label):
        rank = group.get("zhaoPriorityRank")
        if rank is None:
            # Historical direct solver outputs still use Zhao labels.
            cls = group["srClass"]
            rank = int(cls[1:]) if cls in {f"M{i}" for i in range(1, 8)} else None
        if type(rank) is not int or not 1 <= rank <= 7:
            raise CBSResponseConversionError(f"Missing or invalid Zhao class priority at {label}")
        key = (group["node"], group["port"])
        prior = self._ports.setdefault(key, [])
        if prior and (rank <= prior[-1][0] or speed_bps != prior[-1][3]):
            raise CBSResponseConversionError(f"Inconsistent CBS class order or link speed at {label}")
        registers = configured_registers(group, slope, speed_bps, label,
                                         [(s, frame) for _, s, frame, _ in prior])
        prior.append((rank, slope, int(group["maxFrameBits"]), speed_bps))
        return registers


@dataclass(frozen=True)
class PreparedCBSQueue:
    """Finished device-compatible values, independent of response serialization."""

    node_id: str
    port_id: str
    traffic_class: str
    queue_id: int
    idle_slope_bps: int
    send_slope_magnitude_bps: int
    hi_credit_bytes: int
    lo_credit_magnitude_bytes: int


def _port_index(topology: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index = {}
    for node in topology["nodes"]:
        for port in node["ports"]:
            key = (node["name"], port["id"])
            if key in index:
                raise CBSResponseConversionError(f"Duplicate topology port {key}")
            index[key] = port
    return index


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


def prepare_cbs_configuration(
    request: dict[str, Any], legacy: dict[str, Any],
) -> list[PreparedCBSQueue]:
    """Prepare finished queue values before response mapping; preserve raw results."""
    rows = legacy.get("sharedStreamResults") or []
    if not (rows and all(row.get("deadlineSatisfied") is True for row in rows)
            and legacy.get("allDeadlinesSatisfied") is True):
        return []
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

    prepared: list[PreparedCBSQueue] = []
    seen_groups: set[tuple[str, str, str]] = set()
    seen_queue_targets: set[tuple[str, str, int]] = set()
    # Proposed slope after hardware rounding, indexed by physical queue per port.
    configured_slopes_by_port: dict[tuple[str, str], dict[int, int]] = {}
    configured_port_speeds: dict[tuple[str, str], int] = {}
    configured = PortConfiguration()
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
        slope = admin_idle_slope(group["sharedCBS"]["idleSlopeBps"], speed_bps, caps, label)
        port_key = (node_id, port_id)
        prior_speed = configured_port_speeds.get(port_key)
        if prior_speed is not None and prior_speed != speed_bps:
            raise CBSResponseConversionError(f"Conflicting link speeds at {node_id}/{port_id}")
        configured_port_speeds[port_key] = speed_bps
        # seen_queue_targets above already prevents two optimized classes sharing a queue.
        configured_slopes_by_port.setdefault(port_key, {})[queue_id] = slope
        registers = configured.registers(group, slope, speed_bps, label)
        prepared.append(PreparedCBSQueue(
            node_id, port_id, traffic_class, queue_id, slope,
            registers["sendSlopeBps"], registers["hiCreditBytes"],
            registers["loCreditBytes"],
        ))

        baseline_queues = [q for q in port_cfg.get("queueConfigs", []) if q["queueId"] == queue_id]
        if len(baseline_queues) > 1:
            raise CBSResponseConversionError(f"Duplicate input queue config for {label} queue {queue_id}")
        existing_queue = baseline_queues[0] if baseline_queues else {}
        baseline_cbs = [c for c in existing_queue.get("cbs", []) if c["egressQueueId"] == queue_id]
        if len(baseline_cbs) > 1:
            raise CBSResponseConversionError(f"Duplicate input CBS config for {label} queue {queue_id}")

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
    return prepared



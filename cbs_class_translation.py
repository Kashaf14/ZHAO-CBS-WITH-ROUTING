"""Map client CBS class IDs to Zhao's priority-ordered M1..M7 symbols.

This adapter derives scheduling priorities from the standardized
OptimizerRequest v1.4 using two per-port configuration tables:

- trafficClassTable: maps PCP values to egress queues.
- queuePriorityTable: defines queue scheduling priorities.

Lower schedulingPriority values represent higher priority.

The adapter does not infer priorities from queue IDs or PCP values.
It does not modify the original request or CBS mathematical algorithm.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class CBSClassTranslationError(ValueError):
    """Insufficient or contradictory information to assign Zhao SR classes."""


def _valid_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 7


def _configuration_ports(plane: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for node in (plane.get("configuration") or {}).get("nodeConfigs", []):
        node_id = node["nodeId"]
        for port in node.get("portConfigs", []):
            key = (node_id, port["portId"])
            if key in result:
                raise CBSClassTranslationError(f"Duplicate CNC port configuration: {key}")
            result[key] = port
    return result


def _traffic_class_index(
    port: dict[str, Any], pcp: int, *, node_id: str, port_id: str,
) -> int:
    label = f"{node_id}/{port_id}"
    if port.get("pcpMappingEnabled") is not True:
        raise CBSClassTranslationError(
            f"Explicit, enabled PCP mapping required at {label}; "
            "default switch mappings are not inferred"
        )
    table = (port.get("pcpConfig") or {}).get("trafficClassTable", [])
    matches = [entry.get("egressQueueId") for entry in table
               if entry.get("priority") == pcp]
    if len(matches) != 1 or not _valid_index(matches[0]):
        raise CBSClassTranslationError(
            f"Expected exactly one valid IEEE TC index for PCP {pcp} at {label}"
        )
    return matches[0]


def _scheduling_priority(
    port: dict[str, Any],
    queue_id: int,
    *,
    node_id: str,
    port_id: str,
) -> int:
    label = f"{node_id}/{port_id}"

    table = port.get("queuePriorityTable")

    if not isinstance(table, list) or not table:
        raise CBSClassTranslationError(
            f"Missing queuePriorityTable at {label}"
        )

    matches = [
        entry.get("schedulingPriority")
        for entry in table
        if entry.get("queueId") == queue_id
    ]

    if len(matches) != 1:
        raise CBSClassTranslationError(
            f"Expected exactly one scheduling priority "
            f"for queue {queue_id} at {label}"
        )

    priority = matches[0]

    if (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or priority < 1
    ):
        raise CBSClassTranslationError(
            f"Invalid scheduling priority for queue "
            f"{queue_id} at {label}"
        )

    return priority

def derive_zhao_class_mapping(
    request: dict[str, Any],
    stream_egress_ports: Mapping[str, Sequence[tuple[str, str]]],
) -> dict[str, str]:
    """Return {original client class ID: Zhao M1..M7} without mutating inputs.

    stream_egress_ports: stream ID -> every (node ID, port ID) *egress* used by
    that stream. It must cover every active stream in this CBS-only request.

    Conservative constraints of the current CBS adapter:
    - each logical class must use exactly one IEEE TC index across all routes;
    - distinct logical classes must have distinct indices;
    - 1..7 logical CBS classes are supported;
    - unknown, missing, duplicated, or contradictory mappings fail closed.

    This maps priority labels only. It does not prove that a mapped queue uses
    CBS or that a physical switch implements the assumed queue scheduling.
    """
    if request.get("schemaVersion") != "1.4":
        raise CBSClassTranslationError("Expected OptimizerRequest schemaVersion 1.4")
    mechanisms = [m.get("name") for m in request.get("mechanisms", [])]
    if mechanisms != ["CBS"]:
        raise CBSClassTranslationError("Class mapping requires a CBS-only request")

    plane = request["forwardingPlane"]
    ports = _configuration_ports(plane)
    seen_stream_ids: set[str] = set()
    index_by_class: dict[str, int] = {}
    priority_by_class: dict[str, int] = {}
    active_stream_ids: set[str] = set()

    for stream in plane.get("streams", []):
        if stream.get("change") == "remove":
            continue
        definition = stream["definition"]
        stream_id = definition["id"]
        if not isinstance(stream_id, str) or not stream_id or stream_id in seen_stream_ids:
            raise CBSClassTranslationError(f"Missing or duplicate stream ID: {stream_id!r}")
        seen_stream_ids.add(stream_id)
        active_stream_ids.add(stream_id)

        class_id = definition.get("trafficClassId")
        if not isinstance(class_id, str) or not class_id:
            raise CBSClassTranslationError(f"Missing logical traffic class for {stream_id}")
        pcp = (stream.get("configuration") or {}).get("pcp")
        if not _valid_index(pcp):
            raise CBSClassTranslationError(f"Missing or invalid PCP for {stream_id}")
        path_ports = stream_egress_ports.get(stream_id)
        if not path_ports:
            raise CBSClassTranslationError(f"Missing resolved CBS egress ports for {stream_id}")

        for node_id, port_id in path_ports:
            port = ports.get((node_id, port_id))
            if port is None:
                raise CBSClassTranslationError(
                    f"Missing CNC configuration for egress {node_id}/{port_id}"
                )
            tc_index = _traffic_class_index(
                port, pcp, node_id=node_id, port_id=port_id
            )
            previous = index_by_class.setdefault(class_id, tc_index)
            if previous != tc_index:
                raise CBSClassTranslationError(
                    f"Class {class_id!r} uses inconsistent IEEE TC indices: "
                    f"{previous} and {tc_index} (at {node_id}/{port_id})"
                )
            priority = _scheduling_priority(
                port,
                tc_index,
                node_id=node_id,
                port_id=port_id,
            )

            previous_priority = priority_by_class.setdefault(
                class_id, priority
            )

            if previous_priority != priority:
                raise CBSClassTranslationError(
                    f"Class {class_id!r} uses inconsistent "
                    f"scheduling priorities across ports"
                )

    if not index_by_class or len(index_by_class) > 7:
        raise CBSClassTranslationError("Expected 1..7 active CBS SR classes")
    if set(stream_egress_ports) != active_stream_ids:
        raise CBSClassTranslationError(
            "Egress-port input must cover exactly the active CBS request streams"
        )

    scope = (request.get("optimizationTask") or {}).get("scope") or {}
    target_classes = scope.get("trafficClasses")
    if target_classes is not None:
        if any(cls not in index_by_class for cls in target_classes):
            raise CBSClassTranslationError("Target CBS class has no active stream")

    inverse: dict[int, str] = {}
    for name, tc_index in index_by_class.items():
        if tc_index in inverse and inverse[tc_index] != name:
            raise CBSClassTranslationError(
                f"Different client classes {inverse[tc_index]!r} and {name!r} "
                f"share TC {tc_index}; queue-level aggregation is not implemented"
            )
        inverse[tc_index] = name

    # Zhao M1 is the highest-priority CBS SR class PRESENT, not necessarily TC7.
    if len(set(priority_by_class.values())) != len(priority_by_class):
        raise CBSClassTranslationError(
            "Different CBS classes share the same scheduling priority"
        )

    return {
        name: f"M{position}"
        for position, (name, priority) in enumerate(
            sorted(priority_by_class.items(), key=lambda item: item[1]),
            start=1,
        )
    }
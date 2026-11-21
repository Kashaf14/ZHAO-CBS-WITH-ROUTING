"""Pre-optimization CBS input and hardware checks for targeted egress queues.

This checks required Zhao inputs and hardware facts before numerical computation.
Calculated idleSlope, credits, rounding and total reserved capacity remain response checks.
"""

from collections import defaultdict

from cbs_response_adapter import (
    CBSResponseConversionError,
    _get_device_cbs_capabilities,
    _mapped_egress_queue,
    _port_index,
)
from algorithm.cbs_nclass_candidate import _cbs_egress_context

def validate_cbs_precheck(request: dict, usecase, optimizer) -> None:
    """Reject a complete request if any required CBS output queue is inapplicable.

    Only targeted streams need CBS configuration. Other classes can still be
    consulted by the solver for non-preemptive blocking, without configuring CBS
    on their routes. The solver's own route and benchmark-port selection is used.
    """
    # Zhao's non-preemptive blocking model needs an explicit lower-priority
    # Best Effort maximum frame size. This is optimizer-specific, not a
    # mandatory field in the algorithm-independent shared request contract.
    cbs_mechanism = next(
        (m for m in request.get("mechanisms", [])
         if isinstance(m, dict) and m.get("name") == "CBS"),
        None,
    )
    constraints = cbs_mechanism.get("constraints") if cbs_mechanism else None
    be_size = (
        constraints.get("maxBestEffortFrameSizeBytes")
        if isinstance(constraints, dict) else None
    )
    if type(be_size) is not int or be_size <= 0:
        raise CBSResponseConversionError(
            "Zhao CBS requires a positive integer "
            "mechanisms[CBS].constraints.maxBestEffortFrameSizeBytes"
        )

    plane = request["forwardingPlane"]
    topology_ports = _port_index(plane["topology"])

    # As in the response adapter, input port configurations are addressed by
    # physical node/port identifiers, never inferred from the PCP number.
    config_ports = {}
    seen_nodes = set()
    for node in (plane.get("configuration") or {}).get("nodeConfigs", []):
        node_id = node["nodeId"]
        if node_id in seen_nodes:
            raise CBSResponseConversionError(f"Duplicate input config node: {node_id}")
        seen_nodes.add(node_id)
        for port in node.get("portConfigs", []):
            key = (node_id, port["portId"])
            if key in config_ports:
                raise CBSResponseConversionError(f"Duplicate input port config: {key}")
            config_ports[key] = port

    targets = set(getattr(usecase, "target_traffic_classes", []) or [])
    if not targets:
        raise CBSResponseConversionError("No target CBS traffic classes supplied")

    # Group by (output node, output port, SR class), as in the CBS result.
    # The shared egress helper uses the existing fixed routes and honors the
    # opt-in benchmark port; it does not perform Zhao's calculations.
    group_pcps = defaultdict(set)
    for stream in usecase.streams:
        traffic_class = stream["srClass"]
        if traffic_class not in targets:
            continue  # Background-only traffic does not receive a CBS config.
        pcp = stream.get("pcp")
        if isinstance(pcp, bool) or not isinstance(pcp, int) or not 0 <= pcp <= 7:
            raise CBSResponseConversionError(
                f"Targeted CBS stream {stream['name']} has no valid PCP"
            )
        try:
            context = _cbs_egress_context(optimizer, stream, usecase)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise CBSResponseConversionError(
                f"Cannot resolve CBS egress ports for stream {stream['name']}: {exc}"
            ) from exc
        if not context["egressPorts"]:
            raise CBSResponseConversionError(
                f"No CBS egress ports for targeted stream {stream['name']}"
            )
        for egress in context["egressPorts"]:
            group_pcps[(egress["node"], egress["port"], traffic_class)].add(pcp)

    if not group_pcps:
        raise CBSResponseConversionError("No output ports for targeted CBS traffic")

    used_queues = set()
    queues_per_port = defaultdict(set)
    for (node_id, port_id, traffic_class), pcps in sorted(group_pcps.items()):
        label = f"{node_id}/{port_id}/{traffic_class}"
        port_key = (node_id, port_id)
        if port_key not in topology_ports:
            raise CBSResponseConversionError(f"CBS requires nonexistent output port {label}")

        # Port-specific 'supported' overrides device-level 'supported'.
        caps = _get_device_cbs_capabilities(request, node_id, port_id)
        if caps.get("supported") is False:
            raise CBSResponseConversionError(f"CBS explicitly unsupported at {label}")
        if caps.get("supported") is not True:
            raise CBSResponseConversionError(
                f"Missing explicit CBS support capability at {label}"
            )

        if len(pcps) != 1:
            raise CBSResponseConversionError(
                f"Multiple PCPs on shared CBS group {label}: {sorted(pcps)}"
            )
        if port_key not in config_ports:
            raise CBSResponseConversionError(f"Missing input port config {label}")
        queue_id = _mapped_egress_queue(config_ports[port_key], next(iter(pcps)), label)
        queue_count = topology_ports[port_key]["number_of_queues"]
        if (isinstance(queue_count, bool) or not isinstance(queue_count, int)
                or queue_count <= 0 or isinstance(queue_id, bool)
                or not isinstance(queue_id, int) or not 0 <= queue_id < queue_count):
            raise CBSResponseConversionError(
                f"Invalid physical queue {queue_id} (port queue count {queue_count}) at {label}"
            )

        if (node_id, port_id, queue_id) in used_queues:
            raise CBSResponseConversionError(
                f"More than one CBS class targets physical queue {node_id}/{port_id}/{queue_id}"
            )
        used_queues.add((node_id, port_id, queue_id))
        queues_per_port[port_key].add(queue_id)

        supported_queues = caps.get("supportedQueues")
        if (supported_queues is not None and queue_id not in supported_queues
                and str(queue_id) not in supported_queues):
            raise CBSResponseConversionError(
                f"Physical queue {queue_id} does not support CBS at {label}"
            )

    # This lower-bound check is decisive even before accounting for any
    # untouched CBS queues. The response adapter remains responsible for
    # computed reservations and any post-optimization checks.
    for (node_id, port_id), queue_ids in sorted(queues_per_port.items()):
        caps = _get_device_cbs_capabilities(request, node_id, port_id)
        limit = caps.get("maxCbsQueues")
        if limit is not None and len(queue_ids) > limit:
            raise CBSResponseConversionError(
                f"CBS requires {len(queue_ids)} queues at {node_id}/{port_id}, "
                f"but maxCbsQueues is {limit}"
            )

"""Experimental priority-ordered CBS solver for a variable set of SR classes.

Zhao et al. (2024), Eqs. 4, 5, 11, 13-15, 23, 24.
The same algorithm is used for every active class; there is NO M1/M2/M3
branch or a separate class-specific optimizer. Class priority is M1 > ... > M7
in Zhao's modeled 8-queue architecture, not a hardware guarantee.

Direct-call only. It does not register capabilities or touch the response
contract, CNC, or the existing production/experimental solver dispatch.

KNOWN BOUNDS: fixed routed unicast, one frame per interval, configuration only,
explicit BE frame maximum, no mixed source/transit within a port/class group,
N in [1,7], all active higher-priority SR classes targeted; lower-priority
classes may be present only as frame-size blocking background.
No independent NC schedulability or physical queue feasibility is established.
"""
from __future__ import annotations

import math

from .cbs_multiclass_math import calculate_class_cbs
from .cbs_priority_dependencies import higher_priority_for_port, priority_index


_NS_PER_S = 1_000_000_000


def _validate_domain(usecase):
    requested = getattr(usecase, "target_traffic_classes", None)
    if not isinstance(requested, (tuple, list)) or not requested:
        raise ValueError("Explicit target_traffic_classes required for experimental N-class solver")
    if len(set(requested)) != len(requested):
        raise ValueError("Duplicate target traffic classes")
    for cls in requested:
        priority_index(cls)  # Reject unknown labels; avoid guessed priority mapping.
    if not 1 <= len(requested) <= 7:
        raise ValueError("Zhao experimental solver supports 1..7 active SR classes")
    # if getattr(usecase, "optimization_task", None) != "configuration":
    #     raise ValueError("N-class experimental solver supports configuration only")
    routes = getattr(usecase, "fixed_routes", None)
    if not isinstance(routes, dict) or not routes:
        raise ValueError("Explicit fixed unicast routes required")
    streams = usecase.streams
    if not streams:
        raise ValueError("No CBS streams to optimize")
    actual = {s.get("srClass") for s in streams}
    for cls in actual:
        priority_index(cls)  # Unknown background SR classes must not be ignored.
    targets = set(requested)
    if not targets.issubset(actual):
        raise ValueError(
            "Every targeted CBS SR class must have at least one stream; "
            f"requested={sorted(targets)!r}, present={sorted(actual)!r}"
        )
    # A lower-priority background SR class contributes frame-size blocking,
    # but does not need an idleSlope to calculate the targeted higher classes.
    # A skipped *higher* class would instead require its CBS reservation and
    # credit bound: reject rather than silently underestimating interference.
    missing_higher = sorted(
        (cls for cls in actual - targets
         if any(priority_index(cls) < priority_index(target) for target in targets)),
        key=priority_index,
    )
    if missing_higher:
        raise ValueError(
            "Active higher-priority CBS SR classes must be optimization targets: "
            f"{missing_higher!r}"
        )
    names = [s["name"] for s in streams]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate stream identifiers")
    for s in streams:
        name = s["name"]
        if s.get("maxFramesPerInterval") != 1:
            raise ValueError(f"Only one frame per interval supported: {name}")
        if (len(s.get("tlkrPssblPlcmnt", [])) != 1
                or len(s.get("lstnsrPssblPlcmnt", [])) != 1
                or len(s["lstnsrPssblPlcmnt"][0]) != 1):
            raise ValueError(f"Fixed unicast endpoints required: {name}")
        route = routes.get(name)
        if not isinstance(route, dict) or not route.get("path"):
            raise ValueError(f"Explicit fixed path required: {name}")
        for field in ("maxFrameSize", "interval", "maxLatency", "beMaxFrameSize"):
            x = s.get(field)
            if (isinstance(x, bool) or not isinstance(x, (float, int))
                    or not math.isfinite(x) or x <= 0):
                raise ValueError(f"Invalid {field}: {name}")
    return sorted(requested, key=priority_index)


def _cbs_egress_context(optimizer, stream, usecase):
    """Resolve the egress stages used by CBS, including benchmark selection."""
    context = optimizer.build_stream_topology_context(
        stream, usecase.links, usecase.fixed_routes
    )
    benchmark_port = getattr(usecase, "cbs_benchmark_output_port", None)
    if benchmark_port is not None:
        # Preserve physical routing but analyze ONLY the specified egress port.
        # Never apply this filtering to normal multi-hop optimization.
        matching = [
            port for port in context["egressPorts"]
            if (port["node"], port["port"]) == tuple(benchmark_port)
        ]
        if len(matching) != 1:
            raise ValueError(
                f"Stream {stream['name']} must traverse benchmark egress "
                f"{benchmark_port!r} exactly once"
            )
        context["egressPorts"] = [{**matching[0], "h": 0}]

    return context


def _stream_context(optimizer, stream, usecase):
    """Prepare the same Zhao Theorem-1 inputs for every SR class."""
    context = _cbs_egress_context(optimizer, stream, usecase)
    burst = stream["maxFrameSize"] * 8  # Single frame per interval only.
    rho = burst * _NS_PER_S / stream["interval"]
    ports = context["egressPorts"]
    if not ports:
        raise ValueError(f"No egress ports for stream {stream['name']}")
    local = optimizer.calculate_local_deadlines(
        burst, rho, stream["maxLatency"], len(ports)
    )
    return {
        **context,
        "deadline": stream["maxLatency"],
        "frameSize": stream["maxFrameSize"],
        "interval": stream["interval"],
        "rateBps": rho,
        "burstBits": burst,
        "rhoBps": rho,
        "beMaxFrameBits": stream["beMaxFrameSize"] * 8,
        "localDeadlinesNs": local,
        "portBurstsBits": optimizer.calculate_port_bursts(burst, rho, local),
    }


def _breakpoints(optimizer, curve):
    """Candidate vertices of the concave aggregate arrival envelope (Eq. 11)."""
    xs = {0.0}
    for point in optimizer.find_xy_point(curve, idle_slope=0.0):
        x = point["xNs"] / _NS_PER_S
        if not math.isfinite(x) or x < 0:
            raise ValueError("Invalid arrival-curve breakpoint")
        xs.add(x)
    return [
        (
            x * _NS_PER_S,
            sum(min(
                p["flowSlopeBps"] * x + p["flowInterceptBits"],
                p["linkSlopeBps"] * x + p["linkInterceptBits"],
                p["cbsSlopeBps"] * x + p["cbsInterceptBits"],
            ) for p in curve["predecessorCurves"]),
        )
        for x in sorted(xs)
    ]


def _solve_class(optimizer, cls, usecase, all_contexts, previously_solved):
    """Solve one SR class. Called in strictly increasing priority order."""
    contexts = [
        _stream_context(optimizer, s, usecase)
        for s in usecase.streams if s["srClass"] == cls
    ]
    groups = optimizer.group_streams_by_port_class(contexts)
    if not groups:
        raise ValueError(f"No port groups for {cls}")
    optimizer.calculate_class_local_deadlines(contexts, groups, cls)
    for g in groups:
        d = g.get("theorem1DeadlineNs")
        if (isinstance(d, bool) or not isinstance(d, (int, float))
                or not math.isfinite(d) or d <= 0):
            raise ValueError(f"Invalid local deadline for {cls} at {g['node']}/{g['port']}")
        g["requiredDeadlineNs"] = d
    optimizer.update_group_bursts_from_theorem1(contexts, groups)
    optimizer.calculate_blocking_frames(
        all_contexts, groups, {f"M{i}": i - 1 for i in range(1, 8)}
    )

    all_class_groups = previously_solved + groups
    index = {}
    for group in groups:
        key = (group["node"], group["port"], cls)
        if key in index:
            raise ValueError(f"Duplicate class group: {key}")
        index[key] = group
    pending = set(index)
    while pending:
        progressed = False
        for key in sorted(pending):
            group = index[key]
            pred_values = [p.get("predecessor") for p in group["streamPositions"]]
            if not pred_values:
                raise ValueError(f"Port group has no streams: {key}")
            if any(p is None for p in pred_values) and any(
                p is not None for p in pred_values
            ):
                raise ValueError(f"Mixed source/transit group is not supported: {key}")
            predecessors = {
                (p["node"], p["port"], cls)
                for p in pred_values if p is not None
            }
            missing = predecessors - set(index)
            if missing:
                raise ValueError(f"Missing same-class predecessors: {key}: {sorted(missing)}")
            if any("sharedCBS" not in index[p] for p in predecessors):
                continue

            hp = higher_priority_for_port(group, all_class_groups)
            if predecessors:
                curve = optimizer.build_shared_m1_arrival_curve(
                    group, [index[p] for p in sorted(predecessors)]
                )
                points = _breakpoints(optimizer, curve)
                group["sharedArrivalCurve"] = curve
            else:
                points = [(0.0, group["aggregatePortBurstBits"])]
            if not points:
                raise ValueError(f"No arrival curve candidates: {key}")

            solutions = []
            for x_ns, y_bits in points:
                parameters = calculate_class_cbs(
                    x_ns=x_ns,
                    y_bits=y_bits,
                    local_deadline_ns=group["requiredDeadlineNs"],
                    own_max_frame_bits=group["maxFrameBits"],
                    lower_priority_max_frame_bits=group["blockingFrameBits"],
                    link_speed_bps=group["linkSpeedBps"],
                    higher_priority_classes=hp,
                )
                solutions.append((parameters.idle_slope_bps, x_ns, y_bits, parameters))
            slope, _, _, chosen = max(solutions, key=lambda row: row[0])
            # Minimum long-term service rate is required for stability.
            # Do not modify Zhao's deadline-bound calculation.
            if slope <= group["aggregateRhoBps"]:
                slope = math.nextafter(float(group["aggregateRhoBps"]), math.inf)
            if slope + sum(h.idle_slope_bps for h in hp) >= group["linkSpeedBps"]:
                raise ValueError(f"CBS reservation exceeds residual link capacity: {key}")

            # The CBS service latency is independent of the current class's
            # idleSlope, but the credits and actual delay must be recomputed.
            latency_s = chosen.service_latency_s
            x_ns, y_bits = max(
                points,
                key=lambda xy: latency_s + xy[1] / slope - xy[0] / _NS_PER_S,
            )
            delay_ns = (latency_s + y_bits / slope - x_ns / _NS_PER_S) * _NS_PER_S
            if delay_ns > group["requiredDeadlineNs"] + 1e-4:
                raise ValueError(f"Analytical local deadline exceeded: {key}")
            group["sharedXYPoint"] = {"xNs": x_ns, "yBits": y_bits}
            group["sharedCBS"] = {
                "idleSlopeBps": slope,
                "sendSlopeBps": slope - group["linkSpeedBps"],
                "creditMinBits": (
                    (slope - group["linkSpeedBps"]) * group["maxFrameBits"]
                    / group["linkSpeedBps"]
                ),
                "creditMaxBits": slope * latency_s,
            }
            group["sharedDelayNs"] = delay_ns
            group["diagnosticBreakpointCount"] = len(points)
            pending.remove(key)
            progressed = True
        if not progressed:
            raise ValueError(f"Unresolved/cyclic {cls} port dependencies: {sorted(pending)}")

    rows = optimizer.calculate_shared_stream_delays(contexts, groups)
    return contexts, groups, rows


def optimize_nclass_candidate(optimizer, usecase):
    """Direct-call, variable-N CBS calculation; never advertises support to Manager."""
    ordered_classes = _validate_domain(usecase)
    all_contexts = [
        _stream_context(optimizer, s, usecase) for s in usecase.streams
    ]
    contexts, groups, rows = [], [], []
    for cls in ordered_classes:
        class_contexts, class_groups, class_rows = _solve_class(
            optimizer, cls, usecase, all_contexts, groups
        )
        contexts.extend(class_contexts)
        groups.extend(class_groups)
        rows.extend(class_rows)

    targeted_names = {
        s["name"] for s in usecase.streams if s["srClass"] in ordered_classes
    }
    if len(rows) != len(targeted_names) or {
        r["stream"] for r in rows
    } != targeted_names:
        raise ValueError("Incomplete targeted N-class stream results")
    port_totals = {}
    for g in groups:
        port = (g["node"], g["port"])
        record = port_totals.setdefault(port, {"capacity": g["linkSpeedBps"], "used": 0})
        if record["capacity"] != g["linkSpeedBps"]:
            raise ValueError(f"Conflicting link speeds on {port}")
        record["used"] += g["sharedCBS"]["idleSlopeBps"]
    for port, total in port_totals.items():
        if total["used"] >= total["capacity"]:
            raise ValueError(f"Combined SR reservations exhaust {port}")

    return {
        "optimizer": "CBS",
        "status": "stream parsing successful",
        "streams": contexts,
        "portClassGroups": groups,
        "sharedStreamResults": rows,
        "overallWorstCaseDelayNs": max(r["sharedPathDelayNs"] for r in rows),
        "allDeadlinesSatisfied": all(r["deadlineSatisfied"] for r in rows),
        "benchmarkEgressPort": (
            list(usecase.cbs_benchmark_output_port)
            if getattr(usecase, "cbs_benchmark_output_port", None) else None
        ),
        "validationNote": (
            "Experimental variable-N Zhao analytical CBS candidate only. "
            "Untargeted lower-priority streams contribute blocking only; "
            "Local deadline bounds are not independent NC schedulability verification; "
            "Manager/CNC and actual queue mapping not validated."
        ),
    }

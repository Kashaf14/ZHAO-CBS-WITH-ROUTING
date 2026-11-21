"""Experimental M1+M2 CBS *configuration* path for directed, fixed-route TC1-style traffic.

This module deliberately delegates M1 to the existing, previously regressed
CBSOptimizer.optimize() implementation. M2 uses the separately tested Zhao
(2024) equations in cbs_multiclass_math. It is not a new optimizer algorithm.

Limits (fail closed): exactly M1 and M2 as both targets, unicast, one frame per
interval, explicit fixed routes and BE blocking sizes, one link speed per shared
port, no mixed source/transit traffic in a single M2 group. Further topology and
independent schedulability verification are NOT established by this module.

Not registered as an optimizer capability until separate integration tests pass.
"""
from __future__ import annotations

from copy import copy
import math

from .cbs_multiclass_math import calculate_class_cbs
from .cbs_priority_dependencies import higher_priority_for_port


def _m2_stream_context(optimizer, stream, usecase):
    """Construct M2 grouping inputs, avoiding the M1-only CBS solver."""
    context = optimizer.build_stream_topology_context(
        stream, usecase.links, getattr(usecase, "fixed_routes", None)
    )
    burst_bits = stream["maxFrameSize"] * 8
    rho_bps = burst_bits * 1_000_000_000 / stream["interval"]
    ports = context["egressPorts"]
    preliminary = optimizer.calculate_local_deadlines(
        burst_bits, rho_bps, stream["maxLatency"], len(ports)
    )
    return {
        **context,
        "deadline": stream["maxLatency"],
        "frameSize": stream["maxFrameSize"],
        "interval": stream["interval"],
        "rateBps": rho_bps,
        "burstBits": burst_bits,
        "rhoBps": rho_bps,
        "beMaxFrameBits": stream["beMaxFrameSize"] * 8,
        "localDeadlinesNs": preliminary,
        "portBurstsBits": optimizer.calculate_port_bursts(burst_bits, rho_bps, preliminary),
    }


def _arrival_at(curve, x_s):
    """Equation 11: sum of minimum flow, link and CBS shaping bounds."""
    return sum(
        min(
            pred["flowSlopeBps"] * x_s + pred["flowInterceptBits"],
            pred["linkSlopeBps"] * x_s + pred["linkInterceptBits"],
            pred["cbsSlopeBps"] * x_s + pred["cbsInterceptBits"],
        )
        for pred in curve["predecessorCurves"]
    )


def _breakpoints(optimizer, curve):
    """Critical candidates for a concave piecewise-linear arrival envelope.

    Include t=0, since it can dominate source-like or tightly shaped traffic.
    The long-term traffic-rate stability condition is checked separately.
    """
    xs = {0.0}
    for point in optimizer.find_xy_point(curve, idle_slope=0.0):
        x_s = point["xNs"] / 1_000_000_000
        if not math.isfinite(x_s) or x_s < 0:
            raise ValueError("Non-finite or negative arrival-curve breakpoint")
        xs.add(x_s)
    return [(x * 1_000_000_000, _arrival_at(curve, x)) for x in sorted(xs)]


def _calculate_m2(optimizer, usecase, m1_result):
    m2_streams = [s for s in usecase.streams if s["srClass"] == "M2"]
    contexts = [_m2_stream_context(optimizer, stream, usecase) for stream in m2_streams]
    groups = optimizer.group_streams_by_port_class(contexts)
    optimizer.calculate_class_local_deadlines(contexts, groups, "M2")
    for group in groups:
        d = group["theorem1DeadlineNs"]
        if not math.isfinite(d) or d <= 0:
            raise ValueError("M2 Theorem-1 local deadline must be finite and positive")
        group["requiredDeadlineNs"] = d
    optimizer.update_group_bursts_from_theorem1(contexts, groups)

    all_contexts = [
        optimizer.build_stream_topology_context(
            stream, usecase.links, usecase.fixed_routes
        )
        for stream in usecase.streams
    ]
    # M3+ may block M2 even when those classes are not optimization targets.
    optimizer.calculate_blocking_frames(
        all_contexts, groups, {f"M{i}": i - 1 for i in range(1, 8)}
    )

    # All active class groups on these ports; the M1 groups already have CBS
    # reservations, while the M2 groups are the pending current-class inputs.
    all_class_groups = m1_result["portClassGroups"] + groups

    groups_by_key = {}
    for group in groups:
        key = (group["node"], group["port"], "M2")
        if key in groups_by_key:
            raise ValueError(f"Duplicate M2 egress port/class group: {key}")
        groups_by_key[key] = group

    pending = set(groups_by_key)
    while pending:
        progress = False
        for key in sorted(pending):
            group = groups_by_key[key]
            positions = group["streamPositions"]
            pred_values = [p.get("predecessor") for p in positions]
            if any(p is None for p in pred_values) and any(p is not None for p in pred_values):
                raise ValueError(f"Unsupported mixed source/transit M2 traffic at {key}")

            predecessors = {
                (p["node"], p["port"], "M2")
                for p in pred_values if p is not None
            }
            if predecessors - set(groups_by_key):
                raise ValueError(f"Missing M2 predecessor groups at {key}")
            if any("sharedCBS" not in groups_by_key[p] for p in predecessors):
                continue

            hp = higher_priority_for_port(group, all_class_groups)
            if predecessors:
                curve = optimizer.build_shared_m1_arrival_curve(
                    group, [groups_by_key[p] for p in sorted(predecessors)]
                )
                candidates = _breakpoints(optimizer, curve)
                group["sharedArrivalCurve"] = curve
            else:
                candidates = [(0.0, group["aggregatePortBurstBits"])]

            # The required rate is max over critical points, subject to a
            # traffic-rate floor. No unsupported rate-floor fallback is hidden.
            solutions = []
            for x_ns, y_bits in candidates:
                solution = calculate_class_cbs(
                    x_ns=x_ns,
                    y_bits=y_bits,
                    local_deadline_ns=group["requiredDeadlineNs"],
                    own_max_frame_bits=group["maxFrameBits"],
                    lower_priority_max_frame_bits=group["blockingFrameBits"],
                    link_speed_bps=group["linkSpeedBps"],
                    higher_priority_classes=hp,
                )
                solutions.append((solution.idle_slope_bps, x_ns, y_bits, solution))
            slope, x_ns, y_bits, chosen = max(solutions, key=lambda v: v[0])
            if slope + 1e-6 < group["aggregateRhoBps"]:
                raise ValueError(f"Unimplemented rate-floor fallback at {key}")
            for x, y in candidates:
                delay_ns = (
                    chosen.service_latency_s + y / slope - x / 1_000_000_000
                ) * 1_000_000_000
                if delay_ns > group["requiredDeadlineNs"] + 1e-4:
                    raise ValueError(f"M2 analytical local deadline exceeded at {key}")

            group["sharedXYPoint"] = {"xNs": x_ns, "yBits": y_bits}
            group["sharedCBS"] = {
                "idleSlopeBps": chosen.idle_slope_bps,
                "sendSlopeBps": chosen.send_slope_bps,
                "creditMinBits": chosen.credit_min_bits,
                "creditMaxBits": chosen.credit_max_bits,
            }
            group["sharedDelayNs"] = chosen.worst_case_delay_ns
            group["diagnosticBreakpointCount"] = len(candidates)
            pending.remove(key)
            progress = True
        if not progress:
            raise ValueError(f"M2 predecessor dependency is cyclic or unresolved: {sorted(pending)}")

    results = optimizer.calculate_shared_stream_delays(contexts, groups)
    return contexts, groups, results


def optimize_m1_m2_candidate(optimizer, usecase, *, blocking_background_streams=()):
    """Direct-call experimental M1+M2 configuration, not Manager-advertised."""
    target = list(getattr(usecase, "target_traffic_classes", []) or [])
    if sorted(target) != ["M1", "M2"]:
        raise ValueError("M1+M2 candidate requires exactly both target classes")
    if getattr(usecase, "optimization_task", None) != "configuration":
        raise ValueError("M1+M2 candidate supports configuration only; not reconfiguration")
    if not getattr(usecase, "fixed_routes", None):
        raise ValueError("M1+M2 candidate requires explicit fixed stream routes")
    all_streams = usecase.streams
    classes = {s.get("srClass") for s in all_streams}
    if classes != {"M1", "M2"}:
        raise ValueError(f"M1+M2 candidate rejects missing or unsupported classes: {classes}")
    stream_ids = [s["name"] for s in all_streams]
    if len(stream_ids) != len(set(stream_ids)):
        raise ValueError("Duplicate stream IDs")
    for stream in all_streams:
        if stream.get("maxFramesPerInterval") != 1:
            raise ValueError("M1+M2 candidate supports one frame per interval")
        if (len(stream.get("tlkrPssblPlcmnt", [])) != 1
                or len(stream.get("lstnsrPssblPlcmnt", [])) != 1
                or len(stream["lstnsrPssblPlcmnt"][0]) != 1):
            raise ValueError("M1+M2 candidate supports only fixed unicast endpoints")
        if (stream["name"] not in usecase.fixed_routes
                or not usecase.fixed_routes[stream["name"]].get("path")):
            raise ValueError(f"Missing explicit fixed path for stream {stream['name']}")
        if stream.get("beMaxFrameSize") is None or stream["beMaxFrameSize"] <= 0:
            raise ValueError(f"Explicit positive BE maximum frame size required for {stream['name']}")
        if (stream["interval"] <= 0 or stream["maxLatency"] <= 0
                or stream["maxFrameSize"] <= 0):
            raise ValueError(f"Invalid positive traffic parameters for {stream['name']}")

    # Additional SR traffic is used ONLY to calculate non-preemptive
    # blocking for higher classes; it is not optimized in this function.
    background = list(blocking_background_streams)
    if background:
        if any(s.get("srClass") != "M3" for s in background):
            raise ValueError("Only M3 background traffic is verified in Step 10")
        all_names = [s["name"] for s in all_streams + background]
        if len(all_names) != len(set(all_names)):
            raise ValueError("Duplicate optimized/background stream IDs")
        for stream in background:
            if stream.get("maxFramesPerInterval") != 1:
                raise ValueError("M3 blocking background must be single-frame")
            if stream["name"] not in usecase.fixed_routes:
                raise ValueError("Missing fixed route for M3 blocking background")

    # Recompute M1 and M2 from scratch, including every M3 frame as a
    # lower-priority blocker. Never adjust cached idleSlopes after the fact.
    blocking_usecase = copy(usecase)
    blocking_usecase.streams = all_streams + background
    m1_usecase = copy(blocking_usecase)
    m1_usecase.target_traffic_classes = ["M1"]
    m1_result = optimizer.optimize(m1_usecase)
    if m1_result.get("allDeadlinesSatisfied") is not True:
        raise ValueError("M1 baseline did not satisfy its analytical deadlines")
    m2_contexts, m2_groups, m2_results = _calculate_m2(
        optimizer, blocking_usecase, m1_result
    )

    groups = m1_result["portClassGroups"] + m2_groups
    stream_results = m1_result["sharedStreamResults"] + m2_results
    if len(stream_results) != len(all_streams):
        raise ValueError("Incomplete M1+M2 stream results")
    return {
        "optimizer": "CBS",
        "status": "stream parsing successful",
        "streams": m1_result["streams"] + m2_contexts,
        "portClassGroups": groups,
        "sharedStreamResults": stream_results,
        "overallWorstCaseDelayNs": max(r["sharedPathDelayNs"] for r in stream_results),
        "allDeadlinesSatisfied": all(r["deadlineSatisfied"] for r in stream_results),
        "validationNote": (
            "Experimental TC1-derived M1+M2 analytical configuration. "
            "No independent NC or live Manager/CNC validation performed."
        ),
    }

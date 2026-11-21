"""Direct-call, experimental M3 extension of the tested M1+M2 CBS pipeline.

This is an EXPERIMENTAL M3 configuration, not a general 3-class capability:
* M1 and M2 are recalculated from scratch with M3 streams supplied as
  non-target lower-priority traffic for same-port non-preemptive blocking.
* M3 is computed after the resulting higher-priority CBS reservations.
* Fixed unicast routes, one frame per interval, explicitly known BE frame
  size, configuration only, no mixed source/transit group; no rate-floor
  fallback or independent NC schedulability verification.
* It does not register the optimizer for M3 or change response contracts.

Zhao et al. (2024) Eqs. (4), (5), (23), (24) are evaluated by the existing
`calculate_class_cbs` helper, with higher-priority classes selected by the
Step 7 port-specific priority selector. Do not claim Zhao TC1 M3 reproduction:
TC1 has only M1 and M2.
"""
from __future__ import annotations

from copy import copy
import math

from .cbs_multiclass_math import calculate_class_cbs
from .cbs_multiclass_pipeline import (
    _m2_stream_context, _breakpoints, optimize_m1_m2_candidate,
)
from .cbs_priority_dependencies import higher_priority_for_port


def _validate_synthetic_m3_domain(usecase):
    targets = getattr(usecase, "target_traffic_classes", None)
    if targets is None or sorted(targets) != ["M1", "M2", "M3"]:
        raise ValueError("M3 candidate requires M1, M2, M3 target classes together")
    if getattr(usecase, "optimization_task", None) != "configuration":
        raise ValueError("M3 candidate supports configuration only")
    if not getattr(usecase, "fixed_routes", None):
        raise ValueError("M3 candidate requires fixed routes")
    streams = usecase.streams
    if {s.get("srClass") for s in streams} != {"M1", "M2", "M3"}:
        raise ValueError("M3 candidate requires exactly M1, M2, M3 stream classes")
    names = [s["name"] for s in streams]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate stream IDs")
    for s in streams:
        name = s["name"]
        if s.get("maxFramesPerInterval") != 1:
            raise ValueError(f"Only one frame per interval is supported: {name}")
        if (len(s.get("tlkrPssblPlcmnt", [])) != 1
                or len(s.get("lstnsrPssblPlcmnt", [])) != 1
                or len(s["lstnsrPssblPlcmnt"][0]) != 1):
            raise ValueError(f"Only fixed unicast streams supported: {name}")
        route = usecase.fixed_routes.get(name)
        if not route or not route.get("path"):
            raise ValueError(f"Missing fixed path: {name}")
        for field in ("maxFrameSize", "interval", "maxLatency", "beMaxFrameSize"):
            value = s.get(field)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"Invalid {field} in {name}")


def _calculate_m3(optimizer, usecase, higher_result):
    streams = [s for s in usecase.streams if s["srClass"] == "M3"]
    contexts = [_m2_stream_context(optimizer, s, usecase) for s in streams]
    groups = optimizer.group_streams_by_port_class(contexts)
    optimizer.calculate_class_local_deadlines(contexts, groups, "M3")
    for g in groups:
        deadline = g["theorem1DeadlineNs"]
        if not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("Invalid M3 Theorem-1 deadline")
        g["requiredDeadlineNs"] = deadline
    optimizer.update_group_bursts_from_theorem1(contexts, groups)

    all_topology_contexts = [
        optimizer.build_stream_topology_context(
            s, usecase.links, usecase.fixed_routes
        ) for s in usecase.streams
    ]
    optimizer.calculate_blocking_frames(
        all_topology_contexts, groups, {"M1": 0, "M2": 1, "M3": 2}
    )

    all_class_groups = higher_result["portClassGroups"] + groups
    by_key = {}
    for group in groups:
        key = (group["node"], group["port"], "M3")
        if key in by_key:
            raise ValueError(f"Duplicate M3 group: {key}")
        by_key[key] = group

    pending = set(by_key)
    while pending:
        progressed = False
        for key in sorted(pending):
            group = by_key[key]
            positions = group["streamPositions"]
            predecessors_raw = [p.get("predecessor") for p in positions]
            if any(p is None for p in predecessors_raw) and any(
                p is not None for p in predecessors_raw
            ):
                raise ValueError(f"Mixed source/transit M3 group unsupported: {key}")
            predecessors = {
                (p["node"], p["port"], "M3")
                for p in predecessors_raw if p is not None
            }
            if predecessors - set(by_key):
                raise ValueError(f"Missing M3 predecessor group at {key}")
            if any("sharedCBS" not in by_key[p] for p in predecessors):
                continue

            higher = higher_priority_for_port(group, all_class_groups)
            if predecessors:
                curve = optimizer.build_shared_m1_arrival_curve(
                    group, [by_key[p] for p in sorted(predecessors)]
                )
                candidates = _breakpoints(optimizer, curve)
                group["sharedArrivalCurve"] = curve
            else:
                candidates = [(0.0, group["aggregatePortBurstBits"])]

            solutions = []
            for x_ns, y_bits in candidates:
                param = calculate_class_cbs(
                    x_ns=x_ns,
                    y_bits=y_bits,
                    local_deadline_ns=group["requiredDeadlineNs"],
                    own_max_frame_bits=group["maxFrameBits"],
                    lower_priority_max_frame_bits=group["blockingFrameBits"],
                    link_speed_bps=group["linkSpeedBps"],
                    higher_priority_classes=higher,
                )
                solutions.append((param.idle_slope_bps, x_ns, y_bits, param))
            slope, x_ns, y_bits, chosen = max(solutions, key=lambda s: s[0])
            if slope + 1e-6 < group["aggregateRhoBps"]:
                raise ValueError(f"M3 unimplemented rate-floor fallback at {key}")
            for x, y in candidates:
                worst_ns = (
                    chosen.service_latency_s + y / slope - x / 1_000_000_000
                ) * 1_000_000_000
                if worst_ns > group["requiredDeadlineNs"] + 1e-4:
                    raise ValueError(f"M3 analytical local deadline exceeded at {key}")
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
            progressed = True
        if not progressed:
            raise ValueError(f"M3 predecessor dependency unresolved: {sorted(pending)}")

    results = optimizer.calculate_shared_stream_delays(contexts, groups)
    return contexts, groups, results


def optimize_m1_m2_m3_candidate(optimizer, usecase):
    """Run experimental M1+M2+M3 directly; no Manager capability registration."""
    _validate_synthetic_m3_domain(usecase)
    two_classes = copy(usecase)
    two_classes.streams = [s for s in usecase.streams if s["srClass"] in ("M1", "M2")]
    two_classes.target_traffic_classes = ["M1", "M2"]
    higher_result = optimize_m1_m2_candidate(
        optimizer, two_classes,
        blocking_background_streams=[
            s for s in usecase.streams if s["srClass"] == "M3"
        ],
    )

    m3_contexts, m3_groups, m3_results = _calculate_m3(
        optimizer, usecase, higher_result
    )
    stream_rows = higher_result["sharedStreamResults"] + m3_results
    if len(stream_rows) != len(usecase.streams) or {
        row["stream"] for row in stream_rows
    } != {s["name"] for s in usecase.streams}:
        raise ValueError("Incomplete M1+M2+M3 stream results")
    all_groups = higher_result["portClassGroups"] + m3_groups
    for group in all_groups:
        used = sum(
            g["sharedCBS"]["idleSlopeBps"] for g in all_groups
            if g["node"] == group["node"] and g["port"] == group["port"]
        )
        if used >= group["linkSpeedBps"]:
            raise ValueError("Combined M1/M2/M3 reservation exhausts port capacity")
    return {
        "optimizer": "CBS",
        "status": "stream parsing successful",
        "streams": higher_result["streams"] + m3_contexts,
        "portClassGroups": all_groups,
        "sharedStreamResults": stream_rows,
        "overallWorstCaseDelayNs": max(r["sharedPathDelayNs"] for r in stream_rows),
        "allDeadlinesSatisfied": all(r["deadlineSatisfied"] for r in stream_rows),
        "validationNote": (
            "Experimental M3 direct-call analytical candidate only; "
            "M1/M2 lower-priority blocking recalculated with M3 traffic; "
            "not independent NC verification or Manager/CNC integration."
        ),
    }

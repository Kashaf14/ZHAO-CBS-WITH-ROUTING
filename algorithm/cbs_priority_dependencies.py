"""Experimental class-priority dependencies for future Zhao N-class CBS.

This module builds the higher-priority inputs to Theorem 2 from actual
per-port traffic class groups. No changes to the original M1/M2 pipeline.

It intentionally recognizes only ordered CBS SR classes M1..M7, matching the
paper's 1 <= N_SR <= 7 indexed classes. Missing or uncomputed higher-priority
groups are rejected rather than treated as zero interference.
"""
from __future__ import annotations

import math

from .cbs_multiclass_math import HigherPriorityClass


def priority_index(traffic_class: str) -> int:
    """Lower numeric M class is higher priority: M1 > M2 > ... > M7."""
    if not isinstance(traffic_class, str) or traffic_class not in {
        f"M{i}" for i in range(1, 8)
    }:
        raise ValueError(f"Unsupported or unverified CBS SR class: {traffic_class!r}")
    return int(traffic_class[1:])


def higher_priority_for_port(current_group: dict, all_port_groups: list[dict]):
    """Return a tuple of HigherPriorityClass, ordered highest to lowest priority.

    `all_port_groups` MUST include every active SR class group on the port,
    even when no idleSlope is computed yet. This prevents accidentally
    omitting real higher-priority traffic.

    The blocking size of lower-priority traffic and BE remains handled by the
    existing `calculate_blocking_frames()` path. This function is ONLY about
    the CBS higher-priority terms in Zhao Eqs. (4), (5), and (23).
    """
    node = current_group["node"]
    port = current_group["port"]
    current_class = current_group["srClass"]
    current_priority = priority_index(current_class)
    current_speed = current_group["linkSpeedBps"]
    if (isinstance(current_speed, bool) or
            not isinstance(current_speed, (int, float)) or
            not math.isfinite(current_speed) or current_speed <= 0):
        raise ValueError(f"Invalid current link speed on {node}/{port}")

    matching = {}
    for group in all_port_groups:
        if group["node"] != node or group["port"] != port:
            continue
        traffic_class = group["srClass"]
        priority_index(traffic_class)
        if traffic_class in matching:
            raise ValueError(f"Duplicate CBS class on {node}/{port}: {traffic_class}")
        speed = group["linkSpeedBps"]
        if (isinstance(speed, bool) or not isinstance(speed, (int, float))
                or not math.isfinite(speed) or speed != current_speed):
            raise ValueError(f"Inconsistent link speed on {node}/{port}: {traffic_class}")
        matching[traffic_class] = group

    if current_class not in matching:
        raise ValueError(f"Current CBS group missing on {node}/{port}: {current_class}")

    higher = []
    for traffic_class in sorted(matching, key=priority_index):
        if priority_index(traffic_class) >= current_priority:
            break
        group = matching[traffic_class]
        slope = (group.get("sharedCBS") or {}).get("idleSlopeBps")
        frame = group.get("maxFrameBits")
        if (isinstance(slope, bool) or not isinstance(slope, (int, float))
                or not math.isfinite(slope) or not 0 < slope < current_speed):
            raise ValueError(
                f"Uncomputed or invalid higher-priority CBS reservation: "
                f"{node}/{port}/{traffic_class}"
            )
        if (isinstance(frame, bool) or not isinstance(frame, (int, float))
                or not math.isfinite(frame) or frame <= 0):
            raise ValueError(
                f"Invalid higher-priority max frame: {node}/{port}/{traffic_class}"
            )
        higher.append(HigherPriorityClass(
            idle_slope_bps=slope,
            max_frame_bits=frame,
        ))
    if sum(item.idle_slope_bps for item in higher) >= current_speed:
        raise ValueError(f"Higher-priority CBS reservations exhaust {node}/{port}")
    return tuple(higher)

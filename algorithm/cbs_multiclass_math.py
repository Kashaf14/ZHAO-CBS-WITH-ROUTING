"""Standalone CBS per-class calculation from Zhao et al. (2024), Eqs. (4), (5), (23), (24).

Scope: pure mathematical helper, NOT connected to the CBS optimizer pipeline.

Inputs are SI units: link_speed_bps and idleSlope in bit/s, frame lengths and
credits in *bits*, x/local_deadline in nanoseconds, y in bits. Higher-priority
classes refer to other CBS classes on the SAME egress port and must be supplied
in the correct priority order by the caller (the formula itself uses sums).

No guarantee of independent schedulability or network-wide minimum bandwidth
follows from using this helper by itself. The input arrival-curve critical point
(x,y) and local deadlines must first be correctly derived.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True)
class HigherPriorityClass:
    """Previously allocated CBS class on the same egress port.

    max_frame_bits is the largest frame of this class at that egress port,
    NOT its aggregate burst or downstream propagated burst.
    """

    idle_slope_bps: float
    max_frame_bits: float


@dataclass(frozen=True)
class ClassCBSParameters:
    idle_slope_bps: float
    send_slope_bps: float
    credit_min_bits: float
    credit_max_bits: float
    service_latency_s: float
    worst_case_delay_ns: float


def _finite_number(label: str, value: float, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{label} must be a real number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{label} must be > 0")
    if not positive and value < 0:
        raise ValueError(f"{label} must be >= 0")
    return value


def calculate_class_cbs(
    *,
    x_ns: float,
    y_bits: float,
    local_deadline_ns: float,
    own_max_frame_bits: float,
    lower_priority_max_frame_bits: float,
    link_speed_bps: float,
    higher_priority_classes: Sequence[HigherPriorityClass] = (),
) -> ClassCBSParameters:
    """Return candidate minimum CBS reservation and credit limits for one class.

    Zhao et al. Eq. (5): creditMin_j = (idleSlope_j-C)*L_j/C.
    Eq. (4): creditMax_i/idleSlope_i =
        (L_lower - sum(creditMin_j)) / (C - sum(idleSlope_j)).
    Eq. (23): idleSlope_i = y / (localDeadline + x - creditMax_i/idleSlope_i).
    Eq. (24): WCD_i = creditMax_i/idleSlope_i + y/idleSlope_i - x.

    The higher-priority inputs must already be calculated, and L_lower must be
    the largest frame length among *strictly lower-priority* traffic and BE at
    this port; the current class's frame length is supplied separately.

    Raises ValueError for invalid/infeasible local CBS parameters. This check is
    only local: it does not prove end-to-end deadline feasibility.
    """
    x_ns = _finite_number("x_ns", x_ns)
    y_bits = _finite_number("y_bits", y_bits, positive=True)
    local_deadline_ns = _finite_number("local_deadline_ns", local_deadline_ns, positive=True)
    own_max_frame_bits = _finite_number("own_max_frame_bits", own_max_frame_bits, positive=True)
    lower_priority_max_frame_bits = _finite_number(
        "lower_priority_max_frame_bits", lower_priority_max_frame_bits
    )
    link_speed_bps = _finite_number("link_speed_bps", link_speed_bps, positive=True)

    higher_idle_slope_sum = 0.0
    higher_credit_min_sum = 0.0
    for index, cls in enumerate(higher_priority_classes):
        if not isinstance(cls, HigherPriorityClass):
            raise ValueError(f"higher_priority_classes[{index}] must be HigherPriorityClass")
        hp_idle = _finite_number(f"higher_priority_classes[{index}].idle_slope_bps",
                                 cls.idle_slope_bps, positive=True)
        hp_frame = _finite_number(f"higher_priority_classes[{index}].max_frame_bits",
                                  cls.max_frame_bits, positive=True)
        if hp_idle >= link_speed_bps:
            raise ValueError(f"Higher-priority class {index} idleSlope >= link capacity")
        higher_idle_slope_sum += hp_idle
        higher_credit_min_sum += (hp_idle - link_speed_bps) * hp_frame / link_speed_bps

    residual_capacity = link_speed_bps - higher_idle_slope_sum
    if not math.isfinite(residual_capacity) or residual_capacity <= 0:
        raise ValueError("Higher-priority CBS reservations exhaust link capacity")

    # Equivalent to Eq. (4) after factoring out the still-unknown idleSlope_i.
    service_latency_s = (
        lower_priority_max_frame_bits - higher_credit_min_sum
    ) / residual_capacity
    available_s = (local_deadline_ns + x_ns) / 1_000_000_000 - service_latency_s
    if not math.isfinite(available_s) or available_s <= 0:
        raise ValueError("Deadline plus x is insufficient for CBS service latency")

    idle_slope_bps = y_bits / available_s
    if not math.isfinite(idle_slope_bps) or idle_slope_bps <= 0:
        raise ValueError("Calculated idleSlope is invalid")
    if idle_slope_bps >= residual_capacity:
        raise ValueError("CBS idleSlope plus higher-priority reservations reaches link capacity")

    send_slope_bps = idle_slope_bps - link_speed_bps
    credit_min_bits = send_slope_bps * own_max_frame_bits / link_speed_bps
    credit_max_bits = idle_slope_bps * service_latency_s
    worst_case_delay_ns = (
        service_latency_s + y_bits / idle_slope_bps - x_ns / 1_000_000_000
    ) * 1_000_000_000
    return ClassCBSParameters(
        idle_slope_bps=idle_slope_bps,
        send_slope_bps=send_slope_bps,
        credit_min_bits=credit_min_bits,
        credit_max_bits=credit_max_bits,
        service_latency_s=service_latency_s,
        worst_case_delay_ns=worst_case_delay_ns,
    )

"""CBS configuration quantization, separate from the analytical solver and JSON mapping.

Zhao Eqs. (4), (5) are evaluated with configured integer idle slopes.
This does not re-optimize or independently validate rounded deadline bounds.
"""
from __future__ import annotations

import math
from fractions import Fraction


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

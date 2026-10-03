"""Shared helper for family compute functions."""

from __future__ import annotations

from math import isfinite

NAN = float("nan")


def put(values: list[float], valid: list[bool], x, ok: bool) -> None:
    """Append one feature value.

    Appends ``float(x)`` with valid=True when ``ok`` and x is a finite number;
    otherwise appends NaN with valid=False.  This is the single funnel through
    which every feature value flows, so NaN can never leak into a valid=True
    slot (conventions §6).
    """
    if ok and x is not None:
        fx = float(x)
        if isfinite(fx):
            values.append(fx)
            valid.append(True)
            return
    values.append(NAN)
    valid.append(False)

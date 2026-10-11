"""Result types and statistics of the MVP engine: Pearson IC, the realized-IC
result and the per-parent outcome (split out of :mod:`iap.mvp.engine`,
v1.13 E4; re-exported there).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from iap.contracts.types import (
    AlphaSignal,
    Attribution,
    ParentOrder,
    TCAResult,
)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation (``None`` with fewer than 3 pairs or a degenerate side)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    return sxy / math.sqrt(sxx * syy)


@dataclass(frozen=True)
class IcResult:
    """Realized IC of one alpha at one horizon (module docstring).

    ``ic`` / ``ic_cost`` are the Pearson correlations of the expected
    return against the mid-to-mid / cost-adjusted label (``None`` with
    fewer than three valid pairs or a degenerate side); ``ic_shifted`` is
    the mid IC with the signal lagged by one decision (the shift-by-one
    leakage test); ``n`` counts the valid pairs, ``n_signals`` the
    confidence > 0 decisions the labels were requested for.
    """

    horizon: str
    ic: float | None
    ic_cost: float | None
    ic_shifted: float | None
    n: int
    n_signals: int


@dataclass
class OrderOutcome:
    """Everything the report needs about one finalised parent order."""

    parent: ParentOrder
    signal: AlphaSignal
    tca: TCAResult | None
    attribution: Attribution | None
    realized_bps: float
    filled_notional: float
    n_children_submitted: int
    n_children_rejected: int
    venue_qty: dict[int, int]
    latencies_ns: tuple[int, ...]

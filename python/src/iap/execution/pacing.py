"""Signal-aware pacing (v1.17 WS-B item B3, Python only, opt-in).

Three named opt-ins on ``ParentOrder``, each with the plain algo as its
control and each reading the parent's ``signal_source`` causally at every
decision (``iap.execution.signal``):

- ``pov_signal_pacing``: the POV participation of each TRADE is scaled by
  :func:`pacing_scale` of the signal — adverse (the price is expected to
  run away) participates MORE now, favourable LESS — and the target
  accumulates ``p_t * dV`` event by event instead of ``p * V``;
- ``slice_signal_tilt``: at every TWAP / VWAP / IS slice decision the
  REMAINING quantity is re-apportioned over the remaining slices with the
  current slice's weight multiplied by the same scale (renormalised, never
  negative, the last slice takes the rest);
- ``ac_signal_drift``: an Almgren-Chriss IS parent re-solves its trajectory
  at every slice over the remaining window with the signal's expected
  return as the AC drift term (``iap.execution.optimal.ac_trajectory``
  ``drift``).

``scale = clip(1 - gain * alignment_z, min_scale, max_scale)`` where
``alignment_z = -z`` for a buy and ``+z`` for a sell (positive = waiting is
expected to pay).  ``gain = 0`` or no reading gives ``scale = 1``: the plain
algo.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from iap.execution.urgency import alignment


@dataclass(frozen=True, slots=True)
class PacingParams:
    """Shape of the signal -> participation / slice scale map."""

    gain: float = 0.5  #: scale change per signal unit
    min_scale: float = 0.25  #: floor (>= 0: a tilt never goes negative)
    max_scale: float = 3.0  #: cap
    max_participation: float = 0.5  #: POV: ``p * scale`` never exceeds this
    max_age_ns: int = 10_000_000_000  #: stale readings count as no signal
    latency_ns: int = 0  #: read the signal as of ``t - latency_ns``

    def __post_init__(self) -> None:
        if self.gain < 0 or self.min_scale < 0 or self.max_scale < self.min_scale:
            raise ValueError("need gain >= 0 and 0 <= min_scale <= max_scale")
        if not 0.0 < self.max_participation < 1.0:
            raise ValueError("max_participation must be in (0, 1)")
        if self.max_age_ns < 0 or self.latency_ns < 0:
            raise ValueError("max_age_ns and latency_ns must be >= 0")


def pacing_scale(side: int, z: float | None, params: PacingParams) -> float:
    """``clip(1 - gain * alignment, min, max)``; ``1`` without a reading."""
    if z is None or not math.isfinite(z):
        return 1.0
    s = 1.0 - params.gain * alignment(side, z)
    return min(max(s, params.min_scale), params.max_scale)


def apportion(qty: int, weights: Sequence[float]) -> list[int]:
    """Largest-remainder apportionment of ``qty`` over ``weights`` (the
    pinned rule of ``iap.execution.algos.slice_quantities``)."""
    wsum = float(sum(weights))
    n = len(weights)
    if n == 0:
        return []
    if wsum <= 0.0:
        out = [0] * n
        out[-1] = qty
        return out
    q = [0] * n
    frac = []
    assigned = 0
    for i, w in enumerate(weights):
        target = float(qty) * w / wsum
        floor = math.floor(target)
        q[i] = int(floor)
        assigned += q[i]
        frac.append((-(target - floor), i))
    frac.sort()
    left = qty - assigned
    for k in range(n):
        if left <= 0:
            break
        q[frac[k][1]] += 1
        left -= 1
    return q


def tilted_slice_qty(remaining_qty: int, remaining_weights: Sequence[float], scale: float) -> int:
    """Quantity of the CURRENT slice: ``remaining_qty`` apportioned over
    ``remaining_weights`` with the first weight scaled by ``scale``; the
    last slice takes everything."""
    if remaining_qty <= 0:
        return 0
    if len(remaining_weights) <= 1:
        return remaining_qty
    w = [max(0.0, scale) * remaining_weights[0], *remaining_weights[1:]]
    return apportion(remaining_qty, w)[0]

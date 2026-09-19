"""Parent-order execution algorithms: TWAP / VWAP / POV / IS (spec section 17).

Python reference port of ``cpp/include/iap/execution/algos.hpp`` +
``algos.cpp``. A parent order is worked over an event-time window
``[start_ts, end_ts)`` by scheduling child orders; every schedule is a pure
function of the parent and the replayed stream (deterministic, no wall
clock).

Pinned schedules:

- **Slice decision times** (TWAP/VWAP/IS): slice ``i`` of ``N`` is due at
  ``due_i = start_ts + i * (end_ts - start_ts) // N`` (integer division)
  and is issued while processing the first event with ``exchange_ts >=
  due_i``. Slice weights map to integer child quantities by
  largest-remainder apportionment (floor each target, hand the remaining
  shares to the largest fractional parts, ties to the earlier slice) —
  quantities sum exactly to the parent qty.
- **TWAP**: equal weights (``w_i = 1``).
- **VWAP**: pinned U-shaped session volume curve (``time_of_day_default``):
  ``w_i = 1 + x_i^2``, ``x_i = (2i - (N-1)) / (N-1)`` (``N >= 2``; ``N = 1``
  takes all).
- **IS**: front-loaded exponential decay ``w_i = exp(-risk_aversion * i /
  max(1, N-1))``.
- **POV**: no precomputed slices. Tracks cumulative TRADE volume ``V(t)``
  of the parent's instrument inside the window; after each TRADE ``target =
  floor(participation * V(t))``; whenever the target exceeds the quantity
  COMMITTED (filled + still open/in-flight) a child covers the deficit
  (capped at ``max_child_qty`` and the parent's remainder).

Child sizing (pinned): a slice larger than ``max_child_qty`` is split into
``ceil(slice / max_child_qty)`` children, all decided at the same event.
Every child carries ``expire_ts = end_ts`` (simulator rule 7): no child
outlives its parent's window. TWAP/VWAP children are passive LIMIT orders
joining the same-side best at decision time (MARKET when that side is
empty); POV and IS children are MARKET orders. Venue: ``parent.venue_id``
or SOR-routed when ``venue_id == 0``. The scheduling itself lives in
``iap.execution.replay``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import List


class AlgoType(IntEnum):
    """Parent order algorithm."""

    TWAP = 0
    VWAP = 1
    POV = 2
    IS = 3


@dataclass(frozen=True, slots=True)
class ParentOrder:
    """A parent order and its algo parameters."""

    parent_id: int = 0
    instrument_id: int = 0
    venue_id: int = 0  #: 0 => SOR-routed
    side: int = 0  #: 0 = buy, 1 = sell
    qty: int = 0
    algo: AlgoType = AlgoType.TWAP
    start_ts: int = 0
    end_ts: int = 0
    slices: int = 8  #: TWAP / VWAP / IS
    participation: float = 0.05  #: POV
    risk_aversion: float = 1.0  #: IS
    max_child_qty: int = 1000


def slice_weights(parent: ParentOrder) -> List[float]:
    """Slice weights for TWAP/VWAP/IS (raises ValueError for POV or slices <= 0)."""
    if parent.algo == AlgoType.POV:
        raise ValueError("POV has no precomputed slice weights")
    n = parent.slices
    if n <= 0:
        raise ValueError("slices must be > 0")
    if n == 1:
        return [1.0]
    w: List[float] = []
    for i in range(n):
        if parent.algo == AlgoType.TWAP:
            w.append(1.0)
        elif parent.algo == AlgoType.VWAP:
            x = (2.0 * i - (n - 1)) / (n - 1)
            w.append(1.0 + x * x)
        else:  # IS
            w.append(math.exp(-parent.risk_aversion * i / float(n - 1)))
    return w


def slice_quantities(parent: ParentOrder) -> List[int]:
    """Integer child quantities per slice (largest remainder; sums to ``qty``)."""
    if parent.qty <= 0:
        raise ValueError("parent qty must be > 0")
    w = slice_weights(parent)
    wsum = 0.0
    for x in w:
        wsum += x
    n = len(w)
    q = [0] * n
    frac = []
    assigned = 0
    for i in range(n):
        target = float(parent.qty) * w[i] / wsum
        floor = math.floor(target)
        q[i] = int(floor)
        assigned += q[i]
        # Largest remainder; ties resolved toward the earlier slice.
        frac.append((-(target - floor), i))
    frac.sort()
    left = parent.qty - assigned
    for k in range(n):
        if left <= 0:
            break
        q[frac[k][1]] += 1
        left -= 1
    return q


def slice_times(parent: ParentOrder) -> List[int]:
    """Due time of each slice: ``start_ts + i * span // N``."""
    if parent.end_ts <= parent.start_ts:
        raise ValueError("parent window must have end_ts > start_ts")
    n = parent.slices
    if n <= 0:
        raise ValueError("slices must be > 0")
    span = parent.end_ts - parent.start_ts
    return [parent.start_ts + i * span // n for i in range(n)]

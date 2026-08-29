"""Event-time forward labels at pinned horizons (conventions §7, spec §13).

Given a per-instrument event-time mid series (one sample per book update:
``(exchange_ts, mid, half_spread)`` in price units) and a sorted list of
anchor timestamps (the feature emission times), this module computes, for
every anchor t and every horizon h in {10ms, 50ms, 100ms, 500ms, 1s, 5s,
10s, 30s, 1m, 5m, 15m}:

- ``mid``  : mid-to-mid forward return   m(t+h)/m(t) - 1
- ``cost`` : cost-adjusted forward return, crossing the half-spread at both
             ends (buy at the ask now, sell at the bid later):
             ((m(t+h) - hs(t+h)) - (m(t) + hs(t))) / m(t)

where m(t) / hs(t) is the mid / half-spread prevailing at t — the latest
series sample with ts <= t.

No-lookahead alignment (pinned):

- the anchor state uses only events with exchange_ts <= t (the present);
- the forward state at t+h is the prevailing state after all events with
  exchange_ts <= t+h — i.e. it is determined exclusively by the anchor state
  plus events *strictly after* the anchor (mid changes between t and t+h);
- a label is VALID only when the stream has been observed through t+h
  (``last_event_ts >= t + h``) and both endpoint mids exist.  Horizons that
  run past the end of the session are invalid, never extrapolated.

Implementation: one two-pointer sweep per horizon — anchors are
non-decreasing in time, so each of the 11 pointers only moves forward:
O(len(anchors) * H + len(series)) total, no per-anchor binary search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

_NS_MS = 1_000_000
_NS_S = 1_000_000_000

#: Pinned label horizons (order is normative for serialized outputs).
HORIZONS_NS: Dict[str, int] = {
    "10ms": 10 * _NS_MS,
    "50ms": 50 * _NS_MS,
    "100ms": 100 * _NS_MS,
    "500ms": 500 * _NS_MS,
    "1s": 1 * _NS_S,
    "5s": 5 * _NS_S,
    "10s": 10 * _NS_S,
    "30s": 30 * _NS_S,
    "1m": 60 * _NS_S,
    "5m": 300 * _NS_S,
    "15m": 900 * _NS_S,
}
HORIZON_ORDER = tuple(HORIZONS_NS)


@dataclass
class MidSeries:
    """Per-instrument event-time mid series (one sample per book update)."""

    ts: List[int] = field(default_factory=list)
    mid: List[float] = field(default_factory=list)
    half_spread: List[float] = field(default_factory=list)

    def append(self, ts: int, mid: float, half_spread: float) -> None:
        if self.ts and ts < self.ts[-1]:
            raise ValueError("MidSeries timestamps must be non-decreasing")
        self.ts.append(ts)
        self.mid.append(mid)
        self.half_spread.append(half_spread)

    def __len__(self) -> int:
        return len(self.ts)


@dataclass
class LabelResult:
    """Labels for one horizon across all anchors (parallel arrays)."""

    horizon: str
    mid: List[float]  # NaN where invalid
    cost: List[float]  # NaN where invalid
    valid: List[bool]


def compute_labels(
    anchors_ts: Sequence[int],
    series: MidSeries,
    last_event_ts: int,
    horizons: Sequence[str] = HORIZON_ORDER,
) -> Dict[str, LabelResult]:
    """Two-pointer forward-label sweep (see module docstring for semantics).

    ``anchors_ts`` must be non-decreasing.  ``last_event_ts`` is the
    timestamp of the last event observed for the instrument's stream — a
    label is only valid when ``anchor + horizon <= last_event_ts``.
    """
    n = len(anchors_ts)
    for i in range(1, n):
        if anchors_ts[i] < anchors_ts[i - 1]:
            raise ValueError("anchors_ts must be non-decreasing")
    for h in horizons:
        if h not in HORIZONS_NS:
            raise ValueError(f"unknown horizon {h!r}")

    ts = series.ts
    mids = series.mid
    hss = series.half_spread
    m = len(ts)
    nan = float("nan")

    # base pointer: latest series index with ts <= anchor
    out: Dict[str, LabelResult] = {}
    base_idx = [-1] * n
    j = -1
    for i in range(n):
        t = anchors_ts[i]
        while j + 1 < m and ts[j + 1] <= t:
            j += 1
        base_idx[i] = j

    for h in horizons:
        h_ns = HORIZONS_NS[h]
        lab_mid = [nan] * n
        lab_cost = [nan] * n
        lab_valid = [False] * n
        k = -1  # latest series index with ts <= anchor + h
        for i in range(n):
            t = anchors_ts[i]
            target = t + h_ns
            while k + 1 < m and ts[k + 1] <= target:
                k += 1
            b = base_idx[i]
            if b < 0 or k < 0 or last_event_ts < target:
                continue
            m0, hs0 = mids[b], hss[b]
            m1, hs1 = mids[k], hss[k]
            if m0 <= 0.0:
                continue
            lab_mid[i] = m1 / m0 - 1.0
            lab_cost[i] = ((m1 - hs1) - (m0 + hs0)) / m0
            lab_valid[i] = True
        out[h] = LabelResult(horizon=h, mid=lab_mid, cost=lab_cost,
                             valid=lab_valid)
    return out

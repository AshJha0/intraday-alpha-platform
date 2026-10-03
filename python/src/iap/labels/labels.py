"""Event-time forward labels at pinned horizons (conventions §7, spec §13).

Given a per-instrument event-time mid series (one sample per BOOK REFRESH:
``(exchange_ts, mid, half_spread, tradable)`` in price units) and a sorted
list of anchor timestamps (the feature emission times), this module computes,
for every anchor t and every horizon h in {10ms, 50ms, 100ms, 500ms, 1s, 5s,
10s, 30s, 1m, 5m, 15m}:

- ``mid``  : mid-to-mid forward return   m(t+h)/m(t) - 1
- ``cost`` : cost-adjusted forward return, crossing the half-spread at both
             ends (buy at the ask now, sell at the bid later):
             ((m(t+h) - hs(t+h)) - (m(t) + hs(t))) / m(t)

where m(t) / hs(t) is the mid / half-spread prevailing at t — the latest
series sample with ts <= t.

**Tradability (pinned, API_FEATURES §6).**  A series sample is ``tradable``
when, at that refresh, the merged book was two-sided (``book_ok``), NO venue
of the instrument was stale, and no venue reported HALT or AUCTION.  A label
is only a tradable forward return if the market was continuously observable
and open across the whole horizon, so a label at (t, h) is VALID only when
ALL of:

1. the stream was observed through t+h (``last_event_ts >= t + h``) —
   horizons running past the end of the session are invalid, never
   extrapolated (bit ``NOT_OBSERVED``);
2. an anchor sample exists with ``ts <= t``, its mid is > 0 (bit
   ``NO_ANCHOR``) and it is ``tradable`` (bit ``ANCHOR_NOT_TRADABLE``);
3. a forward sample exists with ``ts <= t + h`` (bit ``NO_FORWARD``), it is
   ``tradable`` and it is FRESH: ``t + h - ts_forward <= max_age_ns``
   (bit ``FORWARD_STALE``) — a mid frozen since the last quote of the day is
   not a price you could have traded at;
4. EVERY sample in ``(t, t + h]`` is tradable (bit ``BLACKOUT``) — a halt,
   a re-opening auction or a stale-venue gap anywhere inside the horizon
   invalidates the label, so a pre-halt signal is never credited with the
   reopen jump.

``max_age_ns`` is pinned as ``max(LABEL_MAX_AGE_FLOOR_NS, 2 x median
inter-sample gap of that instrument's series)`` (:func:`max_sample_age`):
a fixed floor of 5 s for equity books, scaled up for sparse FX streams
where a 15 s gap between LP quotes is normal, not a data outage.  On the
bundled equities the median gap is about 2 s, so the floor binds and a
forward mid older than 5 s invalidates the label (``forward_stale``).

Per-anchor reasons are returned as a bitmask (:class:`LabelReason`) so a
research report can say WHY a horizon has few usable rows.

No-lookahead alignment (pinned):

- the anchor state uses only events with exchange_ts <= t (the present);
- the forward state at t+h is the prevailing state after all events with
  exchange_ts <= t+h — i.e. it is determined exclusively by the anchor state
  plus events *strictly after* the anchor (mid changes between t and t+h).

Implementation: one two-pointer sweep per horizon plus a prefix count of
non-tradable samples — anchors are non-decreasing in time, so each of the 11
pointers only moves forward: O(len(anchors) * H + len(series)) total, no
per-anchor binary search.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

_NS_MS = 1_000_000
_NS_S = 1_000_000_000

#: Pinned label horizons (order is normative for serialized outputs).
HORIZONS_NS: dict[str, int] = {
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

#: Freshness floor for the prevailing-mid age (pinned, see module docstring).
LABEL_MAX_AGE_FLOOR_NS = 5 * _NS_S
#: Multiplier applied to the median inter-sample gap.
LABEL_MAX_AGE_GAP_MULT = 2


class LabelReason:
    """Bit flags recorded per (anchor, horizon) when a label is invalid."""

    OK = 0
    NOT_OBSERVED = 1 << 0  # stream ended before t + h
    NO_ANCHOR = 1 << 1  # no prevailing sample at t, or mid <= 0
    ANCHOR_NOT_TRADABLE = 1 << 2  # book not two-sided / stale / halted at t
    NO_FORWARD = 1 << 3  # no prevailing sample at t + h
    FORWARD_STALE = 1 << 4  # prevailing mid at t + h older than max_age
    BLACKOUT = 1 << 5  # a non-tradable sample inside (t, t + h]

    NAMES = (
        (NOT_OBSERVED, "not_observed"),
        (NO_ANCHOR, "no_anchor"),
        (ANCHOR_NOT_TRADABLE, "anchor_not_tradable"),
        (NO_FORWARD, "no_forward"),
        (FORWARD_STALE, "forward_stale"),
        (BLACKOUT, "blackout"),
    )

    @classmethod
    def describe(cls, mask: int) -> list[str]:
        """Reason names set in ``mask`` (pinned order)."""
        return [name for bit, name in cls.NAMES if mask & bit]


@dataclass
class MidSeries:
    """Per-instrument event-time mid series (one sample per book refresh).

    ``tradable[i]`` is False for a refresh whose merged book was one-sided,
    whose instrument had a stale venue, or whose venues reported HALT /
    AUCTION.  Non-tradable samples carry ``mid = nan``.
    """

    ts: list[int] = field(default_factory=list)
    mid: list[float] = field(default_factory=list)
    half_spread: list[float] = field(default_factory=list)
    tradable: list[bool] = field(default_factory=list)

    def append(self, ts: int, mid: float, half_spread: float, tradable: bool = True) -> None:
        if self.ts and ts < self.ts[-1]:
            raise ValueError("MidSeries timestamps must be non-decreasing")
        self.ts.append(ts)
        self.mid.append(mid)
        self.half_spread.append(half_spread)
        self.tradable.append(bool(tradable))

    def __len__(self) -> int:
        return len(self.ts)

    def median_gap_ns(self) -> int:
        """Median gap between DISTINCT sample timestamps (0 with < 2).

        Several venue refreshes routinely share one ``exchange_ts`` (a
        consolidated book refreshes once per venue message); counting those
        zero gaps would collapse the median to 0 and make the freshness
        bound far too tight for a sparse FX stream.  The quote *cadence* is
        the gap between distinct instants.
        """
        seen: list[int] = []
        for t in self.ts:
            if not seen or t != seen[-1]:
                seen.append(t)
        if len(seen) < 2:
            return 0
        gaps = sorted(seen[i + 1] - seen[i] for i in range(len(seen) - 1))
        m = len(gaps)
        return int(gaps[m // 2] if m % 2 else (gaps[m // 2 - 1] + gaps[m // 2]) // 2)


def max_sample_age(series: MidSeries) -> int:
    """Pinned freshness bound for the prevailing mid of an instrument."""
    return max(LABEL_MAX_AGE_FLOOR_NS, LABEL_MAX_AGE_GAP_MULT * series.median_gap_ns())


@dataclass
class LabelResult:
    """Labels for one horizon across all anchors (parallel arrays)."""

    horizon: str
    mid: list[float]  # NaN where invalid
    cost: list[float]  # NaN where invalid
    valid: list[bool]
    reason: list[int] = field(default_factory=list)  # LabelReason bitmask
    #: realised reopen return per anchor (``blackout_reopen=True`` only;
    #: empty otherwise): NaN except where the label is invalid for BLACKOUT
    #: alone and a tradable sample exists at or after t + h
    reopen_mid: list[float] = field(default_factory=list)


def compute_labels(
    anchors_ts: Sequence[int],
    series: MidSeries,
    last_event_ts: int,
    horizons: Sequence[str] = HORIZON_ORDER,
    max_age_ns: int | None = None,
    blackout_reopen: bool = False,
) -> dict[str, LabelResult]:
    """Two-pointer forward-label sweep (see module docstring for semantics).

    ``anchors_ts`` must be non-decreasing.  ``last_event_ts`` is the
    timestamp of the last event observed for the instrument's stream.
    ``max_age_ns`` defaults to :func:`max_sample_age` of the series.

    ``blackout_reopen`` (opt-in; the labels themselves are unchanged) also
    fills :attr:`LabelResult.reopen_mid`: for an anchor whose label is
    invalid for ``BLACKOUT`` and nothing else, the return from the anchor
    mid to the FIRST tradable mid at or after ``t + h`` — what the position
    was actually worth once the market could be traded again.  It is not a
    tradable ``h``-horizon return (which is why the label stays invalid);
    it exists so the selection the BLACKOUT rule makes can be measured
    (:func:`iap.validation.metrics.ic_with_blackout_reopen`).
    """
    n = len(anchors_ts)
    for i in range(1, n):
        if anchors_ts[i] < anchors_ts[i - 1]:
            raise ValueError("anchors_ts must be non-decreasing")
    for h in horizons:
        if h not in HORIZONS_NS:
            raise ValueError(f"unknown horizon {h!r}")
    if max_age_ns is None:
        max_age_ns = max_sample_age(series)
    if max_age_ns <= 0:
        raise ValueError("max_age_ns must be positive")

    ts = series.ts
    mids = series.mid
    hss = series.half_spread
    tradable = series.tradable
    m = len(ts)
    nan = float("nan")

    # prefix count of NON-tradable samples: bad[i] = count over ts[:i]
    bad = [0] * (m + 1)
    for i in range(m):
        bad[i + 1] = bad[i] + (0 if tradable[i] else 1)

    # next_tradable[i] = smallest index >= i of a tradable sample with a
    # positive mid (m when none) — only needed for the reopen return
    next_tradable = [m] * (m + 1)
    if blackout_reopen:
        for i in range(m - 1, -1, -1):
            next_tradable[i] = i if (tradable[i] and mids[i] > 0.0) else next_tradable[i + 1]

    # base pointer: latest series index with ts <= anchor
    out: dict[str, LabelResult] = {}
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
        lab_reason = [0] * n
        lab_reopen = [nan] * n if blackout_reopen else []
        k = -1  # latest series index with ts <= anchor + h
        for i in range(n):
            t = anchors_ts[i]
            target = t + h_ns
            while k + 1 < m and ts[k + 1] <= target:
                k += 1
            b = base_idx[i]
            reason = 0
            if last_event_ts < target:
                reason |= LabelReason.NOT_OBSERVED
            if b < 0:
                reason |= LabelReason.NO_ANCHOR
            elif not tradable[b]:
                reason |= LabelReason.ANCHOR_NOT_TRADABLE
            elif not (mids[b] > 0.0):
                reason |= LabelReason.NO_ANCHOR
            if k < 0:
                reason |= LabelReason.NO_FORWARD
            else:
                if not tradable[k] or not (mids[k] > 0.0):
                    reason |= LabelReason.BLACKOUT
                if target - ts[k] > max_age_ns:
                    reason |= LabelReason.FORWARD_STALE
            if b >= 0 and k >= b and bad[k + 1] - bad[b + 1] > 0:
                reason |= LabelReason.BLACKOUT
            lab_reason[i] = reason
            if reason:
                if blackout_reopen and reason == LabelReason.BLACKOUT:
                    # the forward sample itself when it is tradable (the
                    # blackout was inside the horizon), else the first
                    # tradable sample after it (the reopen)
                    r = next_tradable[k]
                    if r < m:
                        lab_reopen[i] = mids[r] / mids[b] - 1.0
                continue
            m0, hs0 = mids[b], hss[b]
            m1, hs1 = mids[k], hss[k]
            lab_mid[i] = m1 / m0 - 1.0
            lab_cost[i] = ((m1 - hs1) - (m0 + hs0)) / m0
            lab_valid[i] = True
        out[h] = LabelResult(
            horizon=h,
            mid=lab_mid,
            cost=lab_cost,
            valid=lab_valid,
            reason=lab_reason,
            reopen_mid=lab_reopen,
        )
    return out

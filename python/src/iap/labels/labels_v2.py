"""Labels v2 — the 15 m / 30 m / 1 h horizons with session-close truncation (v1.17 C4).

The pinned label set (:mod:`iap.labels.labels`, ``HORIZONS_NS``, 10 ms to
15 m) is fingerprinted by the open pre-registrations and by
``iap.alpha.base.VALID_HORIZONS``; this module ADDS the longer horizons in
a separate contract and changes nothing there.

**Horizons** (:data:`HORIZONS_V2_NS`): ``15m``, ``30m``, ``1h``.  ``15m``
is the one horizon shared with v1: computed here it carries the v2
session rule below, so a ``15m`` v2 label is not the v1 ``15m`` label on
the last 15 minutes of a session.

**Session-spanning rule (pinned).**  No horizon crosses a session close.
For an anchor ``t`` with the session closing at ``close``:

- ``t >= close``: the label is INVALID (bit ``AFTER_CLOSE``);
- ``t + h <= close``: the forward instant is ``t + h`` as in v1;
- ``t + h > close``: the forward instant is ``close`` — the label is the
  return to the LAST tradable state at or before the close, it is VALID
  when that state passes the v1 rules (observed, fresh, no blackout, both
  ends tradable) and it is FLAGGED ``truncated`` with its
  ``effective_horizon_ns = close - t``.  A truncated label is a real,
  tradable return over a shorter horizon (the position had to be flat at
  the close), not an ``h``-horizon return: a consumer that wants only
  full-horizon labels filters on ``truncated``; one that models the
  session book keeps them.

The v1 rules are unchanged otherwise: ``NOT_OBSERVED`` (stream ended
before the forward instant), ``NO_ANCHOR`` / ``ANCHOR_NOT_TRADABLE``,
``NO_FORWARD`` / ``FORWARD_STALE`` against the same freshness bound
(:func:`iap.labels.labels.max_sample_age` or the trailing bound) and
``BLACKOUT`` for any non-tradable sample in ``(t, forward]``.  The cost
label crosses the half-spread at both ends exactly as v1.

**Frame columns** (:func:`label_columns_v2`): ``label_mid_<h>_v2``,
``label_cost_<h>_v2``, ``label_valid_<h>_v2``, ``label_reason_<h>_v2``,
``label_truncated_<h>_v2``, ``label_horizon_ns_<h>_v2`` — the ``_v2``
suffix keeps them apart from the v1 columns of the same horizon name.

Implementation: the same two-pointer sweep as v1 over a per-anchor forward
instant; O(len(anchors) x H + len(series)).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from iap.labels.labels import (
    FRESHNESS_MODES,
    LABEL_MAX_AGE_FLOOR_NS,
    LabelReason,
    MidSeries,
    max_sample_age,
    trailing_max_sample_age,
)

__all__ = [
    "HORIZONS_V2_NS",
    "HORIZON_V2_ORDER",
    "LABELS_V2_VERSION",
    "LabelReasonV2",
    "LabelResultV2",
    "compute_labels_v2",
    "label_columns_v2",
    "session_close_ts",
]

_NS_S = 1_000_000_000
_NS_DAY = 86_400 * _NS_S

#: Pinned v2 horizons (order is normative for serialized outputs).
HORIZONS_V2_NS: dict[str, int] = {
    "15m": 900 * _NS_S,
    "30m": 1_800 * _NS_S,
    "1h": 3_600 * _NS_S,
}
HORIZON_V2_ORDER = tuple(HORIZONS_V2_NS)
#: Contract version of this module's outputs.
LABELS_V2_VERSION = 1


class LabelReasonV2(LabelReason):
    """The v1 bits plus ``AFTER_CLOSE`` (anchor at or after the session close)."""

    AFTER_CLOSE = 1 << 6

    NAMES = (*LabelReason.NAMES, (AFTER_CLOSE, "after_close"))


@dataclass
class LabelResultV2:
    """Labels for one v2 horizon across all anchors (parallel arrays)."""

    horizon: str
    mid: list[float]  # NaN where invalid
    cost: list[float]  # NaN where invalid
    valid: list[bool]
    reason: list[int] = field(default_factory=list)  # LabelReasonV2 bitmask
    #: True where the forward instant was pulled back to the session close
    truncated: list[bool] = field(default_factory=list)
    #: the horizon actually measured (``h``, or ``close - t`` when truncated;
    #: 0 where the anchor is at or after the close)
    effective_horizon_ns: list[int] = field(default_factory=list)


def session_close_ts(ts_ns: int, close_second_of_day: int, utc_offset_s: int = 0) -> int:
    """The session close instant (UTC ns) of the SESSION-LOCAL day holding
    ``ts_ns``: ``close_second_of_day`` seconds after that day's local
    midnight, ``utc_offset_s`` the zone's offset at ``ts_ns``
    (:meth:`iap.features.context.SessionClock.offset_seconds`)."""
    local = int(ts_ns) + int(utc_offset_s) * _NS_S
    midnight_local = (local // _NS_DAY) * _NS_DAY
    return midnight_local + int(close_second_of_day) * _NS_S - int(utc_offset_s) * _NS_S


def label_columns_v2(horizon: str) -> dict[str, str]:
    """The frame column names of one v2 horizon (module docs)."""
    if horizon not in HORIZONS_V2_NS:
        raise ValueError(f"unknown v2 horizon {horizon!r}; known: {HORIZON_V2_ORDER}")
    return {
        "mid": f"label_mid_{horizon}_v2",
        "cost": f"label_cost_{horizon}_v2",
        "valid": f"label_valid_{horizon}_v2",
        "reason": f"label_reason_{horizon}_v2",
        "truncated": f"label_truncated_{horizon}_v2",
        "horizon_ns": f"label_horizon_ns_{horizon}_v2",
    }


def compute_labels_v2(
    anchors_ts: Sequence[int],
    series: MidSeries,
    last_event_ts: int,
    session_close: int | Sequence[int],
    horizons: Sequence[str] = HORIZON_V2_ORDER,
    max_age_ns: int | None = None,
    freshness: str = "whole_day",
) -> dict[str, LabelResultV2]:
    """Forward labels at the v2 horizons, truncated at the session close
    (module docs).

    ``anchors_ts`` non-decreasing; ``session_close`` is one close instant
    (every anchor in one session) or one per anchor (a multi-session anchor
    list: each anchor's own close, non-decreasing).  ``max_age_ns`` /
    ``freshness`` as :func:`iap.labels.labels.compute_labels`.
    """
    n = len(anchors_ts)
    for i in range(1, n):
        if anchors_ts[i] < anchors_ts[i - 1]:
            raise ValueError("anchors_ts must be non-decreasing")
    for h in horizons:
        if h not in HORIZONS_V2_NS:
            raise ValueError(f"unknown v2 horizon {h!r}; known: {HORIZON_V2_ORDER}")
    if freshness not in FRESHNESS_MODES:
        raise ValueError(f"unknown freshness {freshness!r}; known: {FRESHNESS_MODES}")
    if isinstance(session_close, int | np.integer):
        closes = [int(session_close)] * n
    else:
        closes = [int(c) for c in session_close]
        if len(closes) != n:
            raise ValueError("session_close must be one instant or one per anchor")
        for i in range(1, n):
            if closes[i] < closes[i - 1]:
                raise ValueError("per-anchor session_close must be non-decreasing")
    age_by_sample: list[int] | None = None
    if freshness == "trailing":
        if max_age_ns is not None:
            raise ValueError("freshness='trailing' derives the bound; do not pass max_age_ns")
        age_by_sample = trailing_max_sample_age(series)
        max_age_ns = LABEL_MAX_AGE_FLOOR_NS
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

    bad = [0] * (m + 1)
    for i in range(m):
        bad[i + 1] = bad[i] + (0 if tradable[i] else 1)

    base_idx = [-1] * n
    j = -1
    for i in range(n):
        t = anchors_ts[i]
        while j + 1 < m and ts[j + 1] <= t:
            j += 1
        base_idx[i] = j

    out: dict[str, LabelResultV2] = {}
    for h in horizons:
        h_ns = HORIZONS_V2_NS[h]
        lab_mid = [nan] * n
        lab_cost = [nan] * n
        lab_valid = [False] * n
        lab_reason = [0] * n
        lab_trunc = [False] * n
        lab_eff = [0] * n
        k = -1  # latest series index with ts <= forward instant (non-decreasing)
        for i in range(n):
            t = anchors_ts[i]
            close = closes[i]
            if t >= close:
                lab_reason[i] = LabelReasonV2.AFTER_CLOSE
                continue
            target = t + h_ns
            if target > close:
                target = close
                lab_trunc[i] = True
            lab_eff[i] = target - t
            while k + 1 < m and ts[k + 1] <= target:
                k += 1
            b = base_idx[i]
            reason = 0
            if last_event_ts < target:
                reason |= LabelReasonV2.NOT_OBSERVED
            if b < 0:
                reason |= LabelReasonV2.NO_ANCHOR
            elif not tradable[b]:
                reason |= LabelReasonV2.ANCHOR_NOT_TRADABLE
            elif not (mids[b] > 0.0):
                reason |= LabelReasonV2.NO_ANCHOR
            if k < 0:
                reason |= LabelReasonV2.NO_FORWARD
            else:
                if not tradable[k] or not (mids[k] > 0.0):
                    reason |= LabelReasonV2.BLACKOUT
                bound = max_age_ns if age_by_sample is None else age_by_sample[k]
                if target - ts[k] > bound:
                    reason |= LabelReasonV2.FORWARD_STALE
            if b >= 0 and k >= b and bad[k + 1] - bad[b + 1] > 0:
                reason |= LabelReasonV2.BLACKOUT
            lab_reason[i] = reason
            if reason:
                continue
            m0, hs0 = mids[b], hss[b]
            m1, hs1 = mids[k], hss[k]
            lab_mid[i] = m1 / m0 - 1.0
            lab_cost[i] = ((m1 - hs1) - (m0 + hs0)) / m0
            lab_valid[i] = True
        out[h] = LabelResultV2(
            horizon=h,
            mid=lab_mid,
            cost=lab_cost,
            valid=lab_valid,
            reason=lab_reason,
            truncated=lab_trunc,
            effective_horizon_ns=lab_eff,
        )
    return out

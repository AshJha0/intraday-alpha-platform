"""Time-of-day family (spec §10, "Time of day").

Clock features are pure functions of the event timestamp (UTC) and the
instrument's configured session; normalized features divide the current
value of a metric by the expanding mean of that metric in the same 5-minute
bucket of the day, built online from the data itself (the profile is updated
*after* each emission reads it, so a value is never normalized by a profile
that already contains it — no lookahead).

- ``minute_of_day``   = minutes since UTC midnight (float, fractional)
- ``session_frac``    = (minute_of_day - open) / (close - open), clipped to [0,1]
- ``is_open_phase``   = 1 during the first 30 minutes of the session
- ``is_close_phase``  = 1 during the last 30 minutes of the session
- ``is_trading`` / ``is_auction`` / ``is_halt``: consolidated session status
  flags from per-venue STATUS events (halt if ANY venue halted; auction if
  any venue in auction and none halted; trading otherwise)
- ``norm_volume_m5``  = traded_volume_w1m / profile mean of that bucket
- ``norm_spread_m5``  = spread_bps       / profile mean
- ``norm_vol_m5``     = rvol_w1m         / profile mean
- ``norm_depth_m5``   = quoted_depth_total / profile mean

Validity: clock and status flags are always valid; normalized features need
the underlying metric to be valid, >= 10 prior profile observations in the
bucket, and a strictly positive profile mean.
"""

from __future__ import annotations

from typing import List

from iap.features._famutil import put
from iap.features.rolling import SessionProfile
from iap.features.spec import WINDOW_NS, FeatureSpec, mkspec

FAMILY = "tod"

#: Profile metric keys (pinned; see engine._profile_metrics for the values).
PROFILE_METRICS = ("volume", "spread", "vol", "depth")
OPEN_CLOSE_PHASE_MIN = 30


def specs() -> List[FeatureSpec]:
    """Registry entries for the time-of-day family (pinned order)."""
    out: List[FeatureSpec] = []
    out.append(mkspec("minute_of_day_v1", FAMILY,
                      "Minutes since UTC midnight (fractional)."))
    out.append(mkspec("session_frac_v1", FAMILY,
                      "Fraction of the configured session elapsed, clipped to [0,1]."))
    out.append(mkspec("is_open_phase_v1", FAMILY,
                      f"1 during the first {OPEN_CLOSE_PHASE_MIN} session minutes."))
    out.append(mkspec("is_close_phase_v1", FAMILY,
                      f"1 during the last {OPEN_CLOSE_PHASE_MIN} session minutes."))
    out.append(mkspec("is_trading_v1", FAMILY,
                      "1 when no venue reports HALT or AUCTION."))
    out.append(mkspec("is_auction_v1", FAMILY,
                      "1 when any venue reports AUCTION (and none HALT)."))
    out.append(mkspec("is_halt_v1", FAMILY, "1 when any venue reports HALT."))
    docs = {
        "volume": "traded_volume_w1m normalized by its expanding 5-minute-of-day "
                  "session profile mean (>= 10 prior observations).",
        "spread": "spread_bps normalized by its expanding 5-minute-of-day "
                  "session profile mean.",
        "vol": "rvol_w1m normalized by its expanding 5-minute-of-day "
               "session profile mean.",
        "depth": "quoted_depth_total normalized by its expanding "
                 "5-minute-of-day session profile mean.",
    }
    deps = {"volume": ("traded_volume_w1m_v1",), "spread": ("spread_bps_v1",),
            "vol": ("rvol_w1m_v1",), "depth": ("quoted_depth_total_v1",)}
    for m in PROFILE_METRICS:
        out.append(mkspec(f"norm_{m}_m5_v1", FAMILY, docs[m],
                          depends_on=deps[m], bucket_minutes=5))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 11 time-of-day values for the current emission."""
    t = st.t
    minute = ((t // 1_000_000_000) % 86_400) / 60.0 + ((t % 1_000_000_000) / 6e10)
    put(values, valid, minute, True)
    o, c = st.ctx.session_open_min, st.ctx.session_close_min
    frac = (minute - o) / (c - o) if c > o else 0.0
    put(values, valid, min(max(frac, 0.0), 1.0), True)
    put(values, valid, 1.0 if o <= minute < o + OPEN_CLOSE_PHASE_MIN else 0.0, True)
    put(values, valid, 1.0 if c - OPEN_CLOSE_PHASE_MIN <= minute < c else 0.0, True)
    put(values, valid, 1.0 if (not st.halt and not st.auction) else 0.0, True)
    put(values, valid, 1.0 if (st.auction and not st.halt) else 0.0, True)
    put(values, valid, 1.0 if st.halt else 0.0, True)
    bucket = SessionProfile.bucket_of(t)
    for m in PROFILE_METRICS:
        cur = _metric_value(st, m)
        v = None
        if cur is not None:
            n, mean = st.profile.prior(m, bucket)
            if n >= SessionProfile.MIN_OBS and mean > 0.0:
                v = cur / mean
        put(values, valid, v, v is not None)


def _metric_value(st, metric: str):
    """Current value of a profile metric, or None when not computable.

    Pinned mapping: volume -> traded_volume_w1m, spread -> spread_bps,
    vol -> rvol_w1m, depth -> quoted_depth_total.  The engine uses this same
    function when it folds the post-emission observation into the profile.
    """
    if metric == "volume":
        return st.trades["1m"].sums[1] if st.warm(WINDOW_NS["1m"]) else None
    if metric == "spread":
        return st.spread_bps if st.book_ok else None
    if metric == "vol":
        return st.rvol("1m")
    if metric == "depth":
        return float(st.db10 + st.da10) if st.book_ok else None
    raise ValueError(f"unknown profile metric {metric!r}")

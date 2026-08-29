"""Liquidity family (spec §10, "Liquidity").

Formulas (windows are half-open event-time intervals (t-w, t]):

- ``quoted_depth_total``   = depth_bid_l10 + depth_ask_l10 (two-sided top-10 size)
- ``quoted_depth_ratio``   = depth_bid_l10 / quoted_depth_total
- ``quoted_depth_avg_w``   = mean quoted_depth_total sampled at book refreshes over w
- ``effective_spread_bps_w`` = mean over TRADEs in w of 2*|trade_price - mid|/mid*1e4
                             (mid = prevailing consolidated mid at the trade)
- ``traded_volume_w``      = summed TRADE qty over w
- ``participation_w``      = traded_volume_w / (traded_volume_w + quoted_depth_avg_w)
                             (activity vs standing liquidity proxy in [0, 1))
- ``depth_slope_{side}_v1``= price-impact slope of that side of the book:
                             ((P1 - Pk)*tick/mid*1e4) / cum_depth_k for bid
                             (ask uses Pk - P1), over the best k = min(5, levels)
                             price levels; needs >= 2 levels.  Units: bps per
                             quantity unit — smaller is deeper/flatter.
- ``resiliency_halflife``  = ln(2) * (Qb + Qa)/2 / (L1 replenishment rate over
                             10s, both sides + EPS).  Seconds needed to
                             rebuild half the current L1 depth at the observed
                             replenishment pace (resiliency proxy; larger =
                             less resilient).

Validity: instantaneous features need book_ok; windowed features need warmup;
effective spread needs at least one mid-valid TRADE in the window.
"""

from __future__ import annotations

from math import log
from typing import List

from iap.features._famutil import put
from iap.features.spec import EPS, WINDOW_NS, FeatureSpec, mkspec

FAMILY = "liquidity"

WINDOWS = ("10s", "1m")
LN2 = log(2.0)


def specs() -> List[FeatureSpec]:
    """Registry entries for the liquidity family (pinned order)."""
    out: List[FeatureSpec] = []
    out.append(mkspec("quoted_depth_total_v1", FAMILY,
                      "Two-sided quoted depth over the best 10 levels.",
                      depends_on=("depth_bid_l10_v1", "depth_ask_l10_v1")))
    out.append(mkspec("quoted_depth_ratio_v1", FAMILY,
                      "Bid share of two-sided top-10 depth: bid/(bid+ask).",
                      depends_on=("quoted_depth_total_v1",)))
    for w in WINDOWS:
        out.append(mkspec(f"quoted_depth_avg_w{w}_v1", FAMILY,
                          f"Mean two-sided top-10 quoted depth over {w}.",
                          depends_on=("quoted_depth_total_v1",), window=w))
    for w in WINDOWS:
        out.append(mkspec(
            f"effective_spread_bps_w{w}_v1", FAMILY,
            f"Mean effective spread over TRADEs in {w}: 2*|price-mid|/mid*1e4.",
            window=w))
    for w in WINDOWS:
        out.append(mkspec(f"traded_volume_w{w}_v1", FAMILY,
                          f"Total TRADE qty over {w}.", window=w))
    for w in WINDOWS:
        out.append(mkspec(
            f"participation_w{w}_v1", FAMILY,
            f"Trading activity vs liquidity over {w}: "
            f"volume/(volume + mean quoted depth).",
            depends_on=(f"traded_volume_w{w}_v1", f"quoted_depth_avg_w{w}_v1"),
            window=w))
    for side in ("bid", "ask"):
        out.append(mkspec(
            f"depth_slope_{side}_v1", FAMILY,
            f"Book slope of the {side} side: bps distance from L1 to L<=5 "
            f"divided by the cumulative depth through that level.",
            side=side, levels=5))
    out.append(mkspec(
        "resiliency_halflife_v1", FAMILY,
        "Seconds to rebuild half the current L1 depth at the 10s "
        "replenishment rate: ln(2)*(Qb+Qa)/2 / (rep_rate_10s + EPS).",
        depends_on=("queue_replenish_rate_bid_w10s_v1",
                    "queue_replenish_rate_ask_w10s_v1"),
        window="10s"))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 13 liquidity values for the current emission."""
    ok = st.book_ok
    qd = (st.db10 + st.da10) if ok else None
    put(values, valid, qd, ok)
    put(values, valid, st.db10 / qd if ok and qd else None, ok and bool(qd))
    depth_means = {}
    for w in WINDOWS:
        win = st.depthavg[w]
        wok = st.warm(WINDOW_NS[w]) and win.count > 0
        if wok:
            depth_means[w] = win.sums[11] / win.count  # dtot slot
        put(values, valid, depth_means.get(w), wok)
    for w in WINDOWS:
        tw = st.trades[w]
        wok = st.warm(WINDOW_NS[w]) and tw.sums[5] > 0  # eff_n
        put(values, valid, tw.sums[4] / tw.sums[5] if wok else None, wok)
    vols = {}
    for w in WINDOWS:
        wok = st.warm(WINDOW_NS[w])
        if wok:
            vols[w] = st.trades[w].sums[1]
        put(values, valid, vols.get(w), wok)
    for w in WINDOWS:
        v = None
        if w in vols and w in depth_means:
            v = vols[w] / (vols[w] + depth_means[w] + EPS)
        put(values, valid, v, v is not None)
    for side, levels in (("bid", st.depth_bid), ("ask", st.depth_ask)):
        slope = None
        if ok and len(levels) >= 2:
            k = min(5, len(levels))
            p1 = levels[0][0]
            pk = levels[k - 1][0]
            cum = sum(q for _, q in levels[:k])
            dist_bps = abs(p1 - pk) * st.tick / st.mid * 1e4
            slope = dist_bps / cum if cum > 0 else None
        put(values, valid, slope, slope is not None)
    hl = None
    if ok and st.warm(WINDOW_NS["10s"]):
        rep = (st.queue["10s"].sums[1] + st.queue["10s"].sums[3]) / 10.0
        hl = LN2 * (st.bid_q + st.ask_q) / 2.0 / (rep + EPS)
    put(values, valid, hl, hl is not None)

"""Regime family (spec §10, "Regime").

- ``trend_score_w``   = (ln mid(t) - ln mid(t-w)) / (rvol_w * sqrt(w_s) + EPS)
                        — t-stat-like normalized drift over the window
- ``meanrev_score_w`` = -(mid2 - mean(mid2 over w)) / (std(mid2 over w) + EPS)
                        — z-score reversion signal (positive = mid below its
                        window mean, expect upward reversion). mid2 samples
                        are recorded at mid changes; mean/std use exact
                        integer window sums.
- ``vol_regime_ratio``  = rvol_w1m / (rvol_w5m + EPS)
- ``vol_regime_flag``   = 1 if vol_regime_ratio > 1 (short vol above long vol)
- ``liq_regime_ratio``  = quoted_depth_total / (mean quoted depth over 1m + EPS)
- ``liq_regime_flag``   = 1 if liq_regime_ratio > 1 (more liquid than usual)

Validity: window warmup plus the underlying inputs (mid history spanning w
for trend, >= 1 mid sample for meanrev, valid rvols / depth averages for the
ratio features).  EPS guard (API_FEATURES §4): every ratio above is INVALID
when its denominator is <= 0 (rvol_w == 0, std == 0, rvol_w5m == 0, mean
depth == 0) — an unobserved denominator is undefined, never 1e12-scaled.
"""

from __future__ import annotations

from math import sqrt
from typing import List

from iap.features._famutil import put
from iap.features.spec import EPS, WINDOW_NS, FeatureSpec, mkspec

FAMILY = "regime"

WINDOWS = ("10s", "1m", "5m")


def specs() -> List[FeatureSpec]:
    """Registry entries for the regime family (pinned order)."""
    out: List[FeatureSpec] = []
    for w in WINDOWS:
        out.append(mkspec(
            f"trend_score_w{w}_v1", FAMILY,
            f"Normalized drift over {w}: ret_log / (rvol_w{w} * sqrt(w_s) + EPS).",
            depends_on=(f"rvol_w{w}_v1",), window=w))
    for w in WINDOWS:
        out.append(mkspec(
            f"meanrev_score_w{w}_v1", FAMILY,
            f"Mean-reversion z-score over {w}: -(mid - mean)/(std + EPS).",
            window=w))
    out.append(mkspec("vol_regime_flag_v1", FAMILY,
                      "1 when rvol_w1m > rvol_w5m (elevated short-term vol).",
                      depends_on=("vol_regime_ratio_v1",)))
    out.append(mkspec("vol_regime_ratio_v1", FAMILY,
                      "rvol_w1m / (rvol_w5m + EPS).",
                      depends_on=("rvol_w1m_v1", "rvol_w5m_v1")))
    out.append(mkspec("liq_regime_flag_v1", FAMILY,
                      "1 when current quoted depth exceeds its 1m mean.",
                      depends_on=("liq_regime_ratio_v1",)))
    out.append(mkspec("liq_regime_ratio_v1", FAMILY,
                      "quoted_depth_total / (mean quoted depth over 1m + EPS).",
                      depends_on=("quoted_depth_total_v1",
                                  "quoted_depth_avg_w1m_v1")))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 10 regime values for the current emission."""
    t = st.t
    for w in WINDOWS:
        wn = WINDOW_NS[w]
        v = None
        rv = st.rvol(w)
        if st.book_ok and rv is not None and st.rv[w].count > 0:
            past = st.logmid_at(t - wn)
            if past is not None:
                v = (st.logmid - past) / (rv * sqrt(wn / 1e9) + EPS)
        put(values, valid, v, v is not None)
    for w in WINDOWS:
        win = st.midstat[w]  # sums: [mid2, mid2^2] (exact ints)
        v = None
        if st.book_ok and st.warm(WINDOW_NS[w]) and win.count > 0:
            n = win.count
            mean = win.sums[0] / n
            var = win.sums[1] / n - mean * mean
            std = sqrt(max(var, 0.0))
            # exact integer dispersion test (mid2 samples are integers):
            # n*sum(x^2) - sum(x)^2 > 0 iff the samples are not all equal
            if n * win.sums[1] - win.sums[0] * win.sums[0] > 0:
                v = -(st.mid2 - mean) / (std + EPS)
        put(values, valid, v, v is not None)
    rv1m, rv5m = st.rvol("1m"), st.rvol("5m")
    ratio = (
        rv1m / (rv5m + EPS)
        if (rv1m is not None and rv5m is not None and st.rv["5m"].count > 0)
        else None
    )
    put(values, valid, 1.0 if (ratio is not None and ratio > 1.0) else
        (0.0 if ratio is not None else None), ratio is not None)
    put(values, valid, ratio, ratio is not None)
    lr = None
    da = st.depthavg["1m"]
    if st.book_ok and st.warm(WINDOW_NS["1m"]) and da.count > 0:
        if da.sums[11] > 0:  # exact integer depth sum
            lr = (st.db10 + st.da10) / (da.sums[11] / da.count + EPS)
    put(values, valid, 1.0 if (lr is not None and lr > 1.0) else
        (0.0 if lr is not None else None), lr is not None)
    put(values, valid, lr, lr is not None)

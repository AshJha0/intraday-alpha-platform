"""Volatility family (spec §10, "Volatility").

All statistics are computed from per-mid-change log returns
``dlm = ln(mid2) - ln(mid2_prev)`` recorded at every book refresh where the
consolidated mid changed (zero returns are not sampled — pinned).  Windows
are half-open event-time intervals (t-w, t].

- ``rvol_w``          = sqrt( sum of dlm^2 over w / w_seconds )
                        (realized volatility per sqrt-second)
- ``volofvol_w5m``    = population std of dlm^2 over 5m:
                        sqrt(max(E[x^2] - E[x]^2, 0)) with x = dlm^2
                        (volatility-of-volatility proxy)
- ``mean_abs_ret_w``  = mean |dlm| over w (per mid change)
- ``range_bps_w``     = (max mid2 - min mid2) over w / mid2_now * 1e4
- ``jump_flag_w1m``   = 1 if max|dlm| over 1m > 4 * mean|dlm| over 1m
                        (requires >= 30 mid changes in the window)
- ``jump_count_w5m``  = number of jump events in 5m; a jump event is a mid
                        change whose |dlm| exceeded 4x the then-prevailing
                        1m mean|dlm| (>= 30 prior samples) at the moment it
                        happened (threshold jump detector, no lookahead)
- ``vol_ratio_a_b``   = rvol_a / (rvol_b + EPS) (volatility acceleration)

Validity: warmup of the window; mean/std statistics need enough samples
(volofvol >= 2, jump_flag >= 30); ranges need at least one sample.
"""

from __future__ import annotations

from math import sqrt
from typing import List

from iap.features._famutil import put
from iap.features.spec import EPS, WINDOW_NS, FeatureSpec, mkspec

FAMILY = "vol"

RV_WINDOWS = ("10s", "1m", "5m")
JUMP_K = 4.0
JUMP_MIN_OBS = 30


def specs() -> List[FeatureSpec]:
    """Registry entries for the volatility family (pinned order)."""
    out: List[FeatureSpec] = []
    for w in RV_WINDOWS:
        out.append(mkspec(f"rvol_w{w}_v1", FAMILY,
                          f"Realized vol over {w}: sqrt(sum dlm^2 / {w} in seconds).",
                          window=w))
    out.append(mkspec("volofvol_w5m_v1", FAMILY,
                      "Std of squared mid-change log returns over 5m "
                      "(vol-of-vol proxy).", window="5m"))
    for w in ("10s", "1m"):
        out.append(mkspec(f"mean_abs_ret_w{w}_v1", FAMILY,
                          f"Mean |dlm| per mid change over {w}.", window=w))
    for w in RV_WINDOWS:
        out.append(mkspec(f"range_bps_w{w}_v1", FAMILY,
                          f"High-low mid range over {w} in bps of current mid.",
                          window=w))
    out.append(mkspec("jump_flag_w1m_v1", FAMILY,
                      f"1 if max|dlm| over 1m > {JUMP_K}x mean|dlm| over 1m "
                      f"(>= {JUMP_MIN_OBS} mid changes).", window="1m",
                      k=JUMP_K, min_obs=JUMP_MIN_OBS))
    out.append(mkspec("jump_count_w5m_v1", FAMILY,
                      f"Number of threshold jumps (|dlm| > {JUMP_K}x prevailing "
                      f"1m mean|dlm|) detected in the last 5m.", window="5m",
                      k=JUMP_K, min_obs=JUMP_MIN_OBS))
    out.append(mkspec("vol_ratio_w10s_w1m_v1", FAMILY,
                      "rvol_w10s / (rvol_w1m + EPS): short-vs-medium vol ratio.",
                      depends_on=("rvol_w10s_v1", "rvol_w1m_v1")))
    out.append(mkspec("vol_ratio_w1m_w5m_v1", FAMILY,
                      "rvol_w1m / (rvol_w5m + EPS): medium-vs-long vol ratio.",
                      depends_on=("rvol_w1m_v1", "rvol_w5m_v1")))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 13 volatility values for the current emission."""
    rvols = {w: st.rvol(w) for w in RV_WINDOWS}
    for w in RV_WINDOWS:
        put(values, valid, rvols[w], rvols[w] is not None)
    # volofvol: rv sums layout [sum_sq, sum_abs, sum_quad]
    win = st.rv["5m"]
    vv = None
    if st.warm(WINDOW_NS["5m"]) and win.count >= 2:
        n = win.count
        ex = win.sums[0] / n          # E[dlm^2]
        ex2 = win.sums[2] / n         # E[dlm^4]
        vv = sqrt(max(ex2 - ex * ex, 0.0))
    put(values, valid, vv, vv is not None)
    for w in ("10s", "1m"):
        win = st.rv[w]
        wok = st.warm(WINDOW_NS[w]) and win.count > 0
        put(values, valid, win.sums[1] / win.count if wok else None, wok)
    for w in RV_WINDOWS:
        ext = st.ext_mid[w]
        r = None
        if st.book_ok and st.warm(WINDOW_NS[w]) and ext.max() is not None:
            r = (ext.max() - ext.min()) / st.mid2 * 1e4
        put(values, valid, r, r is not None)
    win = st.rv["1m"]
    jf = None
    if st.warm(WINDOW_NS["1m"]) and win.count >= JUMP_MIN_OBS:
        mx = st.ext_absdlm.max()
        mean_abs = win.sums[1] / win.count
        jf = 1.0 if (mx is not None and mx > JUMP_K * mean_abs) else 0.0
    put(values, valid, jf, jf is not None)
    wok = st.warm(WINDOW_NS["5m"])
    put(values, valid, st.jumps.count if wok else None, wok)
    for a, b in (("10s", "1m"), ("1m", "5m")):
        v = None
        if rvols[a] is not None and rvols[b] is not None:
            v = rvols[a] / (rvols[b] + EPS)
        put(values, valid, v, v is not None)

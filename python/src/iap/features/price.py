"""Price family — mid returns over pinned horizons (spec §10, "Price").

All returns are computed from the consolidated mid in integer double-tick
units ``mid2 = best_bid_ticks + best_ask_ticks`` (the factor tick_size/2
cancels in every ratio), sampled at mid-change events.  ``mid2(t-h)`` means
the latest mid sample with event time <= t-h (at-or-before lookup — no
interpolation, no lookahead).

Formulas (horizon h in {1s, 5s, 10s, 30s, 1m}):

- ``ret_simple_h``      = mid2(t) / mid2(t-h) - 1
- ``ret_log_h``         = ln(mid2(t)) - ln(mid2(t-h))
- ``ret_accel_h``       = ret_log(t, h) - ret_log(t-h, h)
                          (return acceleration: change of the h-return)
- ``ret_resid_h``       = ret_log_h - beta_w5m * ref_ret_log_h
                          (residual vs the cross-asset reference instrument:
                          ETF for equities, EUR/USD for FX; 0 for the
                          reference itself since beta = 1 there)
- ``mid_change_ticks_h``= (mid2(t) - mid2(t-h)) / 2   [ticks]
- ``ret_vol_adj_h``     = ret_log_h / (rvol_w1m + EPS)
                          (volatility-adjusted return)

Validity: book_ok and the mid history spans t-h (t-2h for acceleration);
residuals additionally need a valid beta_w5m and reference return;
vol-adjusted returns need a valid rvol_w1m.
"""

from __future__ import annotations

from typing import List

from iap.features._famutil import put
from iap.features.spec import EPS, WINDOW_NS, FeatureSpec, mkspec

FAMILY = "price"

HORIZONS = ("1s", "5s", "10s", "30s", "1m")


def specs() -> List[FeatureSpec]:
    """Registry entries for the price family (pinned order)."""
    out: List[FeatureSpec] = []
    for h in HORIZONS:
        out.append(mkspec(
            f"ret_simple_{h}_v1", FAMILY,
            f"Simple mid-to-mid return over {h}: mid(t)/mid(t-{h}) - 1 "
            f"(mid sampled at-or-before t-{h}).",
            horizon=h))
    for h in HORIZONS:
        out.append(mkspec(
            f"ret_log_{h}_v1", FAMILY,
            f"Log mid return over {h}: ln mid(t) - ln mid(t-{h}).",
            horizon=h))
    for h in HORIZONS:
        out.append(mkspec(
            f"ret_accel_{h}_v1", FAMILY,
            f"Return acceleration over {h}: ret_log(t,{h}) - ret_log(t-{h},{h}).",
            depends_on=(f"ret_log_{h}_v1",),
            horizon=h))
    for h in HORIZONS:
        out.append(mkspec(
            f"ret_resid_{h}_v1", FAMILY,
            f"Residual log return over {h} vs the cross-asset reference: "
            f"ret_log_{h} - beta_w5m * ref_ret_{h}.",
            depends_on=(f"ret_log_{h}_v1", "beta_w5m_v1", f"ref_ret_{h}_v1"),
            horizon=h))
    for h in HORIZONS:
        out.append(mkspec(
            f"mid_change_ticks_{h}_v1", FAMILY,
            f"Mid change over {h} in ticks: (mid_ticks(t) - mid_ticks(t-{h})).",
            horizon=h))
    for h in HORIZONS:
        out.append(mkspec(
            f"ret_vol_adj_{h}_v1", FAMILY,
            f"Volatility-adjusted log return over {h}: ret_log_{h} / (rvol_w1m + EPS).",
            depends_on=(f"ret_log_{h}_v1", "rvol_w1m_v1"),
            horizon=h))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 30 price-family values for the current emission."""
    t = st.t
    ok = st.book_ok
    rets = {}
    logs = {}
    for h in HORIZONS:
        hn = WINDOW_NS[h]
        past = st.mid2_at(t - hn) if ok else None
        if ok and past is not None:
            rets[h] = st.mid2 / past - 1.0
            logs[h] = st.logmid - st.logmid_at(t - hn)
        put(values, valid, rets.get(h), h in rets)
    for h in HORIZONS:
        put(values, valid, logs.get(h), h in logs)
    for h in HORIZONS:
        hn = WINDOW_NS[h]
        accel = None
        if h in logs:
            lm1 = st.logmid_at(t - hn)
            lm2 = st.logmid_at(t - 2 * hn)
            if lm2 is not None:
                accel = logs[h] - (lm1 - lm2)
        put(values, valid, accel, accel is not None)
    beta = st.beta_w5m()
    for h in HORIZONS:
        resid = None
        if h in logs and beta is not None:
            rr = st.ref_ret_log(WINDOW_NS[h])
            if rr is not None:
                resid = logs[h] - beta * rr
        put(values, valid, resid, resid is not None)
    for h in HORIZONS:
        hn = WINDOW_NS[h]
        chg = None
        if ok:
            past = st.mid2_at(t - hn)
            if past is not None:
                chg = (st.mid2 - past) / 2.0
        put(values, valid, chg, chg is not None)
    rv1m = st.rvol("1m")
    for h in HORIZONS:
        va = None
        if h in logs and rv1m is not None:
            va = logs[h] / (rv1m + EPS)
        put(values, valid, va, va is not None)

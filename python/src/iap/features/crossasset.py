"""Cross-asset family (spec §10, "Cross-asset").

Reference instrument (pinned in :mod:`iap.features.context`): the ETF for
equities, EUR/USD for FX pairs; the reference instrument references itself
(beta = 1, residual = 0, correlations = 1 there).  Because the event stream
is globally ordered by exchange_ts, the reference state is always current
up to the emission time — reads are at-or-before t, never ahead (no
lookahead).

Paired 1s log-return samples are recorded at every own mid change:

- contemporaneous pair:  x = own ln mid(t) - ln mid(t-1s),
                         y = ref ln mid(t) - ln mid(t-1s)
- lead-lag pair:         x as above,
                         y_lag = ref ln mid(t-1s) - ln mid(t-2s)
                         (reference return one second *ahead* of own return —
                         a positive correlation means the reference leads)

Formulas (window sums over half-open (t-w, t]):

- ``ref_ret_h``        = reference instrument log mid return over horizon h
- ``beta_w5m``         = cov(x, y) / var(y) over 5m contemporaneous pairs
- ``corr_contemp_w``   = corr(x, y) over w. For FX pairs this is the pinned
                         "correlation proxy vs EUR/USD" (ref = EUR/USD).
- ``leadlag_corr_w``   = corr(x, y_lag) over w (reference-leads correlation)
- ``resid_vol_w1m``    = sqrt(max(var(x) - cov(x,y)^2/var(y), 0)) over 1m —
                         volatility of the beta-residual return

Validity: reference history must span the needed horizon; moment statistics
need window warmup, >= 4 pairs, and var > 1e-18 in every denominator.
"""

from __future__ import annotations

from math import sqrt
from typing import List, Optional

from iap.features._famutil import put
from iap.features.spec import WINDOW_NS, FeatureSpec, mkspec

FAMILY = "xasset"

REF_HORIZONS = ("1s", "5s", "10s", "30s", "1m")
CORR_WINDOWS = ("1m", "5m")
MIN_PAIRS = 4
MIN_VAR = 1e-18


def specs() -> List[FeatureSpec]:
    """Registry entries for the cross-asset family (pinned order)."""
    out: List[FeatureSpec] = []
    for h in REF_HORIZONS:
        out.append(mkspec(
            f"ref_ret_{h}_v1", FAMILY,
            f"Reference-instrument (ETF / EUR/USD) log mid return over {h} "
            f"(lead-lag input).", horizon=h))
    out.append(mkspec(
        "beta_w5m_v1", FAMILY,
        "OLS beta of own vs reference contemporaneous 1s log returns over 5m: "
        "cov(x,y)/var(y).", window="5m", min_pairs=MIN_PAIRS))
    for w in CORR_WINDOWS:
        out.append(mkspec(
            f"corr_contemp_w{w}_v1", FAMILY,
            f"Correlation of own vs reference contemporaneous 1s log returns "
            f"over {w} (FX: correlation proxy vs EUR/USD).",
            window=w, min_pairs=MIN_PAIRS))
    for w in CORR_WINDOWS:
        out.append(mkspec(
            f"leadlag_corr_w{w}_v1", FAMILY,
            f"Correlation of own 1s return vs reference 1s return lagged 1s "
            f"over {w} (positive = reference leads).",
            window=w, min_pairs=MIN_PAIRS))
    out.append(mkspec(
        "resid_vol_w1m_v1", FAMILY,
        "Std of the beta-residual 1s return over 1m: "
        "sqrt(max(var(x) - cov^2/var(y), 0)).",
        depends_on=("beta_w5m_v1",), window="1m", min_pairs=MIN_PAIRS))
    return out


def _moments(win) -> Optional[tuple]:
    """(var_x, var_y, cov) population moments from a pair window, or None."""
    n = win.count
    if n < MIN_PAIRS:
        return None
    sx, sy, sxx, syy, sxy = win.sums
    mx, my = sx / n, sy / n
    var_x = sxx / n - mx * mx
    var_y = syy / n - my * my
    cov = sxy / n - mx * my
    return var_x, var_y, cov


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 11 cross-asset values for the current emission."""
    t = st.t
    for h in REF_HORIZONS:
        put(values, valid, st.ref_ret_log(WINDOW_NS[h]),
            st.ref_ret_log(WINDOW_NS[h]) is not None)
    beta = st.beta_w5m()
    put(values, valid, beta, beta is not None)
    for w in CORR_WINDOWS:
        v = None
        if st.warm(WINDOW_NS[w]):
            m = _moments(st.xc[w])
            if m is not None and m[0] > MIN_VAR and m[1] > MIN_VAR:
                v = m[2] / sqrt(m[0] * m[1])
        put(values, valid, v, v is not None)
    for w in CORR_WINDOWS:
        v = None
        if st.warm(WINDOW_NS[w]):
            m = _moments(st.xl[w])
            if m is not None and m[0] > MIN_VAR and m[1] > MIN_VAR:
                v = m[2] / sqrt(m[0] * m[1])
        put(values, valid, v, v is not None)
    v = None
    if st.warm(WINDOW_NS["1m"]):
        m = _moments(st.xc["1m"])
        if m is not None and m[1] > MIN_VAR:
            v = sqrt(max(m[0] - m[2] * m[2] / m[1], 0.0))
    put(values, valid, v, v is not None)

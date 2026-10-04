"""Statistics of the planted-signal power study (:mod:`iap.research.power`).

Everything here is a pure function of its arguments: no I/O, no wall clock,
no random numbers.  None of it is read by a promotion gate — the gate
statistic stays :func:`iap.validation.metrics.pooled_slope_hac_tstat`; these
functions exist to say *how much power the gate has and where it goes*.

* :func:`wilson_interval` — the binomial interval a detection rate is
  reported with (3 seeds cannot tell 33 % from 80 %; the interval says so).
* :func:`session_index` / :func:`run_fraction` — which session a row is in
  and how far through the run it is.
* :func:`pooled_slope_session_hac` — the pooled-slope HAC t with the
  Bartlett cross-products restricted to bucket pairs of the SAME session
  (sessions are independent draws; the last bucket of one day and the first
  of the next are not neighbours), with the effective-sample accounting
  beside it.
* :func:`slope_break_z` — HAC z of the difference between the pooled slope
  before and after a split point: the detector of a mid-sample break.
* :func:`ideal_ic_order_flow` / :func:`ideal_ic_lead_lag` — the IC the
  planted mechanism would give a detector that observed the efficient price
  itself (no tick grid, no quote staleness): the analytical ceiling the
  measured IC is compared with.
* :func:`fit_t_model`, :func:`normal_power`, :func:`minimum_detectable_level`,
  :func:`sessions_needed` — the power model ``t ~ N((kappa0 + kappa *
  level) * sqrt(sessions), sd)`` fitted to the study's own t-statistics, and
  what it implies for the minimum detectable effect and the sessions it
  needs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

import numpy as np

__all__ = [
    "DAY_NS",
    "Z_95",
    "expected_z_tanh",
    "fit_t_model",
    "ideal_ic_lead_lag",
    "ideal_ic_order_flow",
    "minimum_detectable_level",
    "normal_power",
    "pooled_slope_session_hac",
    "run_fraction",
    "session_index",
    "sessions_needed",
    "slope_break_z",
    "wilson_interval",
]

NS_S = 1_000_000_000
DAY_NS = 86_400 * NS_S
EPS = 1e-12

#: two-sided 95 % normal quantile (the interval every rate is reported with)
Z_95 = 1.959963984540054

_NORMAL = NormalDist()


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval of a binomial proportion ``successes / n``.

    Unlike the Wald interval it is never empty at 0 or ``n`` successes:
    0 of 20 gives [0, 0.161], 20 of 20 gives [0.839, 1], 1 of 3 gives
    [0.061, 0.792].  ``n`` = 0 returns the uninformative [0, 1].
    """
    if n < 0 or not 0 <= successes <= n:
        raise ValueError("need 0 <= successes <= n")
    if z <= 0.0:
        raise ValueError("z must be positive")
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    lo = 0.0 if successes == 0 else max(0.0, centre - half)
    hi = 1.0 if successes == n else min(1.0, centre + half)
    return lo, hi


def session_index(ts: np.ndarray) -> np.ndarray:
    """0-based session number of every row: the rank of its UTC day among
    the days present (the synthetic sessions never span midnight UTC)."""
    ts = np.asarray(ts, dtype=np.int64)
    _, inverse = np.unique(ts // DAY_NS, return_inverse=True)
    return inverse.astype(np.int64)


def run_fraction(ts: np.ndarray) -> np.ndarray:
    """Position of every row in the run, in [0, 1]: sessions are equal
    slices and a row sits inside its slice in proportion to the time since
    the session's first row — the generator's ``planted.break`` clock."""
    ts = np.asarray(ts, dtype=np.int64)
    if ts.size == 0:
        return np.empty(0)
    sess = session_index(ts)
    n_sessions = int(sess.max()) + 1
    first = np.full(n_sessions, np.iinfo(np.int64).max, dtype=np.int64)
    last = np.full(n_sessions, np.iinfo(np.int64).min, dtype=np.int64)
    np.minimum.at(first, sess, ts)
    np.maximum.at(last, sess, ts)
    span = np.maximum(last - first, 1).astype(float)
    within = (ts - first[sess]) / span[sess]
    return (sess + np.clip(within, 0.0, 1.0)) / n_sessions


def _session_lrv(moment: np.ndarray, ts: np.ndarray, lags: int, bucket_ns: int) -> tuple:
    """Long-run variance of the sum of ``moment`` with fixed event-time
    buckets and Bartlett weights, cross-products within a session only.
    Returns ``(lrv, n_buckets, n_sessions)``."""
    sess = session_index(ts)
    key = sess * (DAY_NS // bucket_ns + 1) + (ts % DAY_NS) // bucket_ns
    uniq, inverse = np.unique(key, return_inverse=True)
    s = np.bincount(inverse, weights=moment)
    bucket_session = uniq // (DAY_NS // bucket_ns + 1)
    lrv = float(np.sum(s * s))
    for lag in range(1, min(lags, s.size - 1) + 1):
        same = bucket_session[lag:] == bucket_session[:-lag]
        lrv += 2.0 * (1.0 - lag / (lags + 1.0)) * float(np.sum(s[lag:][same] * s[:-lag][same]))
    return lrv, int(s.size), int(bucket_session.max()) + 1 if s.size else 0


def pooled_slope_session_hac(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    lags: int = 2,
    bucket_ns: int = 300 * NS_S,
    min_buckets: int = 8,
) -> dict:
    """Pooled slope of label on score with session-clustered HAC errors.

    The estimator is the one of
    :func:`iap.validation.metrics.pooled_slope_hac_tstat` — global means,
    fixed ``bucket_ns`` event-time buckets, Bartlett weights out to ``lags``
    consecutive non-empty buckets — except that a bucket pair contributes a
    cross-product only when both buckets belong to the same session.  With
    one session the two statistics are identical.

    Returns ``{"t", "ic", "slope", "n_pairs", "n_buckets", "n_sessions",
    "n_eff", "design_effect"}``.  ``n_eff = (t / ic)^2`` is the number of
    independent pairs that would give this t at this IC, and
    ``design_effect = n_pairs / n_eff`` how many rows one independent
    observation costs (overlapping labels and windows, duplicated ticks).
    ``t`` and the fields derived from it are ``None`` with fewer than
    ``min_buckets`` buckets, a degenerate side or a non-positive variance.
    """
    ts = np.asarray(ts, dtype=np.int64)
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    if not (ts.shape == x.shape == y.shape):
        raise ValueError("ts/score/label length mismatch")
    if lags < 0 or bucket_ns <= 0:
        raise ValueError("lags must be >= 0 and bucket_ns positive")
    ok = np.isfinite(x) & np.isfinite(y)
    ts, x, y = ts[ok], x[ok], y[ok]
    out: dict = {
        "t": None,
        "ic": None,
        "slope": None,
        "n_pairs": int(x.size),
        "n_buckets": 0,
        "n_sessions": 0,
        "n_eff": None,
        "design_effect": None,
    }
    if x.size == 0 or x.std() <= EPS or y.std() <= EPS:
        return out
    dx = x - x.mean()
    dy = y - y.mean()
    sxx = float(np.sum(dx * dx))
    slope = float(np.sum(dx * dy)) / sxx
    ic = float(np.sum(dx * dy) / math.sqrt(sxx * float(np.sum(dy * dy))))
    lrv, n_buckets, n_sessions = _session_lrv(dx * (dy - slope * dx), ts, lags, bucket_ns)
    out.update(ic=ic, slope=slope, n_buckets=n_buckets, n_sessions=n_sessions)
    if n_buckets < min_buckets or not lrv > 0.0:
        return out
    t = float(slope * sxx / math.sqrt(lrv))
    out["t"] = t
    if abs(ic) > EPS and abs(t) > EPS:
        n_eff = (t / ic) ** 2
        out["n_eff"] = n_eff
        out["design_effect"] = x.size / n_eff
    return out


def slope_break_z(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    at_fraction: float = 0.5,
    lags: int = 2,
    bucket_ns: int = 300 * NS_S,
    min_buckets: int = 8,
) -> dict:
    """HAC z of ``slope(before) - slope(after)`` for a split of the run at
    ``at_fraction`` (:func:`run_fraction`).

    Each half has its own means, slope and session-clustered HAC variance;
    the halves do not overlap, so the variances add.  Under a stable effect
    (or none) z is approximately standard normal; a reversal of an effect of
    slope b gives ``E[z] ~ 2b / se``.  Returns ``{"z", "slope_pre",
    "slope_post", "t_pre", "t_post"}`` with ``None`` where a half is
    degenerate.
    """
    if not 0.0 < at_fraction < 1.0:
        raise ValueError("at_fraction must be in (0, 1)")
    ts = np.asarray(ts, dtype=np.int64)
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    if not (ts.shape == x.shape == y.shape):
        raise ValueError("ts/score/label length mismatch")
    ok = np.isfinite(x) & np.isfinite(y)
    ts, x, y = ts[ok], x[ok], y[ok]
    out: dict = {"z": None, "slope_pre": None, "slope_post": None, "t_pre": None, "t_post": None}
    if ts.size == 0:
        return out
    post = run_fraction(ts) >= at_fraction
    halves = []
    for name, mask in (("pre", ~post), ("post", post)):
        res = pooled_slope_session_hac(ts[mask], x[mask], y[mask], lags, bucket_ns, min_buckets)
        out[f"slope_{name}"] = res["slope"]
        out[f"t_{name}"] = res["t"]
        halves.append(res)
    if any(h["t"] is None or abs(h["t"]) <= EPS for h in halves):
        return out
    variance = sum((h["slope"] / h["t"]) ** 2 for h in halves)
    if not variance > 0.0:
        return out
    out["z"] = float((halves[0]["slope"] - halves[1]["slope"]) / math.sqrt(variance))
    return out


def expected_z_tanh(ratio: float = 1.0, steps: int = 4001, span: float = 10.0) -> float:
    """``E[Z * tanh(ratio * Z)]`` for standard normal Z (trapezoid rule on
    [-span, span]): the correlation a sign drawn with
    ``P(+1) = 0.5 + 0.5 * tanh(ratio * Z)`` has with Z.  0.6057 at ratio 1;
    it tends to ``E|Z|`` = 0.7979 as the tanh saturates."""
    if ratio < 0.0 or steps < 3 or span <= 0.0:
        raise ValueError("ratio >= 0, steps >= 3 and span > 0 required")
    z = np.linspace(-span, span, steps)
    f = z * np.tanh(ratio * z) * np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    return float(np.sum((f[1:] + f[:-1]) * 0.5) * (z[1] - z[0]))


def ideal_ic_order_flow(
    strength: float,
    kernel_decay: float,
    kernel_steps: int,
    window_steps: int,
    horizon_steps: int,
    sigma_ratios: Sequence[float] = (1.0,),
) -> float:
    """IC of a ONE-TRADE imbalance over the last ``window_steps`` seconds
    against the efficient-price move over the next ``horizon_steps``
    seconds, under the generator's planted order flow.

    A trade in grid step ``k`` is a buy with probability
    ``0.5 + 0.5 * strength * tanh(g / scale)``, ``g`` the kernel-weighted
    move of steps ``k .. k + kernel_steps - 1`` (weights ``decay^j``).  Its
    sign therefore correlates ``c * w_j / |w|`` with the move of step
    ``k + j``, where ``c = strength * E[Z tanh(r Z)]`` averaged over the
    volatility regimes (``r`` = regime sigma over the low-regime sigma the
    scale is built from).  A row at the start of step ``n`` whose only trade
    printed ``a`` steps earlier (``a`` = 1..window, uniform) sees the moves
    ``j = a .. a + h - 1`` of that trade's kernel inside its label, so

        IC = c * mean_a( sum_{j=a}^{a+h-1} w_j ) / (|w| * sqrt(h)).

    Regimes are taken as equally likely (the two-state chain is symmetric)
    and constant over one kernel; more than one trade in the window makes
    the imbalance a little more informative than this.
    """
    if not 0.0 <= strength < 1.0:
        raise ValueError("strength must be in [0, 1)")
    if not 0.0 < kernel_decay <= 1.0 or kernel_steps < 1:
        raise ValueError("kernel_decay in (0, 1] and kernel_steps >= 1 required")
    if window_steps < 1 or horizon_steps < 1 or not sigma_ratios:
        raise ValueError("window_steps, horizon_steps >= 1 and a regime required")
    w = [kernel_decay**j for j in range(kernel_steps)]
    norm = math.sqrt(sum(v * v for v in w))
    c = strength * sum(expected_z_tanh(float(r)) for r in sigma_ratios) / len(sigma_ratios)
    covered = [
        sum(w[j] for j in range(a, a + horizon_steps) if j < kernel_steps)
        for a in range(1, window_steps + 1)
    ]
    return c * (sum(covered) / window_steps) / (norm * math.sqrt(horizon_steps))


def ideal_ic_lead_lag(beta: float, lag_steps: int, horizon_steps: int) -> float:
    """IC of the leader's last one-second efficient-price move against the
    follower's efficient-price move over the next ``horizon_steps`` seconds
    under the generator's planted lead-lag.

    The follower's move at step ``k`` gains ``beta`` times the leader's move
    at step ``k - lag_steps`` (same tick volatility), so the one lagged step
    correlates ``beta / sqrt(1 + beta^2)``.  A row inside step ``n`` has just
    seen the leader's move of step ``n - 1``; the follower repeats it in step
    ``n - 1 + lag_steps``, which a label over steps ``n .. n + h - 1`` covers
    only when ``lag_steps <= h`` — where it is one of ``h`` equally noisy
    steps:

        IC = beta / sqrt(1 + beta^2) / sqrt(h)   if lag_steps <= h, else 0.

    A one-second label is therefore blind to a two-second lag.
    """
    if lag_steps < 1 or horizon_steps < 1:
        raise ValueError("lag_steps and horizon_steps must be >= 1")
    if lag_steps > horizon_steps:
        return 0.0
    return beta / math.sqrt(1.0 + beta * beta) / math.sqrt(horizon_steps)


def fit_t_model(points: Sequence[tuple[float, float, float]], intercept: bool = True) -> dict:
    """Fit ``t = (kappa0 + kappa * level) * sqrt(sessions) + e``.

    ``points`` are ``(level, sessions, t)`` of individual runs, the null
    (level 0) included.  ``kappa`` is the expected t one unit of the
    reference effect adds on one session; ``kappa0`` the expected t of the
    NULL on one session — not zero when the data has a relation of its own
    between the signal and the label (the synthetic trade imbalance
    mean-reverts a little, so the order-flow detectors start below zero and
    the planted effect has to overcome that first).  ``intercept=False``
    forces ``kappa0 = 0``.  ``sd`` is the standard deviation of the
    residuals (about 1 when the t-statistic is well calibrated).  Returns
    ``{"kappa0", "kappa", "sd", "n"}`` — ``None`` values with fewer than 3
    usable points or no variation in level.
    """
    rows = [
        (math.sqrt(float(sessions)), float(level) * math.sqrt(float(sessions)), float(t))
        for level, sessions, t in points
        if t is not None and math.isfinite(float(t)) and level >= 0.0 and sessions > 0.0
    ]
    none = {"kappa0": None, "kappa": None, "sd": None, "n": len(rows)}
    if len(rows) < 3:
        return none
    suu = sum(u * u for u, _, _ in rows)
    svv = sum(v * v for _, v, _ in rows)
    suv = sum(u * v for u, v, _ in rows)
    sut = sum(u * t for u, _, t in rows)
    svt = sum(v * t for _, v, t in rows)
    if intercept:
        det = suu * svv - suv * suv
        if not det > EPS * max(suu * svv, 1.0):
            return none
        kappa0 = (sut * svv - svt * suv) / det
        kappa = (svt * suu - sut * suv) / det
        dof = len(rows) - 2
    else:
        if not svv > 0.0:
            return none
        kappa0, kappa, dof = 0.0, svt / svv, len(rows) - 1
    rss = sum((t - kappa0 * u - kappa * v) ** 2 for u, v, t in rows)
    return {"kappa0": kappa0, "kappa": kappa, "sd": math.sqrt(rss / max(dof, 1)), "n": len(rows)}


def normal_power(mean_t: float, sd: float, threshold: float) -> float:
    """``P(t >= threshold)`` for ``t ~ N(mean_t, sd^2)``."""
    if sd <= 0.0:
        raise ValueError("sd must be positive")
    return 1.0 - _NORMAL.cdf((threshold - mean_t) / sd)


def _required_mean(sd: float, threshold: float, power: float) -> float:
    if not 0.0 < power < 1.0:
        raise ValueError("power must be in (0, 1)")
    if sd <= 0.0:
        raise ValueError("sd must be positive")
    return threshold + _NORMAL.inv_cdf(power) * sd


def minimum_detectable_level(
    kappa: float,
    sd: float,
    threshold: float,
    sessions: float,
    power: float = 0.8,
    kappa0: float = 0.0,
) -> float | None:
    """Smallest multiple of the reference effect detected with probability
    ``power`` at ``threshold`` on ``sessions`` sessions under the fitted
    model; ``None`` when ``kappa`` is not positive (the detector does not
    respond to the effect at all)."""
    if sessions <= 0.0:
        raise ValueError("sessions must be positive")
    if kappa is None or not kappa > 0.0:
        return None
    return (_required_mean(sd, threshold, power) / math.sqrt(sessions) - kappa0) / kappa


def sessions_needed(
    kappa: float,
    sd: float,
    threshold: float,
    level: float = 1.0,
    power: float = 0.8,
    kappa0: float = 0.0,
) -> float | None:
    """Sessions at which an effect of ``level`` times the reference is
    detected with probability ``power`` at ``threshold`` under the fitted
    model (not rounded); ``None`` when the expected t per root-session,
    ``kappa0 + kappa * level``, is not positive."""
    if level <= 0.0:
        raise ValueError("level must be positive")
    if kappa is None:
        return None
    per_root_session = kappa0 + kappa * level
    if not per_root_session > 0.0:
        return None
    return (_required_mean(sd, threshold, power) / per_root_session) ** 2

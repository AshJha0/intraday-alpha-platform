"""Alpha validation metrics (spec §13, conventions §7).

All metrics operate on aligned numpy arrays of scores (expected returns)
and event-time forward labels; invalid entries are NaN and are dropped
pairwise.  Everything here is deterministic and closed-form.

Newey-West-lite t-statistic (documented, pinned): the IC time series is
formed by bucketing test rows into fixed event-time buckets (default 5
minutes), computing the IC inside each bucket, and testing the mean bucket
IC against 0 with a Newey-West long-run variance using Bartlett weights and
a lag count L:

    lrv = g0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * g_l
    t   = mean(ic) / sqrt(lrv / n_buckets)

where g_l is the lag-l autocovariance of the bucket-IC series.

**L scales with the label horizon (pinned, round-3)**: overlapping labels
induce autocorrelation for as long as the horizon spans buckets, so
``L = ceil(horizon_ns / bucket_ns) + 1`` (:func:`nw_lags`) — a 15-minute
label over 5-minute buckets overlaps 3 buckets and gets L = 4, where the
old fixed L = 2 under-covered the long-run variance and inflated the
t-stat.  The lag count actually used is reported in every alpha JSON
(``nw_lags``).  This is still "lite" because L follows a pinned rule rather
than a data-driven bandwidth selection — deterministic and reproducible.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np

EPS = 1e-12
NS_S = 1_000_000_000

#: pinned label horizons in nanoseconds (mirrors iap.labels.HORIZONS_NS)
HORIZONS_NS: Dict[str, int] = {
    "10ms": 10_000_000,
    "50ms": 50_000_000,
    "100ms": 100_000_000,
    "500ms": 500_000_000,
    "1s": 1 * NS_S,
    "5s": 5 * NS_S,
    "10s": 10 * NS_S,
    "30s": 30 * NS_S,
    "1m": 60 * NS_S,
    "5m": 300 * NS_S,
    "15m": 900 * NS_S,
}
HORIZON_ORDER: Tuple[str, ...] = tuple(HORIZONS_NS)


def _pairwise(x: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.shape != y.shape:
        raise ValueError("score/label length mismatch")
    ok = np.isfinite(x) & np.isfinite(y)
    return x[ok], y[ok]


def ic(scores: np.ndarray, labels: np.ndarray, min_obs: int = 32) -> float:
    """Pearson information coefficient (NaN with < min_obs pairs or a
    degenerate side)."""
    x, y = _pairwise(scores, labels)
    if x.size < min_obs or x.std() <= EPS or y.std() <= EPS:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def _ranks(v: np.ndarray) -> np.ndarray:
    """Average ranks (ties get the mean rank), deterministic."""
    order = np.argsort(v, kind="mergesort")
    ranks = np.empty(len(v), dtype=float)
    sv = v[order]
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i : j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return ranks


def rank_ic(scores: np.ndarray, labels: np.ndarray, min_obs: int = 32) -> float:
    """Spearman rank IC (Pearson correlation of average ranks)."""
    x, y = _pairwise(scores, labels)
    if x.size < min_obs:
        return float("nan")
    rx, ry = _ranks(x), _ranks(y)
    if rx.std() <= EPS or ry.std() <= EPS:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def hit_rate(scores: np.ndarray, labels: np.ndarray, min_obs: int = 32) -> float:
    """P(sign(score) == sign(label)) over pairs where both are nonzero."""
    x, y = _pairwise(scores, labels)
    nz = (x != 0.0) & (y != 0.0)
    if nz.sum() < min_obs:
        return float("nan")
    return float(np.mean(np.sign(x[nz]) == np.sign(y[nz])))


def bucket_ics(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    bucket_ns: int = 300 * NS_S,
    min_obs: int = 8,
) -> np.ndarray:
    """IC per fixed event-time bucket (buckets with < min_obs pairs skipped)."""
    ts = np.asarray(ts, dtype=np.int64)
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    ts, x, y = ts[ok], x[ok], y[ok]
    if ts.size == 0:
        return np.empty(0)
    buckets = ts // bucket_ns
    out: List[float] = []
    for b in np.unique(buckets):
        m = buckets == b
        if m.sum() < min_obs:
            continue
        xb, yb = x[m], y[m]
        if xb.std() <= EPS or yb.std() <= EPS:
            continue
        out.append(float(np.corrcoef(xb, yb)[0, 1]))
    return np.asarray(out)


def nw_lags(horizon_ns: int, bucket_ns: int = 300 * NS_S) -> int:
    """Pinned Newey-West lag count for a label horizon (see docstring)."""
    if horizon_ns <= 0 or bucket_ns <= 0:
        raise ValueError("horizon_ns and bucket_ns must be positive")
    return int(-(-int(horizon_ns) // int(bucket_ns))) + 1


def newey_west_tstat(series: np.ndarray, lags: int = 2) -> float:
    """t-stat of mean(series) vs 0 with Bartlett-weighted long-run variance
    (see module docstring — 'Newey-West-lite')."""
    s = np.asarray(series, dtype=float)
    s = s[np.isfinite(s)]
    n = s.size
    if n < 8:
        return float("nan")
    d = s - s.mean()
    lrv = float(np.mean(d * d))
    for lag in range(1, min(lags, n - 1) + 1):
        gamma = float(np.mean(d[lag:] * d[:-lag]))
        lrv += 2.0 * (1.0 - lag / (lags + 1.0)) * gamma
    lrv = max(lrv, 0.0)
    if lrv <= EPS:
        return float("nan")
    return float(s.mean() / np.sqrt(lrv / n))


def decay_curve(
    scores: np.ndarray,
    frame,
    horizons: Sequence[str] = HORIZON_ORDER,
) -> Dict[str, float]:
    """IC of one score series against every pinned horizon's mid label."""
    out: Dict[str, float] = {}
    for h in horizons:
        lab = frame[f"label_mid_{h}"].to_numpy(dtype=float).copy()
        lab[~frame[f"label_valid_{h}"].to_numpy(dtype=bool)] = np.nan
        out[h] = ic(scores, lab)
    return out


def signal_turnover(
    ts: np.ndarray, scores: np.ndarray, conf: np.ndarray, conf_min: float = 0.25
) -> float:
    """Position flips per hour of a sign-following unit strategy.

    Position proxy: sign(score) where confidence >= conf_min else flat.
    Turnover = number of position changes / elapsed event-time hours.
    """
    ts = np.asarray(ts, dtype=np.int64)
    pos = np.sign(np.where(np.asarray(conf, float) >= conf_min, scores, 0.0))
    pos[~np.isfinite(pos)] = 0.0
    if len(pos) < 2:
        return float("nan")
    changes = int(np.sum(pos[1:] != pos[:-1]))
    hours = (ts[-1] - ts[0]) / (3600.0 * NS_S)
    if hours <= 0:
        return float("nan")
    return changes / hours


def capacity_proxy_usd(
    adv_base_units: float,
    ref_price: float,
    max_participation: float,
    lot_value_multiplier: float = 1.0,
) -> float:
    """Deployable one-shot notional proxy (documented, coarse):

    capacity = max_participation * ADV * ref_price * multiplier

    ADV is in base units/day (configs/instruments.json), max_participation
    from configs/execution.json defaults.  This is an upper-bound style
    proxy — it ignores alpha decay vs execution time and assumes the full
    participation cap is achievable at model horizons; reports must treat
    it as an order-of-magnitude number only.
    """
    return float(max_participation * adv_base_units * ref_price * lot_value_multiplier)

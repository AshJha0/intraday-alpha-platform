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

**Bucket ICs are weighted by their pair count (pinned, round-4).**  Buckets
are fixed *time* windows, so they carry wildly different amounts of evidence
— 81 to 2 592 pairs in this dataset.  An equal-weighted mean treats an
81-pair IC as evidence equal to a 2 592-pair one, and the headline t then
swings with whichever thin bucket happens to be included: dropping a single
81-pair bucket moved EQ03's reported t from 4.89 to 11.46.  The mean, the
Bartlett autocovariances and the effective sample size are therefore all
computed with pair-count weights (:func:`newey_west_tstat`).  Pair counts
were chosen over Fisher-z ``n-3`` weights because they keep the reported
statistic a weighted mean IC — the same quantity the gates read — instead of
silently changing it to a mean of transformed ICs; the two weightings are
nearly identical anyway at these bucket sizes.  Every report carries the
bucket-size distribution (``ic_bucket_pairs``) so the weighting is
auditable.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from iap.backtest.engine import SESSION_GAP_NS

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


def bucket_ics_with_counts(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    bucket_ns: int = 300 * NS_S,
    min_obs: int = 8,
) -> Tuple[np.ndarray, np.ndarray]:
    """IC per fixed event-time bucket **and** each bucket's pair count.

    The counts are the weights :func:`newey_west_tstat` uses: fixed time
    buckets carry very different amounts of evidence, and an equal-weighted
    mean lets a thin bucket move the headline t-stat by more than the data
    in it justifies (see the module docstring).
    """
    ts = np.asarray(ts, dtype=np.int64)
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    ts, x, y = ts[ok], x[ok], y[ok]
    if ts.size == 0:
        return np.empty(0), np.empty(0, dtype=np.int64)
    buckets = ts // bucket_ns
    out: List[float] = []
    counts: List[int] = []
    for b in np.unique(buckets):
        m = buckets == b
        if m.sum() < min_obs:
            continue
        xb, yb = x[m], y[m]
        if xb.std() <= EPS or yb.std() <= EPS:
            continue
        out.append(float(np.corrcoef(xb, yb)[0, 1]))
        counts.append(int(m.sum()))
    return np.asarray(out), np.asarray(counts, dtype=np.int64)


def bucket_ics(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    bucket_ns: int = 300 * NS_S,
    min_obs: int = 8,
) -> np.ndarray:
    """IC per fixed event-time bucket (buckets with < min_obs pairs skipped)."""
    return bucket_ics_with_counts(ts, scores, labels, bucket_ns, min_obs)[0]


def bucket_size_summary(counts: np.ndarray) -> Dict[str, float]:
    """Distribution of bucket pair counts, for auditing the NW weighting.

    A t-stat built from 30 buckets of 2 000 pairs is a very different
    estimator from one built from 30 buckets ranging 81..2 592, and the
    report has to make that visible rather than quoting only ``n_buckets``.
    """
    c = np.asarray(counts, dtype=float)
    c = c[np.isfinite(c)]
    if c.size == 0:
        return {"n_buckets": 0, "total_pairs": 0, "min": float("nan"),
                "p25": float("nan"), "median": float("nan"),
                "p75": float("nan"), "max": float("nan"),
                "mean": float("nan")}
    return {
        "n_buckets": int(c.size),
        "total_pairs": int(c.sum()),
        "min": float(c.min()),
        "p25": float(np.quantile(c, 0.25)),
        "median": float(np.median(c)),
        "p75": float(np.quantile(c, 0.75)),
        "max": float(c.max()),
        "mean": float(c.mean()),
    }


def nw_lags(horizon_ns: int, bucket_ns: int = 300 * NS_S) -> int:
    """Pinned Newey-West lag count for a label horizon (see docstring)."""
    if horizon_ns <= 0 or bucket_ns <= 0:
        raise ValueError("horizon_ns and bucket_ns must be positive")
    return int(-(-int(horizon_ns) // int(bucket_ns))) + 1


def newey_west_tstat(
    series: np.ndarray,
    lags: int = 2,
    weights: Optional[np.ndarray] = None,
) -> float:
    """t-stat of the (weighted) mean of ``series`` vs 0, with a
    Bartlett-weighted long-run variance (see module docstring).

    ``weights`` is the evidence behind each entry — the bucket pair counts
    from :func:`bucket_ics_with_counts`.  The weighted estimator is the
    ordinary one with the sample moments taken under pair weights
    ``w_i w_{i+l}``: the point estimate is ``sum(w*s)/sum(w)``, each
    autocovariance is ``sum(w_i w_{i+l} d_i d_{i+l}) / sum(w_i w_{i+l})``,
    and the denominator uses Kish's effective sample size
    ``(sum w)^2 / sum(w^2)`` in place of ``n``.  With equal weights every one
    of those reduces exactly to the unweighted formula, so the pinned
    hand-calculation still holds bit-for-bit.
    """
    s = np.asarray(series, dtype=float)
    if weights is None:
        w = np.ones(s.shape, dtype=float)
    else:
        w = np.asarray(weights, dtype=float)
        if w.shape != s.shape:
            raise ValueError("weights/series length mismatch")
    ok = np.isfinite(s) & np.isfinite(w) & (w > 0.0)
    s, w = s[ok], w[ok]
    n = s.size
    if n < 8:
        return float("nan")
    sw = float(w.sum())
    m = float(np.sum(w * s) / sw)
    d = s - m

    def gamma(lag: int) -> float:
        if lag == 0:
            pw = w * w
            num = float(np.sum(pw * d * d))
        else:
            pw = w[lag:] * w[:-lag]
            num = float(np.sum(pw * d[lag:] * d[:-lag]))
        den = float(pw.sum())
        return num / den if den > 0.0 else 0.0

    lrv = gamma(0)
    for lag in range(1, min(lags, n - 1) + 1):
        lrv += 2.0 * (1.0 - lag / (lags + 1.0)) * gamma(lag)
    lrv = max(lrv, 0.0)
    if lrv <= EPS:
        return float("nan")
    n_eff = sw * sw / float(np.sum(w * w))
    return float(m / np.sqrt(lrv / n_eff))


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


def signal_turnover_detail(
    ts: np.ndarray,
    scores: np.ndarray,
    conf: np.ndarray,
    conf_min: float = 0.25,
    session_gap_ns: int = SESSION_GAP_NS,
) -> Dict[str, float]:
    """Flips, ACTIVE hours and wall span of a sign-following unit strategy.

    Position proxy: sign(score) where confidence >= conf_min else flat.

    The denominator is summed inter-row event time with every gap larger
    than ``session_gap_ns`` excluded (the same pinned session-gap constant
    the backtester's ``BacktestConfig`` carries), NOT the wall span.
    Turnover is a *cost* statistic: dividing flips by ``ts[-1] - ts[0]``
    bills the strategy for the overnight and weekend hours in which it
    cannot flip, and understates the rate it actually pays spread at by the
    ratio of closed to open time — ~4.6x on this platform's equity frames,
    where 29.4 flips/h wall-span is 134.7 flips/h of open market.  Both
    denominators are returned so a report can show its work.
    """
    ts = np.asarray(ts, dtype=np.int64)
    pos = np.sign(np.where(np.asarray(conf, float) >= conf_min, scores, 0.0))
    pos[~np.isfinite(pos)] = 0.0
    nan = float("nan")
    if len(pos) < 2 or len(ts) != len(pos):
        return {"flips": nan, "active_hours": nan, "span_hours": nan,
                "flips_per_hour": nan}
    changes = float(np.sum(pos[1:] != pos[:-1]))
    gaps = np.diff(ts)
    intra = gaps[(gaps > 0) & (gaps <= int(session_gap_ns))]
    active_hours = float(intra.sum()) / (3600.0 * NS_S)
    span_hours = float(ts[-1] - ts[0]) / (3600.0 * NS_S)
    return {
        "flips": changes,
        "active_hours": active_hours,
        "span_hours": span_hours,
        "flips_per_hour": changes / active_hours if active_hours > 0 else nan,
    }


def signal_turnover(
    ts: np.ndarray,
    scores: np.ndarray,
    conf: np.ndarray,
    conf_min: float = 0.25,
    session_gap_ns: int = SESSION_GAP_NS,
) -> float:
    """Position flips per hour of OPEN market time (see
    :func:`signal_turnover_detail` for the denominator and why)."""
    return signal_turnover_detail(
        ts, scores, conf, conf_min, session_gap_ns)["flips_per_hour"]


def capacity_proxy_usd(
    adv_base_units: float,
    ref_price: float,
    max_participation: float,
    lot_value_multiplier: float = 1.0,
) -> float:
    """Deployable one-shot notional proxy (documented, coarse):

    capacity = max_participation * ADV * ref_price * multiplier

    ADV is in base units/day (configs/instruments/instruments.json), max_participation
    from configs/execution/execution.json defaults.  This is an upper-bound style
    proxy — it ignores alpha decay vs execution time and assumes the full
    participation cap is achievable at model horizons; reports must treat
    it as an order-of-magnitude number only.
    """
    return float(max_participation * adv_base_units * ref_price * lot_value_multiplier)

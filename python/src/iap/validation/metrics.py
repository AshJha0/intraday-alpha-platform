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

from collections.abc import Sequence

import numpy as np

from iap.backtest.engine import SESSION_GAP_NS
from iap.labels.frames import DEFAULT_IC_ROWS, scored_labels

EPS = 1e-12
NS_S = 1_000_000_000

#: pinned label horizons in nanoseconds (mirrors iap.labels.HORIZONS_NS)
HORIZONS_NS: dict[str, int] = {
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
HORIZON_ORDER: tuple[str, ...] = tuple(HORIZONS_NS)


def _pairwise(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
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


def instrument_ics(
    instrument_ids: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    min_obs: int = 32,
) -> dict[str, object]:
    """IC that does not let one instrument's scale dominate the pool.

    The pooled IC (:func:`ic` over every instrument's rows concatenated)
    weights each row by the magnitude of its label and score.  Labels are
    returns: an instrument whose returns are ten times more volatile
    contributes a hundred times the cross-product, so the "universe" IC is
    largely that one instrument's IC — and a difference in MEAN return or
    mean score between instruments shows up as correlation that no row of
    any single instrument carries.  Two scale-free alternatives:

    * ``instrument_mean`` — the IC is computed inside each instrument and
      the per-instrument ICs are averaged with equal weight (instruments
      with fewer than ``min_obs`` pairs or a degenerate side are left out
      and counted in ``n_skipped``);
    * ``vol_scaled`` — each instrument's scores and labels are demeaned and
      divided by that instrument's own standard deviation, then pooled: one
      correlation over all rows, with every instrument on a unit scale.

    Returns ``{"instrument_mean", "vol_scaled", "by_instrument",
    "n_instruments", "n_skipped"}`` (NaN where nothing could be computed;
    ``by_instrument`` maps ``str(instrument_id)`` to its IC).  Additive
    diagnostics: the gates read the pooled IC.
    """
    ids = np.asarray(instrument_ids)
    x = np.asarray(scores, dtype=float)
    y = np.asarray(labels, dtype=float)
    if not (ids.shape == x.shape == y.shape):
        raise ValueError("instrument/score/label length mismatch")
    ok = np.isfinite(x) & np.isfinite(y)
    ids, x, y = ids[ok], x[ok], y[ok]
    by: dict[str, float] = {}
    zx: list[np.ndarray] = []
    zy: list[np.ndarray] = []
    skipped = 0
    for iid in np.unique(ids):
        m = ids == iid
        xi, yi = x[m], y[m]
        if xi.size < min_obs or xi.std() <= EPS or yi.std() <= EPS:
            skipped += 1
            continue
        by[str(int(iid))] = float(np.corrcoef(xi, yi)[0, 1])
        zx.append((xi - xi.mean()) / xi.std())
        zy.append((yi - yi.mean()) / yi.std())
    if not by:
        return {
            "instrument_mean": float("nan"),
            "vol_scaled": float("nan"),
            "by_instrument": {},
            "n_instruments": 0,
            "n_skipped": skipped,
        }
    return {
        "instrument_mean": float(np.mean(list(by.values()))),
        "vol_scaled": float(np.corrcoef(np.concatenate(zx), np.concatenate(zy))[0, 1]),
        "by_instrument": by,
        "n_instruments": len(by),
        "n_skipped": skipped,
    }


def ic_with_blackout_reopen(
    scores: np.ndarray,
    label_mid: np.ndarray,
    label_valid: np.ndarray,
    label_reason: np.ndarray,
    reopen_mid: np.ndarray,
    min_obs: int = 32,
) -> dict[str, float]:
    """IC that keeps the rows a halt / auction / stale gap removed.

    A label whose horizon contains a non-tradable sample is INVALID
    (``LabelReason.BLACKOUT``) and every IC drops it.  That is the right
    call for "the return you could have traded", but it is a selection on
    the outcome: the rows dropped are exactly the ones before a halt or a
    stale-book gap, where a signal is most likely to be wrong-footed by the
    reopen jump, so the surviving sample flatters it.  This variant scores
    those rows at the realised REOPEN return
    (``compute_labels(..., blackout_reopen=True)`` -> ``reopen_mid``: the
    return from the anchor mid to the first tradable mid at or after the
    horizon) and every other row as usual.

    Only rows invalid for ``BLACKOUT`` ALONE are added — a row that is also
    unobserved, unanchored or stale has no price to score against.  Returns
    ``{"ic", "ic_valid_only", "n_valid", "n_blackout_scored"}``.
    """
    from iap.labels.labels import LabelReason

    x = np.asarray(scores, dtype=float)
    lab = np.asarray(label_mid, dtype=float)
    valid = np.asarray(label_valid, dtype=bool)
    reason = np.asarray(label_reason, dtype=np.int64)
    reopen = np.asarray(reopen_mid, dtype=float)
    if not (x.shape == lab.shape == valid.shape == reason.shape == reopen.shape):
        raise ValueError("score/label/reason/reopen length mismatch")
    base = np.where(valid, lab, np.nan)
    rescued = (~valid) & (reason == LabelReason.BLACKOUT) & np.isfinite(reopen)
    full = np.where(rescued, reopen, base)
    return {
        "ic": ic(x, full, min_obs),
        "ic_valid_only": ic(x, base, min_obs),
        "n_valid": int(np.sum(np.isfinite(x) & np.isfinite(base))),
        "n_blackout_scored": int(np.sum(np.isfinite(x) & rescued)),
    }


def hac_mean_variance(
    series: np.ndarray,
    lags: int = 2,
    weights: np.ndarray | None = None,
) -> tuple[float, float, int]:
    """``(mean, variance of the mean, n)`` of a (pair-count weighted) series
    under a Bartlett long-run variance — the two ingredients of
    :func:`newey_west_tstat` (``t = mean / sqrt(variance)``), exposed so two
    such means can be compared.  NaN variance when it cannot be estimated
    (fewer than 2 entries or a non-positive long-run variance)."""
    s = np.asarray(series, dtype=float)
    if weights is None:
        w = np.ones(s.shape, dtype=float)
    else:
        w = np.asarray(weights, dtype=float)
        if w.shape != s.shape:
            raise ValueError("weights/series length mismatch")
    ok = np.isfinite(s) & np.isfinite(w) & (w > 0.0)
    s, w = s[ok], w[ok]
    n = int(s.size)
    if n == 0:
        return float("nan"), float("nan"), 0
    sw = float(w.sum())
    m = float(np.sum(w * s) / sw)
    if n < 2:
        return m, float("nan"), n
    d = s - m
    lrv = float(np.sum(w * w * d * d) / np.sum(w * w))
    for lag in range(1, min(lags, n - 1) + 1):
        pw = w[lag:] * w[:-lag]
        den = float(pw.sum())
        gamma = float(np.sum(pw * d[lag:] * d[:-lag])) / den if den > 0.0 else 0.0
        lrv += 2.0 * (1.0 - lag / (lags + 1.0)) * gamma
    if not lrv > EPS:
        return m, float("nan"), n
    n_eff = sw * sw / float(np.sum(w * w))
    return m, float(lrv / n_eff), n


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
) -> tuple[np.ndarray, np.ndarray]:
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
    out: list[float] = []
    counts: list[int] = []
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


def bucket_size_summary(counts: np.ndarray) -> dict[str, float]:
    """Distribution of bucket pair counts, for auditing the NW weighting.

    A t-stat built from 30 buckets of 2 000 pairs is a very different
    estimator from one built from 30 buckets ranging 81..2 592, and the
    report has to make that visible rather than quoting only ``n_buckets``.
    """
    c = np.asarray(counts, dtype=float)
    c = c[np.isfinite(c)]
    if c.size == 0:
        return {
            "n_buckets": 0,
            "total_pairs": 0,
            "min": float("nan"),
            "p25": float("nan"),
            "median": float("nan"),
            "p75": float("nan"),
            "max": float("nan"),
            "mean": float("nan"),
        }
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
    weights: np.ndarray | None = None,
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


def pooled_slope_hac_tstat(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    lags: int = 2,
    bucket_ns: int = 300 * NS_S,
    min_buckets: int = 8,
) -> float:
    """HAC t-stat of the POOLED slope of label on score (additive statistic).

    :func:`newey_west_tstat` over :func:`bucket_ics_with_counts` tests the
    mean of *within-bucket* Pearson correlations.  A within-bucket
    correlation demeans score and label inside each bucket, so any signal
    that lives BETWEEN buckets — a score whose 5-minute average predicts the
    5-minute average return — is removed from the test statistic, while the
    gate IC it sits beside is pooled and keeps it.  The two therefore do not
    test the same quantity.  This function tests the pooled one:

        b      = sum((x - xbar) * (y - ybar)) / Sxx,   Sxx = sum((x - xbar)^2)
        e_i    = (y_i - ybar) - b * (x_i - xbar)
        s_k    = sum over pairs i in bucket k of (x_i - xbar) * e_i
        G_l    = sum_k s_k * s_{k+l}
        var(b) = (G_0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * G_l) / Sxx^2
        t      = b / sqrt(var(b))

    with global (pooled) means, the same fixed event-time buckets, the same
    Bartlett weights and the same lag rule (:func:`nw_lags`) as the
    within-bucket statistic, and consecutive non-empty buckets treated as
    adjacent exactly as :func:`newey_west_tstat` treats its series.  Rows
    inside a bucket may be arbitrarily dependent (the bucket sum absorbs
    it); dependence across buckets is covered out to ``lags``.  The slope's
    t equals the pooled IC's t (correlation is the slope of the
    standardized variables), so this is the HAC significance of the number
    the gate actually reads.

    Returns NaN with fewer than ``min_buckets`` non-empty buckets, a
    degenerate side, or a non-positive long-run variance.
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
    if x.size == 0 or x.std() <= EPS or y.std() <= EPS:
        return float("nan")
    dx = x - x.mean()
    dy = y - y.mean()
    sxx = float(np.sum(dx * dx))
    slope = float(np.sum(dx * dy)) / sxx
    moment = dx * (dy - slope * dx)
    _, inverse = np.unique(ts // bucket_ns, return_inverse=True)
    s = np.bincount(inverse, weights=moment)
    n = s.size
    if n < min_buckets:
        return float("nan")
    lrv = float(np.sum(s * s))
    for lag in range(1, min(lags, n - 1) + 1):
        lrv += 2.0 * (1.0 - lag / (lags + 1.0)) * float(np.sum(s[lag:] * s[:-lag]))
    if not lrv > 0.0:
        return float("nan")
    return float(slope * sxx / np.sqrt(lrv))


def decay_curve(
    scores: np.ndarray,
    frame,
    horizons: Sequence[str] = HORIZON_ORDER,
    ic_rows: str = DEFAULT_IC_ROWS,
) -> dict[str, float]:
    """IC of one score series against every pinned horizon's mid label, on
    the rows ``ic_rows`` scores (:mod:`iap.labels.frames`)."""
    out: dict[str, float] = {}
    for h in horizons:
        lab, _ = scored_labels(frame, h, ic_rows)
        out[h] = ic(scores, lab)
    return out


def signal_turnover_detail(
    ts: np.ndarray,
    scores: np.ndarray,
    conf: np.ndarray,
    conf_min: float = 0.25,
    session_gap_ns: int = SESSION_GAP_NS,
) -> dict[str, float]:
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
        return {"flips": nan, "active_hours": nan, "span_hours": nan, "flips_per_hour": nan}
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
    return signal_turnover_detail(ts, scores, conf, conf_min, session_gap_ns)["flips_per_hour"]


def capacity_breakeven(
    cost_model,
    edge_return: float,
    mid: float,
    half_spread: float,
    asset_class: str,
    adv: float,
    lot_size: int = 1,
) -> dict[str, float]:
    """Edge-based capacity: the size at which edge per trade equals cost.

    :func:`capacity_proxy_usd` is ``max_participation * ADV * price`` — a
    liquidity cap that is the same for an alpha with a 5 bp edge and one
    with none.  This estimate asks the economic question instead: with an
    expected gross return of ``edge_return`` per round trip, how large can
    one trade be before its own spread, fee and impact consume the edge?
    (:meth:`iap.backtest.costs.CostModel.breakeven_size`, under whichever
    impact model the cost model carries.)

    Returns ``{"units", "notional", "participation"}``: the breakeven size
    in qty units, in quote currency and as a fraction of ADV.  All three are
    0 for an edge that does not cover spread + fee, and ``inf`` when the
    cost model charges no impact.  Additive: no gate reads it.
    """
    units = cost_model.breakeven_size(edge_return, mid, half_spread, asset_class, adv, lot_size)
    unit = float(lot_size) if asset_class == "FX" else 1.0
    return {
        "units": float(units),
        "notional": float(units * unit * mid),
        "participation": float(units * unit / adv),
    }


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

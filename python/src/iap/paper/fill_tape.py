"""Fill-model tape test (REL-05): calibrate on days A, predict on held-out day B.

Public data only.  The fill hazard (:mod:`iap.execution.fill_hazard`) is fitted
on survival samples of the FIT days; for the EVAL days (disjoint, never used to
fit or to define any bucket edge) it predicts ``P(fill within ttl)`` for each
hypothetical resting order, and the prediction is scored against what the tape
shows the order would have done (the label comes from
:func:`iap.labels.maker_labels.maker_labels`, i.e. the simulator's queue rules
replayed on the tape: queue position at the post and later EXECUTE prints).

The outcome is therefore "would the tape have executed this order under the
simulator's queue rules", NOT a measured fill of a real order.  This tests the
hazard model against its own label generator out of sample; it is not Level 2
validation and does not test the simulator's queue rules against real fills.

Scores: Brier score of the model, of the naive baseline (the pooled fill rate of
the FIT set, constant) and the skill ``1 - Brier / Brier_naive``; reliability
data (equal-width probability bins: mean predicted, observed rate, n) and
per-venue and per-liquidity-bucket breakdowns.  Liquidity buckets are terciles
of ``queue_ahead`` with the edges taken from the FIT set only.  Cells under
``MIN_N`` samples are flagged.  Samples of one day are serially dependent, so no
significance statement is made here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from iap.core.events import MarketEvent
from iap.execution.config import ExecConfig
from iap.execution.fill_hazard import FillHazardModel, samples_from_maker_labels
from iap.labels.maker_labels import maker_labels

MIN_N = 100
N_BINS = 10
BUCKETS = ("low", "mid", "high")  # queue_ahead terciles: short queue ... long queue


def day_samples(
    events: Sequence[MarketEvent],
    decision_ts: Sequence[int],
    *,
    instrument_id: int,
    venue_id: int,
    exec_config: ExecConfig,
    ttl_ns: int,
    day: str,
    qty: int = 1,
) -> pd.DataFrame:
    """Survival samples of one day with ``day``, ``venue_id`` and ``fill_within_ttl``."""
    labels = maker_labels(
        events,
        decision_ts,
        instrument_id=instrument_id,
        exec_config=exec_config,
        venue_id=venue_id,
        ttl_ns=ttl_ns,
        qty=qty,
    )
    s = samples_from_maker_labels(labels, ttl_ns=ttl_ns)
    s["day"] = day
    s["venue_id"] = venue_id
    s["fill_within_ttl"] = (s["filled"] & (s["duration_ns"] <= ttl_ns)).astype(float)
    return s


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _cell(p: np.ndarray, y: np.ndarray, base: float) -> dict[str, Any]:
    n = len(y)
    out: dict[str, Any] = {"n": n, "underpowered": n < MIN_N}
    if n:
        b, bn = _brier(p, y), _brier(np.full(n, base), y)
        out.update(
            {
                "observed_rate": float(y.mean()),
                "mean_predicted": float(p.mean()),
                "brier": b,
                "brier_naive": bn,
                "skill": (1.0 - b / bn) if bn > 0 else None,
            }
        )
    return out


def reliability(p: np.ndarray, y: np.ndarray, n_bins: int = N_BINS) -> list[dict[str, Any]]:
    """Equal-width bins on [0, 1]; empty bins are listed with ``n`` 0."""
    idx = np.minimum((p * n_bins).astype(int), n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        rows.append(
            {
                "bin_lo": b / n_bins,
                "bin_hi": (b + 1) / n_bins,
                "n": int(m.sum()),
                "mean_predicted": float(p[m].mean()) if m.any() else None,
                "observed_rate": float(y[m].mean()) if m.any() else None,
            }
        )
    return rows


def tape_test(
    fit: Mapping[str, pd.DataFrame],
    evaluate: Mapping[str, pd.DataFrame],
    *,
    ttl_ns: int,
    model: FillHazardModel | None = None,
) -> dict[str, Any]:
    """Fit on ``fit`` (day -> :func:`day_samples` frame), score on ``evaluate``.

    Raises when a day appears in both sets, or when any (day, venue) of the
    evaluation frames is also in the fit frames - the evaluation set must be
    separate.  Returns a JSON-able report (``level2`` is always ``not validated``).
    """
    if not fit or not evaluate:
        raise ValueError("need at least one fit day and one evaluation day")
    both = set(fit) & set(evaluate)
    if both:
        raise ValueError(f"days in both the fit and the evaluation set: {sorted(both)}")
    fit_df = pd.concat(list(fit.values()), ignore_index=True)
    ev_df = pd.concat(list(evaluate.values()), ignore_index=True)
    if set(fit_df["day"]) & set(ev_df["day"]):
        raise ValueError("a frame's 'day' column overlaps between fit and evaluation sets")
    mdl = (model or FillHazardModel()).fit(fit_df)
    base = float(fit_df["fill_within_ttl"].mean())
    p = np.asarray(mdl.p_fill_within(ev_df, ttl_ns), dtype=float)
    y = ev_df["fill_within_ttl"].to_numpy(dtype=float)

    q_edges = np.quantile(fit_df["queue_ahead"].to_numpy(dtype=float), [1 / 3, 2 / 3])
    bucket = np.searchsorted(q_edges, ev_df["queue_ahead"].to_numpy(dtype=float), side="right")

    by_venue = {
        str(v): _cell(p[m], y[m], base)
        for v in sorted(ev_df["venue_id"].unique())
        for m in [(ev_df["venue_id"] == v).to_numpy()]
    }
    by_bucket = {BUCKETS[b]: _cell(p[bucket == b], y[bucket == b], base) for b in range(3)}
    by_cell = {
        f"{v}/{BUCKETS[b]}": _cell(p[m], y[m], base)
        for v in sorted(ev_df["venue_id"].unique())
        for b in range(3)
        for m in [((ev_df["venue_id"] == v).to_numpy()) & (bucket == b)]
        if m.any()
    }
    return {
        "schema": "iap.fill_tape_test",
        "version": 1,
        "level2": "not validated",
        "label": "tape-implied under the simulator's queue rules, not a measured real fill",
        "ttl_ns": int(ttl_ns),
        "fit_days": sorted(fit),
        "eval_days": sorted(evaluate),
        "n_fit": int(len(fit_df)),
        "n_eval": int(len(ev_df)),
        "naive_baseline_rate": base,
        "queue_ahead_edges_from_fit": [float(e) for e in q_edges],
        "overall": _cell(p, y, base),
        "reliability": reliability(p, y),
        "by_venue": by_venue,
        "by_liquidity_bucket": by_bucket,
        "by_venue_bucket": by_cell,
        "model": {"link": mdl.link, "n_samples": mdl.n_samples, "n_filled": mdl.n_filled},
    }

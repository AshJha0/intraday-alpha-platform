"""Per-fold diagnostics and a bootstrap interval for net P&L.

Up to v1.4.0 :func:`iap.validation.validate.validate_alpha` computed its
cost survival, decay curve and regime split on the LAST walk-forward fold
only (the largest-train model on its test segment) and reported net P&L as
one number.  Both are thin evidence: "net P&L > 0 at 1x costs" from a single
fold cannot tell an alpha that survives costs in every fold from one that
survives in the last and loses in the other three, and a point estimate
carries no statement about its own noise.

Since v1.5.0 these views are REPORTED FIELDS of every validation result
(``fold_diagnostics``, ``n_folds_survive_1x_cost``, ``net_pnl_1x_pooled``,
``net_pnl_bootstrap``): ``validate_alpha`` builds one :func:`fold_row` per
fold inside its own walk-forward loop.  They are report-only — no gate
reads them.  The cost gate still reads the last fold's net P&L at 1x, and a
gate on the bootstrap interval was NOT added: the lifecycle gate table
(``iap.lifecycle.gates``) reads one scalar, ``net_return_bps > 0``, from an
``ExperimentResult``, so gating on an interval needs a new evidence field
and a new gate row in the 17-edge table that Python, Java and Rust pin —
a redesign, not a default change.

* :func:`fold_row` — one fold's diagnostics: net P&L across the pinned cost
  grid and whether it survives 1x costs, the decay curve of the
  standardized signal, and the regime split;
* :func:`fold_diagnostics` — the standalone form: the same walk-forward
  split, a fresh model per fold, a :func:`fold_row` for EVERY fold, plus
  the pooled 1-minute bar P&L of all test segments and its bootstrap
  interval (what ``validate_alpha`` embeds in its report);
* :func:`stationary_bootstrap_ci` — a percentile confidence interval for the
  SUM of a dependent series by the stationary bootstrap (Politis & Romano,
  1994): blocks of geometrically distributed length (mean ``mean_block``)
  starting at uniformly drawn positions, wrapped circularly, concatenated to
  the original length.  Random block lengths make the resampled series
  stationary; block resampling keeps the short-range dependence a plain
  bootstrap would destroy (bar P&L of a position held across bars is
  autocorrelated).  All randomness is SplitMix64 from an explicit ``seed``
  (PLATFORM_CONVENTIONS.md §3) — this is the component the
  ``ExperimentSpec.seed`` exists for: pass ``spec.seed`` and the interval is
  pinned by the spec.

Pinned defaults: 1 000 resamples, 95 % level, ``mean_block =
max(1, round(n ** (1/3)))`` (the usual rate for a block bootstrap of the
mean).  Percentiles use the lower empirical quantile (no interpolation), so
the interval is a pair of resampled values and is identical on every
platform.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from iap.backtest.engine import Backtester, BacktestResult
from iap.core.rng import SplitMix64
from iap.labels.frames import DEFAULT_IC_ROWS, scored_labels
from iap.validation.metrics import HORIZONS_NS, decay_curve
from iap.validation.splits import MIN_TEST_PAIRS, WalkForwardSplitter
from iap.validation.stress import COST_MULTIPLIERS, cost_stress_results, regime_split

__all__ = [
    "BOOTSTRAP_LEVEL",
    "BOOTSTRAP_RESAMPLES",
    "bar_series",
    "fold_diagnostics",
    "fold_row",
    "stationary_bootstrap_ci",
]

BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_LEVEL = 0.95


def _fnum(v: float) -> float | None:
    return float(v) if np.isfinite(v) else None


def stationary_bootstrap_ci(
    values: Sequence[float],
    seed: int,
    n_boot: int = BOOTSTRAP_RESAMPLES,
    mean_block: float | None = None,
    level: float = BOOTSTRAP_LEVEL,
) -> dict[str, object]:
    """Stationary-bootstrap percentile interval for ``sum(values)``.

    Returns ``{"estimate", "ci_low", "ci_high", "level", "n", "n_boot",
    "mean_block", "seed", "frac_resamples_le_zero"}``.  ``ci_low`` /
    ``ci_high`` are ``None`` when the series has fewer than 8 finite
    entries (an interval from a handful of bars is noise).  Non-finite
    entries are dropped.  Deterministic for a given ``seed``.
    """
    if n_boot < 1:
        raise ValueError("n_boot must be >= 1")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    v = np.asarray(values, dtype=float).ravel()
    v = v[np.isfinite(v)]
    n = int(v.size)
    if mean_block is None:
        mean_block = float(max(1, round(n ** (1.0 / 3.0)))) if n else 1.0
    if not mean_block >= 1.0:
        raise ValueError("mean_block must be >= 1")
    out: dict[str, object] = {
        "estimate": float(v.sum()) if n else 0.0,
        "ci_low": None,
        "ci_high": None,
        "level": float(level),
        "n": n,
        "n_boot": int(n_boot),
        "mean_block": float(mean_block),
        "seed": int(seed),
        "frac_resamples_le_zero": None,
    }
    if n < 8:
        return out
    rng = SplitMix64(int(seed))
    # circular prefix sums: sum(v[s : s + L]) wrapping = csum[s + L] - csum[s]
    csum = np.concatenate(([0.0], np.cumsum(np.concatenate((v, v)))))
    p = 1.0 / mean_block
    log_q = math.log(1.0 - p) if p < 1.0 else 0.0
    sums = np.empty(n_boot)
    for b in range(n_boot):
        total = 0.0
        filled = 0
        while filled < n:
            start = rng.below(n)
            if p < 1.0:
                u = rng.uniform()
                # geometric length on {1, 2, ...} with success probability p
                length = int(math.log(1.0 - u) / log_q) + 1
            else:
                length = 1
            length = min(length, n - filled, n)
            total += float(csum[start + length] - csum[start])
            filled += length
        sums[b] = total
    ordered = np.sort(sums)
    tail = (1.0 - level) / 2.0
    lo = ordered[min(n_boot - 1, max(0, int(math.floor(tail * n_boot))))]
    hi = ordered[min(n_boot - 1, max(0, int(math.ceil((1.0 - tail) * n_boot)) - 1))]
    out["ci_low"] = float(lo)
    out["ci_high"] = float(hi)
    out["frac_resamples_le_zero"] = float(np.mean(sums <= 0.0))
    return out


_KEY_1X = f"x{1.0:g}"


def fold_row(
    fold_index: int,
    scores: Mapping[int, pd.DataFrame],
    test: Mapping[int, pd.DataFrame],
    horizon: str,
    beta: float,
    backtester: Backtester,
    asset_class: str,
    multipliers: Sequence[float] = COST_MULTIPLIERS,
    ic_rows: str = DEFAULT_IC_ROWS,
) -> tuple[dict[str, object], dict[str, dict], dict[str, BacktestResult]]:
    """One fold's diagnostics from its fitted model's ``scores`` on ``test``.

    Returns ``(row, cost, results)``: ``row`` is the reported record
    (``fold``, ``n_test_pairs``, ``degenerate``, ``net_pnl_by_cost``,
    ``trade_count_1x``, ``survives_1x_cost``, ``decay_ic_by_horizon``,
    ``regime``); ``cost`` the cost-stress grid of the fold
    (:func:`iap.validation.stress.cost_stress` shape) and ``results`` the
    backtest result per grid point, so the caller reads the 1x bar P&L
    without running the backtest again.  The standardized signal
    ``z = er / beta`` is what decay and regime read; rows with confidence
    <= 0 carry no prediction.  ``backtester`` must already carry the label
    horizon (:meth:`Backtester.for_horizon`) when its policy needs one.
    """
    if 1.0 not in [float(m) for m in multipliers]:
        raise ValueError("multipliers must include 1.0 (the cost-survival grid point)")
    n_pairs = 0
    decay: dict[str, list[float]] = {}
    for iid, sc in scores.items():
        er = sc["expected_return"].to_numpy(dtype=float).copy()
        er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
        lab, _ = scored_labels(test[iid], horizon, ic_rows)
        n_pairs += int(np.sum(np.isfinite(er) & np.isfinite(lab)))
        z = er / beta if beta != 0.0 else er
        horizons = [h for h in HORIZONS_NS if f"label_mid_{h}" in test[iid].columns]
        for h, v in decay_curve(z, test[iid], horizons, ic_rows).items():
            if np.isfinite(v):
                decay.setdefault(h, []).append(v)
    cost, results = cost_stress_results(backtester, test, scores, asset_class, multipliers)
    if "vol_regime_flag_v1" in next(iter(test.values())).columns:
        regime = {
            k: _fnum(v)
            for k, v in regime_split(scores, test, horizon, beta=beta, ic_rows=ic_rows).items()
        }
    else:
        regime = {}
    row: dict[str, object] = {
        "fold": int(fold_index),
        "n_test_pairs": n_pairs,
        "degenerate": n_pairs < MIN_TEST_PAIRS,
        "net_pnl_by_cost": {k: _fnum(v["total_pnl"]) for k, v in cost.items()},
        "trade_count_1x": int(cost[_KEY_1X]["trade_count"]),
        "survives_1x_cost": bool(cost[_KEY_1X]["total_pnl"] > 0.0),
        "decay_ic_by_horizon": {h: _fnum(float(np.mean(v))) for h, v in decay.items()},
        "regime": regime,
    }
    return row, cost, results


def bar_series(results: Sequence[BacktestResult]) -> list[float]:
    """The 1-minute bar P&L of several backtests pooled by bar timestamp, in
    time order — the series the bootstrap interval is taken over."""
    bars: dict[int, float] = {}
    for result in results:
        for r in result.per_instrument.values():
            for t, pnl in zip(r.bar_ts, r.bar_pnl, strict=False):
                bars[int(t)] = bars.get(int(t), 0.0) + float(pnl)
    return [bars[t] for t in sorted(bars)]


def fold_diagnostics(
    model_factory,
    frames: Mapping[int, pd.DataFrame],
    backtester: Backtester,
    n_folds: int = 4,
    embargo_ns: int = 60_000_000_000,
    seed: int = 0,
    n_boot: int = BOOTSTRAP_RESAMPLES,
    multipliers: Sequence[float] = COST_MULTIPLIERS,
    ic_rows: str = DEFAULT_IC_ROWS,
) -> dict[str, object]:
    """Cost survival, decay and regime split for EVERY walk-forward fold,
    and a stationary-bootstrap interval for the pooled net P&L.

    The split, the per-fold fresh model and the scoring conventions are
    ``validate_alpha``'s own, and since v1.5.0 ``validate_alpha`` reports the
    same block itself; this standalone form exists for callers that want the
    diagnostics without the rest of the validation.  Returns::

        {"folds": [fold_row, ...],
         "n_folds_run", "n_folds_survive_1x_cost",
         "net_pnl_1x_pooled", "net_pnl_bootstrap": {...}}

    ``net_pnl_bootstrap`` is :func:`stationary_bootstrap_ci` over the 1x-cost
    1-minute bar P&L of all test segments in time order, seeded with
    ``seed`` (pass ``ExperimentSpec.seed``).
    """
    probe = model_factory()
    horizon = probe.horizon
    horizon_ns = HORIZONS_NS[horizon]
    universe = probe.universe(list(frames))
    uframes = {i: frames[i] for i in universe}
    asset_class = "FX" if probe.asset_class == "FX" else "EQUITY"
    splitter = WalkForwardSplitter(n_folds=n_folds, embargo_ns=embargo_ns)
    bt = backtester.for_horizon(horizon)

    folds: list[dict] = []
    results_1x: list[BacktestResult] = []
    for fold, train, test in splitter.split_frames(uframes, horizon_ns):
        model = model_factory()
        model.fit(train)
        scores = model.score(test)
        beta = float(model.params().get("beta", 0.0) or 0.0)
        row, _, results = fold_row(
            fold.index, scores, test, horizon, beta, bt, asset_class, multipliers, ic_rows
        )
        folds.append(row)
        results_1x.append(results[_KEY_1X])
    series = bar_series(results_1x)
    return {
        "folds": folds,
        "n_folds_run": len(folds),
        "n_folds_survive_1x_cost": sum(1 for f in folds if f["survives_1x_cost"]),
        "net_pnl_1x_pooled": float(sum(series)),
        "net_pnl_bootstrap": stationary_bootstrap_ci(series, seed, n_boot=n_boot),
    }

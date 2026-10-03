"""Per-fold diagnostics and a bootstrap interval for net P&L (additive).

:func:`iap.validation.validate.validate_alpha` computes its cost survival,
decay curve, regime split and stress grid on the LAST walk-forward fold only
(the largest-train model on its test segment), and reports net P&L as one
number.  Both are thin evidence: a gate that reads "net P&L > 0 at 1x costs"
from a single fold cannot tell an alpha that survives costs in every fold
from one that survives in the last and loses in the other three, and a
point estimate carries no statement about its own noise.

This module adds the missing views WITHOUT touching the report
``validate_alpha`` returns (the committed reports and the
``ExperimentResult`` contract are pinned; no gate reads anything here):

* :func:`fold_diagnostics` — the same walk-forward split, a fresh model per
  fold, and for EVERY fold: net P&L across the pinned cost grid and whether
  it survives 1x costs, the decay curve of the standardized signal, and the
  regime split; plus the pooled 1-minute bar P&L of all test segments and
  its bootstrap interval.
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

from iap.backtest.engine import Backtester
from iap.core.rng import SplitMix64
from iap.validation.metrics import HORIZONS_NS, decay_curve
from iap.validation.splits import MIN_TEST_PAIRS, WalkForwardSplitter
from iap.validation.stress import COST_MULTIPLIERS, cost_stress, regime_split

__all__ = [
    "BOOTSTRAP_LEVEL",
    "BOOTSTRAP_RESAMPLES",
    "fold_diagnostics",
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


def fold_diagnostics(
    model_factory,
    frames: Mapping[int, pd.DataFrame],
    backtester: Backtester,
    n_folds: int = 4,
    embargo_ns: int = 60_000_000_000,
    seed: int = 0,
    n_boot: int = BOOTSTRAP_RESAMPLES,
    multipliers: Sequence[float] = COST_MULTIPLIERS,
) -> dict[str, object]:
    """Cost survival, decay and regime split for EVERY walk-forward fold,
    and a stationary-bootstrap interval for the pooled net P&L.

    The split, the per-fold fresh model and the scoring conventions are
    ``validate_alpha``'s own (the standardized signal ``z = er / beta`` is
    what decay and regime read; rows with confidence <= 0 carry no
    prediction).  Returns::

        {"folds": [{"fold", "n_test_pairs", "degenerate",
                    "net_pnl_by_cost": {"x0.5": ..., "x1": ..., "x2": ...},
                    "survives_1x_cost": bool,
                    "decay_ic_by_horizon": {...}, "regime": {...}}, ...],
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
    key_1x = f"x{1.0:g}"
    if 1.0 not in [float(m) for m in multipliers]:
        raise ValueError("multipliers must include 1.0 (the cost-survival grid point)")

    folds: list[dict] = []
    bars: dict[int, float] = {}
    for fold, train, test in splitter.split_frames(uframes, horizon_ns):
        model = model_factory()
        model.fit(train)
        scores = model.score(test)
        beta = float(model.params().get("beta", 0.0) or 0.0)
        n_pairs = 0
        decay: dict[str, list[float]] = {}
        for iid, sc in scores.items():
            er = sc["expected_return"].to_numpy(dtype=float).copy()
            er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
            lab = test[iid][f"label_mid_{horizon}"].to_numpy(dtype=float)
            ok = test[iid][f"label_valid_{horizon}"].to_numpy(dtype=bool)
            n_pairs += int(np.sum(np.isfinite(er) & np.isfinite(lab) & ok))
            z = er / beta if beta != 0.0 else er
            horizons = [h for h in HORIZONS_NS if f"label_mid_{h}" in test[iid].columns]
            for h, v in decay_curve(z, test[iid], horizons).items():
                if np.isfinite(v):
                    decay.setdefault(h, []).append(v)
        cost = cost_stress(backtester, test, scores, asset_class, multipliers)
        result = backtester.run(test, scores, asset_class)
        for r in result.per_instrument.values():
            for t, pnl in zip(r.bar_ts, r.bar_pnl):
                bars[int(t)] = bars.get(int(t), 0.0) + float(pnl)
        if "vol_regime_flag_v1" in next(iter(test.values())).columns:
            regime = {
                k: _fnum(v) for k, v in regime_split(scores, test, horizon, beta=beta).items()
            }
        else:
            regime = {}
        folds.append(
            {
                "fold": fold.index,
                "n_test_pairs": n_pairs,
                "degenerate": n_pairs < MIN_TEST_PAIRS,
                "net_pnl_by_cost": {k: _fnum(v["total_pnl"]) for k, v in cost.items()},
                "survives_1x_cost": bool(cost[key_1x]["total_pnl"] > 0.0),
                "decay_ic_by_horizon": {h: _fnum(float(np.mean(v))) for h, v in decay.items()},
                "regime": regime,
            }
        )
    series = [bars[t] for t in sorted(bars)]
    return {
        "folds": folds,
        "n_folds_run": len(folds),
        "n_folds_survive_1x_cost": sum(1 for f in folds if f["survives_1x_cost"]),
        "net_pnl_1x_pooled": float(sum(series)),
        "net_pnl_bootstrap": stationary_bootstrap_ci(series, seed, n_boot=n_boot),
    }

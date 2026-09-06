"""Per-alpha validation orchestrator (spec §13 + §20 promotion gates).

``validate_alpha`` runs the full evidence chain for one alpha on one data
set and returns a JSON-serializable report:

1. expanding walk-forward (purged + embargoed) — per-fold OOS IC/RankIC/
   hit rate, pooled Newey-West-lite t-stat over 5-minute bucket ICs;
2. leakage tests (label-column guard + shift-by-one);
3. decay curve across the 11 pinned horizons (fit at the pinned horizon,
   scored OOS, correlated against every horizon's label);
4. turnover and capacity proxies;
5. cost / latency / regime stress (via the research backtester);
6. verdict per the pinned §20 gates (see GATES below).

Pinned promotion gates (spec §20 steps 4-7 distilled; thresholds pinned
here and echoed in every report):

- PROMOTE requires ALL of:
    leakage passed; OOS pooled IC >= 0.010; NW t-stat >= 3.0;
    fold sign consistency >= 0.70; at least 3 NON-DEGENERATE folds;
    hypothesis_confirmed (fitted beta agrees with the stated rationale);
    cost survival (net backtest P&L > 0 at 1.0x costs).
- ITERATE: leakage passed, OOS IC >= 0.005 and NW t-stat >= 1.5 (evidence
  of signal, fails at least one PROMOTE gate).
- REJECT: everything else, and ALWAYS when leakage fails.

Round-3 honesty rules baked into the report (all pinned):

- **Degenerate folds count as failures.**  A fold with fewer than
  ``MIN_TEST_PAIRS`` usable score/label pairs is reported
  (``degenerate: true``) and counted as a *failed* fold in
  ``fold_sign_consistency`` instead of silently vanishing from the mean.
  ``n_nondegenerate_folds`` is reported and gates PROMOTE.
- **The pooled IC is computed on z, not on expected_return.**  Folds fit
  different betas, so concatenating ``expected_return`` weights each fold by
  |beta_k| and a sign flip between folds can cancel the IC.  The gate uses
  the pooled standardized signal (``expected_return / beta_k`` per fold,
  falling back to the raw er when beta is 0); the er-pooled IC is kept as
  ``oos_ic_pooled_er`` for continuity.
- **Crossed-book conditioning.**  A consolidated FX book is CROSSED
  (``spread_ticks_v1 < 0``) whenever one LP's quote is stale; the mid then
  reverts mechanically when that LP refreshes, and a vol-scaled momentum
  signal is paid for measuring exactly that artefact.  Every report splits
  the OOS IC into ``oos_ic_uncrossed`` / ``oos_ic_crossed`` and records
  ``crossed_frac``.  **The PROMOTE gate uses the UNCROSSED IC.**
- **Newey-West lag count** follows the horizon (``metrics.nw_lags``) and is
  reported as ``nw_lags``.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from iap.backtest.engine import Backtester
from iap.validation.leakage import LeakageTester
from iap.validation.metrics import (
    HORIZONS_NS,
    bucket_ics,
    capacity_proxy_usd,
    decay_curve,
    hit_rate,
    ic,
    newey_west_tstat,
    nw_lags,
    rank_ic,
    signal_turnover,
)
from iap.validation.splits import (
    MIN_NONDEGENERATE_FOLDS,
    MIN_TEST_PAIRS,
    WalkForwardSplitter,
)
from iap.validation.stress import (
    cost_stress,
    latency_stress,
    latency_stress_time,
    regime_split,
)

GATES = {
    "min_oos_ic": 0.010,
    "min_nw_tstat": 3.0,
    "min_fold_sign_consistency": 0.70,
    "min_nondegenerate_folds": MIN_NONDEGENERATE_FOLDS,
    "min_test_pairs": MIN_TEST_PAIRS,
    "iterate_min_ic": 0.005,
    "iterate_min_tstat": 1.5,
}


def _pooled_arrays(scores, frames, horizon):
    """(ts, expected_return, label, crossed) pooled over the universe.

    ``crossed`` marks rows whose consolidated book was crossed
    (``spread_ticks_v1 < 0``, i.e. at least one venue quote was stale).
    """
    ts, xs, ys, cs = [], [], [], []
    for iid, sc in scores.items():
        df = frames[iid]
        er = sc["expected_return"].to_numpy(dtype=float).copy()
        er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
        lab = df[f"label_mid_{horizon}"].to_numpy(dtype=float).copy()
        lab[~df[f"label_valid_{horizon}"].to_numpy(dtype=bool)] = np.nan
        if "spread_ticks_v1" in df.columns:
            sp = df["spread_ticks_v1"].to_numpy(dtype=float)
            crossed = np.isfinite(sp) & (sp < 0.0)
        else:
            crossed = np.zeros(len(df), dtype=bool)
        ts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        xs.append(er)
        ys.append(lab)
        cs.append(crossed)
    if not xs:
        return (np.empty(0, np.int64), np.empty(0), np.empty(0),
                np.empty(0, bool))
    return (np.concatenate(ts), np.concatenate(xs), np.concatenate(ys),
            np.concatenate(cs))


def _fnum(v: float) -> Optional[float]:
    return float(v) if np.isfinite(v) else None


def validate_alpha(
    model_factory,
    frames: Mapping[int, pd.DataFrame],
    backtester: Backtester,
    capacity_meta: Mapping[int, dict],
    max_participation: float,
    n_folds: int = 4,
    embargo_ns: int = 60_000_000_000,
) -> dict:
    """Full validation of one alpha.  ``model_factory()`` returns a fresh
    unfitted model (a fresh instance per fold — no state bleeds across)."""
    probe = model_factory()
    horizon = probe.horizon
    horizon_ns = HORIZONS_NS[horizon]
    universe = probe.universe(list(frames))
    uframes = {i: frames[i] for i in universe}
    splitter = WalkForwardSplitter(n_folds=n_folds, embargo_ns=embargo_ns)

    fold_rows: List[dict] = []
    pooled_ts: List[np.ndarray] = []
    pooled_x: List[np.ndarray] = []      # standardized signal (z), gate input
    pooled_er: List[np.ndarray] = []     # expected_return (diagnostic)
    pooled_y: List[np.ndarray] = []
    pooled_c: List[np.ndarray] = []
    last_model = None
    last_test = None
    for fold, train, test in splitter.split_frames(uframes, horizon_ns):
        model = model_factory()
        model.fit(train)
        scores = model.score(test)
        ts, er, y, crossed = _pooled_arrays(scores, test, horizon)
        # Pool the standardized signal: folds fit different betas, so pooling
        # expected_return weights each fold by |beta_k| (pinned, round-3).
        beta = float(model.params().get("beta", 0.0) or 0.0)
        z = er / beta if beta != 0.0 else er
        n_pairs = int(np.sum(np.isfinite(er) & np.isfinite(y)))
        degenerate = n_pairs < MIN_TEST_PAIRS
        fold_rows.append(
            {
                "fold": fold.index,
                "n_train": int(model.params().get("n_train", 0)),
                "n_test_rows": int(len(y)),
                "n_test_pairs": n_pairs,
                "degenerate": degenerate,
                "test_start": int(fold.test_start),
                "test_end": int(fold.test_end),
                "ic": _fnum(ic(er, y)),
                "rank_ic": _fnum(rank_ic(er, y)),
                "hit_rate": _fnum(hit_rate(er, y)),
                "beta_fit": model.params().get("beta_fit"),
            }
        )
        pooled_ts.append(ts)
        pooled_x.append(z)
        pooled_er.append(er)
        pooled_y.append(y)
        pooled_c.append(crossed)
        last_model, last_test = model, test

    ts = np.concatenate(pooled_ts)
    x = np.concatenate(pooled_x)
    er_pooled = np.concatenate(pooled_er)
    y = np.concatenate(pooled_y)
    crossed = np.concatenate(pooled_c)
    oos_ic = ic(x, y)
    oos_ic_er = ic(er_pooled, y)
    oos_rank_ic = rank_ic(x, y)
    oos_hit = hit_rate(x, y)
    lags = nw_lags(horizon_ns)
    bics = bucket_ics(ts, x, y)
    nw_t = newey_west_tstat(bics, lags=lags)

    # Crossed-book conditioning (pinned): a crossed consolidated book means a
    # stale venue quote; its mid reverts when that venue refreshes.
    pairs_ok = np.isfinite(x) & np.isfinite(y)
    n_pairs_all = int(pairs_ok.sum())
    crossed_frac = (
        float(np.mean(crossed[pairs_ok])) if n_pairs_all else float("nan")
    )
    unc = ~crossed
    oos_ic_uncrossed = ic(np.where(unc, x, np.nan), np.where(unc, y, np.nan))
    oos_ic_crossed = ic(np.where(crossed, x, np.nan),
                        np.where(crossed, y, np.nan))
    bics_unc = bucket_ics(ts[unc], x[unc], y[unc])
    nw_t_uncrossed = newey_west_tstat(bics_unc, lags=lags)

    # Degenerate folds count as FAILED folds, never as missing data.
    n_folds_run = len(fold_rows)
    n_nondegenerate = sum(1 for r in fold_rows if not r["degenerate"])
    positive = sum(
        1 for r in fold_rows
        if (not r["degenerate"]) and r["ic"] is not None and r["ic"] > 0
    )
    sign_consistency = (
        float(positive / n_folds_run) if n_folds_run else float("nan")
    )

    # leakage + decay + turnover on the last (largest-train) fold
    leak = LeakageTester().run(last_model, last_test).to_dict()
    scores_last = last_model.score(last_test)
    decay: Dict[str, Optional[float]] = {}
    turnover_vals = []
    for iid, sc in scores_last.items():
        d = decay_curve(sc["expected_return"].to_numpy(), last_test[iid])
        for h, v in d.items():
            decay.setdefault(h, [])
            if np.isfinite(v):
                decay[h].append(v)
        tv = signal_turnover(
            last_test[iid]["exchange_ts"].to_numpy(),
            sc["expected_return"].to_numpy(),
            sc["confidence"].to_numpy(),
        )
        if np.isfinite(tv):
            turnover_vals.append(tv)
    decay_out = {
        h: (_fnum(float(np.mean(v))) if v else None) for h, v in decay.items()
    }
    turnover = float(np.mean(turnover_vals)) if turnover_vals else float("nan")

    capacity = {
        str(iid): capacity_proxy_usd(
            float(capacity_meta[iid]["adv"]),
            float(capacity_meta[iid]["ref_price"]),
            max_participation,
            float(capacity_meta[iid].get("lot_value_multiplier", 1.0)),
        )
        for iid in universe
    }

    # stress (fit on all-but-last-segment model, applied to its test set)
    asset_class = "FX" if probe.asset_class == "FX" else "EQUITY"
    stress = {
        "cost": cost_stress(backtester, last_test, scores_last, asset_class),
        "latency": latency_stress(
            backtester, last_test, scores_last, asset_class, horizon
        ),
        "latency_time": latency_stress_time(
            backtester, last_test, scores_last, asset_class, horizon
        ),
        "regime": {
            k: _fnum(v)
            for k, v in regime_split(scores_last, last_test, horizon).items()
        },
    }
    net_pnl_1x = stress["cost"]["x1"]["total_pnl"]

    hypothesis_confirmed = bool(last_model.params().get("hypothesis_confirmed", False))
    # The PROMOTE gate reads the UNCROSSED IC: a crossed consolidated book is
    # a stale-quote artefact, not a tradable state (pinned, round-3).
    gate_ic = oos_ic_uncrossed if np.isfinite(oos_ic_uncrossed) else oos_ic
    gate_t = nw_t_uncrossed if np.isfinite(nw_t_uncrossed) else nw_t
    promote = (
        leak["passed"]
        and np.isfinite(gate_ic)
        and gate_ic >= GATES["min_oos_ic"]
        and np.isfinite(gate_t)
        and gate_t >= GATES["min_nw_tstat"]
        and np.isfinite(sign_consistency)
        and sign_consistency >= GATES["min_fold_sign_consistency"]
        and n_nondegenerate >= GATES["min_nondegenerate_folds"]
        and hypothesis_confirmed
        and net_pnl_1x > 0.0
    )
    iterate = (
        not promote
        and leak["passed"]
        and np.isfinite(gate_ic)
        and gate_ic >= GATES["iterate_min_ic"]
        and np.isfinite(gate_t)
        and gate_t >= GATES["iterate_min_tstat"]
    )
    verdict = "PROMOTE" if promote else ("ITERATE" if iterate else "REJECT")

    return {
        "alpha_id": probe.alpha_id,
        "name": probe.name,
        "asset_class": probe.asset_class,
        "horizon": horizon,
        "universe": universe,
        "gates": GATES,
        "folds": fold_rows,
        "n_folds_run": n_folds_run,
        "n_nondegenerate_folds": n_nondegenerate,
        "n_degenerate_folds": n_folds_run - n_nondegenerate,
        "oos_ic": _fnum(oos_ic),
        "oos_ic_pooled_er": _fnum(oos_ic_er),
        "oos_ic_uncrossed": _fnum(oos_ic_uncrossed),
        "oos_ic_crossed": _fnum(oos_ic_crossed),
        "crossed_frac": _fnum(crossed_frac),
        "gate_ic": _fnum(gate_ic),
        "oos_rank_ic": _fnum(oos_rank_ic),
        "oos_hit_rate": _fnum(oos_hit),
        "nw_tstat": _fnum(nw_t),
        "nw_tstat_uncrossed": _fnum(nw_t_uncrossed),
        "nw_lags": int(lags),
        "n_ic_buckets": int(bics.size),
        "n_ic_buckets_uncrossed": int(bics_unc.size),
        "fold_sign_consistency": _fnum(sign_consistency),
        "hypothesis_confirmed": hypothesis_confirmed,
        "leakage": leak,
        "decay_ic_by_horizon": decay_out,
        "turnover_flips_per_hour": _fnum(turnover),
        "capacity_usd_by_instrument": capacity,
        "stress": stress,
        "net_pnl_1x_cost": _fnum(net_pnl_1x),
        "verdict": verdict,
    }

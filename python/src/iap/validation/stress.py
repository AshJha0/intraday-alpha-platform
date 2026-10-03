"""Stress module (spec §13: "stress parameters, costs, latency and regimes").

Three pinned stress axes for every alpha:

- **Costs**: full backtest at cost multipliers {0.5, 1.0, 2.0} (grid pinned
  in configs/execution/execution.json ``cost_model.cost_multipliers_stress``).
- **Latency**: two pinned grids.  The ROW grid shifts the decision by
  {0, 1, 5} emission events (kept for continuity with earlier reports); the
  TIME grid delays execution by {100 ms, 500 ms, 1 s, 5 s} of event time
  (``LATENCY_TIMES_NS``).  Only the time grid is comparable across
  instruments: one emission row is ~3.3 s on equities and ~15 s on FX in
  this dataset, so "+1 event" means two very different latencies and a
  report that quotes it is not describing a latency budget at all.
- **Regimes**: IC split by the volatility-regime flag
  (``vol_regime_flag_v1``: 1 = short-horizon vol elevated) — a promotable
  alpha should not owe its entire IC to one regime.

**Stress version (pinned).**  Every stressed backtest must differ from the
base backtest in the stressed parameter ONLY.  The cost and time-latency
grids do (``dataclasses.replace`` on the base config, the base reporting
currency carried).  The ROW latency grid historically did not: it rebuilt
the config from four fields and silently dropped ``max_decision_age_ns``,
``flatten_at_session_end`` and ``session_gap_ns``, so its P&L column
described a strategy that carries positions overnight and fills stale
decisions even when the base strategy does neither.

- ``version=1`` (:data:`STRESS_VERSION_LEGACY`, the default) reproduces that
  historic row grid exactly.  It is the default only because the committed
  reports under ``research/alpha_reports`` were produced with it and are
  pinned; it is a known defect, kept reproducible.
- ``version=2`` (:data:`STRESS_VERSION_CARRY`) derives the row-grid config
  with ``dataclasses.replace(base, latency_rows=base.latency_rows + k,
  latency_ns=None)`` — every other field (decision age, session flattening,
  session gap, position policy, ...) is the base strategy's.  ``latency_ns``
  is cleared on purpose: the grid is DEFINED in rows, and a set
  ``latency_ns`` overrides ``latency_rows`` in the engine, which would make
  every row of the grid the same backtest.  New work (the power study,
  any regenerated report) should pass ``version=2``.

A non-finite row-grid IC is reported as ``None`` (JSON ``null``), never as a
raw NaN, like every other statistic in a validation report.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from iap.backtest.engine import Backtester, BacktestConfig
from iap.validation.metrics import ic

COST_MULTIPLIERS = (0.5, 1.0, 2.0)
LATENCY_SHIFTS = (0, 1, 5)
#: Pinned TIME latency grid (ns): 100 ms, 500 ms, 1 s, 5 s.  Zero is not a
#: grid point: a fill in the same instant as the decision is not a latency
#: budget, and it would charge the first row of a frame before any
#: conversion rate prevails (the currency layer fails closed there).
LATENCY_TIMES_NS = (100_000_000, 500_000_000, 1_000_000_000, 5_000_000_000)

#: Row-latency grid as historically computed (drops decision age / session
#: flattening / session gap from the stressed config) — see module docs.
STRESS_VERSION_LEGACY = 1
#: Row-latency grid that carries every base-config field.
STRESS_VERSION_CARRY = 2
STRESS_VERSIONS = (STRESS_VERSION_LEGACY, STRESS_VERSION_CARRY)


def _fnum(v: float) -> Optional[float]:
    return float(v) if np.isfinite(v) else None


def _pooled(
    scores: Mapping[int, pd.DataFrame],
    frames: Mapping[int, pd.DataFrame],
    horizon: str,
    lag_events: int = 0,
    row_mask=None,
    beta: float = 0.0,
):
    """Pooled (score, label) arrays.

    ``beta`` is the fitted coefficient of the model that produced ``scores``.
    When it is nonzero the score is divided by it, i.e. the STANDARDIZED
    signal ``z`` is scored — exactly what the gate IC in
    :mod:`iap.validation.validate` uses.  Correlation is scale-invariant but
    not sign-invariant, so scoring the raw ``expected_return`` of a
    free-signed fit reports ``+IC`` for a model the gate measures at ``-IC``:
    EQ09 published ``ic_high_vol = +0.1441`` next to ``gate_ic = -0.0650``.
    """
    xs, ys = [], []
    for iid, sc in scores.items():
        df = frames[iid]
        er = sc["expected_return"].to_numpy(dtype=float).copy()
        er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
        if beta != 0.0:
            er = er / beta
        lab = df[f"label_mid_{horizon}"].to_numpy(dtype=float).copy()
        lab[~df[f"label_valid_{horizon}"].to_numpy(dtype=bool)] = np.nan
        if row_mask is not None:
            lab = lab.copy()
            lab[~row_mask(df)] = np.nan
        if lag_events > 0:
            if len(er) <= lag_events:
                continue
            er = er[:-lag_events]
            lab = lab[lag_events:]
        xs.append(er)
        ys.append(lab)
    if not xs:
        return np.empty(0), np.empty(0)
    return np.concatenate(xs), np.concatenate(ys)


def cost_stress(
    backtester_base: Backtester,
    frames: Mapping[int, pd.DataFrame],
    scores: Mapping[int, pd.DataFrame],
    asset_class: str,
    multipliers: Sequence[float] = COST_MULTIPLIERS,
) -> Dict[str, dict]:
    """Net backtest results across the pinned cost-multiplier grid."""
    out: Dict[str, dict] = {}
    for m in multipliers:
        bt = Backtester(
            backtester_base.cost_model.with_multiplier(m),
            backtester_base.meta,
            backtester_base.config,
            reporting_ccy=backtester_base.reporting_ccy,
        )
        res = bt.run(frames, scores, asset_class)
        out[f"x{m:g}"] = {
            "total_pnl": res.total_pnl,
            "total_costs": res.total_costs,
            "trade_count": res.trade_count,
        }
    return out


def latency_stress(
    backtester_base: Backtester,
    frames: Mapping[int, pd.DataFrame],
    scores: Mapping[int, pd.DataFrame],
    asset_class: str,
    horizon: str,
    shifts: Sequence[int] = LATENCY_SHIFTS,
    beta: float = 0.0,
    version: int = STRESS_VERSION_LEGACY,
) -> Dict[str, dict]:
    """IC and net P&L when execution lags the decision by extra events.

    ``beta`` standardizes the score before scoring it (see :func:`_pooled`).
    ``version`` selects how the stressed config is derived from the base
    one (module docs): 1 = the historic four-field rebuild, 2 = the base
    config with only the row latency changed."""
    if version not in STRESS_VERSIONS:
        raise ValueError(f"unknown stress version {version!r}; known: {STRESS_VERSIONS}")
    out: Dict[str, dict] = {}
    base = backtester_base.config
    base_latency = base.latency_rows
    for k in shifts:
        x, y = _pooled(scores, frames, horizon, lag_events=k, beta=beta)
        if version == STRESS_VERSION_LEGACY:
            cfg = BacktestConfig(
                max_pos_qty=base.max_pos_qty,
                conf_min=base.conf_min,
                latency_rows=base_latency + k,
                bar_ns=base.bar_ns,
            )
        else:
            cfg = replace(base, latency_rows=base_latency + k, latency_ns=None)
        bt = Backtester(
            backtester_base.cost_model,
            backtester_base.meta,
            cfg,
            reporting_ccy=backtester_base.reporting_ccy,
        )
        res = bt.run(frames, scores, asset_class)
        out[f"+{k}ev"] = {
            "ic": _fnum(ic(x, y)),
            "total_pnl": res.total_pnl,
            "latency_rows": cfg.latency_rows,
        }
    return out


def latency_stress_time(
    backtester_base: Backtester,
    frames: Mapping[int, pd.DataFrame],
    scores: Mapping[int, pd.DataFrame],
    asset_class: str,
    horizon: str,
    latencies_ns: Sequence[int] = LATENCY_TIMES_NS,
) -> Dict[str, dict]:
    """Net P&L when execution is delayed by a real amount of EVENT TIME.

    Unlike the row grid this is comparable across instruments and datasets:
    a decision at t executes at the first row with
    ``exchange_ts >= t + latency_ns`` (API_ALPHA / backtester §latency).
    """
    out: Dict[str, dict] = {}
    base = backtester_base.config
    for ns in latencies_ns:
        cfg = replace(base, latency_ns=int(ns))
        bt = Backtester(
            backtester_base.cost_model,
            backtester_base.meta,
            cfg,
            reporting_ccy=backtester_base.reporting_ccy,
        )
        res = bt.run(frames, scores, asset_class)
        label = (
            "0ms"
            if ns == 0
            else (f"{ns // 1_000_000}ms" if ns < 1_000_000_000 else f"{ns // 1_000_000_000}s")
        )
        out[label] = {
            "latency_ns": int(ns),
            "total_pnl": res.total_pnl,
            "trade_count": res.trade_count,
        }
    return out


def regime_split(
    scores: Mapping[int, pd.DataFrame],
    frames: Mapping[int, pd.DataFrame],
    horizon: str,
    beta: float = 0.0,
) -> Dict[str, float]:
    """IC in high-vol vs low-vol regimes (vol_regime_flag_v1).

    ``beta`` standardizes the score before scoring it (see :func:`_pooled`):
    a regime IC computed on the raw ``expected_return`` of a free-signed fit
    carries the opposite sign to the gate IC and reads as a regime the alpha
    "works in" when it is the regime it is most wrong in.
    """

    def high(df: pd.DataFrame) -> np.ndarray:
        f = df["vol_regime_flag_v1"].to_numpy(dtype=float)
        return np.where(np.isfinite(f), f, 0.0) > 0.5

    def low(df: pd.DataFrame) -> np.ndarray:
        f = df["vol_regime_flag_v1"].to_numpy(dtype=float)
        return np.isfinite(f) & (f < 0.5)

    xh, yh = _pooled(scores, frames, horizon, row_mask=high, beta=beta)
    xl, yl = _pooled(scores, frames, horizon, row_mask=low, beta=beta)
    return {"ic_high_vol": ic(xh, yh), "ic_low_vol": ic(xl, yl)}

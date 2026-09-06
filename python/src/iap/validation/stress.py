"""Stress module (spec §13: "stress parameters, costs, latency and regimes").

Three pinned stress axes for every alpha:

- **Costs**: full backtest at cost multipliers {0.5, 1.0, 2.0} (grid pinned
  in configs/execution.json ``cost_model.cost_multipliers_stress``).
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
"""

from __future__ import annotations

from typing import Dict, Mapping, Sequence

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


def _pooled(
    scores: Mapping[int, pd.DataFrame],
    frames: Mapping[int, pd.DataFrame],
    horizon: str,
    lag_events: int = 0,
    row_mask=None,
):
    xs, ys = [], []
    for iid, sc in scores.items():
        df = frames[iid]
        er = sc["expected_return"].to_numpy(dtype=float).copy()
        er[sc["confidence"].to_numpy(dtype=float) <= 0.0] = np.nan
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
) -> Dict[str, dict]:
    """IC and net P&L when execution lags the decision by extra events."""
    out: Dict[str, dict] = {}
    base_latency = backtester_base.config.latency_rows
    for k in shifts:
        x, y = _pooled(scores, frames, horizon, lag_events=k)
        cfg = BacktestConfig(
            max_pos_qty=backtester_base.config.max_pos_qty,
            conf_min=backtester_base.config.conf_min,
            latency_rows=base_latency + k,
            bar_ns=backtester_base.config.bar_ns,
        )
        bt = Backtester(backtester_base.cost_model, backtester_base.meta, cfg)
        res = bt.run(frames, scores, asset_class)
        out[f"+{k}ev"] = {
            "ic": ic(x, y),
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
        cfg = BacktestConfig(
            max_pos_qty=base.max_pos_qty,
            conf_min=base.conf_min,
            bar_ns=base.bar_ns,
            latency_ns=int(ns),
            max_decision_age_ns=base.max_decision_age_ns,
            flatten_at_session_end=base.flatten_at_session_end,
            session_gap_ns=base.session_gap_ns,
        )
        bt = Backtester(backtester_base.cost_model, backtester_base.meta, cfg,
                        reporting_ccy=backtester_base.reporting_ccy)
        res = bt.run(frames, scores, asset_class)
        label = "0ms" if ns == 0 else (
            f"{ns // 1_000_000}ms" if ns < 1_000_000_000
            else f"{ns // 1_000_000_000}s")
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
) -> Dict[str, float]:
    """IC in high-vol vs low-vol regimes (vol_regime_flag_v1)."""
    def high(df: pd.DataFrame) -> np.ndarray:
        f = df["vol_regime_flag_v1"].to_numpy(dtype=float)
        return np.where(np.isfinite(f), f, 0.0) > 0.5

    def low(df: pd.DataFrame) -> np.ndarray:
        f = df["vol_regime_flag_v1"].to_numpy(dtype=float)
        return np.isfinite(f) & (f < 0.5)

    xh, yh = _pooled(scores, frames, horizon, row_mask=high)
    xl, yl = _pooled(scores, frames, horizon, row_mask=low)
    return {"ic_high_vol": ic(xh, yh), "ic_low_vol": ic(xl, yl)}

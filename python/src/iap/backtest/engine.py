"""Fast vectorized research backtester (spec §18, research engine).

Pinned semantics (mirrored by the accounting-identity tests):

- **Decision at t, execution at t + latency**: the signal at row ``i``
  produces a target position; the trade toward that target executes at row
  ``i + latency_rows`` (default 1 — never the decision row), at that row's
  ``mid ± half_spread`` (sign of the trade), plus fees and linear impact
  (:mod:`iap.backtest.costs`).  Rows whose mid/half-spread are invalid
  cannot execute; the previous position carries (the stale target is NOT
  queued — a real router would re-evaluate).
- **Position rule** (pinned, deliberately simple): target =
  ``sign(expected_return) * max_pos_qty`` when ``confidence >= conf_min``,
  else flat.  No pyramiding, no hysteresis — a research backtester
  measures signal economics, not execution tuning.
- **Accounting identity** (tested exactly): with cash updated only by
  executions and equity marked at the last valid mid,

      equity_end = sum_i pos_i * (mark_{i+1} - mark_i) - total_costs

  i.e. cash + inventory mark-to-market equals price-move P&L minus costs.
  Open terminal positions stay marked at the final mid (no forced
  liquidation; the cost of closing is visible in the stress section where
  it matters).

Annualized Sharpe (documented intraday scaling): per-row equity changes are
aggregated into fixed 1-minute event-time bars; Sharpe =
``mean(bar_pnl) / std(bar_pnl) * sqrt(bars_per_year)`` with
``bars_per_year = 252 * session_hours * 60`` (session_hours pinned: 6.5
EQUITY, 21 FX).  This assumes independent bar P&L — reported as the
standard research-scaling caveat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

import numpy as np
import pandas as pd

from iap.backtest.costs import CostModel

NS_S = 1_000_000_000
BAR_NS = 60 * NS_S
SESSION_HOURS = {"EQUITY": 6.5, "ETF": 6.5, "FX": 21.0}
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class BacktestConfig:
    max_pos_qty: int = 1000       # matches execution defaults max_child_qty
    conf_min: float = 0.5
    latency_rows: int = 1         # decision t executes t+latency (>= 1)
    bar_ns: int = BAR_NS

    def __post_init__(self) -> None:
        if self.latency_rows < 0:
            raise ValueError("latency_rows must be >= 0")
        if self.max_pos_qty <= 0:
            raise ValueError("max_pos_qty must be positive")


@dataclass
class InstrumentResult:
    instrument_id: int
    total_pnl: float
    gross_pnl: float              # price-move P&L before costs
    total_costs: float
    spread_cost: float
    fee_cost: float
    impact_cost: float
    trade_count: int
    traded_qty: int
    n_rows: int
    equity: np.ndarray = field(repr=False)
    positions: np.ndarray = field(repr=False)
    bar_ts: np.ndarray = field(repr=False)
    bar_pnl: np.ndarray = field(repr=False)


@dataclass
class BacktestResult:
    per_instrument: Dict[int, InstrumentResult]
    asset_class: str

    @property
    def total_pnl(self) -> float:
        return float(sum(r.total_pnl for r in self.per_instrument.values()))

    @property
    def total_costs(self) -> float:
        return float(sum(r.total_costs for r in self.per_instrument.values()))

    @property
    def gross_pnl(self) -> float:
        return float(sum(r.gross_pnl for r in self.per_instrument.values()))

    @property
    def trade_count(self) -> int:
        return int(sum(r.trade_count for r in self.per_instrument.values()))

    def metrics(self, capital: float) -> dict:
        """Aggregate metrics on the pooled 1-minute bar P&L series."""
        bars: Dict[int, float] = {}
        for r in self.per_instrument.values():
            for t, p in zip(r.bar_ts, r.bar_pnl):
                bars[int(t)] = bars.get(int(t), 0.0) + float(p)
        ts = np.array(sorted(bars), dtype=np.int64)
        pnl = np.array([bars[int(t)] for t in ts])
        hours = SESSION_HOURS[self.asset_class]
        bars_per_year = TRADING_DAYS_PER_YEAR * hours * 60.0
        if pnl.size >= 8 and pnl.std() > 0:
            sharpe = float(pnl.mean() / pnl.std() * np.sqrt(bars_per_year))
        else:
            sharpe = float("nan")
        equity = np.cumsum(pnl)
        peak = np.maximum.accumulate(equity)
        maxdd = float(np.max(peak - equity)) if equity.size else 0.0
        wins = pnl[pnl != 0.0]
        days = max(len(np.unique(ts // (86_400 * NS_S))), 1)
        traded = sum(r.traded_qty for r in self.per_instrument.values())
        return {
            "total_pnl": self.total_pnl,
            "gross_pnl": self.gross_pnl,
            "total_costs": self.total_costs,
            "cost_drag_frac": (
                float(self.total_costs / abs(self.gross_pnl))
                if abs(self.gross_pnl) > 0
                else float("nan")
            ),
            "sharpe_ann": sharpe,
            "sharpe_scaling": (
                f"1m bars, sqrt({TRADING_DAYS_PER_YEAR}*{hours}h*60) bars/yr"
            ),
            "hit_rate_bars": (
                float(np.mean(wins > 0)) if wins.size >= 8 else float("nan")
            ),
            "max_drawdown": maxdd,
            "max_drawdown_frac_capital": (
                maxdd / capital if capital > 0 else float("nan")
            ),
            "trade_count": self.trade_count,
            "turnover_qty_per_day": traded / days,
            "n_bars": int(pnl.size),
        }


def _ffill(values: np.ndarray, initial: float) -> np.ndarray:
    """Vectorized forward-fill with an initial value (NaN initial allowed:
    leading gaps then stay NaN)."""
    v = np.concatenate(([initial], np.asarray(values, dtype=float)))
    idx = np.where(np.isfinite(v), np.arange(len(v)), 0)
    np.maximum.accumulate(idx, out=idx)
    return v[idx][1:]


class Backtester:
    """Vectorized per-instrument research backtester."""

    def __init__(
        self,
        cost_model: CostModel,
        instrument_meta: Mapping[int, dict],
        config: Optional[BacktestConfig] = None,
    ) -> None:
        """``instrument_meta[iid]``: dict with tick_size, lot_size, adv,
        asset_class (built from ReferenceData or configs/instruments.json)."""
        self.cost_model = cost_model
        self.meta = dict(instrument_meta)
        self.config = config or BacktestConfig()

    def run_instrument(
        self, iid: int, frame: pd.DataFrame, scores: pd.DataFrame
    ) -> InstrumentResult:
        if len(frame) != len(scores):
            raise ValueError("frame/scores row mismatch")
        meta = self.meta[iid]
        cfg = self.config
        # real value per (qty unit x price unit): FX qty unit = lot_size
        # base ccy (conventions §1); equity qty already in shares
        unit = float(meta["lot_size"]) if meta["asset_class"] == "FX" else 1.0
        n = len(frame)
        ts = frame["exchange_ts"].to_numpy(dtype=np.int64)
        mid = frame["mid_price_v1"].to_numpy(dtype=float)
        hs = (
            frame["spread_ticks_v1"].to_numpy(dtype=float)
            * float(meta["tick_size"])
            / 2.0
        )
        er = scores["expected_return"].to_numpy(dtype=float)
        conf = scores["confidence"].to_numpy(dtype=float)

        # decision at i -> desired target at execution row i + latency
        target = np.where(conf >= cfg.conf_min, np.sign(er), 0.0) * cfg.max_pos_qty
        target[~np.isfinite(er)] = 0.0
        exec_target = np.full(n, np.nan)
        if cfg.latency_rows == 0:
            exec_target[:] = target
        elif cfg.latency_rows < n:
            exec_target[cfg.latency_rows :] = target[: n - cfg.latency_rows]

        executable = np.isfinite(mid) & np.isfinite(hs) & (hs >= 0.0)
        exec_target[~executable] = np.nan  # cannot trade here; carry position
        pos = _ffill(exec_target, 0.0)
        trades = np.diff(pos, prepend=0.0)
        trade_rows = trades != 0.0

        costs = np.zeros(n)
        spread_total = fee_total = impact_total = 0.0
        if trade_rows.any():
            comp = self.cost_model.cost_components(
                trades[trade_rows],
                mid[trade_rows],
                hs[trade_rows],
                meta["asset_class"],
                float(meta["adv"]),
                int(meta["lot_size"]),
            )
            costs[trade_rows] = comp["spread"] + comp["fee"] + comp["impact"]
            spread_total = float(comp["spread"].sum())
            fee_total = float(comp["fee"].sum())
            impact_total = float(comp["impact"].sum())

        # cash accounting: executions move cash at the mid; the spread paid
        # (|q| * hs, the taker's mid-to-touch slippage) is charged through
        # the explicit cost components together with fees and impact, so the
        # stress multiplier scales ALL cost terms uniformly and nothing is
        # double-counted.  Effective buy price = mid + multiplier*hs + ...
        cash_flow = -trades * unit * np.where(trade_rows, mid, 0.0) - costs
        cash = np.cumsum(cash_flow)
        mark = _ffill(mid, np.nan)
        if not np.isfinite(mark).all():
            first = np.flatnonzero(np.isfinite(mid))
            fill_val = mid[first[0]] if first.size else 0.0
            mark = np.where(np.isfinite(mark), mark, fill_val)
        equity = cash + pos * unit * mark

        gross = float(np.sum(pos[:-1] * unit * np.diff(mark))) if n > 1 else 0.0
        total_costs = float(costs.sum())

        # 1-minute bar P&L for Sharpe/drawdown aggregation
        bar_ids = ts // cfg.bar_ns
        dEq = np.diff(equity, prepend=0.0)
        uniq, inv = np.unique(bar_ids, return_inverse=True)
        bar_pnl = np.zeros(len(uniq))
        np.add.at(bar_pnl, inv, dEq)
        bar_ts = uniq * cfg.bar_ns

        return InstrumentResult(
            instrument_id=iid,
            total_pnl=float(equity[-1]) if n else 0.0,
            gross_pnl=gross,
            total_costs=total_costs,
            spread_cost=spread_total,
            fee_cost=fee_total,
            impact_cost=impact_total,
            trade_count=int(trade_rows.sum()),
            traded_qty=int(np.abs(trades).sum()),
            n_rows=n,
            equity=equity,
            positions=pos,
            bar_ts=bar_ts,
            bar_pnl=bar_pnl,
        )

    def run(
        self,
        frames: Mapping[int, pd.DataFrame],
        scores: Mapping[int, pd.DataFrame],
        asset_class: str,
    ) -> BacktestResult:
        per: Dict[int, InstrumentResult] = {}
        for iid in sorted(scores):
            if iid not in frames:
                raise ValueError(f"scores for unknown instrument {iid}")
            per[iid] = self.run_instrument(iid, frames[iid], scores[iid])
        return BacktestResult(per_instrument=per, asset_class=asset_class)


def ensemble_scores(
    all_scores: Mapping[str, Mapping[int, pd.DataFrame]],
    betas: Mapping[str, float],
) -> Dict[int, pd.DataFrame]:
    """Equal-weight ensemble across alphas (per asset class).

    Each alpha contributes its z-score (expected_return / beta when beta is
    nonzero — undoing the return scaling so alphas with different fitted
    magnitudes weigh equally); the ensemble expected_return is re-scaled by
    the mean |beta| so units remain a return, and confidence is the mean of
    the member confidences.  All member score frames are row-aligned by
    construction (same input frames).
    """
    members = [a for a in sorted(all_scores) if abs(betas.get(a, 0.0)) > 0]
    if not members:
        raise ValueError("no alphas with nonzero beta to ensemble")
    iids = sorted(set.intersection(*(set(all_scores[a]) for a in members)))
    mean_abs_beta = float(np.mean([abs(betas[a]) for a in members]))
    out: Dict[int, pd.DataFrame] = {}
    for iid in iids:
        zsum = None
        csum = None
        for a in members:
            sc = all_scores[a][iid]
            z = sc["expected_return"].to_numpy(dtype=float) / betas[a]
            c = sc["confidence"].to_numpy(dtype=float)
            zsum = z if zsum is None else zsum + z
            csum = c if csum is None else csum + c
        z = zsum / len(members)
        conf = csum / len(members)
        base = all_scores[members[0]][iid]
        out[iid] = pd.DataFrame(
            {
                "exchange_ts": base["exchange_ts"].to_numpy(),
                "expected_return": mean_abs_beta * z,
                "confidence": conf,
            }
        )
    return out

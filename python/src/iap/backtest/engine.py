"""Fast vectorized research backtester (spec §18, research engine).

Pinned semantics (mirrored by the accounting-identity tests):

- **Decision at t, execution at t + latency**: the signal at row ``i``
  produces a target position; the trade toward that target executes at
  ``t + latency`` at that row's ``mid ± half_spread`` (sign of the trade),
  plus fees and linear impact (:mod:`iap.backtest.costs`).  Rows whose
  mid/half-spread are invalid cannot execute; the previous position carries
  (the stale target is NOT queued — a real router would re-evaluate).

  Latency has TWO pinned modes (round-3):

  - ``latency_rows`` (default 1) — the historic rows mode, kept as the
    golden regression.  A row is 3.3 s on equities and 15 s on FX in the
    bundled data, so "+1 row" is NOT comparable across instruments.
  - ``latency_ns`` — TIME mode: the decision at ``t`` executes at the first
    row with ``exchange_ts >= t + latency_ns``.  Set it and it overrides
    ``latency_rows``.  This is the mode any latency claim in a report must
    use.

- **Decision age** (pinned): ``max_decision_age_ns`` drops a target whose
  execution row is older than the bound instead of filling it.  Without it a
  16:05 decision "filled" at the 20:00 close print and a 20:00 decision at
  the next day's 13:30 open — the overnight gap credited to a 1-second
  alpha.  ``None`` keeps the unbounded legacy behaviour (goldens only).

- **Session flattening** (pinned): with ``flatten_at_session_end=True`` the
  target is forced to 0 on the last row before any gap larger than
  ``session_gap_ns`` and on the final row of the frame, so no position is
  carried across a session boundary.  A gap in the row stream IS the session
  boundary here: it needs no calendar and works for FX and equities alike.
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

- **Currency** (pinned, API_PORTFOLIO_TCA.md §4): every instrument's cash,
  costs and marks are in its quote currency (JPY for USD/JPY, CAD for
  USD/CAD, ...). Nothing is summed across currencies: each row's equity
  increment is converted into the reporting currency (USD) at the
  prevailing mid of the conversion pair from the SAME frame set (EUR/USD
  mid for EUR, 1 / USD/JPY mid for JPY; no lookahead — the latest pair row
  with ``exchange_ts <= t``), and ``total_pnl`` / ``bar_pnl`` / costs are
  reported in USD; ``total_pnl_native`` keeps the quote-currency figure.
  No FX translation P&L is charged on open inventory (increment-based
  conversion). A quote currency without a conversion pair in the frames is
  an error (fail closed), never silently summed as USD.

Annualized Sharpe (documented intraday scaling): per-row equity changes are
aggregated into fixed 1-minute event-time bars; Sharpe =
``mean(bar_pnl) / std(bar_pnl) * sqrt(bars_per_year)`` with
``bars_per_year = 252 * session_hours * 60`` (session_hours pinned: 6.5
EQUITY, 21 FX).  This assumes independent bar P&L — reported as the
standard research-scaling caveat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

import numpy as np
import pandas as pd

from iap.backtest.costs import CostModel

NS_S = 1_000_000_000
BAR_NS = 60 * NS_S
SESSION_HOURS = {"EQUITY": 6.5, "ETF": 6.5, "FX": 21.0}
#: Bar gap above which the Sharpe series is NOT zero-filled (session break).
SESSION_GAP_NS = 30 * 60 * NS_S
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class BacktestConfig:
    max_pos_qty: int = 1000       # matches execution defaults max_child_qty
    conf_min: float = 0.5
    latency_rows: int = 1         # rows mode: decision t executes t+latency
    bar_ns: int = BAR_NS
    #: TIME-mode latency (ns); overrides latency_rows when set (pinned)
    latency_ns: Optional[int] = None
    #: drop a target whose execution row is older than this (ns)
    max_decision_age_ns: Optional[int] = None
    #: force flat before any row gap larger than session_gap_ns
    flatten_at_session_end: bool = False
    #: row gap that marks a session boundary (pinned default: 30 minutes)
    session_gap_ns: int = 30 * 60 * NS_S

    def __post_init__(self) -> None:
        if self.latency_rows < 0:
            raise ValueError("latency_rows must be >= 0")
        if self.max_pos_qty <= 0:
            raise ValueError("max_pos_qty must be positive")
        if self.latency_ns is not None and self.latency_ns < 0:
            raise ValueError("latency_ns must be >= 0")
        if self.max_decision_age_ns is not None and self.max_decision_age_ns <= 0:
            raise ValueError("max_decision_age_ns must be positive")
        if self.session_gap_ns <= 0:
            raise ValueError("session_gap_ns must be positive")


@dataclass
class InstrumentResult:
    instrument_id: int
    total_pnl: float              # reporting currency (USD)
    gross_pnl: float              # price-move P&L before costs (reporting ccy)
    total_costs: float            # reporting ccy
    spread_cost: float
    fee_cost: float
    impact_cost: float
    trade_count: int
    traded_qty: int
    n_rows: int
    equity: np.ndarray = field(repr=False)      # reporting ccy, cumulative
    positions: np.ndarray = field(repr=False)
    bar_ts: np.ndarray = field(repr=False)
    bar_pnl: np.ndarray = field(repr=False)     # reporting ccy
    quote_currency: str = "USD"
    total_pnl_native: float = 0.0               # quote-currency equity


@dataclass
class BacktestResult:
    per_instrument: Dict[int, InstrumentResult]
    asset_class: str

    @property
    def total_pnl(self) -> float:
        """Total P&L in the reporting currency (converted per row)."""
        return float(sum(r.total_pnl for r in self.per_instrument.values()))

    @property
    def total_pnl_native_by_ccy(self) -> Dict[str, float]:
        """Unconverted P&L per quote currency (never summed across)."""
        out: Dict[str, float] = {}
        for r in self.per_instrument.values():
            out[r.quote_currency] = out.get(r.quote_currency, 0.0) + r.total_pnl_native
        return out

    @property
    def total_costs(self) -> float:
        return float(sum(r.total_costs for r in self.per_instrument.values()))

    @property
    def gross_pnl(self) -> float:
        return float(sum(r.gross_pnl for r in self.per_instrument.values()))

    @property
    def trade_count(self) -> int:
        return int(sum(r.trade_count for r in self.per_instrument.values()))

    def metrics(self, capital: float, bar_ns: int = BAR_NS) -> dict:
        """Aggregate metrics on the pooled 1-minute bar P&L series.

        Quiet bars are ZERO-FILLED within each contiguous session span
        (pinned, round-3): a minute in which nothing traded is a real bar
        with P&L 0.  Dropping it inflates std(bar_pnl) — the FX book has
        ~1 260 minutes per day but only ~600 bars with rows — and biases the
        annualized Sharpe.  A gap longer than ``SESSION_GAP_NS`` is treated
        as a session boundary and is NOT filled (no overnight zero bars).
        """
        bars: Dict[int, float] = {}
        for r in self.per_instrument.values():
            for t, p in zip(r.bar_ts, r.bar_pnl):
                bars[int(t)] = bars.get(int(t), 0.0) + float(p)
        present = np.array(sorted(bars), dtype=np.int64)
        if present.size:
            filled: List[int] = []
            for i, b in enumerate(present):
                filled.append(int(b))
                if i + 1 < present.size:
                    nxt = int(present[i + 1])
                    if nxt - int(b) <= SESSION_GAP_NS:
                        filled.extend(range(int(b) + bar_ns, nxt, bar_ns))
            ts = np.array(sorted(filled), dtype=np.int64)
            pnl = np.array([bars.get(int(t), 0.0) for t in ts])
        else:
            ts = present
            pnl = np.empty(0)
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
            "n_bars_with_rows": int(present.size),
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
        reporting_ccy: str = "USD",
    ) -> None:
        """``instrument_meta[iid]``: dict with tick_size, lot_size, adv,
        asset_class and, for FX, base_currency / quote_currency (built from
        ReferenceData or configs/instruments.json; equities default to the
        reporting currency). The quote->reporting conversion table is derived
        from the FX pairs in the meta (a pair quoted ``X/REPORTING`` converts
        X with its mid, ``REPORTING/X`` converts X with 1 / mid)."""
        self.cost_model = cost_model
        self.meta = dict(instrument_meta)
        self.config = config or BacktestConfig()
        self.reporting_ccy = reporting_ccy
        self.fx_conversion: Dict[str, tuple] = {}
        for pid, m in sorted(self.meta.items()):
            base = m.get("base_currency")
            quote = m.get("quote_currency")
            if base is None or quote is None:
                continue
            if quote == reporting_ccy and base not in self.fx_conversion:
                self.fx_conversion[base] = (pid, False)
            elif base == reporting_ccy and quote not in self.fx_conversion:
                self.fx_conversion[quote] = (pid, True)

    def quote_currency(self, iid: int) -> str:
        """Quote currency of an instrument (reporting ccy when unspecified)."""
        m = self.meta[iid]
        return str(m.get("quote_currency") or m.get("currency")
                   or self.reporting_ccy)

    def reference_rate(self, ccy: str) -> float:
        """Static quote->reporting rate from the conversion pair's
        ``ref_price`` (capital / sizing arithmetic; fail closed)."""
        if ccy == self.reporting_ccy:
            return 1.0
        if ccy not in self.fx_conversion:
            raise ValueError(f"no conversion pair for {ccy} -> {self.reporting_ccy}")
        pid, invert = self.fx_conversion[ccy]
        ref = float(self.meta[pid]["ref_price"])
        return 1.0 / ref if invert else ref

    def rate_series(
        self, ccy: str, ts: np.ndarray, frames: Mapping[int, pd.DataFrame]
    ) -> np.ndarray:
        """Prevailing quote->reporting rate at each ``ts`` (no lookahead)
        from the conversion pair's ``mid_price_v1`` rows in ``frames``.
        Fails closed when the pair or its mids are missing."""
        if ccy == self.reporting_ccy:
            return np.ones(len(ts))
        if ccy not in self.fx_conversion:
            raise ValueError(f"no conversion pair for {ccy} -> {self.reporting_ccy}")
        pid, invert = self.fx_conversion[ccy]
        if pid not in frames:
            raise ValueError(
                f"conversion pair {pid} for {ccy} is not in the frame set")
        pf = frames[pid]
        pts = pf["exchange_ts"].to_numpy(dtype=np.int64)
        pmid = pf["mid_price_v1"].to_numpy(dtype=float)
        ok = np.isfinite(pmid) & (pmid > 0)
        pts, pmid = pts[ok], pmid[ok]
        if pts.size == 0:
            raise ValueError(f"conversion pair {pid} has no valid mid")
        idx = np.searchsorted(pts, ts, side="right") - 1
        # Rows before the pair's first mid carry NaN: run_instrument fails
        # closed if any P&L increment lands there (no rate is ever invented
        # for real P&L); zero-increment warm-up rows are tolerated.
        mid = np.where(idx >= 0, pmid[np.maximum(idx, 0)], np.nan)
        return 1.0 / mid if invert else mid

    def run_instrument(
        self, iid: int, frame: pd.DataFrame, scores: pd.DataFrame,
        rate: Optional[np.ndarray] = None,
    ) -> InstrumentResult:
        """Run one instrument. ``rate`` is the per-row quote->reporting
        conversion (None = identity, i.e. the instrument is quoted in the
        reporting currency)."""
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

        # decision at i -> desired target at its execution row
        target = np.where(conf >= cfg.conf_min, np.sign(er), 0.0) * cfg.max_pos_qty
        target[~np.isfinite(er)] = 0.0
        exec_target = np.full(n, np.nan)
        if cfg.latency_ns is not None:
            # TIME mode: execute at the first row at-or-after t + latency.
            # Ascending assignment leaves the NEWEST aged-in decision at each
            # execution row (a real router acts on the freshest target).
            j = np.searchsorted(ts, ts + int(cfg.latency_ns), side="left")
            for i in range(n):
                k = int(j[i])
                if k >= n:
                    break
                if (cfg.max_decision_age_ns is not None
                        and ts[k] - ts[i] > cfg.max_decision_age_ns):
                    continue  # decision went stale before a row arrived
                exec_target[k] = target[i]
        elif cfg.latency_rows == 0:
            exec_target[:] = target
        elif cfg.latency_rows < n:
            exec_target[cfg.latency_rows:] = target[: n - cfg.latency_rows]
            if cfg.max_decision_age_ns is not None:
                age = np.full(n, 0, dtype=np.int64)
                age[cfg.latency_rows:] = (
                    ts[cfg.latency_rows:] - ts[: n - cfg.latency_rows])
                exec_target[age > cfg.max_decision_age_ns] = np.nan

        if cfg.flatten_at_session_end and n:
            # Flat before every session boundary (a row gap) and at the end.
            gaps = np.diff(ts, append=ts[-1] + cfg.session_gap_ns + 1)
            exec_target[gaps > cfg.session_gap_ns] = 0.0

        executable = np.isfinite(mid) & np.isfinite(hs) & (hs >= 0.0)
        exec_target[~executable] = np.nan  # cannot trade here; carry position
        pos = _ffill(exec_target, 0.0)
        trades = np.diff(pos, prepend=0.0)
        trade_rows = trades != 0.0

        costs = np.zeros(n)
        comp: Dict[str, np.ndarray] = {}
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
        equity_native = cash + pos * unit * mark

        # currency conversion (pinned): per-row equity increments, costs and
        # price-move P&L converted at the prevailing rate; nothing is summed
        # across currencies
        if rate is None:
            rate = np.ones(n)
        rate = np.asarray(rate, dtype=float)
        if rate.shape != (n,) or np.any(rate[np.isfinite(rate)] <= 0):
            raise ValueError("rate must be a positive per-row series")
        dEq_native = np.diff(equity_native, prepend=0.0)
        missing = ~np.isfinite(rate)
        if np.any(missing & (dEq_native != 0.0)):
            first_bad = int(np.flatnonzero(missing & (dEq_native != 0.0))[0])
            raise ValueError(
                f"instrument {iid}: P&L at row {first_bad} (ts {ts[first_bad]}) "
                f"has no {self.quote_currency(iid)} conversion rate (fail closed)")
        rate = np.where(missing, 1.0, rate)  # zero increments only
        dEq = dEq_native * rate
        equity = np.cumsum(dEq)
        gross = (float(np.sum(pos[:-1] * unit * np.diff(mark) * rate[1:]))
                 if n > 1 else 0.0)
        total_costs = float((costs * rate).sum())
        spread_total = float((comp["spread"] * rate[trade_rows]).sum()) \
            if trade_rows.any() else 0.0
        fee_total = float((comp["fee"] * rate[trade_rows]).sum()) \
            if trade_rows.any() else 0.0
        impact_total = float((comp["impact"] * rate[trade_rows]).sum()) \
            if trade_rows.any() else 0.0

        # 1-minute bar P&L for Sharpe/drawdown aggregation (reporting ccy)
        bar_ids = ts // cfg.bar_ns
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
            quote_currency=self.quote_currency(iid),
            total_pnl_native=float(equity_native[-1]) if n else 0.0,
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
            ccy = self.quote_currency(iid)
            rate = None
            if ccy != self.reporting_ccy:
                rate = self.rate_series(
                    ccy, frames[iid]["exchange_ts"].to_numpy(dtype=np.int64),
                    frames)
            per[iid] = self.run_instrument(iid, frames[iid], scores[iid], rate)
        return BacktestResult(per_instrument=per, asset_class=asset_class)


def ensemble_scores(
    all_scores: Mapping[str, Mapping[int, pd.DataFrame]],
    betas: Mapping[str, float],
    eligible: Optional[Mapping[str, bool]] = None,
) -> Dict[int, pd.DataFrame]:
    """Equal-weight ensemble across alphas (per asset class).

    Each alpha contributes its z-score (expected_return / beta when beta is
    nonzero — undoing the return scaling so alphas with different fitted
    magnitudes weigh equally); the ensemble expected_return is re-scaled by
    the mean |beta| so units remain a return, and confidence is the mean of
    the member confidences.  All member score frames are row-aligned by
    construction (same input frames).

    ``eligible`` (pinned, round-3): only alphas mapped to True enter the
    ensemble.  Reports pass ``verdict != REJECT`` here — an equal-weight
    basket that includes REJECTed and hypothesis-contradicting alphas is not
    a portfolio anyone would run, and reporting its P&L as "the ensemble"
    overstates what the research actually supports.  With ``None`` every
    alpha with a nonzero beta is included and the caller must label the
    table accordingly.
    """
    members = [
        a for a in sorted(all_scores)
        if abs(betas.get(a, 0.0)) > 0
        and (eligible is None or bool(eligible.get(a, False)))
    ]
    if not members:
        raise ValueError("no eligible alphas with nonzero beta to ensemble")
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

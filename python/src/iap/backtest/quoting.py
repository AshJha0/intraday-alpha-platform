"""Signal-skewed two-sided quoting with inventory limits (v1.10 M5).

An Avellaneda-Stoikov style market maker replayed through the FIFO
execution simulator. It is opt-in research code: nothing else changes.

Pinned rules (``QuotingBacktester.run_instrument``):

1. **Replay.** One instrument's ordered event stream goes through
   :class:`ExecutionSimulator` (latency, queue position, fills only from
   observed trades, maker rebate / taker fee per fill from the venue config,
   e.g. ``configs/venues/venues.json``). An optional calibration supplies
   the latency table, the impact coefficient and the adverse-selection
   floor, composed through :class:`iap.backtest.maker.MakerBacktester`.
2. **Decisions.** Each score row (``exchange_ts``, ``expected_return``) is
   a requote that sees every event with ``exchange_ts <= t``. With mid
   ``m``, inventory ``q`` (in units of ``qty``), variance rate ``sigma2``
   (price^2 per second; EWMA of mid changes between decisions, or
   ``sigma`` fixed) and ``tau = tau_s``::

       r = m + alpha_weight * er * m - gamma * sigma2 * q * tau
       h = max(0.5 * gamma * sigma2 * tau + ln(1 + gamma / k) / gamma,
               min_half_spread_ticks * tick,
               (adverse_selection_bps - rebate_bps) * m / 1e4   [as_floor])
       bid = floor((r - h) / tick),  ask = ceil((r + h) / tick)

   then clamped to never cross: ``bid <= best_ask - 1``, ``ask >= best_bid
   + 1`` (join or improve the touch, never take). ``alpha_weight = 0`` is
   the no-skew (gamma only) baseline.
3. **Inventory limit (hard).** A side's quote size is ``min(qty,
   max_inventory -/+ q - working)`` where ``working`` is the remaining size
   of every non-terminal order on that side (including ones whose cancel
   is in flight), so ``|q| <= max_inventory`` holds at every fill.
4. **Refresh.** A side whose desired price or size differs from its
   resting order is cancelled through the latency path and a new order is
   submitted; fills that land before the cancel arrives count.
5. **Flatten.** At the first event at/after ``flatten_ts`` (default: after
   the last event) every working order is swept (``cancel_all``) and the
   inventory crosses at the touch of the pre-event book, paying the taker
   fee and the linear impact rule; without a two-sided book it is marked
   at the last mid (``forced_flatten``).
6. **Accounting.** For fill ``i`` with signed qty ``d_i`` (+ buy), price
   ``p_i`` and pre-event mid ``m_i``, markout mid ``m_i^h`` (mid at
   ``ts_i + markout_ns``, capped at the flatten time) and flatten mid
   ``M``, since ``sum d_i = 0`` after flattening::

       gross     = sum -d_i p_i
                 = spread_captured + markout + inventory_pnl + flatten_cost
       spread_captured = sum_quotes d_i (m_i - p_i)
       markout         = sum_quotes d_i (m_i^h - m_i)     (adverse selection, < 0 bad)
       inventory_pnl   = sum_quotes d_i (M - m_i^h)
       flatten_cost    = d_f (M - p_f)
       net       = gross + rebates - taker_fees - impact

   all times ``qty_unit``; the identity is tested.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from iap.backtest.maker import MakerBacktester, MakerConfig
from iap.core.events import MarketEvent
from iap.execution.calibration import ExecCalibration
from iap.execution.config import ExecConfig
from iap.execution.simulator import ExecutionSimulator
from iap.execution.types import ChildOrder, Liquidity, OrderType


@dataclass(frozen=True)
class QuotingConfig:
    """Quoting parameters (module docstring for the rules)."""

    qty: int = 100
    max_inventory: int = 500
    gamma: float = 0.1
    k: float = 50.0
    tau_s: float = 10.0
    alpha_weight: float = 1.0
    sigma: float | None = None
    sigma_halflife: int = 20
    sigma_init: float = 1e-4
    min_half_spread_ticks: float = 0.5
    as_floor: bool = True
    as_horizon: str = "1s"
    adverse_selection_bps: float | None = None
    markout_ns: int = 1_000_000_000
    flatten_ts: int | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0 or self.max_inventory < self.qty:
            raise ValueError("qty must be positive and max_inventory >= qty")
        if self.gamma <= 0 or self.k <= 0 or self.tau_s < 0:
            raise ValueError("gamma and k must be positive, tau_s >= 0")
        if self.sigma_halflife <= 0 or self.markout_ns <= 0:
            raise ValueError("sigma_halflife and markout_ns must be positive")


PNL_PARTS = ("spread_captured", "markout", "inventory_pnl", "flatten_cost")


@dataclass
class QuotingResult:
    """Outcome of one quoting run on one instrument (one session)."""

    instrument_id: int
    fills: pd.DataFrame = field(repr=False)
    inventory: pd.DataFrame = field(repr=False)
    pnl: dict[str, float]
    counters: dict[str, int]
    as_bps: float
    as_source: str

    @property
    def net_pnl(self) -> float:
        return self.pnl["net"]

    def summary(self) -> dict[str, Any]:
        c = self.counters
        f = self.fills
        quotes = f[~f["flatten"]] if len(f) else f
        inv = self.inventory
        out: dict[str, Any] = {"instrument_id": self.instrument_id, **c, **self.pnl}
        out["as_bps_floor"] = self.as_bps
        out["as_source"] = self.as_source
        for s, name in ((0, "bid"), (1, "ask")):
            posted = c[f"{name}_posted_qty"]
            filled = int(quotes.loc[quotes["side"] == s, "qty"].sum()) if len(quotes) else 0
            out[f"{name}_fill_rate"] = filled / posted if posted else None
        out["n_quote_fills"] = len(quotes)
        out["maker_share"] = float((quotes["liquidity"] == "MAKER").mean()) if len(quotes) else None
        if len(inv) > 1:
            q = inv["q"].to_numpy(dtype=float)
            dt = np.diff(inv["ts"].to_numpy(dtype=np.int64)).astype(float)
            w = dt.sum()
            out["max_abs_inventory"] = int(np.abs(q).max())
            out["mean_abs_inventory"] = float((np.abs(q[:-1]) * dt).sum() / w) if w else 0.0
            out["time_at_limit"] = (
                float((dt * (np.abs(q[:-1]) >= self.counters["max_inventory"])).sum() / w)
                if w
                else 0.0
            )
        else:
            out["max_abs_inventory"] = int(np.abs(inv["q"]).max()) if len(inv) else 0
            out["mean_abs_inventory"] = 0.0
            out["time_at_limit"] = 0.0
        return out


class QuotingBacktester:
    """Event-replay two-sided quoting backtest (module docstring, rules 1-6)."""

    def __init__(
        self,
        exec_config: ExecConfig,
        config: QuotingConfig | None = None,
        calibration: ExecCalibration | None = None,
    ) -> None:
        self.config = config or QuotingConfig()
        # composition: the maker backtester owns calibration handling and
        # the adverse-selection source rule
        self._maker = MakerBacktester(
            exec_config,
            MakerConfig(
                as_horizon=self.config.as_horizon,
                adverse_selection_bps=self.config.adverse_selection_bps,
            ),
            calibration,
        )
        self.exec_config = self._maker.exec_config
        self.calibration = calibration

    def run_instrument(
        self,
        events: Sequence[MarketEvent],
        scores: pd.DataFrame,
        instrument_id: int,
        *,
        venue_id: int | None = None,
    ) -> QuotingResult:
        cfg = self.config
        ins = self.exec_config.instrument(instrument_id)
        evs = [e for e in events if e.instrument_id == instrument_id]
        if not evs:
            raise ValueError(f"no event for instrument {instrument_id}")
        if venue_id is None:
            venue_id = evs[0].venue_id
        venue = self.exec_config.venue(venue_id)
        ts = scores["exchange_ts"].to_numpy(dtype=np.int64)
        if ts.size and np.any(np.diff(ts) < 0):
            raise ValueError("scores must be sorted by exchange_ts")
        er = scores["expected_return"].to_numpy(dtype=float)
        as_bps, as_source = self._maker.adverse_selection(instrument_id)
        tick, unit = ins.tick_size, ins.qty_unit
        flat_ts = cfg.flatten_ts if cfg.flatten_ts is not None else evs[-1].exchange_ts + 1

        sim = ExecutionSimulator(self.exec_config, self.calibration)
        c: dict[str, int] = dict.fromkeys(
            (
                "decisions",
                "no_book",
                "requotes",
                "cancels",
                "bid_posted_qty",
                "ask_posted_qty",
                "bid_blocked_by_limit",
                "ask_blocked_by_limit",
                "forced_flatten",
            ),
            0,
        )
        c["max_inventory"] = cfg.max_inventory
        live: dict[int, int | None] = {0: None, 1: None}  # side -> current order id
        q = [0]
        fills: list[dict[str, Any]] = []
        inv_path: list[tuple[int, int]] = [(evs[0].exchange_ts, 0)]
        mids_ts: list[int] = []
        mids: list[float] = []
        vol = {"mid": None, "t": None, "var": cfg.sigma_init}
        alpha_ewm = 1.0 - 0.5 ** (1.0 / cfg.sigma_halflife)

        def book_state():
            book = sim.venue_book(instrument_id, venue_id)
            if not sim.venue_open(book):
                return None
            bb, ba = book.best_bid(), book.best_ask()
            if bb is None or ba is None or ba[0] <= bb[0]:
                return None
            return bb[0], ba[0]

        def rebate_bps(mid: float) -> float:
            if venue.is_fx:
                return -venue.commission_per_million / 100.0
            return 1e4 * venue.maker_rebate_per_share / mid

        def working(side: int) -> int:
            return sum(
                o.remaining
                for o in sim.orders.values()
                if o.side == side and not o.is_terminal and o.order_id in mine
            )

        mine: set[int] = set()

        def decide(r: int) -> None:
            c["decisions"] += 1
            t = int(ts[r])
            bs = book_state()
            if bs is None:
                c["no_book"] += 1
                return
            bpx, apx = bs
            mid = 0.5 * (bpx + apx) * tick
            if vol["mid"] is not None and t > vol["t"]:
                rate = (mid - vol["mid"]) ** 2 / ((t - vol["t"]) / 1e9)
                vol["var"] += alpha_ewm * (rate - vol["var"])
            vol["mid"], vol["t"] = mid, t
            s2 = cfg.sigma**2 if cfg.sigma is not None else vol["var"]
            e = er[r] if math.isfinite(er[r]) else 0.0
            inv_units = q[0] / cfg.qty
            res = mid + cfg.alpha_weight * e * mid - cfg.gamma * s2 * inv_units * cfg.tau_s
            h = 0.5 * cfg.gamma * s2 * cfg.tau_s + math.log1p(cfg.gamma / cfg.k) / cfg.gamma
            h = max(h, cfg.min_half_spread_ticks * tick)
            if cfg.as_floor:
                h = max(h, (as_bps - rebate_bps(mid)) * mid / 1e4)
            want = {
                0: min(math.floor((res - h) / tick + 1e-9), apx - 1),
                1: max(math.ceil((res + h) / tick - 1e-9), bpx + 1),
            }
            for side in (0, 1):
                oid = live[side]
                cur = None if oid is None or sim.orders[oid].is_terminal else sim.orders[oid]
                other = working(side) - (cur.remaining if cur is not None else 0)
                room = cfg.max_inventory - (q[0] if side == 0 else -q[0]) - other
                size = max(0, min(cfg.qty, room))
                if cur is not None and cur.limit_ticks == want[side] and cur.remaining == size:
                    continue
                if cur is not None:
                    if cur.cancel_arrival_ts == 0:
                        sim.cancel(cur.order_id, t)
                        c["cancels"] += 1
                    # the in-flight order still counts toward working size
                    room -= cur.remaining
                    size = max(0, min(cfg.qty, room))
                live[side] = None
                if size <= 0:
                    c["bid_blocked_by_limit" if side == 0 else "ask_blocked_by_limit"] += 1
                    continue
                new = sim.submit(
                    ChildOrder(
                        instrument_id=instrument_id,
                        venue_id=venue_id,
                        side=side,
                        type=OrderType.LIMIT,
                        qty=size,
                        limit_ticks=want[side],
                        decision_ts=t,
                    )
                )
                mine.add(new)
                live[side] = new
                c["requotes"] += 1
                c["bid_posted_qty" if side == 0 else "ask_posted_qty"] += size

        def record(f, pre_mid: float | None, flatten: bool) -> None:
            d = f.qty if f.side == 0 else -f.qty
            px = f.price_ticks * tick
            fills.append(
                {
                    "ts": f.ts,
                    "side": f.side,
                    "qty": f.qty,
                    "px": px,
                    "mid": pre_mid if pre_mid is not None else px,
                    "liquidity": f.liquidity.name if hasattr(f, "liquidity") else "TAKER",
                    "fee": f.fee,
                    "impact": f.impact_cost,
                    "flatten": flatten,
                }
            )
            q[0] += d
            inv_path.append((f.ts, q[0]))

        seen = 0
        last_mid: float | None = None
        r, n = 0, int(ts.size)
        flattened = False
        flat_mid = None
        for ev in evs:
            t = ev.exchange_ts
            if t >= flat_ts:
                break
            while r < n and ts[r] < t:
                decide(r)
                r += 1
            bs = book_state()
            pre_mid = None if bs is None else 0.5 * (bs[0] + bs[1]) * tick
            if pre_mid is not None:
                last_mid = pre_mid
            sim.on_event(ev)
            post = book_state()
            if post is not None:
                mids_ts.append(t)
                mids.append(0.5 * (post[0] + post[1]) * tick)
            fl = sim.fills
            while seen < len(fl):
                f = fl[seen]
                seen += 1
                if f.order_id in mine:
                    record(f, pre_mid if f.liquidity == Liquidity.MAKER else last_mid, False)
        # ---- flatten (rule 5)
        sim.cancel_all()
        fl = sim.fills
        while seen < len(fl):
            f = fl[seen]
            seen += 1
            if f.order_id in mine:
                record(f, last_mid, False)
        t_end = min(flat_ts, evs[-1].exchange_ts)
        bs = book_state()
        if bs is not None:
            flat_mid = 0.5 * (bs[0] + bs[1]) * tick
        else:
            flat_mid = last_mid if last_mid is not None else (fills[-1]["px"] if fills else 0.0)
        if q[0] != 0:
            d = -q[0]
            qty = abs(d)
            if bs is None:
                px = flat_mid
                fee = imp = 0.0
                c["forced_flatten"] += 1
            else:
                px = (bs[0] if d < 0 else bs[1]) * tick
                notional = qty * unit * px
                fee = (
                    venue.commission_per_million * notional / 1e6
                    if venue.is_fx
                    else venue.taker_fee_per_share * qty
                )
                imp_bps = self.exec_config.impact_coeff_bps_per_pct_adv * (
                    qty * unit / ins.adv * 100
                )
                imp = imp_bps * 1e-4 * notional
            fills.append(
                {
                    "ts": t_end,
                    "side": 0 if d > 0 else 1,
                    "qty": qty,
                    "px": px,
                    "mid": flat_mid,
                    "liquidity": "TAKER",
                    "fee": fee,
                    "impact": imp,
                    "flatten": True,
                }
            )
            q[0] = 0
            inv_path.append((t_end, 0))
            flattened = True
        fdf = pd.DataFrame(
            fills,
            columns=["ts", "side", "qty", "px", "mid", "liquidity", "fee", "impact", "flatten"],
        )
        # markout mid per fill: mid after the last event at/before ts + h (capped)
        mts = np.asarray(mids_ts, dtype=np.int64)
        mv = np.asarray(mids, dtype=float)

        def mid_at(t: int) -> float:
            if t >= t_end or mts.size == 0:
                return flat_mid
            j = int(np.searchsorted(mts, t, side="right")) - 1
            return float(mv[j]) if j >= 0 else flat_mid

        if len(fdf):
            fdf["mid_h"] = [
                flat_mid if fl_ else mid_at(int(t) + cfg.markout_ns)
                for t, fl_ in zip(fdf["ts"], fdf["flatten"], strict=True)
            ]
            fdf["d"] = np.where(fdf["side"] == 0, 1.0, -1.0) * fdf["qty"] * unit
        else:
            fdf["mid_h"] = pd.Series(dtype=float)
            fdf["d"] = pd.Series(dtype=float)
        qt = fdf[~fdf["flatten"].astype(bool)]
        ft = fdf[fdf["flatten"].astype(bool)]
        pnl = {
            "spread_captured": float((qt["d"] * (qt["mid"] - qt["px"])).sum()),
            "markout": float((qt["d"] * (qt["mid_h"] - qt["mid"])).sum()),
            "inventory_pnl": float((qt["d"] * (flat_mid - qt["mid_h"])).sum()),
            "flatten_cost": float((ft["d"] * (flat_mid - ft["px"])).sum()),
        }
        pnl["gross"] = float((-fdf["d"] * fdf["px"]).sum())
        maker = fdf["liquidity"] == "MAKER"
        pnl["rebates"] = float(-fdf.loc[maker, "fee"].sum())
        pnl["taker_fees"] = float(fdf.loc[~maker, "fee"].sum())
        pnl["impact"] = float(fdf["impact"].sum())
        pnl["net"] = pnl["gross"] + pnl["rebates"] - pnl["taker_fees"] - pnl["impact"]
        c["flattened"] = int(flattened)
        inv = pd.DataFrame(inv_path + [(t_end, 0)], columns=["ts", "q"])
        return QuotingResult(instrument_id, fdf, inv, pnl, c, as_bps, as_source)

    def run_days(
        self,
        days: Mapping[str, tuple[Sequence[MarketEvent], pd.DataFrame]],
        instrument_id: int,
        *,
        venue_id: int | None = None,
    ) -> pd.DataFrame:
        """One flattened session per day; returns a per-day table."""
        rows = []
        for day, (evs, sc) in days.items():
            s = self.run_instrument(evs, sc, instrument_id, venue_id=venue_id).summary()
            rows.append({"day": day, **s})
        return pd.DataFrame(rows)


def sharpe_per_day(daily_net: Sequence[float], annualize: int = 252) -> float | None:
    """Annualized Sharpe of per-day net P&L (None for < 2 days or zero std)."""
    x = np.asarray(daily_net, dtype=float)
    if x.size < 2:
        return None
    sd = x.std(ddof=1)
    return None if sd == 0 else float(x.mean() / sd * math.sqrt(annualize))

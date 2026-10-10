"""Maker-side alpha backtest (v1.9 M2, with the M4 conditional rules).

The research backtester (:mod:`iap.backtest.engine`) only TAKES liquidity:
every trade pays the half-spread and the taker fee on both legs, about
0.7 bp round trip on AAPL against an expected 1 s move near 0.07 bp. This
module tests the other side of the book: the alpha decides which side to
quote, and the order RESTS at the touch, so a fill earns the half-spread
and the maker rebate and pays only its adverse-selection markout. It is a
separate, opt-in mode; the taker backtest stays the default everywhere.

Pinned rules (``MakerBacktester.run_instrument``):

1. **Replay.** One ordered event stream of the instrument is replayed
   through the FIFO execution simulator (:class:`ExecutionSimulator`,
   rules 1-9: latency, queue position from the displayed size ahead,
   fills only from observed trades, maker rebate / FX commission per fill).
   An optional calibration (:mod:`iap.execution.calibration`) supplies the
   latency table and the impact coefficient; without one the synthetic
   venue config applies.
2. **Decisions.** Each score row (``exchange_ts``, ``expected_return``,
   ``confidence``, optional ``z``) is a decision that sees every event with
   ``exchange_ts <= t``. A decision acts only while FLAT with no working
   order; it posts ``qty`` as a LIMIT at the touch of the side the alpha
   points to (``er > 0`` buys at the best bid, ``er < 0`` sells at the best
   ask), expiring ``ttl_ns`` after the decision.
3. **Gate** (expected edge > adverse selection), in bps of the mid::

       edge = |er| * 1e4 + half_spread + rebate - exit_cost
       trade iff  edge > adverse_selection + margin_bps

   ``rebate`` = ``maker_rebate_per_share / mid`` (equity) or minus the FX
   commission; ``exit_cost`` = ``half_spread + taker_fee / mid`` +
   linear impact for ``exit="taker"``, 0 for ``exit="mid"`` (a mark-to-mid
   diagnostic, not a tradable exit), and for ``exit="passive"`` the
   expectation ``p * (-half_spread - rebate) + (1 - p) * taker_exit_cost``
   with ``p`` = ``passive_exit_fill_prob``, else the calibration's touch
   ``p_any_fill``, else 0.5. ``adverse_selection`` is the
   calibration's measured maker markout at ``as_horizon`` (per instrument
   when available), else ``MakerConfig.adverse_selection_bps``, else 0
   (recorded as ``as_source`` so an uncalibrated gate is never mistaken
   for a calibrated one).
4. **Tails** (M4, every condition optional, ANDed): ``min_spread_ticks``;
   ``min_abs_z`` on the scores' ``z`` column (use
   :meth:`LinearAlpha.score_uncapped` to see past the pinned clip);
   ``min_abs_er``; ``min_queue_imbalance`` = our side's L1 / (bid + ask
   L1) at decision time (the far queue is thin); ``min_p_far_deplete`` =
   the calibrated probability that the FAR touch depletes within
   ``horizon_ns`` (``1 - exp(-hazard * horizon)``); and an external
   ``allow`` mask (the metalabel / GBM :class:`MakerFilter`).
5. **Position.** On the first fill the unfilled remainder is cancelled
   (through the latency path; fills that land before the cancel add to the
   position). The position is held ``horizon_ns`` from the first fill and
   then closed at the first event at or after that time against the book
   state before it: ``exit="taker"`` crosses at the touch paying the taker
   fee and the simulator's linear impact rule; ``exit="mid"`` marks at the
   mid. ``exit="passive"`` (opt-in) instead POSTS the whole position at the
   far touch (sell at the best ask / buy at the best bid) through the FIFO
   simulator, earning the half-spread and the maker rebate when it fills;
   each post rests ``exit_timeout_ns``, is reposted at the new touch up to
   ``exit_reprices`` times, and the remainder then crosses as a taker
   (``exit_timeout``). A position still open at the end of the stream is
   closed by crossing on the last state (``forced_exits``).
6. **Accounting** per round trip, side sign ``s``, quote-currency units::

       gross       = s * (exit_px - entry_px) * qty * qty_unit
       net         = gross - entry_fees - exit_fee - exit_impact

   with ``entry_fees`` negative for a rebate. The measured markout is what
   the gross term charges: ``gross = (half_spread_earned - adverse_selection
   - exit_slippage) * qty * qty_unit`` where ``half_spread_earned = s *
   (entry_mid - entry_px)`` (``entry_mid`` = the mid before the filling
   event, the MAKER reference of ``iap.tca.markout``), ``adverse_selection =
   s * (entry_mid - exit_mid)`` and ``exit_slippage = s * (exit_mid -
   exit_px)``. With a passive exit ``exit_px`` is the qty-weighted price of
   the passive exit fills and any crossed remainder, ``exit_fee`` the sum of
   their fees (a rebate on the passive part), and ``exit_mid`` the mid before
   the completing fill (or at the cross): a fully passive exit has NEGATIVE
   exit slippage (the second half-spread, earned), a timeout pays it. The
   identity is tested for every exit mode.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from iap.core.events import MarketEvent
from iap.execution.calibration import ExecCalibration, apply_calibration
from iap.execution.config import ExecConfig
from iap.execution.simulator import ExecutionSimulator
from iap.execution.sor import NO_ROUTE
from iap.execution.sor_v2 import CostAwareRouter
from iap.execution.types import ChildOrder, Liquidity, OrderType

EXIT_MODES = ("taker", "mid", "passive")


@dataclass(frozen=True)
class MakerConfig:
    """Maker backtest parameters (module docstring for the rules)."""

    qty: int = 100
    ttl_ns: int = 1_000_000_000
    horizon_ns: int = 1_000_000_000
    exit: str = "taker"
    conf_min: float = 0.0
    margin_bps: float = 0.0
    #: horizon name of the calibration markout used as adverse selection
    as_horizon: str = "1s"
    #: fallback adverse selection (bps) when the calibration has none
    adverse_selection_bps: float | None = None
    # ---- passive exit (exit="passive") ----
    #: how long each passive exit post rests before it is repriced / crossed
    exit_timeout_ns: int = 1_000_000_000
    #: reposts at the new touch after a timeout before crossing the remainder
    exit_reprices: int = 0
    #: P(passive exit fills) the gate assumes; None = the calibration's
    #: touch ``p_any_fill``, else 0.5
    passive_exit_fill_prob: float | None = None
    # ---- M4 tail conditions (None / 0 = off) ----
    min_spread_ticks: int = 1
    min_abs_z: float | None = None
    min_abs_er: float = 0.0
    min_queue_imbalance: float | None = None
    min_p_far_deplete: float | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0 or self.ttl_ns <= 0 or self.horizon_ns <= 0:
            raise ValueError("qty, ttl_ns and horizon_ns must be positive")
        if self.exit not in EXIT_MODES:
            raise ValueError(f"exit must be one of {EXIT_MODES}, got {self.exit!r}")
        if self.exit_timeout_ns <= 0 or self.exit_reprices < 0:
            raise ValueError("exit_timeout_ns must be positive and exit_reprices >= 0")
        p = self.passive_exit_fill_prob
        if p is not None and not 0.0 <= p <= 1.0:
            raise ValueError("passive_exit_fill_prob must be in [0, 1]")
        if self.min_spread_ticks < 0:
            raise ValueError("min_spread_ticks must be >= 0")
        if self.min_queue_imbalance is not None and not 0.0 <= self.min_queue_imbalance <= 1.0:
            raise ValueError("min_queue_imbalance must be in [0, 1]")
        if self.min_p_far_deplete is not None and not 0.0 <= self.min_p_far_deplete <= 1.0:
            raise ValueError("min_p_far_deplete must be in [0, 1]")


@dataclass
class MakerResult:
    """Outcome of one maker backtest run on one instrument."""

    instrument_id: int
    trips: pd.DataFrame = field(repr=False)
    counters: dict[str, int]
    as_bps: float
    as_source: str

    @property
    def net_pnl(self) -> float:
        return float(self.trips["net"].sum()) if len(self.trips) else 0.0

    def summary(self) -> dict[str, Any]:
        """Totals and per-trip means (bps of entry price) for a report."""
        t = self.trips
        out: dict[str, Any] = {
            "instrument_id": self.instrument_id,
            **self.counters,
            "fill_rate": (
                self.counters["filled_orders"] / self.counters["posted"]
                if self.counters["posted"]
                else None
            ),
            "as_bps_gate": self.as_bps,
            "as_source": self.as_source,
            "n_trips": len(t),
        }
        passive = t[t["exit_mode"] == "passive"] if len(t) else t
        n_passive = len(passive)
        out["passive_exit_trips"] = n_passive
        out["passive_exit_fill_rate"] = (
            float((passive["exit_passive_qty"] == passive["qty"]).mean()) if n_passive else None
        )
        out["passive_exit_qty_share"] = (
            float(passive["exit_passive_qty"].sum() / passive["qty"].sum()) if n_passive else None
        )
        out["timeout_cross_rate"] = float(passive["exit_timeout"].mean()) if n_passive else None
        for col in ("gross", "entry_fees", "exit_fee", "exit_impact", "net"):
            out[f"total_{col}"] = float(t[col].sum()) if len(t) else 0.0
        for col in (
            "half_spread_earned_bps",
            "adverse_selection_bps",
            "exit_slippage_bps",
            "net_bps",
        ):
            out[f"mean_{col}"] = float(t[col].mean()) if len(t) else None
        return out


class MakerFilter:
    """Optional metalabel / GBM trade filter for the maker backtest (M4).

    ``model="meta_gbm"`` is the pinned meta-label classifier
    (``HistGradientBoostingClassifier`` with
    :data:`iap.models.metalabel.META_HYPERPARAMS`), trained on a binary
    target such as the M3 ``{side}_not_run_over`` label; the score is the
    positive-class probability. Any :mod:`iap.models.zoo` regressor name
    (``ridge``, ``lightgbm`` ...) regresses a continuous target such as the
    M3 markout; its prediction is the score. ``allow(X)`` is
    ``score >= tau`` (NaN-safe: a row whose score is not finite is blocked).
    Fit it on rows strictly before the backtest window — the filter is a
    model and inherits every leakage rule of one.
    """

    def __init__(self, model: str = "meta_gbm", tau: float = 0.5) -> None:
        self.model_name = model
        self.tau = float(tau)
        self._model: Any = None
        self.model_id: str | None = None  # set by from_registry (v1.11)

    def fit(self, X: np.ndarray, y: np.ndarray) -> MakerFilter:
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        ok = np.isfinite(y)
        if self.model_name == "meta_gbm":
            from sklearn.ensemble import HistGradientBoostingClassifier

            from iap.models.metalabel import META_HYPERPARAMS

            yb = (y[ok] > 0).astype(int)
            if yb.min(initial=1) == yb.max(initial=0):
                raise ValueError("meta_gbm filter needs both classes in y")
            self._model = HistGradientBoostingClassifier(**META_HYPERPARAMS).fit(X[ok], yb)
        else:
            from iap.models.zoo import make_model

            Xf = np.where(np.isfinite(X), X, 0.0)
            self._model = make_model(self.model_name).fit(Xf[ok], y[ok])
        return self

    def score(self, X: np.ndarray) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("MakerFilter.score() before fit()")
        X = np.asarray(X, dtype=float)
        if self.model_name == "meta_gbm":
            return self._model.predict_proba(X)[:, 1]
        return np.asarray(self._model.predict(np.where(np.isfinite(X), X, 0.0)), dtype=float)

    def allow(self, X: np.ndarray) -> np.ndarray:
        s = self.score(X)
        return np.isfinite(s) & (s >= self.tau)

    @classmethod
    def from_registry(cls, registry, model_id: str, tau: float | None = None) -> MakerFilter:
        """A fitted filter loaded (and integrity-checked) from an
        :class:`iap.mlops.ModelRegistry` by id (v1.11).  The record's
        ``params`` carry ``model`` and ``tau``; ``tau`` overrides the latter."""
        from iap.mlops.registry import ModelRegistry

        reg = registry if isinstance(registry, ModelRegistry) else ModelRegistry(registry)
        model, rec = reg.load(model_id)
        f = cls(model=rec.params.get("model", "meta_gbm"), tau=rec.params.get("tau", 0.5))
        if tau is not None:
            f.tau = float(tau)
        f._model = model
        f.model_id = rec.model_id
        return f

    def register(self, registry, **meta):
        """Register this fitted filter (``iap.mlops.ModelRegistry.register``;
        ``meta`` = dataset_version, date_range, features, seed, prereg / exploratory...)."""
        from iap.mlops.registry import ModelRegistry

        if self._model is None:
            raise RuntimeError("MakerFilter.register() before fit()")
        reg = registry if isinstance(registry, ModelRegistry) else ModelRegistry(registry)
        kind = "classifier" if self.model_name == "meta_gbm" else "regressor"
        params = {"model": self.model_name, "tau": self.tau, **meta.pop("params", {})}
        meta.setdefault("name", f"maker_filter/{self.model_name}")
        return reg.register(self._model, kind=kind, params=params, **meta)


class MakerBacktester:
    """Event-replay maker backtest (module docstring, rules 1-6)."""

    def __init__(
        self,
        exec_config: ExecConfig,
        config: MakerConfig | None = None,
        calibration: ExecCalibration | None = None,
        router: CostAwareRouter | None = None,
    ) -> None:
        #: opt-in v1.12 cost-aware router (iap.execution.sor_v2); None = the
        #: single given/first venue, exactly as before
        self.router = router
        self.calibration = calibration
        self.exec_config = apply_calibration(exec_config, calibration)
        self.config = config or MakerConfig()

    def adverse_selection(self, instrument_id: int) -> tuple[float, str]:
        """(bps, source) of the gate's adverse-selection term."""
        if self.calibration is not None:
            v = self.calibration.adverse_selection_bps(self.config.as_horizon, instrument_id)
            if v is not None:
                return v, f"calibration:{self.config.as_horizon}"
        if self.config.adverse_selection_bps is not None:
            return float(self.config.adverse_selection_bps), "config"
        return 0.0, "none"

    def run_instrument(
        self,
        events: Sequence[MarketEvent],
        scores: pd.DataFrame,
        instrument_id: int,
        *,
        venue_id: int | None = None,
        allow: np.ndarray | None = None,
    ) -> MakerResult:
        cfg = self.config
        ins = self.exec_config.instrument(instrument_id)
        if venue_id is None:
            venue_id = next((e.venue_id for e in events if e.instrument_id == instrument_id), None)
            if venue_id is None:
                raise ValueError(f"no event for instrument {instrument_id}")
        venue = self.exec_config.venue(venue_id)
        router = self.router
        route_cands = (
            sorted({e.venue_id for e in events if e.instrument_id == instrument_id})
            if router is not None
            else None
        )
        ts = scores["exchange_ts"].to_numpy(dtype=np.int64)
        if ts.size and np.any(np.diff(ts) < 0):
            raise ValueError("scores must be sorted by exchange_ts")
        er = scores["expected_return"].to_numpy(dtype=float)
        conf = scores["confidence"].to_numpy(dtype=float)
        z = scores["z"].to_numpy(dtype=float) if "z" in scores.columns else None
        if cfg.min_abs_z is not None and z is None:
            raise ValueError("min_abs_z needs a 'z' column in scores")
        if cfg.min_p_far_deplete is not None and self.calibration is None:
            raise ValueError("min_p_far_deplete needs a calibration")
        if allow is not None:
            allow = np.asarray(allow, dtype=bool)
            if allow.shape != ts.shape:
                raise ValueError("allow mask must be row-aligned with scores")
        as_bps, as_source = self.adverse_selection(instrument_id)
        tick = ins.tick_size
        unit = ins.qty_unit

        sim = ExecutionSimulator(self.exec_config, self.calibration)
        c = dict.fromkeys(
            (
                "decisions",
                "busy",
                "filtered",
                "tail_out",
                "gated_out",
                "posted",
                "filled_orders",
                "taker_entries",
                "forced_exits",
                "exit_posts",
                "exit_timeouts",
            ),
            0,
        )
        trips: list[dict[str, Any]] = []
        st: dict[str, Any] = {
            "order": None,
            "pos": 0,
            "side": 0,
            "exit_due": None,
            "exit_order": None,
            "exit_started": False,
        }
        # passive-exit ledger of the current trip
        xl: dict[str, Any] = {"qty": 0, "px_sum": 0.0, "fees": 0.0, "reprices": 0}
        p_exit = cfg.passive_exit_fill_prob
        if p_exit is None:
            p_any = None
            if self.calibration is not None:
                fr = (self.calibration.doc.get("fill_rates") or {}).get("all") or {}
                p_any = fr.get("p_any_fill")
            p_exit = float(p_any) if p_any is not None else 0.5
        entry: dict[str, Any] = {}
        seen_fills = 0
        last_mid: list[float | None] = [None]

        def book_state():
            book = sim.venue_book(instrument_id, venue_id)
            if not sim.venue_open(book):
                return None
            bb, ba = book.best_bid(), book.best_ask()
            if bb is None or ba is None or ba[0] < bb[0]:
                return None
            return bb, ba

        def rebate_bps(mid: float) -> float:
            if venue.is_fx:
                return -venue.commission_per_million / 100.0
            return 1e4 * venue.maker_rebate_per_share / mid

        def exit_cost_bps(mid: float, hs: float) -> float:
            if cfg.exit == "mid":
                return 0.0
            fee = (
                venue.commission_per_million / 100.0
                if venue.is_fx
                else 1e4 * venue.taker_fee_per_share / mid
            )
            imp = self.exec_config.impact_coeff_bps_per_pct_adv * (cfg.qty * unit / ins.adv * 100.0)
            taker = 1e4 * hs / mid + fee + imp
            if cfg.exit == "passive":
                # expected: earn the half-spread + rebate when the post fills,
                # cross as a taker otherwise
                return p_exit * (-1e4 * hs / mid - rebate_bps(mid)) + (1.0 - p_exit) * taker
            return taker

        def decide(r: int) -> None:
            nonlocal venue_id, venue
            c["decisions"] += 1
            if st["order"] is not None or st["pos"] != 0:
                c["busy"] += 1
                return
            if allow is not None and not allow[r]:
                c["filtered"] += 1
                return
            e = er[r]
            if not math.isfinite(e) or e == 0.0 or not conf[r] >= cfg.conf_min:
                c["gated_out"] += 1
                return
            if router is not None:
                # opt-in: pick the entry venue by expected all-in passive cost;
                # the exit then uses the same venue (it only switches while flat)
                vid = router.route_passive(
                    sim.instrument_book(instrument_id),
                    0 if e > 0 else 1,
                    int(ts[r]),
                    tick,
                    route_cands,
                )
                if vid == NO_ROUTE:
                    c["gated_out"] += 1
                    return
                venue_id, venue = vid, self.exec_config.venue(vid)
            bs = book_state()
            if bs is None:
                c["gated_out"] += 1
                return
            (bpx, bq), (apx, aq) = bs
            side = 0 if e > 0 else 1
            mid = 0.5 * (bpx + apx) * tick
            hs = 0.5 * (apx - bpx) * tick
            # M4 tails
            tail_ok = apx - bpx >= cfg.min_spread_ticks and abs(e) >= cfg.min_abs_er
            if tail_ok and cfg.min_abs_z is not None:
                tail_ok = math.isfinite(z[r]) and abs(z[r]) >= cfg.min_abs_z
            if tail_ok and cfg.min_queue_imbalance is not None:
                ours, far = (bq, aq) if side == 0 else (aq, bq)
                tail_ok = ours + far > 0 and ours / (ours + far) >= cfg.min_queue_imbalance
            if tail_ok and cfg.min_p_far_deplete is not None:
                far_side, far_q = (1, aq) if side == 0 else (0, bq)
                lam = self.calibration.queue_depletion_hazard(far_side, far_q)
                p = None if lam is None else 1.0 - math.exp(-lam * cfg.horizon_ns / 1e9)
                tail_ok = p is not None and p >= cfg.min_p_far_deplete
            if not tail_ok:
                c["tail_out"] += 1
                return
            edge = 1e4 * abs(e) + 1e4 * hs / mid + rebate_bps(mid) - exit_cost_bps(mid, hs)
            if not edge > as_bps + cfg.margin_bps:
                c["gated_out"] += 1
                return
            oid = sim.submit(
                ChildOrder(
                    instrument_id=instrument_id,
                    venue_id=venue_id,
                    side=side,
                    type=OrderType.LIMIT,
                    qty=cfg.qty,
                    limit_ticks=bpx if side == 0 else apx,
                    decision_ts=int(ts[r]),
                    expire_ts=int(ts[r]) + cfg.ttl_ns,
                )
            )
            c["posted"] += 1
            st["order"] = oid
            st["side"] = side

        def post_exit(t: int) -> bool:
            bs = book_state()
            if bs is None:
                return False
            (bpx, _), (apx, _) = bs
            oid = sim.submit(
                ChildOrder(
                    instrument_id=instrument_id,
                    venue_id=venue_id,
                    side=1 - st["side"],
                    type=OrderType.LIMIT,
                    qty=abs(st["pos"]),
                    limit_ticks=apx if st["side"] == 0 else bpx,
                    decision_ts=t,
                    expire_ts=t + cfg.exit_timeout_ns,
                )
            )
            st["exit_order"] = oid
            c["exit_posts"] += 1
            return True

        def close(t: int, forced: bool, mid_hint: float | None = None) -> None:
            q_rem = abs(st["pos"])
            cross_px = 0.0
            crossed_at_book = False
            if q_rem > 0:
                bs = book_state()
                if bs is None:
                    if not forced:
                        return  # no two-sided open book: retry on the next event
                    if last_mid[0] is None:
                        return
                    exit_mid = last_mid[0]
                    cross_px = exit_mid  # no book at all: mark (counted as forced)
                else:
                    (bpx, _), (apx, _) = bs
                    exit_mid = 0.5 * (bpx + apx) * tick
                    if cfg.exit == "mid":
                        cross_px = exit_mid
                    else:
                        cross_px = (bpx if st["side"] == 0 else apx) * tick
                        crossed_at_book = True
            else:
                exit_mid = mid_hint if mid_hint is not None else last_mid[0]
            eo = st["exit_order"]
            if eo is not None and not sim.orders[eo].is_terminal:
                sim.cancel(eo, t)
            q = entry["qty"]
            s = 1.0 if st["side"] == 0 else -1.0
            exit_px = (xl["px_sum"] + q_rem * cross_px) / q
            rem_notional = q_rem * unit * cross_px
            taker_fee = exit_impact = 0.0
            if crossed_at_book:
                taker_fee = (
                    venue.commission_per_million * rem_notional / 1e6
                    if venue.is_fx
                    else venue.taker_fee_per_share * q_rem
                )
                imp_bps = self.exec_config.impact_coeff_bps_per_pct_adv * (
                    q_rem * unit / ins.adv * 100.0
                )
                exit_impact = imp_bps * 1e-4 * rem_notional
            exit_fee = xl["fees"] + taker_fee
            timeout = cfg.exit == "passive" and q_rem > 0
            epx = entry["px"]
            gross = s * (exit_px - epx) * q * unit
            net = gross - entry["fees"] - exit_fee - exit_impact
            emid = entry["mid"]
            trips.append(
                {
                    "side": st["side"],
                    "qty": q,
                    "entry_ts": entry["ts"],
                    "entry_px": epx,
                    "entry_mid": emid,
                    "exit_ts": t,
                    "exit_px": exit_px,
                    "exit_mid": exit_mid,
                    "liquidity": entry["liq"],
                    "exit_mode": cfg.exit,
                    "exit_passive_qty": xl["qty"],
                    "exit_timeout": timeout,
                    "exit_reprices": xl["reprices"],
                    "gross": gross,
                    "entry_fees": entry["fees"],
                    "exit_fee": exit_fee,
                    "exit_impact": exit_impact,
                    "net": net,
                    "half_spread_earned_bps": 1e4 * s * (emid - epx) / epx,
                    "adverse_selection_bps": 1e4 * s * (emid - exit_mid) / epx,
                    "exit_slippage_bps": 1e4 * s * (exit_mid - exit_px) / epx,
                    "net_bps": 1e4 * net / (q * unit * epx),
                    "forced": forced,
                }
            )
            if router is not None:
                trips[-1]["venue_id"] = venue_id
            if forced:
                c["forced_exits"] += 1
            if timeout:
                c["exit_timeouts"] += 1
            st["pos"] = 0
            st["exit_due"] = None
            st["exit_order"] = None
            st["exit_started"] = False
            xl.update(qty=0, px_sum=0.0, fees=0.0, reprices=0)

        def exit_step(t: int) -> None:
            """Exit work at an event at/after the hold horizon (rule 5)."""
            if cfg.exit != "passive":
                close(t, forced=False)
                return
            if st["exit_order"] is not None:
                return  # the post is resting
            if not st["exit_started"]:
                st["exit_started"] = post_exit(t)
            elif xl["reprices"] < cfg.exit_reprices:
                if post_exit(t):
                    xl["reprices"] += 1
            else:
                close(t, forced=False)  # timeout: cross the remainder

        r = 0
        n = int(ts.size)
        for ev in events:
            if ev.instrument_id != instrument_id:
                continue
            t = ev.exchange_ts
            while r < n and ts[r] < t:
                if st["pos"] != 0 and st["exit_due"] is not None and ts[r] >= st["exit_due"]:
                    break  # exit first; the decision is handled after it
                decide(r)
                r += 1
            if st["pos"] != 0 and st["exit_due"] is not None and t >= st["exit_due"]:
                exit_step(t)
                while r < n and ts[r] < t:
                    decide(r)
                    r += 1
            bs = book_state()
            pre_mid = None if bs is None else 0.5 * (bs[0][0] + bs[1][0]) * tick
            if pre_mid is not None:
                last_mid[0] = pre_mid
            sim.on_event(ev)
            fills = sim.fills
            while seen_fills < len(fills):
                f = fills[seen_fills]
                seen_fills += 1
                if st["exit_order"] is not None and f.order_id == st["exit_order"]:
                    xl["qty"] += f.qty
                    xl["px_sum"] += f.price_ticks * tick * f.qty
                    xl["fees"] += f.fee + f.impact_cost
                    st["pos"] += f.qty if f.side == 0 else -f.qty
                    if st["pos"] == 0:
                        close(f.ts, forced=False, mid_hint=pre_mid)
                    continue
                if f.order_id != st["order"]:
                    continue
                if st["pos"] == 0:
                    ref = pre_mid if f.liquidity == Liquidity.MAKER else None
                    if ref is None:
                        ref = last_mid[0] if last_mid[0] is not None else f.price_ticks * tick
                    entry.clear()
                    entry.update(
                        px=f.price_ticks * tick,
                        ts=f.ts,
                        mid=ref,
                        fees=0.0,
                        qty=0,
                        liq=f.liquidity.name,
                    )
                    st["exit_due"] = f.ts + cfg.horizon_ns
                    c["filled_orders"] += 1
                    if f.liquidity == Liquidity.TAKER:
                        c["taker_entries"] += 1
                    if not sim.orders[f.order_id].is_terminal:
                        sim.cancel(f.order_id, f.ts)
                q_prev = abs(st["pos"])
                entry["px"] = (entry["px"] * q_prev + f.price_ticks * tick * f.qty) / (
                    q_prev + f.qty
                )
                entry["fees"] += f.fee + f.impact_cost
                entry["qty"] += f.qty
                st["pos"] += f.qty if f.side == 0 else -f.qty
            o = st["order"]
            if o is not None and sim.orders[o].is_terminal:
                st["order"] = None
            eo = st["exit_order"]
            if eo is not None and sim.orders[eo].is_terminal:
                st["exit_order"] = None  # expired: reprice or cross on the next event
        while r < n:
            decide(r)
            r += 1
        sim.cancel_all()
        if st["pos"] != 0:
            close(events[-1].exchange_ts if len(events) else 0, forced=True)
        cols = [
            "side",
            "qty",
            "entry_ts",
            "entry_px",
            "entry_mid",
            "exit_ts",
            "exit_px",
            "exit_mid",
            "liquidity",
            "exit_mode",
            "exit_passive_qty",
            "exit_timeout",
            "exit_reprices",
            "gross",
            "entry_fees",
            "exit_fee",
            "exit_impact",
            "net",
            "half_spread_earned_bps",
            "adverse_selection_bps",
            "exit_slippage_bps",
            "net_bps",
            "forced",
        ]
        if router is not None:
            cols.append("venue_id")
        return MakerResult(
            instrument_id=instrument_id,
            trips=pd.DataFrame(trips, columns=cols),
            counters=c,
            as_bps=as_bps,
            as_source=as_source,
        )

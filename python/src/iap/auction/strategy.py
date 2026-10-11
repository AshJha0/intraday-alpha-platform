"""Minutes-horizon auction-imbalance strategy, backtest and walk-forward.

One decision per (date, symbol, cross type): the first NOII snapshot with
``time_to_cross_s <= decision_s``.  The signal is the imbalance ratio; when
``|ratio| >= threshold`` the strategy takes ``qty`` shares at the touch on
the side ``orientation * sign(ratio)`` (``orientation=+1`` follows the
imbalance, ``-1`` fades it) and exits

* ``exit="cross"`` - in the cross itself (participates as a market-on-close
  / on-open order): earns ``cross_vs_mid_bps``, pays the entry taker cost
  plus ``cross_fee_per_share`` (no spread, no impact - the cross is one
  price for everyone);
* ``exit="taker"`` - with a taker trade at the last mid before the cross:
  earns ``into_cross_drift_bps``, pays a taker cost on both legs.

Taker legs are priced with :meth:`iap.backtest.costs.CostModel.cost_components`
(spread + fee + impact, with the model's multiplier).  The half-spread is
the label frame's ``half_spread_t`` when known, else
``default_half_spread_bps`` of the mid.

:func:`walk_forward` fits the threshold (and, only with
``fit_orientation=True``, the orientation; v1.13) on each fold's purged
train days and applies them to the test day, using
:class:`iap.validation.splits.WalkForwardSplitter` in ``day_aligned`` mode
(horizon = ``decision_s``) and a stationary-bootstrap interval
(:func:`iap.validation.diagnostics.stationary_bootstrap_ci`) on the
out-of-sample daily net P&L.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace

import numpy as np
import pandas as pd

from iap.auction.features import GROUP_KEYS
from iap.backtest.costs import CostModel
from iap.validation.diagnostics import stationary_bootstrap_ci
from iap.validation.splits import WalkForwardSplitter

EXITS = ("cross", "taker")
NS_S = 1_000_000_000
#: Threshold grid the walk-forward fits over (pinned).
DEFAULT_THRESHOLDS = (0.05, 0.1, 0.2, 0.3, 0.5)


@dataclass(frozen=True)
class AuctionStrategyConfig:
    cross_type: str = "C"
    decision_s: float = 300.0
    threshold: float = 0.2
    orientation: int = 1
    exit: str = "cross"
    qty: int = 100
    adv_shares: float = 5_000_000.0
    default_half_spread_bps: float = 1.0
    cross_fee_per_share: float = 0.0010

    def __post_init__(self) -> None:
        if self.exit not in EXITS:
            raise ValueError(f"exit must be one of {EXITS}")
        if self.orientation not in (1, -1):
            raise ValueError("orientation must be +1 or -1")
        if self.decision_s <= 0 or self.qty <= 0 or self.adv_shares <= 0:
            raise ValueError("decision_s, qty and adv_shares must be positive")
        if self.threshold < 0:
            raise ValueError("threshold must be >= 0")


def default_cost_model() -> CostModel:
    """A stand-in for tests (0.0030 USD/share taker fee, sqrt impact); the
    CLI loads ``configs/execution/execution.json`` instead."""
    return CostModel(
        impact_coeff_bps_per_pct_adv=1.0,
        equity_taker_fee_per_share=0.0030,
        fx_commission_per_million=0.0,
    )


def decision_rows(labels: pd.DataFrame, cfg: AuctionStrategyConfig) -> pd.DataFrame:
    """The first snapshot per window at or inside ``decision_s`` of the cross."""
    df = labels[(labels["cross_type"] == cfg.cross_type) & (labels["time_to_cross_s"] > 0)]
    df = df[df["time_to_cross_s"] <= cfg.decision_s]
    df = df.sort_values([*GROUP_KEYS, "ts"], kind="mergesort")
    return df.groupby(GROUP_KEYS, as_index=False).first().sort_values("ts", kind="mergesort")


def _taker_bps(cm: CostModel, qty: int, mid: np.ndarray, hs: np.ndarray, adv: float) -> np.ndarray:
    q = np.full(mid.shape, float(qty))
    comp = cm.cost_components(q, mid, hs, "EQUITY", adv, 1)
    total = comp["spread"] + comp["fee"] + comp["impact"]
    with np.errstate(divide="ignore", invalid="ignore"):
        return 1e4 * total / (q * mid)


@dataclass
class AuctionBacktestResult:
    trades: pd.DataFrame
    config: dict
    folds: list = field(default_factory=list)
    bootstrap: dict | None = None
    #: Configurations the walk-forward evaluated on train days, summed over
    #: folds (v1.13, R-P1-4): the looks the fit consumed.
    looks_consumed: int | None = None

    def summary(self) -> dict:
        t = self.trades
        n = len(t)
        out: dict = {"n_trades": n, "n_days": int(t["date"].nunique()) if n else 0}
        if not n:
            return {**out, "mean_net_bps": None, "t_stat": None}
        net = t["net_bps"].to_numpy(float)
        sd = float(np.std(net, ddof=1)) if n > 1 else float("nan")
        out.update(
            mean_gross_bps=float(t["gross_bps"].mean()),
            mean_cost_bps=float(t["cost_bps"].mean()),
            mean_net_bps=float(net.mean()),
            t_stat=(float(net.mean() / (sd / math.sqrt(n))) if n > 1 and sd > 0 else None),
            hit_rate=float((net > 0).mean()),
            total_net_usd=float(t["net_usd"].sum()),
        )
        return out

    def to_dict(self) -> dict:
        return {
            "config": self.config,
            "summary": self.summary(),
            "folds": self.folds,
            "bootstrap": self.bootstrap,
            **({} if self.looks_consumed is None else {"looks_consumed": self.looks_consumed}),
        }


def _trade_frame(
    dec: pd.DataFrame, cfg: AuctionStrategyConfig, cm: CostModel
) -> tuple[pd.DataFrame, np.ndarray]:
    """Every decision row priced for both sides; returns (frame, valid mask)."""
    mid = dec["mid_t"].to_numpy(float)
    hs = dec["half_spread_t"].to_numpy(float)
    hs = np.where(np.isfinite(hs) & (hs >= 0), hs, mid * cfg.default_half_spread_bps * 1e-4)
    entry = _taker_bps(cm, cfg.qty, mid, hs, cfg.adv_shares)
    if cfg.exit == "cross":
        gross_long = dec["cross_vs_mid_bps"].to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            exit_bps = 1e4 * cm.multiplier * cfg.cross_fee_per_share / mid
    else:
        gross_long = dec["into_cross_drift_bps"].to_numpy(float)
        pre = dec["mid_pre_cross"].to_numpy(float)
        hs_pre = hs / mid * pre
        exit_bps = _taker_bps(cm, cfg.qty, pre, hs_pre, cfg.adv_shares)
    cost = entry + exit_bps
    valid = np.isfinite(mid) & (mid > 0) & np.isfinite(gross_long) & np.isfinite(cost)
    frame = dec[["date", "symbol", "cross_type", "ts", "imbalance_ratio"]].copy()
    frame["mid_t"] = mid
    frame["gross_long_bps"] = gross_long
    frame["cost_bps"] = cost
    return frame, valid


def _apply(frame: pd.DataFrame, valid: np.ndarray, cfg: AuctionStrategyConfig) -> pd.DataFrame:
    ratio = frame["imbalance_ratio"].to_numpy(float)
    side = cfg.orientation * np.sign(ratio)
    take = valid & (np.abs(ratio) >= cfg.threshold) & (side != 0)
    t = frame[take].copy()
    t["side"] = side[take].astype(int)
    t["gross_bps"] = t["side"] * t["gross_long_bps"]
    t["net_bps"] = t["gross_bps"] - t["cost_bps"]
    t["net_usd"] = t["net_bps"] * 1e-4 * cfg.qty * t["mid_t"]
    return t.drop(columns=["gross_long_bps"]).reset_index(drop=True)


def backtest(
    labels: pd.DataFrame,
    cfg: AuctionStrategyConfig | None = None,
    cost_model: CostModel | None = None,
) -> AuctionBacktestResult:
    """In-sample backtest of a fixed (orientation, threshold)."""
    cfg = cfg or AuctionStrategyConfig()
    cm = cost_model or default_cost_model()
    frame, valid = _trade_frame(decision_rows(labels, cfg), cfg, cm)
    return AuctionBacktestResult(_apply(frame, valid, cfg), asdict(cfg))


def fit_orientations(cfg: AuctionStrategyConfig, fit_orientation: bool) -> tuple[int, ...]:
    """Orientations the walk-forward may choose from (v1.13, R-P1-4): the
    registered sign ``cfg.orientation`` only, unless ``fit_orientation``
    opts in to the +/-1 search (which doubles the looks)."""
    return (1, -1) if fit_orientation else (int(cfg.orientation),)


def fit_looks(n_orientations: int, thresholds: tuple[float, ...]) -> int:
    """Configurations one fold's fit evaluates."""
    return int(n_orientations) * len(thresholds)


def _fit(
    frame: pd.DataFrame,
    valid: np.ndarray,
    cfg: AuctionStrategyConfig,
    thresholds: tuple[float, ...],
    min_train_trades: int,
    orientations: tuple[int, ...] = (1, -1),
) -> AuctionStrategyConfig | None:
    best, best_score = None, 0.0
    for orientation in orientations:
        for thr in thresholds:
            c = replace(cfg, orientation=orientation, threshold=thr)
            t = _apply(frame, valid, c)
            if len(t) < min_train_trades:
                continue
            score = float(t["net_bps"].mean()) * math.sqrt(len(t))
            if score > best_score:
                best, best_score = c, score
    return best


def walk_forward(
    labels: pd.DataFrame,
    cfg: AuctionStrategyConfig | None = None,
    cost_model: CostModel | None = None,
    *,
    n_folds: int = 4,
    embargo_ns: int = 0,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    min_train_trades: int = 5,
    seed: int = 20261010,
    fit_orientation: bool = False,
) -> AuctionBacktestResult:
    """Purged, day-aligned walk-forward; out-of-sample trades only.

    The orientation is the registered ``cfg.orientation`` (v1.13, R-P1-4);
    ``fit_orientation=True`` restores the +/-1 search.  Either way the
    result reports ``looks_consumed`` (configurations evaluated on train
    days, summed over folds).

    A fold whose train days admit no (orientation, threshold) with positive
    train net P&L stays flat (no trades) - that is a result, not an error.
    """
    cfg = cfg or AuctionStrategyConfig()
    cm = cost_model or default_cost_model()
    frame, valid = _trade_frame(decision_rows(labels, cfg), cfg, cm)
    ts = frame["ts"].to_numpy(np.int64)
    splitter = WalkForwardSplitter(n_folds=n_folds, embargo_ns=embargo_ns, mode="day_aligned")
    horizon_ns = int(cfg.decision_s * NS_S)
    orientations = fit_orientations(cfg, fit_orientation)
    looks = 0
    oos: list[pd.DataFrame] = []
    folds: list[dict] = []
    for fold in splitter.folds_by_day(ts):
        tr = fold.train_mask(ts, horizon_ns, embargo_ns)
        te = fold.test_mask(ts)
        fitted = _fit(frame[tr], valid[tr], cfg, thresholds, min_train_trades, orientations)
        looks += fit_looks(len(orientations), thresholds)
        row = {
            "fold": fold.index,
            "train_decisions": int(tr.sum()),
            "test_decisions": int(te.sum()),
            "orientation": None if fitted is None else fitted.orientation,
            "threshold": None if fitted is None else fitted.threshold,
        }
        if fitted is not None:
            t = _apply(frame[te], valid[te], fitted)
            t["fold"] = fold.index
            oos.append(t)
            row.update(test_trades=len(t), test_net_bps=float(t["net_bps"].sum()))
        else:
            row.update(test_trades=0, test_net_bps=0.0)
        folds.append(row)
    trades = pd.concat(oos, ignore_index=True) if oos else _apply(frame[:0], valid[:0], cfg)
    daily = trades.groupby("date")["net_usd"].sum().to_list() if len(trades) else []
    boot = stationary_bootstrap_ci(daily, seed=seed)
    return AuctionBacktestResult(trades, asdict(cfg), folds, boot, looks_consumed=looks)

"""Cross-sectional book backtest through the optimizer (v1.17 WS-C item C1).

    python -m iap.research.breadth_book [--dataset-dir DIR | --features-dir DIR]
        [--score eq11|eq10_beta_gap] [--weights linear|deciles]
        [--rebalance-min 5] [--notional 1e6] [--impact cost_model|calibration|none]
        [--calibration PATH --impact-shape sqrt|linear|power]
        [--no-dollar-neutral] [--no-beta-neutral] [--no-sector-neutral]
        [--sizes 0.25,0.5,1,2,4] [--out research/breadth/book] [--tag NAME]

At every rebalance time on a fixed clock (``--rebalance-min``, on the
feature-row clock: each instrument contributes its latest at-or-before
feature value, no later than ``max_age`` old) the driver

1. scores the universe cross-sectionally (``eq11``: the EQ11 transform —
   minus the demeaned, studentised 1-minute log return; ``eq10_beta_gap``: a
   demeaned EQ10 — the gap ``beta * ref_ret_1m - ret_log_1m`` between what
   the reference move implied and what the name did, demeaned and
   studentised; or a caller-supplied ``score_fn`` for planted tests);
2. maps ranks to TARGET weights (``linear``: centred rank, gross 1;
   ``deciles``: +/- equal weight on the top / bottom ``decile_q`` fraction,
   at least one name a side);
3. solves the portfolio problem with the costs INSIDE: alpha =
   ``alpha_bps * 1e-4 * target / max|target|`` (the extreme names carry
   ``alpha_bps`` of expected return per rebalance, the rest in proportion;
   the book trades a name only when that beats its one-leg cost), EWMA
   covariance of the rebalance-grid
   returns (causal, seeded on the first ``cov_init`` returns, no trading
   before), the one-leg linear t-cost (half spread + fee as a return) and
   the C2 :class:`~iap.portfolio.optimizer.ImpactTerm` (from the research
   cost model, or from a calibration's residual fit), subject to the box,
   gross cap, dollar neutrality (``net_cap``), beta and sector neutrality
   (:func:`iap.portfolio.exposures.build_exposure_matrix` rows as the
   optimizer's general exposure constraint), a per-name participation cap
   and a turnover cap — Dykstra projection, so the neutrality rows are met
   exactly;
4. accounts the P&L exactly: marks at the mid prevailing ``exec_delay``
   after the decision time, trades the share difference at that mid, and
   charges spread, fee and impact per name through the SAME rule the
   optimizer saw (:class:`~iap.backtest.costs.CostModel` components, or the
   fitted impact), so ``net == gross - spread - fee - impact`` holds to the
   last float (the identity test).  The book is flattened at the last
   rebalance of each session (``flatten_at_session_end``).

A name whose mid is stale at a rebalance is frozen (its weight is pinned
at the drifted previous weight, marked at its last price) rather than
traded on a stale quote.  An INFEASIBLE solve holds the drifted book and is
counted.

Every run that reads a statistic is ONE ledger look of kind
``breadth_book`` (:class:`iap.validation.ledger.ExperimentLedger`,
dataset-scoped: the dataset's ``dataset_version`` from ``dataset.json``,
else the normalized-data fingerprint).  A dataset directory whose path
contains ``holdout`` is refused without ``--allow-holdout``.  Deterministic:
same inputs and arguments => same JSON bytes (``generated`` omitted under
``--no-timestamp``).

A breadth of 3 (``data/real/multi7``) is a smoke test of the plumbing, not
a breadth result: the report says so.  The C7 report
(:mod:`iap.research.breadth_report`) joins the book's numbers with the
breadth arithmetic, effective bets, break-even size and net Sharpe at size.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from iap.alpha.base import EPS
from iap.alpha.cross_sectional import EQ11CrossSectionalReversal
from iap.alpha.data import asof_to_grid, load_features, make_grid
from iap.backtest.costs import CostModel
from iap.portfolio.covariance import ewma_covariance
from iap.portfolio.exposures import SECTOR_UNKNOWN, build_exposure_matrix, load_sectors
from iap.portfolio.optimizer import Constraints, ImpactTerm, solve
from iap.validation.ledger import ExperimentLedger

_REPO = Path(__file__).resolve().parents[4]
NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S
DEFAULT_OUT = _REPO / "research" / "breadth" / "book"
SCORES = ("eq11", "eq10_beta_gap")
WEIGHT_MAPS = ("linear", "deciles")
IMPACT_SOURCES = ("cost_model", "calibration", "none")
LEDGER_KIND = "breadth_book"
#: A beta is a slow window feature: the last finite value within this age is used.
BETA_MAX_AGE_NS = 30 * 60 * NS_S
#: Columns the book reads from the feature store.
FEATURE_COLUMNS = (
    "exchange_ts",
    "mid_price_v1",
    "spread_bps_v1",
    "ret_log_1m_v1",
    "ref_ret_1m_v1",
    "beta_w5m_v1",
)


# ------------------------------------------------------------- config --


@dataclass
class BookConfig:
    """Every knob of a book run (serialised into the ledger look)."""

    score: str = "eq11"
    weights: str = "linear"
    decile_q: float = 0.1
    rebalance_ns: int = 300 * NS_S
    max_age_ns: int = 60 * NS_S
    exec_delay_ns: int = 1 * NS_S
    notional: float = 1_000_000.0
    alpha_bps: float = 5.0
    risk_aversion: float = 1000.0
    cov_lam: float = 0.94
    cov_init: int = 20
    cov_shrinkage: str | None = None
    w_max: float = 0.10
    gross_cap: float = 1.0
    dollar_neutral: bool = True
    beta_neutral: bool = True
    sector_neutral: bool = True
    neutral_bound: float = 0.0
    participation_cap: float = 0.01  # fraction of ADV per name per rebalance
    turnover_cap: float = 0.5  # L1 weight traded per rebalance
    impact: str = "cost_model"
    impact_coeff: float | None = None  # calibration: bps per (% ADV) ** exponent
    impact_exponent: float | None = None
    min_names: int = 4
    flatten_at_session_end: bool = True
    solver_iters: int = 20
    projection: str = "dykstra"
    dykstra_tol: float = 1e-9
    dykstra_max_iter: int = 30
    feas_tol: float = 1e-6

    def validate(self) -> None:
        if self.score not in SCORES:
            raise ValueError(f"score must be one of {SCORES}")
        if self.weights not in WEIGHT_MAPS:
            raise ValueError(f"weights must be one of {WEIGHT_MAPS}")
        if self.impact not in IMPACT_SOURCES:
            raise ValueError(f"impact must be one of {IMPACT_SOURCES}")
        if self.impact == "calibration" and (
            self.impact_coeff is None or self.impact_exponent is None
        ):
            raise ValueError("impact='calibration' needs impact_coeff and impact_exponent")
        if not (0.0 < self.decile_q <= 0.5):
            raise ValueError("decile_q must lie in (0, 0.5]")
        if self.rebalance_ns <= 0 or self.max_age_ns <= 0 or self.exec_delay_ns < 0:
            raise ValueError("rebalance_ns/max_age_ns must be > 0, exec_delay_ns >= 0")
        if not (math.isfinite(self.notional) and self.notional > 0):
            raise ValueError("notional must be finite and > 0")
        if self.min_names < 2:
            raise ValueError("min_names must be >= 2")
        if self.w_max <= 0 or self.gross_cap <= 0:
            raise ValueError("w_max and gross_cap must be > 0")
        if self.participation_cap <= 0 or self.turnover_cap <= 0:
            raise ValueError("participation_cap and turnover_cap must be > 0")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Instrument:
    instrument_id: int
    symbol: str
    asset_class: str
    adv_shares: float
    lot_size: int
    tick_size: float


# ---------------------------------------------------------- scoring --


def demean_studentise(mat: np.ndarray, min_names: int) -> np.ndarray:
    """Cross-sectionally demeaned and studentised columns (NaN-aware; a
    column with fewer than ``min_names`` finite entries is all NaN)."""
    fin = np.isfinite(mat)
    n = fin.sum(axis=0)
    denom = np.maximum(n, 1)
    vals = np.where(fin, mat, 0.0)
    mean = vals.sum(axis=0) / denom
    var = np.where(fin, (mat - mean[None, :]) ** 2, 0.0).sum(axis=0) / denom
    sig = (mat - mean[None, :]) / (np.sqrt(var)[None, :] + EPS)
    sig[:, n < min_names] = np.nan
    sig[~fin] = np.nan
    return sig


def score_eq11(inputs: Mapping[str, np.ndarray], min_names: int) -> np.ndarray:
    """The EQ11 transform on the grid-sampled 1-minute log return."""
    model = EQ11CrossSectionalReversal()
    model.MIN_NAMES = min_names
    return model.grid_signals(inputs["ret_log_1m_v1"])


def score_eq10_beta_gap(inputs: Mapping[str, np.ndarray], min_names: int) -> np.ndarray:
    """Demeaned EQ10: ``beta * ref_ret_1m - ret_log_1m`` (a name that has
    not matched the reference move is expected to catch up), demeaned and
    studentised; a missing beta reads as 1."""
    beta = np.where(np.isfinite(inputs["beta_w5m_v1"]), inputs["beta_w5m_v1"], 1.0)
    gap = beta * inputs["ref_ret_1m_v1"] - inputs["ret_log_1m_v1"]
    return demean_studentise(gap, min_names)


SCORE_FNS: dict[str, Callable[[Mapping[str, np.ndarray], int], np.ndarray]] = {
    "eq11": score_eq11,
    "eq10_beta_gap": score_eq10_beta_gap,
}


def rank_to_weights(scores: np.ndarray, method: str, decile_q: float = 0.1) -> np.ndarray:
    """Target weights from one cross-section of scores (NaN = not in the
    cross-section, weight 0).  ``linear``: centred rank in [-0.5, 0.5]
    scaled to gross 1; ``deciles``: +1/n_top on the top ``decile_q``
    fraction, -1/n_bot on the bottom (at least one name a side), gross 1.
    Ties are broken by instrument order (stable sort) — deterministic."""
    s = np.asarray(scores, dtype=np.float64)
    out = np.zeros_like(s)
    ok = np.isfinite(s)
    n = int(ok.sum())
    if n < 2:
        return out
    order = np.argsort(s[ok], kind="stable")
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.arange(n, dtype=np.float64)
    if method == "linear":
        r = ranks / (n - 1.0) - 0.5
        w = r / np.sum(np.abs(r))
    elif method == "deciles":
        k = max(1, int(math.floor(decile_q * n)))
        w = np.zeros(n)
        w[ranks >= n - k] = 0.5 / k
        w[ranks < k] = -0.5 / k
    else:
        raise ValueError(f"unknown weight map {method!r}")
    out[ok] = w
    return out


# ------------------------------------------------------- the engine --


@dataclass
class RebalanceRecord:
    ts: int
    day: int
    n_scored: int
    gross_pnl: float
    spread_cost: float
    fee_cost: float
    impact_cost: float
    turnover: float  # L1 weight traded
    traded_notional: float
    gross_exposure: float
    net_exposure: float
    beta_exposure: float
    max_sector_exposure: float
    feasible: bool
    flattened: bool
    rank_ic: float  # Spearman of score vs the next-interval return (NaN when undefined)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.spread_cost - self.fee_cost - self.impact_cost


@dataclass
class BookResult:
    config: BookConfig
    instrument_ids: list[int]
    records: list[RebalanceRecord]
    n_infeasible: int
    n_frozen_name_steps: int
    missing_beta: list[int]
    sector_of: dict[int, str]
    exposure_labels: list[str]
    grid_returns: np.ndarray = field(repr=False)  # (T, N) per-rebalance log returns (NaN ok)
    positions: np.ndarray = field(repr=False)  # (K, N) shares after each rebalance
    prices: np.ndarray = field(repr=False)  # (K, N) marks used
    days: list[int] = field(default_factory=list)

    # -- totals (the identity: net == gross - spread - fee - impact) --
    @property
    def gross_pnl(self) -> float:
        return float(math.fsum(r.gross_pnl for r in self.records))

    @property
    def spread_cost(self) -> float:
        return float(math.fsum(r.spread_cost for r in self.records))

    @property
    def fee_cost(self) -> float:
        return float(math.fsum(r.fee_cost for r in self.records))

    @property
    def impact_cost(self) -> float:
        return float(math.fsum(r.impact_cost for r in self.records))

    @property
    def total_costs(self) -> float:
        return self.spread_cost + self.fee_cost + self.impact_cost

    @property
    def net_pnl(self) -> float:
        return float(math.fsum(r.net_pnl for r in self.records))

    def daily_net_pnl(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for r in self.records:
            out[r.day] = out.get(r.day, 0.0) + r.net_pnl
        return dict(sorted(out.items()))

    def daily_gross_pnl(self) -> dict[int, float]:
        out: dict[int, float] = {}
        for r in self.records:
            out[r.day] = out.get(r.day, 0.0) + r.gross_pnl
        return dict(sorted(out.items()))

    def summary(self) -> dict:
        recs = self.records
        n = len(recs)
        traded = [r for r in recs if r.turnover > 0.0]
        return {
            "n_rebalances": n,
            "n_traded": len(traded),
            "n_days": len(self.days),
            "n_names": len(self.instrument_ids),
            "mean_names_scored": float(np.mean([r.n_scored for r in recs])) if n else 0.0,
            "gross_pnl": self.gross_pnl,
            "spread_cost": self.spread_cost,
            "fee_cost": self.fee_cost,
            "impact_cost": self.impact_cost,
            "total_costs": self.total_costs,
            "net_pnl": self.net_pnl,
            "traded_notional": float(math.fsum(r.traded_notional for r in recs)),
            "mean_turnover": float(np.mean([r.turnover for r in recs])) if n else 0.0,
            "mean_gross_exposure": float(np.mean([r.gross_exposure for r in recs])) if n else 0.0,
            "max_abs_net_exposure": max([abs(r.net_exposure) for r in recs], default=0.0),
            "max_abs_beta_exposure": max([abs(r.beta_exposure) for r in recs], default=0.0),
            "max_abs_sector_exposure": max([r.max_sector_exposure for r in recs], default=0.0),
            "n_infeasible": self.n_infeasible,
            "n_frozen_name_steps": self.n_frozen_name_steps,
            "mean_rank_ic": _nanmean([r.rank_ic for r in recs]),
            "daily_net_pnl": {str(k): v for k, v in self.daily_net_pnl().items()},
            "daily_gross_pnl": {str(k): v for k, v in self.daily_gross_pnl().items()},
        }


def _nanmean(values: Sequence[float]) -> float | None:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    return float(x.mean()) if x.size else None


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 3:
        return float("nan")
    rx = pd.Series(x[ok]).rank().to_numpy()
    ry = pd.Series(y[ok]).rank().to_numpy()
    if rx.std() <= 0 or ry.std() <= 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def impact_cost_dollars(
    dq: np.ndarray, px: np.ndarray, adv_shares: np.ndarray, cfg: BookConfig, cost_model: CostModel
) -> np.ndarray:
    """Impact cost per name of trading ``dq`` shares at ``px`` — the rule the
    optimizer's :class:`ImpactTerm` is built from, so the two agree:
    ``cost_model`` -> its ``impact`` component; ``calibration`` ->
    ``coeff * (100 * |dq| / adv) ** k * 1e-4 * |dq| * px``; ``none`` -> 0."""
    aq = np.abs(np.asarray(dq, dtype=float))
    if cfg.impact == "cost_model":
        comp = cost_model.cost_components(aq, px, np.zeros_like(px), "EQUITY", adv_shares, 1)
        return np.asarray(comp["impact"], dtype=float)
    if cfg.impact == "calibration":
        bps = cfg.impact_coeff * (100.0 * aq / adv_shares) ** cfg.impact_exponent
        return bps * 1e-4 * aq * px
    return np.zeros_like(aq)


def make_impact_term(
    cfg: BookConfig, cost_model: CostModel, adv_notional: np.ndarray
) -> ImpactTerm | None:
    if cfg.impact == "cost_model":
        return ImpactTerm.from_cost_model(cost_model, cfg.notional, adv_notional)
    if cfg.impact == "calibration":
        return ImpactTerm.from_impact_fit(
            cfg.impact_coeff, cfg.impact_exponent, cfg.notional, adv_notional
        )
    return None


def _grid_inputs(
    frames: Mapping[int, pd.DataFrame], ids: Sequence[int], grid: np.ndarray, cfg: BookConfig
) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Grid-sampled inputs (name x grid), execution marks and half spreads."""
    cols = [c for c in FEATURE_COLUMNS if c != "exchange_ts"]
    inputs = {c: np.full((len(ids), len(grid)), np.nan) for c in cols}
    marks = np.full((len(ids), len(grid)), np.nan)
    hs = np.full((len(ids), len(grid)), np.nan)
    exec_grid = grid + cfg.exec_delay_ns
    for k, iid in enumerate(ids):
        df = frames[iid]
        ts = df["exchange_ts"].to_numpy(dtype=np.int64)
        for c in cols:
            if c in df.columns:
                v = df[c].to_numpy(dtype=float)
                if c == "beta_w5m_v1":
                    # the last FINITE beta (the window feature is NaN on many
                    # rows); allowed to be up to BETA_MAX_AGE_NS old
                    fin = np.isfinite(v)
                    inputs[c][k] = asof_to_grid(ts[fin], v[fin], grid, BETA_MAX_AGE_NS)
                else:
                    inputs[c][k] = asof_to_grid(ts, v, grid, cfg.max_age_ns)
        mid = df["mid_price_v1"].to_numpy(dtype=float)
        sp = df["spread_bps_v1"].to_numpy(dtype=float)
        valid = np.isfinite(mid) & (mid > 0) & np.isfinite(sp) & (sp >= 0)
        ts_v, mid_v, sp_v = ts[valid], mid[valid], sp[valid]
        marks[k] = asof_to_grid(ts_v, mid_v, exec_grid, cfg.max_age_ns)
        hs[k] = asof_to_grid(ts_v, sp_v * 1e-4 * mid_v / 2.0, exec_grid, cfg.max_age_ns)
    return inputs, marks, hs


def run_book(
    frames: Mapping[int, pd.DataFrame],
    instruments: Mapping[int, Instrument],
    cost_model: CostModel,
    cfg: BookConfig,
    sectors: Mapping[int, str] | None = None,
    score_fn: Callable[[Mapping[str, np.ndarray], int], np.ndarray] | None = None,
    max_rebalances: int | None = None,
) -> BookResult:
    """Run the book (module docstring).  ``score_fn`` overrides
    ``cfg.score`` (planted-signal tests); ``max_rebalances`` truncates the
    clock (smoke runs)."""
    cfg.validate()
    ids = sorted(i for i in frames if i in instruments and len(frames[i]))
    if len(ids) < cfg.min_names:
        raise ValueError(f"need at least {cfg.min_names} instruments with features, got {len(ids)}")
    n = len(ids)
    grid = make_grid({i: frames[i] for i in ids}, cfg.rebalance_ns)
    inputs, marks, hs = _grid_inputs(frames, ids, grid, cfg)
    fresh_mark = np.isfinite(marks)
    live = fresh_mark.sum(axis=0) >= cfg.min_names
    grid_idx = np.flatnonzero(live)
    if max_rebalances is not None:
        grid_idx = grid_idx[: int(max_rebalances)]
    if grid_idx.size == 0:
        raise ValueError("no rebalance time has enough fresh names")
    fn = score_fn or SCORE_FNS[cfg.score]
    scores_all = fn(inputs, cfg.min_names)
    if scores_all.shape != (n, len(grid)):
        raise ValueError("score_fn must return a (names x grid) matrix")

    adv_shares = np.array([instruments[i].adv_shares for i in ids], dtype=float)
    # ADV in dollars at the first fresh mark of each name (a fixed reference).
    first_px = np.array(
        [marks[k][fresh_mark[k]][0] if fresh_mark[k].any() else np.nan for k in range(n)]
    )
    if not np.all(np.isfinite(first_px)):
        bad = [ids[k] for k in range(n) if not np.isfinite(first_px[k])]
        raise ValueError(f"instruments without a single fresh mark: {bad}")
    adv_notional = adv_shares * first_px
    impact = make_impact_term(cfg, cost_model, adv_notional)
    cm_no_impact = cost_model.with_sqrt_impact(0.0)

    # Betas: last fresh value per name per grid time (missing -> 1, reported).
    beta_grid = inputs["beta_w5m_v1"]
    missing_beta_any: set[int] = set()

    K = grid_idx.size
    days = (grid[grid_idx] // NS_DAY).astype(int)
    records: list[RebalanceRecord] = []
    positions = np.zeros((K, n))
    prices = np.full((K, n), np.nan)
    q = np.zeros(n)  # shares held
    last_px = first_px.copy()  # last known mark per name
    rets = np.full((K, n), np.nan)
    n_infeasible = 0
    n_frozen = 0
    S = None
    ret_hist: list[np.ndarray] = []
    exposure_labels: list[str] = []
    sector_of: dict[int, str] = {}

    for step, g in enumerate(grid_idx):
        t = int(grid[g])
        px = np.where(fresh_mark[:, g], marks[:, g], last_px)
        # 1. mark the book held since the previous rebalance
        gross = float(np.sum(q * (px - last_px))) if step > 0 else 0.0
        if step > 0:
            with np.errstate(divide="ignore", invalid="ignore"):
                r = np.log(px / last_px)
            r[~(fresh_mark[:, g] & np.isfinite(r))] = np.nan
            rets[step] = r
            ret_hist.append(np.where(np.isfinite(r), r, 0.0))
        w_prev = q * px / cfg.notional
        tradable = fresh_mark[:, g]
        n_frozen += int((~tradable).sum())
        flatten = cfg.flatten_at_session_end and (step == K - 1 or days[step + 1] != days[step])
        scores = scores_all[:, g].copy()
        scores[~tradable] = np.nan
        n_scored = int(np.isfinite(scores).sum())
        feasible = True
        if flatten:
            w_new = np.where(tradable, 0.0, w_prev)
        elif len(ret_hist) < cfg.cov_init or n_scored < cfg.min_names:
            w_new = w_prev.copy()  # warm-up / thin cross-section: hold
        else:
            # 2. ranks -> target weights -> alpha
            target = rank_to_weights(scores, cfg.weights, cfg.decile_q)
            peak = float(np.max(np.abs(target)))
            alpha = cfg.alpha_bps * 1e-4 * (target / peak if peak > 0 else target)
            # 3. covariance (causal: returns up to the previous interval)
            S = ewma_covariance(
                np.vstack(ret_hist),
                lam=cfg.cov_lam,
                init_window=cfg.cov_init,
                shrinkage=cfg.cov_shrinkage,
            )
            betas = {
                ids[k]: (float(beta_grid[k, g]) if np.isfinite(beta_grid[k, g]) else None)
                for k in range(n)
            }
            expo = build_exposure_matrix(
                ids,
                betas,
                sectors,
                beta_row=cfg.beta_neutral,
                sector_rows=cfg.sector_neutral,
                bound=cfg.neutral_bound,
            )
            missing_beta_any.update(expo.missing_beta)
            exposure_labels = expo.labels
            sector_of = expo.sector_of
            lo = np.where(tradable, -cfg.w_max, w_prev)
            hi = np.where(tradable, cfg.w_max, w_prev)
            part = np.where(tradable, cfg.participation_cap * adv_notional / cfg.notional, 0.0)
            cons = Constraints(
                w_min=np.minimum(lo, w_prev),
                w_max=np.maximum(hi, w_prev),
                gross_cap=max(cfg.gross_cap, float(np.abs(w_prev).sum())),
                net_cap=cfg.neutral_bound if cfg.dollar_neutral else None,
                participation=part,
                turnover_cap=cfg.turnover_cap,
                currency_matrix=expo.matrix if expo.n_rows else None,
                currency_bounds=expo.bounds if expo.n_rows else None,
            )
            tc = np.where(
                tradable,
                (hs[:, g] + cost_model.equity_taker_fee_per_share) / px,
                0.0,
            )
            tc = np.where(np.isfinite(tc), tc, 0.0)
            res = solve(
                alpha,
                S,
                w_prev,
                cfg.risk_aversion,
                tc,
                cons,
                iters=cfg.solver_iters,
                projection=cfg.projection,
                dykstra_tol=cfg.dykstra_tol,
                dykstra_max_iter=cfg.dykstra_max_iter,
                feas_tol=cfg.feas_tol,
                impact=impact,
                merge_equalities=True,
            )
            feasible = bool(res.feasible)
            if feasible:
                w_new = res.weights
            else:
                n_infeasible += 1
                w_new = w_prev.copy()
        # 4. trade to w_new at px, charge the costs
        q_new = np.where(tradable, w_new * cfg.notional / px, q)
        dq = q_new - q
        comp = cm_no_impact.cost_components(
            dq, px, np.where(tradable, hs[:, g], 0.0), "EQUITY", adv_shares, 1
        )
        spread = float(np.sum(comp["spread"]))
        fee = float(np.sum(comp["fee"]))
        imp = float(np.sum(impact_cost_dollars(dq, px, adv_shares, cfg, cost_model)))
        w_held = q_new * px / cfg.notional
        expo_vals = {}
        if cfg.beta_neutral or cfg.sector_neutral:
            expo_now = build_exposure_matrix(
                ids,
                {
                    ids[k]: (float(beta_grid[k, g]) if np.isfinite(beta_grid[k, g]) else None)
                    for k in range(n)
                },
                sectors,
                beta_row=cfg.beta_neutral,
                sector_rows=cfg.sector_neutral,
                bound=cfg.neutral_bound,
            )
            expo_vals = expo_now.exposures(w_held)
        beta_exp = expo_vals.get("beta", 0.0)
        sec_exp = max(
            [abs(v) for k_, v in expo_vals.items() if k_.startswith("sector:")], default=0.0
        )
        records.append(
            RebalanceRecord(
                ts=t,
                day=int(days[step]),
                n_scored=n_scored,
                gross_pnl=gross,
                spread_cost=spread,
                fee_cost=fee,
                impact_cost=imp,
                turnover=float(np.sum(np.abs(dq * px))) / cfg.notional,
                traded_notional=float(np.sum(np.abs(dq * px))),
                gross_exposure=float(np.sum(np.abs(w_held))),
                net_exposure=float(np.sum(w_held)),
                beta_exposure=float(beta_exp),
                max_sector_exposure=float(sec_exp),
                feasible=feasible,
                flattened=bool(flatten),
                rank_ic=float("nan"),
            )
        )
        positions[step] = q_new
        prices[step] = px
        q = q_new
        last_px = px
    # rank IC of the score used at step k against the return realised over (k, k+1]
    for step in range(K - 1):
        g = grid_idx[step]
        records[step].rank_ic = _spearman(scores_all[:, g], rets[step + 1])
    return BookResult(
        config=cfg,
        instrument_ids=ids,
        records=records,
        n_infeasible=n_infeasible,
        n_frozen_name_steps=n_frozen,
        missing_beta=sorted(missing_beta_any),
        sector_of=sector_of,
        exposure_labels=exposure_labels,
        grid_returns=rets,
        positions=positions,
        prices=prices,
        days=sorted(set(int(d) for d in days)),
    )


# ------------------------------------------------------------ inputs --


def load_instruments(path: str | Path) -> dict[int, Instrument]:
    """EQUITY / ETF rows of an ``instruments.json`` (FX is not a book name)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[int, Instrument] = {}
    for row in doc["instruments"]:
        if row.get("asset_class") not in ("EQUITY", "ETF"):
            continue
        adv = row.get("adv")
        if adv is None and row.get("adv_by_session"):
            adv = float(np.mean(list(row["adv_by_session"].values())))
        if adv is None or float(adv) <= 0:
            raise ValueError(f"{path}: instrument {row.get('instrument_id')} has no positive adv")
        out[int(row["instrument_id"])] = Instrument(
            instrument_id=int(row["instrument_id"]),
            symbol=str(row.get("symbol", row["instrument_id"])),
            asset_class=str(row["asset_class"]),
            adv_shares=float(adv),
            lot_size=int(row.get("lot_size", 100)),
            tick_size=float(row.get("tick_size", 0.01)),
        )
    return out


def sectors_for(
    instruments: Mapping[int, Instrument], taxonomy: Mapping[str, str]
) -> dict[int, str]:
    """instrument id -> sector, for the symbols the taxonomy names (a
    taxonomy value of ``"unknown"`` is the :data:`SECTOR_UNKNOWN` bucket)."""
    out: dict[int, str] = {}
    for iid, inst in instruments.items():
        if inst.symbol in taxonomy:
            s = taxonomy[inst.symbol]
            out[iid] = SECTOR_UNKNOWN if s == "unknown" else s
    return out


def resolve_dataset(
    dataset_dir: Path | None, features_dir: Path | None, config_dir: Path | None
) -> tuple[Path, Path, Path, str]:
    """``(features_dir, instruments.json, execution.json, dataset_version)``
    for a dataset directory (``<dir>/features``, ``<dir>/configs``,
    ``<dir>/dataset.json``) or the repo defaults."""
    if dataset_dir is not None:
        fdir = features_dir or dataset_dir / "features"
        cdir = config_dir or dataset_dir / "configs"
        manifest = dataset_dir / "dataset.json"
        version = None
        if manifest.exists():
            version = json.loads(manifest.read_text(encoding="utf-8")).get("dataset_version")
        if version is None:
            from iap.experiment.tracker import data_version

            version = data_version(dataset_dir) if (dataset_dir / "data").exists() else "unknown"
    else:
        fdir = features_dir or _REPO / "data" / "features"
        cdir = config_dir or _REPO / "configs"
        from iap.experiment.tracker import data_version

        version = data_version(_REPO)
    inst = cdir / "instruments" / "instruments.json"
    if not inst.exists():
        inst = _REPO / "configs" / "instruments" / "instruments.json"
    exe = cdir / "execution" / "execution.json"
    if not exe.exists():
        exe = _REPO / "configs" / "execution" / "execution.json"
    return Path(fdir), inst, exe, str(version)


def calibration_impact(
    path: str | Path, shape: str, horizon: str | None = None
) -> tuple[float, float]:
    """``(coeff, exponent)`` of a calibration's ``impact.residual`` fit in
    :func:`iap.execution.calibration.apply_impact_fit` units."""
    from iap.execution.calibration import load_calibration
    from iap.execution.config import ExecConfig

    cal = load_calibration(path)
    if cal is None:
        raise ValueError(f"no calibration at {path}")
    from iap.execution.calibration import apply_impact_fit

    cfg = apply_impact_fit(ExecConfig(), cal, shape=shape, horizon=horizon)
    return float(cfg.impact_coeff_bps_per_pct_adv), float(cfg.impact_exponent)


# --------------------------------------------------------------- CLI --


def _relative(path: Path) -> str:
    """``path`` relative to the checkout when it lies inside it (so a report
    carries no machine-specific prefix), else as given."""
    p = Path(path).resolve()
    try:
        return p.relative_to(_REPO.resolve()).as_posix()
    except ValueError:
        return str(path)


def _parse_sizes(text: str) -> tuple[float, ...]:
    out = tuple(float(x) for x in text.split(",") if x.strip())
    if not out or any(not (math.isfinite(x) and x > 0) for x in out):
        raise ValueError("--sizes must be positive multiples of the notional")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--dataset-dir", type=Path, default=None)
    src.add_argument("--features-dir", type=Path, default=None)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--tag", default=None, help="file name tag (default: dataset dir name / synthetic)"
    )
    ap.add_argument("--score", choices=SCORES, default="eq11")
    ap.add_argument("--weights", choices=WEIGHT_MAPS, default="linear")
    ap.add_argument("--decile-q", type=float, default=0.1)
    ap.add_argument("--rebalance-min", type=float, default=5.0)
    ap.add_argument("--max-age-s", type=float, default=60.0)
    ap.add_argument("--exec-delay-s", type=float, default=1.0)
    ap.add_argument("--notional", type=float, default=1_000_000.0)
    ap.add_argument("--alpha-bps", type=float, default=5.0)
    ap.add_argument("--risk-aversion", type=float, default=1000.0)
    ap.add_argument("--w-max", type=float, default=0.10)
    ap.add_argument("--gross-cap", type=float, default=1.0)
    ap.add_argument("--participation-cap", type=float, default=0.01)
    ap.add_argument("--turnover-cap", type=float, default=0.5)
    ap.add_argument("--no-dollar-neutral", action="store_true")
    ap.add_argument("--no-beta-neutral", action="store_true")
    ap.add_argument("--no-sector-neutral", action="store_true")
    ap.add_argument("--sectors", type=Path, default=None, help="sector taxonomy JSON")
    ap.add_argument("--impact", choices=IMPACT_SOURCES, default="cost_model")
    ap.add_argument("--calibration", type=Path, default=None)
    ap.add_argument("--impact-shape", choices=("linear", "sqrt", "power"), default="sqrt")
    ap.add_argument("--impact-horizon", default=None)
    ap.add_argument("--cost-multiplier", type=float, default=1.0)
    ap.add_argument("--min-names", type=int, default=4)
    ap.add_argument("--solver-iters", type=int, default=20)
    ap.add_argument("--sizes", default="0.25,0.5,1,2,4", help="notional multiples for the report")
    ap.add_argument("--max-rebalances", type=int, default=None)
    ap.add_argument("--ledger", type=Path, default=_REPO / "research" / "experiments.json")
    ap.add_argument("--no-ledger", action="store_true", help="tests only: record no look")
    ap.add_argument("--allow-holdout", action="store_true")
    ap.add_argument("--no-timestamp", action="store_true")
    args = ap.parse_args(argv)

    if args.dataset_dir is not None and "holdout" in str(args.dataset_dir).lower():
        if not args.allow_holdout:
            print(
                f"{args.dataset_dir}: looks like a holdout dataset; the book is in-sample only "
                "(--allow-holdout to override)",
                file=sys.stderr,
            )
            return 2
    fdir, inst_path, exe_path, version = resolve_dataset(
        args.dataset_dir, args.features_dir, args.config_dir
    )
    cfg = BookConfig(
        score=args.score,
        weights=args.weights,
        decile_q=args.decile_q,
        rebalance_ns=int(round(args.rebalance_min * 60 * NS_S)),
        max_age_ns=int(round(args.max_age_s * NS_S)),
        exec_delay_ns=int(round(args.exec_delay_s * NS_S)),
        notional=args.notional,
        alpha_bps=args.alpha_bps,
        risk_aversion=args.risk_aversion,
        w_max=args.w_max,
        gross_cap=args.gross_cap,
        dollar_neutral=not args.no_dollar_neutral,
        beta_neutral=not args.no_beta_neutral,
        sector_neutral=not args.no_sector_neutral,
        participation_cap=args.participation_cap,
        turnover_cap=args.turnover_cap,
        impact=args.impact,
        min_names=args.min_names,
        solver_iters=args.solver_iters,
    )
    if cfg.impact == "calibration":
        if args.calibration is None:
            ap.error("--impact calibration needs --calibration PATH")
        coeff, k = calibration_impact(args.calibration, args.impact_shape, args.impact_horizon)
        cfg = replace(cfg, impact_coeff=coeff, impact_exponent=k)
    instruments = load_instruments(inst_path)
    frames = load_features(
        fdir, list(instruments), columns=lambda cols: [c for c in cols if c in FEATURE_COLUMNS]
    )
    taxonomy_doc = load_sectors(args.sectors) if (cfg.sector_neutral or args.sectors) else None
    sectors = sectors_for(instruments, taxonomy_doc["sectors"]) if taxonomy_doc else None
    cost_model = CostModel.load(exe_path, multiplier=args.cost_multiplier)
    sizes = _parse_sizes(args.sizes)

    from iap.research.breadth_report import breadth_report, render_markdown, sanitise

    def runner(notional: float):
        return run_book(
            frames,
            instruments,
            cost_model,
            replace(cfg, notional=notional),
            sectors=sectors,
            max_rebalances=args.max_rebalances,
        )

    base = runner(cfg.notional)
    report = breadth_report(base, runner, sizes, cost_model, instruments)
    tag = args.tag or (args.dataset_dir.name if args.dataset_dir else "synthetic")
    report["tag"] = tag
    report["dataset"] = {
        "features_dir": _relative(fdir),
        "instruments": _relative(inst_path),
        "dataset_version": version,
        "smoke_test": len(base.instrument_ids) < 10,
    }
    report["sectors"] = (
        {"source": taxonomy_doc.get("source"), "as_of": taxonomy_doc.get("as_of")}
        if taxonomy_doc
        else None
    )
    report["ledger"] = None
    if not args.no_ledger:
        ledger = ExperimentLedger(args.ledger, dataset_version=version)
        looks = ledger.record(
            cfg.score.upper(),
            LEDGER_KIND,
            config={"book": cfg.to_dict(), "sizes": list(sizes), "tag": tag},
            result={
                "net_pnl": base.net_pnl,
                "gross_pnl": base.gross_pnl,
                "net_sharpe_daily": report["sharpe"]["net_daily_annualised"],
            },
        )
        ledger.save()
        report["ledger"] = {
            "path": _relative(args.ledger),
            "total_experiments": looks,
            "kind": LEDGER_KIND,
        }
    if not args.no_timestamp:
        report["generated"] = datetime.now(UTC).isoformat()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"book_{tag}.json").write_text(
        json.dumps(sanitise(report), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / f"BOOK_{tag}.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {out_dir / f'book_{tag}.json'} and BOOK_{tag}.md")
    s = report["book"]
    print(
        f"names={s['n_names']} rebalances={s['n_rebalances']} gross={s['gross_pnl']:.2f} "
        f"net={s['net_pnl']:.2f} costs={s['total_costs']:.2f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

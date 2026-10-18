"""REL-08 hypothesis screen: the 7 registered one-day variants (K = 7) (v1.18, programme 9.5).

    IAP_RUN_ACTOR=<runner> python -m iap.research.rel08_screen \\
        --dataset-dir data/real/breadth/ds_20190130 [--rule research/v118/rel08_rule_signed.json]
        [--ledger research/experiments.json] [--out research/v118/rel08] [--no-timestamp]

Labelled **"hypothesis screen, one session, no edge claim"**; it cannot promote.
The rule (``research/v118/REL08_CONTINUATION_RULE.md``) is applied by code that
reads every number from the SIGNED JSON copy (:mod:`iap.research.rel08_rule`).

**Gates, in order, before a byte of market data is read** (:func:`main`):

1. the signed rule loads (:func:`~iap.research.rel08_rule.load_signed_rule`:
   unsigned, invalid or mismatched -> exit 2);
2. the holdout registry allows the dataset
   (:func:`iap.research.holdout_registry.guard`): it must be a REAL dataset in
   state ``in_sample`` whose ``dataset_version`` is the one the rule names;
3. the analysis identity the signature bound (scripts, code tree, commit,
   dataset version, family, metrics, thresholds) matches what is about to run,
   and the runner is not the signer (REL-02a);
4. the feature store is an ``xasset_v2`` store (REL-07 reference map).

**Per variant** (:func:`run_screen`): the ledger look (kind ``rel08_screen``,
``data_family`` real, dataset-scoped) is recorded and SAVED before the
variant is evaluated; the result is written onto the same identity afterwards
(no second look).

**Scores** (all point-in-time: only rows at or before the rebalance time t):

* ``vwap_deviation`` (EQ08-style, reversion sign): ``-(mid_t - vwap) / mid_t``,
  vwap the ``traded_volume_w10s_v1``-weighted mean mid over ``(t - h, t]``;
* ``residual_reversion`` (EQ09-style): ``-(r_i - beta_i r_ref)`` over
  ``(t - h, t]``, ``r_ref`` the return of the name's xasset_v2 reference ETF,
  ``beta_i`` the store's ``beta_w5m_v1`` as of t;
* ``etf_catch_up``: ``beta_i r_ref - r_i`` over ``(t - h, t]`` (the same
  expression at 1 / 3 / 5 minutes, catch-up sign);

each demeaned and studentised across names.  The alternative normalisation
(C7) divides the raw score by the name's trailing 30-minute realised
volatility first.  **Label**: the spread-netted forward mid log return over
``[t, t + h]`` (the rule JSON ``implementation.label``); a label whose horizon
crosses the session close is incomplete.

**Books**: :func:`iap.research.breadth_book.run_book` at the registered
economics (the ``book_multi7`` configuration from the JSON ``book`` section),
market-neutral (dollar + beta) or sector-neutral (+ the reference map's
sectors), on the variant's rebalance clock.

**Statistics**: per-rebalance rank IC and net bps, moving-block bootstrap,
Newey-West effective sample size, MDE, Bonferroni lower bounds at
``1 - alpha / K``; criteria C1-C8; the decision map (``underpowered`` /
``stop`` / ``continue``, at most ``max_continue`` continue, the rest
``passed_not_expanded``).  Deterministic: same inputs => same JSON bytes
(``generated`` omitted under ``--no-timestamp``).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha.data import asof_to_grid, make_grid
from iap.backtest.costs import CostModel
from iap.research import breadth_book as bb
from iap.research.rel08_rule import FAMILY, METRICS, Rule, RuleRefused

_REPO = Path(__file__).resolve().parents[4]
NS_S = 1_000_000_000
NS_MIN = 60 * NS_S
NS_DAY = 86_400 * NS_S
LEDGER_KIND = FAMILY
SCREEN_LABEL = "hypothesis screen, one session, no edge claim"
DEFAULT_OUT = _REPO / "research" / "v118" / "rel08"
#: columns read from the store
COLUMNS = (
    "exchange_ts",
    "mid_price_v1",
    "spread_bps_v1",
    "beta_w5m_v1",
    "traded_volume_w10s_v1",
    "ret_log_1m_v1",
    "ref_ret_1m_v1",
)
VOL_WINDOW_MIN = 30
VOL_MIN_OBS = 10
OUTCOMES = ("underpowered", "stop", "continue", "passed_not_expanded")


# ----------------------------------------------------------------- data --


@dataclass
class ScreenData:
    """One session's inputs (in memory; :func:`load_screen_data` builds it)."""

    frames: dict[int, pd.DataFrame]
    instruments: dict[int, bb.Instrument]
    cost_model: CostModel
    ref_of: dict[int, int] = field(default_factory=dict)  # EQUITY id -> reference ETF id
    sector_of: dict[int, str] = field(default_factory=dict)

    def ids(self) -> list[int]:
        return sorted(i for i in self.frames if i in self.instruments and len(self.frames[i]))

    def universe(self, score: str) -> list[int]:
        """The names a score is defined on (rule JSON ``implementation.universe``)."""
        ids = self.ids()
        if score == "vwap_deviation":
            return ids
        return [i for i in ids if self.ref_of.get(i) not in (None, i) and self.ref_of[i] in ids]

    def session_close(self) -> dict[int, int]:
        """day -> last row timestamp of that session across the universe."""
        out: dict[int, int] = {}
        for df in self.frames.values():
            ts = df["exchange_ts"].to_numpy(dtype=np.int64)
            for d in np.unique(ts // NS_DAY):
                last = int(ts[ts // NS_DAY == d].max())
                out[int(d)] = max(out.get(int(d), last), last)
        return out


def _valid_mid(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ts = df["exchange_ts"].to_numpy(dtype=np.int64)
    mid = df["mid_price_v1"].to_numpy(dtype=float)
    sp = (
        df["spread_bps_v1"].to_numpy(dtype=float)
        if "spread_bps_v1" in df.columns
        else np.zeros(len(df))
    )
    ok = np.isfinite(mid) & (mid > 0)
    return ts[ok], mid[ok], np.where(np.isfinite(sp[ok]), np.maximum(sp[ok], 0.0), np.nan)


def mids_at(df: pd.DataFrame, times: np.ndarray, max_age_ns: int) -> np.ndarray:
    """Latest valid mid at or before each time, NaN when older than ``max_age_ns``."""
    ts, mid, _ = _valid_mid(df)
    return asof_to_grid(ts, mid, np.asarray(times, dtype=np.int64), max_age_ns)


def beta_at(df: pd.DataFrame, times: np.ndarray) -> np.ndarray:
    """``beta_w5m_v1``: last finite value at or before t, at most 30 minutes old."""
    if "beta_w5m_v1" not in df.columns:
        return np.full(len(times), np.nan)
    ts = df["exchange_ts"].to_numpy(dtype=np.int64)
    v = df["beta_w5m_v1"].to_numpy(dtype=float)
    fin = np.isfinite(v)
    return asof_to_grid(ts[fin], v[fin], np.asarray(times, dtype=np.int64), bb.BETA_MAX_AGE_NS)


def vwap_at(df: pd.DataFrame, times: np.ndarray, window_ns: int) -> np.ndarray:
    """Volume-weighted mean mid over ``(t - window, t]`` (EQ08 proxy weights)."""
    ts, _, _ = _valid_mid(df)
    mid = df["mid_price_v1"].to_numpy(dtype=float)
    ok = np.isfinite(mid) & (mid > 0)
    w = (
        df["traded_volume_w10s_v1"].to_numpy(dtype=float)[ok]
        if "traded_volume_w10s_v1" in df.columns
        else np.zeros(int(ok.sum()))
    )
    w = np.where(np.isfinite(w), np.maximum(w, 0.0), 0.0)
    cw = np.concatenate([[0.0], np.cumsum(w)])
    cpw = np.concatenate([[0.0], np.cumsum(w * mid[ok])])
    t = np.asarray(times, dtype=np.int64)
    j1 = np.searchsorted(ts, t, side="right")
    j0 = np.searchsorted(ts, t - window_ns, side="right")
    den = cw[j1] - cw[j0]
    num = cpw[j1] - cpw[j0]
    return np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)


def trailing_vol(df: pd.DataFrame, times: np.ndarray, max_age_ns: int) -> np.ndarray:
    """Std of 1-minute mid log returns over the trailing 30 minutes."""
    t = np.asarray(times, dtype=np.int64)
    lags = np.arange(VOL_WINDOW_MIN, -1, -1, dtype=np.int64) * NS_MIN
    m = np.vstack([mids_at(df, t - lag, max_age_ns) for lag in lags])
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(m), axis=0)
    fin = np.isfinite(r)
    n = fin.sum(axis=0)
    rz = np.where(fin, r, 0.0)
    mean = rz.sum(axis=0) / np.maximum(n, 1)
    var = np.where(fin, (rz - mean) ** 2, 0.0).sum(axis=0) / np.maximum(n - 1, 1)
    sd = np.sqrt(var)
    return np.where((n >= VOL_MIN_OBS) & (sd > 0), sd, np.nan)


def raw_scores(
    data: ScreenData,
    ids: Sequence[int],
    score: str,
    horizon_ns: int,
    times: np.ndarray,
    max_age_ns: int,
) -> tuple[np.ndarray, int]:
    """(names x times) raw score before normalisation; second value = beta-missing count."""
    out = np.full((len(ids), len(times)), np.nan)
    missing_beta = 0
    for k, iid in enumerate(ids):
        df = data.frames[iid]
        m1 = mids_at(df, times, max_age_ns)
        if score == "vwap_deviation":
            vw = vwap_at(df, times, horizon_ns)
            out[k] = np.where(np.isfinite(vw) & (m1 > 0), -(m1 - vw) / m1, np.nan)
            continue
        m0 = mids_at(df, times - horizon_ns, max_age_ns)
        ref = data.frames[data.ref_of[iid]]
        r1 = mids_at(ref, times, max_age_ns)
        r0 = mids_at(ref, times - horizon_ns, max_age_ns)
        with np.errstate(divide="ignore", invalid="ignore"):
            ri = np.log(m1 / m0)
            rr = np.log(r1 / r0)
        beta = beta_at(df, times)
        missing_beta += int((~np.isfinite(beta)).sum())
        beta = np.where(np.isfinite(beta), beta, 1.0)
        # residual reversion -(r_i - beta r_ref) and catch-up beta r_ref - r_i coincide
        out[k] = beta * rr - ri
    return out, missing_beta


def compute_scores(
    data: ScreenData,
    ids: Sequence[int],
    score: str,
    horizon_ns: int,
    times: np.ndarray,
    max_age_ns: int,
    min_names: int,
    alt: bool = False,
) -> tuple[np.ndarray, int]:
    """Normalised scores (names x times): demeaned, studentised; ``alt`` divides
    by the trailing realised volatility first (C7)."""
    raw, mb = raw_scores(data, ids, score, horizon_ns, times, max_age_ns)
    if alt:
        vol = np.vstack([trailing_vol(data.frames[i], times, max_age_ns) for i in ids])
        raw = raw / vol
    return bb.demean_studentise(raw, min_names), mb


def compute_labels(
    data: ScreenData, ids: Sequence[int], horizon_ns: int, times: np.ndarray, max_age_ns: int
) -> np.ndarray:
    """Spread-netted forward log return over ``[t, t + h]``; NaN when incomplete
    (crosses the session close) or a mid is stale."""
    close = data.session_close()
    t = np.asarray(times, dtype=np.int64)
    end_ok = np.array([t_ + horizon_ns <= close.get(int(t_ // NS_DAY), -1) for t_ in t])
    out = np.full((len(ids), len(t)), np.nan)
    for k, iid in enumerate(ids):
        ts, mid, sp = _valid_mid(data.frames[iid])
        m0 = asof_to_grid(ts, mid, t, max_age_ns)
        m1 = asof_to_grid(ts, mid, t + horizon_ns, max_age_ns)
        s0 = asof_to_grid(ts, sp, t, max_age_ns)
        s1 = asof_to_grid(ts, sp, t + horizon_ns, max_age_ns)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.log(m1 / m0)
        cost = (s0 + s1) * 1e-4 / 2.0
        net = np.sign(r) * np.maximum(np.abs(r) - cost, 0.0)
        out[k] = np.where(end_ok & np.isfinite(net), net, np.nan)
    return out


# ----------------------------------------------------------- statistics --


def block_length(rebalance_min: float, block_minutes: float) -> int:
    return max(1, int(round(block_minutes / rebalance_min)))


def mbb_means(x: np.ndarray, block: int, draws: int, seed: int) -> np.ndarray:
    """Moving-block bootstrap distribution of the mean of ``x``."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if n == 0:
        return np.full(draws, np.nan)
    b = min(block, n)
    nb = int(math.ceil(n / b))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - b + 1, size=(draws, nb))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(draws, -1)[:, :n]
    return x[idx].mean(axis=1)


def newey_west_inflation(x: np.ndarray, lag: int) -> float:
    """``1 + 2 sum_{l<=lag} (1 - l/(lag+1)) rho_l`` (Bartlett weights)."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if n < 3:
        return float("nan")
    d = x - x.mean()
    v = float(np.dot(d, d)) / n
    if v <= 0:
        return 1.0
    s = 0.0
    for lg in range(1, min(lag, n - 1) + 1):
        rho = float(np.dot(d[lg:], d[:-lg])) / n / v
        s += (1.0 - lg / (lag + 1.0)) * rho
    return 1.0 + 2.0 * s


def series_stats(x: np.ndarray, block: int, rule: Rule) -> dict[str, float]:
    """Mean, bootstrap SE, one-sided ``1 - alpha/K`` lower bound, ESS, MDE."""
    st = rule.section("statistics")
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = int(x.size)
    nan = float("nan")
    if n < 2:
        return {"n": n, "mean": float(x.mean()) if n else nan, "se": nan, "lower": nan,
                "ess": float(n), "nw_inflation": nan, "mde": nan}  # fmt: skip
    means = mbb_means(x, block, int(st["draws"]), int(st["seed"]))
    se = float(np.std(means, ddof=1))
    lower = float(np.quantile(means, 1.0 - rule.level))
    infl = newey_west_inflation(x, block)
    floor = float(st["ess_inflation_floor"])
    ess = n / max(infl if math.isfinite(infl) else floor, floor)
    return {
        "n": n,
        "mean": float(x.mean()),
        "se": se,
        "lower": lower,
        "ess": float(ess),
        "nw_inflation": float(infl),
        "mde": float(st["mde_z_sum"]) * se,
    }


# --------------------------------------------------------------- books --


def book_config(rule: Rule, variant: Mapping[str, Any], min_names: int) -> bb.BookConfig:
    b = rule.section("book")
    reb = float(variant["rebalance_min"])
    return bb.BookConfig(
        weights=str(b["weights"]),
        rebalance_ns=int(round(reb * NS_MIN)),
        max_age_ns=int(round(float(b["max_age_s"]) * NS_S)),
        exec_delay_ns=int(round(float(b["exec_delay_s"]) * NS_S)),
        notional=float(b["notional"]),
        alpha_bps=float(b["alpha_bps"]),
        risk_aversion=float(b["risk_aversion"]),
        cov_lam=float(b["cov_lam"]),
        cov_init=max(2, int(round(float(b["cov_warmup_min"]) / reb))),
        w_max=float(b["w_max"]),
        gross_cap=float(b["gross_cap"]),
        dollar_neutral=bool(b.get("dollar_neutral", True)),
        beta_neutral=bool(b.get("beta_neutral", True)),
        sector_neutral=variant["book"] == "sector_neutral",
        participation_cap=float(b["participation_cap"]),
        turnover_cap=float(b["turnover_cap"]),
        impact=str(b["impact"]),
        min_names=min_names,
        flatten_at_session_end=bool(b["flatten_at_session_end"]),
        solver_iters=int(b["solver_iters"]),
    )


def decision_series(
    res: bb.BookResult, spread_mult: float = 1.0, fee_mult: float = 1.0, impact_mult: float = 1.0
) -> dict[int, float]:
    """decision ts -> net P&L of that decision (module docs; rule ``net_series``)."""
    recs = res.records
    out: dict[int, float] = {}
    prev: int | None = None
    for k, r in enumerate(recs):
        cost = spread_mult * r.spread_cost + fee_mult * r.fee_cost + impact_mult * r.impact_cost
        if r.flattened:
            if prev is not None:
                out[prev] -= cost
            prev = None
            continue
        nxt = recs[k + 1].gross_pnl if k + 1 < len(recs) and recs[k + 1].day == r.day else 0.0
        out[r.ts] = nxt - cost
        prev = r.ts
    return out


def name_contributions(res: bb.BookResult) -> dict[int, float]:
    """Per-name net contribution: gross minus the traded-notional share of each
    rebalance's costs."""
    q, px = res.positions, res.prices
    contrib = np.zeros(q.shape[1])
    prev_q = np.zeros(q.shape[1])
    for k, r in enumerate(res.records):
        if k + 1 < len(res.records) and res.records[k + 1].day == r.day:
            contrib += np.nan_to_num(q[k] * (px[k + 1] - px[k]))
        traded = np.abs((q[k] - prev_q) * np.nan_to_num(px[k]))
        tot = traded.sum()
        if tot > 0:
            contrib -= (r.spread_cost + r.fee_cost + r.impact_cost) * traded / tot
        prev_q = q[k]
    return {iid: float(contrib[j]) for j, iid in enumerate(res.instrument_ids)}


def _run(
    data: ScreenData, ids: Sequence[int], cfg: bb.BookConfig, scores: np.ndarray
) -> bb.BookResult:
    frames = {i: data.frames[i] for i in ids}
    inst = {i: data.instruments[i] for i in ids}
    sectors = {i: data.sector_of[i] for i in ids if i in data.sector_of} or None
    return bb.run_book(
        frames,
        inst,
        data.cost_model,
        cfg,
        sectors=sectors if cfg.sector_neutral else None,
        score_fn=lambda _inputs, _mn: scores,
    )


def _grid(data: ScreenData, ids: Sequence[int], cfg: bb.BookConfig) -> np.ndarray:
    return make_grid({i: data.frames[i] for i in ids}, cfg.rebalance_ns)


def _mean_at(series: Mapping[int, float], ts: Sequence[int]) -> float:
    vals = [series[t] for t in ts if t in series]
    return float(np.mean(vals)) if vals else float("nan")


def evaluate_variant(data: ScreenData, rule: Rule, variant: Mapping[str, Any]) -> dict[str, Any]:
    """Every statistic of one variant (raw; :func:`criteria` judges it)."""
    suff = rule.section("sufficiency")
    costs = rule.section("costs")
    th = rule.thresholds
    notional = float(rule.section("book")["notional"])
    names_min = int(suff["names_per_rebalance_min"])
    max_age = int(round(float(suff["score_max_age_s"]) * NS_S))
    h = int(round(float(variant["horizon_min"]) * NS_MIN))
    reb = float(variant["rebalance_min"])
    block = block_length(reb, float(rule.section("statistics")["block_minutes"]))
    ids = data.universe(str(variant["score"]))
    base: dict[str, Any] = {"id": variant["id"], "variant": dict(variant), "n_names": len(ids)}
    if len(ids) < max(names_min, 2):
        return {**base, "error": f"universe has {len(ids)} names < {names_min}", "n_usable": 0}
    cfg = book_config(rule, variant, names_min)
    grid = _grid(data, ids, cfg)
    scores, missing_beta = compute_scores(
        data, ids, str(variant["score"]), h, grid, max_age, names_min
    )
    labels = compute_labels(data, ids, h, grid, max_age)
    both = np.isfinite(scores) & np.isfinite(labels)
    usable_cols = both.sum(axis=0) >= names_min
    usable_ts = [int(t) for t in grid[usable_cols]]
    # sufficiency (rule section 2)
    n_usable = int(usable_cols.sum())
    frac = both[:, usable_cols].mean(axis=1) if n_usable else np.zeros(len(ids))
    names_ok = int((frac >= float(suff["names_usable_frac"])).sum())
    if n_usable:
        gaps = np.diff(np.concatenate([[grid[0] - cfg.rebalance_ns], grid[usable_cols]]))
        max_gap_min = float(gaps.max()) / NS_MIN
    else:
        max_gap_min = float("inf")
    ic = [bb._spearman(scores[:, g], labels[:, g]) for g in np.flatnonzero(usable_cols)]
    out: dict[str, Any] = {
        **base,
        "block_length": block,
        "n_rebalances": len(grid),
        "n_usable": n_usable,
        "usable_floor": int(suff["usable_rebalances_min"][str(int(reb))]),
        "names_usable_80pct": names_ok,
        "max_gap_min": max_gap_min,
        "missing_beta_cells": missing_beta,
        "rank_ic": series_stats(np.array(ic), block, rule),
    }
    try:
        res = _run(data, ids, cfg, scores)
    except ValueError as exc:
        return {**out, "error": f"book: {exc}"}
    to_bps = 1e4 / notional
    net = decision_series(res)
    net_u = np.array([net[t] * to_bps for t in usable_ts if t in net])
    out["net_bps"] = series_stats(net_u, block, rule)
    out["book"] = {
        "n_infeasible": res.n_infeasible,
        "gross_pnl": res.gross_pnl,
        "spread_cost": res.spread_cost,
        "fee_cost": res.fee_cost,
        "impact_cost": res.impact_cost,
        "net_pnl": res.net_pnl,
        "mean_turnover": float(np.mean([r.turnover for r in res.records])),
        "max_abs_net_exposure": max(abs(r.net_exposure) for r in res.records),
        "max_abs_beta_exposure": max(abs(r.beta_exposure) for r in res.records),
        "max_abs_sector_exposure": max(r.max_sector_exposure for r in res.records),
        "cov_init": cfg.cov_init,
    }
    cons = decision_series(
        res,
        float(costs["conservative_spread_mult"]),
        float(costs["conservative_fee_mult"]),
        float(costs["conservative_impact_mult"]),
    )
    out["conservative_net_bps"] = _mean_at(cons, usable_ts) * to_bps
    contrib = name_contributions(res)
    k_rm = int(th["C6_names_removed"])
    top = sorted(contrib, key=lambda i: (-abs(contrib[i]), i))[:k_rm]
    out["influential_names"] = [
        {"instrument_id": i, "symbol": data.instruments[i].symbol, "net_pnl": contrib[i]}
        for i in top
    ]
    keep = [i for i in ids if i not in top]
    out["influential_removed_net_bps"] = float("nan")
    if len(keep) >= names_min:
        cfg6 = book_config(rule, variant, names_min)
        g6 = _grid(data, keep, cfg6)
        s6, _ = compute_scores(data, keep, str(variant["score"]), h, g6, max_age, names_min)
        try:
            r6 = _run(data, keep, cfg6, s6)
            out["influential_removed_net_bps"] = _mean_at(decision_series(r6), usable_ts) * to_bps
        except ValueError:
            pass
    s7, _ = compute_scores(data, ids, str(variant["score"]), h, grid, max_age, names_min, alt=True)
    try:
        r7 = _run(data, ids, cfg, s7)
        out["alt_normalisation_net_bps"] = _mean_at(decision_series(r7), usable_ts) * to_bps
    except ValueError:
        out["alt_normalisation_net_bps"] = float("nan")
    return out


# ------------------------------------------------------------ decision --


def _retained(x: float, base: float, retention: float, strictly_positive: bool = False) -> bool:
    if not (math.isfinite(x) and math.isfinite(base)) or base == 0:
        return False
    if strictly_positive and x <= 0:
        return False
    return bool(np.sign(x) == np.sign(base) and x * np.sign(base) >= retention * abs(base))


def criteria(stats: Mapping[str, Any], rule: Rule) -> dict[str, bool]:
    """C1-C8 of one variant from its raw statistics (thresholds from the rule)."""
    th = rule.thresholds
    suff = rule.section("sufficiency")
    net = stats.get("net_bps") or {}
    ic = stats.get("rank_ic") or {}

    def f(d: Mapping[str, Any], k: str) -> float:
        v = d.get(k)
        return float(v) if isinstance(v, int | float) and v is not None else float("nan")

    base = f(net, "mean")
    c1 = (
        "error" not in stats
        and stats.get("n_usable", 0) >= stats.get("usable_floor", math.inf)
        and stats.get("names_usable_80pct", 0) >= int(suff["names_usable_min"])
        and stats.get("max_gap_min", math.inf) <= float(suff["max_gap_min"])
    )
    mde = f(net, "mde")
    return {
        "C1": bool(c1),
        "C2": bool(math.isfinite(mde) and mde <= float(th["C2_mde_max_bps"])),
        "C3": bool(
            math.isfinite(base)
            and base >= float(th["C3_net_min_bps"])
            and f(net, "lower") > float(th["C3_lower_min_bps"])
        ),
        "C4": bool(f(net, "ess") >= float(th["C4_ess_min"])),
        "C5": _retained(
            f(stats, "conservative_net_bps"), base, float(th["C5_retention"]), True
        ),
        "C6": _retained(f(stats, "influential_removed_net_bps"), base, float(th["C6_retention"])),
        "C7": _retained(f(stats, "alt_normalisation_net_bps"), base, float(th["C7_retention"])),
        "C8": bool(f(ic, "mean") > float(th["C8_ic_min"]) and f(ic, "lower") > float(th["C8_lower_min"])),
    }  # fmt: skip


def outcome(c: Mapping[str, bool]) -> tuple[str, str]:
    """(outcome, reason) of the decision map (rule section 5), before the cap."""
    if not (c["C1"] and c["C2"] and c["C4"]):
        failed = [k for k in ("C1", "C2", "C4") if not c[k]]
        return "underpowered", "failed " + ",".join(failed)
    if not c["C3"]:
        return "stop", "no net edge detectable at the registered notional (C3)"
    failed = [k for k in ("C5", "C6", "C7", "C8") if not c[k]]
    if failed:
        return "stop", "fragile or cost-sensitive: failed " + ",".join(failed)
    return "continue", "C1-C8 hold"


def decide(results: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    """Criteria and outcome per variant; at most ``max_continue`` continue, ranked
    by the net-bps lower bound (ties by variant order); the rest
    ``passed_not_expanded``."""
    rows = []
    for r in results:
        c = criteria(r, rule)
        o, why = outcome(c)
        rows.append({"id": r["id"], "criteria": c, "outcome": o, "reason": why})
    passing = [k for k, row in enumerate(rows) if row["outcome"] == "continue"]
    cap = int(rule.section("decision")["max_continue"])

    def lb(k: int) -> float:
        v = (results[k].get("net_bps") or {}).get("lower")
        return float(v) if isinstance(v, int | float) else -math.inf

    ranked = sorted(passing, key=lambda k: (-lb(k), k))
    for rank, k in enumerate(ranked):
        if rank >= cap:
            rows[k]["outcome"] = "passed_not_expanded"
            rows[k]["reason"] = f"passed; not among the {cap} highest net-bps lower bounds"
    return rows


# --------------------------------------------------------------- screen --


def look_config(rule: Rule, variant: Mapping[str, Any]) -> dict[str, Any]:
    """The ledger identity of one variant's look."""
    return {
        "variant": dict(variant),
        "rule_message_sha256": rule.message_sha256,
        "rule_document_sha256": rule.doc["rule_document_sha256"],
        "book": dict(rule.section("book")),
        "label": SCREEN_LABEL,
    }


def run_screen(
    data: ScreenData,
    rule: Rule,
    ledger: Any | None = None,
    evaluate: Callable[[ScreenData, Rule, Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate every registered variant; with a ``ledger`` each look is
    recorded and saved BEFORE the variant is evaluated."""
    ev = evaluate or evaluate_variant
    results = []
    looks = []
    for v in rule.variants:
        if ledger is not None:
            total = ledger.record(str(v["id"]), LEDGER_KIND, config=look_config(rule, v))
            ledger.save()
            looks.append({"id": v["id"], "total_after": total})
        results.append(ev(data, rule, v))
        if ledger is not None:
            r = results[-1]
            ledger.record(
                str(v["id"]),
                LEDGER_KIND,
                config=look_config(rule, v),
                result={
                    "net_bps_mean": (r.get("net_bps") or {}).get("mean"),
                    "rank_ic_mean": (r.get("rank_ic") or {}).get("mean"),
                },
            )
            ledger.save()
    decisions = decide(results, rule)
    return {
        "x-version": 1,
        "label": SCREEN_LABEL,
        "session": rule.doc["session"],
        "rule": {
            "path": rule.path and Path(rule.path).name,
            "signed": rule.signed,
            "message_sha256": rule.message_sha256,
            "rule_document_sha256": rule.doc["rule_document_sha256"],
            "K": rule.K,
            "level": rule.level,
            "thresholds": dict(rule.thresholds),
            "metrics": list(METRICS),
        },
        "variants": results,
        "decisions": decisions,
        "summary": {o: [d["id"] for d in decisions if d["outcome"] == o] for o in OUTCOMES},
        "ledger": {"kind": LEDGER_KIND, "data_family": "real", "looks": looks}
        if ledger is not None
        else None,
    }


def sanitise(obj: Any) -> Any:
    """NaN / inf -> None, numpy scalars -> Python (strict JSON)."""
    if isinstance(obj, Mapping):
        return {str(k): sanitise(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [sanitise(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def dumps(report: Mapping[str, Any]) -> str:
    return json.dumps(sanitise(report), indent=2, sort_keys=True, allow_nan=False) + "\n"


# ----------------------------------------------------------- scorecard --


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int | float):
        return f"{v:.{nd}f}" if isinstance(v, float) else str(v)
    return str(v)


def render_scorecard(raw: Mapping[str, Any]) -> str:
    """The scorecard, generated from the raw screen JSON only (no recomputation)."""
    rule = raw["rule"]
    dec = {d["id"]: d for d in raw["decisions"]}
    L: list[str] = [
        f"# REL-08 scorecard: {raw['label']}",
        "",
        f"**{raw['label']}.** Session {raw['session']}. This screen cannot promote; "
        "`continue` permits REL-08b machine time and nothing more.",
        "",
        "## 1. Hypothesis and registration",
        "",
        f"* Family `{FAMILY}`, K = {rule['K']} registered variants; lower bounds at "
        f"{rule['level']:.6f} (Bonferroni).",
        f"* Rule signed: {_fmt(rule['signed'])}; message sha256 `{rule['message_sha256']}`; "
        f"markdown sha256 `{rule['rule_document_sha256']}`.",
        "",
        "## 2. Data",
        "",
        "| variant | names | rebalances | usable | floor | names usable >= 80 % | max gap (min) |",
        "|---|---|---|---|---|---|---|",
    ]
    for v in raw["variants"]:
        L.append(
            f"| {v['id']} | {v.get('n_names')} | {v.get('n_rebalances', 'n/a')} | "
            f"{v.get('n_usable')} | {v.get('usable_floor', 'n/a')} | "
            f"{v.get('names_usable_80pct', 'n/a')} | {_fmt(v.get('max_gap_min'), 1)} |"
        )
    L += [
        "",
        "## 3. Signal (per-rebalance rank IC on the spread-netted label)",
        "",
        "| variant | score | horizon | book | mean IC | lower bound | n |",
        "|---|---|---|---|---|---|---|",
    ]
    for v in raw["variants"]:
        s = v["variant"]
        ic = v.get("rank_ic") or {}
        L.append(
            f"| {v['id']} | {s['score']} | {s['horizon_min']} m / {s['rebalance_min']} m | "
            f"{s['book']} | {_fmt(ic.get('mean'), 4)} | {_fmt(ic.get('lower'), 4)} | "
            f"{ic.get('n', 'n/a')} |"
        )
    L += [
        "",
        "## 4. Net of cost at the registered notional (bps per rebalance)",
        "",
        "| variant | net mean | SE | lower bound | MDE | ESS | gross | costs | net P&L |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for v in raw["variants"]:
        n = v.get("net_bps") or {}
        b = v.get("book") or {}
        costs = None
        if b:
            costs = b["spread_cost"] + b["fee_cost"] + b["impact_cost"]
        L.append(
            f"| {v['id']} | {_fmt(n.get('mean'))} | {_fmt(n.get('se'))} | "
            f"{_fmt(n.get('lower'))} | {_fmt(n.get('mde'))} | {_fmt(n.get('ess'), 1)} | "
            f"{_fmt(b.get('gross_pnl'), 2)} | {_fmt(costs, 2)} | {_fmt(b.get('net_pnl'), 2)} |"
        )
    L += [
        "",
        "## 5. Robustness (C5-C7)",
        "",
        "| variant | conservative costs | influential names removed | alt. normalisation |",
        "|---|---|---|---|",
    ]
    for v in raw["variants"]:
        L.append(
            f"| {v['id']} | {_fmt(v.get('conservative_net_bps'))} | "
            f"{_fmt(v.get('influential_removed_net_bps'))} | "
            f"{_fmt(v.get('alt_normalisation_net_bps'))} |"
        )
    L += [
        "",
        "## 6. Risk and neutrality",
        "",
        "| variant | infeasible solves | max abs net | max abs beta | max abs sector | turnover |",
        "|---|---|---|---|---|---|",
    ]
    for v in raw["variants"]:
        b = v.get("book") or {}
        L.append(
            f"| {v['id']} | {b.get('n_infeasible', 'n/a')} | "
            f"{_fmt(b.get('max_abs_net_exposure'), 6)} | {_fmt(b.get('max_abs_beta_exposure'), 6)} | "
            f"{_fmt(b.get('max_abs_sector_exposure'), 6)} | {_fmt(b.get('mean_turnover'), 4)} |"
        )
    led = raw.get("ledger")
    L += [
        "",
        "## 7. Multiple testing and ledger",
        "",
        f"* One look per variant, kind `{LEDGER_KIND}`, data family real: "
        + (f"{len(led['looks'])} looks recorded before their results." if led else "no ledger."),
        "* One session is one cluster: no day-clustered t is reported and none is implied.",
        "",
        "## 8. Verdict (decision map, rule section 5)",
        "",
        "| variant | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | outcome | reason |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for v in raw["variants"]:
        d = dec[v["id"]]
        c = d["criteria"]
        cells = " | ".join("pass" if c[k] else "fail" for k in sorted(c, key=lambda s: int(s[1:])))
        L.append(f"| {v['id']} | {cells} | **{d['outcome']}** | {d['reason']} |")
    L += [
        "",
        "## 9. What this does not claim",
        "",
        "* No edge claim: one session, in-sample, a hypothesis screen.",
        "* `continue` is permission to spend REL-08b machine time; `stop` and `underpowered` are "
        "final results of the screen.",
        "* The 2025/26 breadth holdout stays unread; promotion needs an untouched confirmation "
        "sample.",
        "",
    ]
    return "\n".join(L)


# ----------------------------------------------------------------- load --


def load_screen_data(dataset_dir: Path, reference_map: Path | None = None) -> ScreenData:
    """Read the store (call only after the gates in :func:`main`)."""
    from iap.alpha.data import load_features
    from iap.features.reference_map import DEFAULT_MAP, FEATURE_IDENTITY, load_reference_map

    fdir, inst_path, exe_path, _ = bb.resolve_dataset(dataset_dir, None, None)
    summary = Path(fdir) / "features_summary.json"
    ident = None
    if summary.is_file():
        ident = (json.loads(summary.read_text(encoding="utf-8")).get("xasset_reference") or {}).get(
            "feature_identity"
        )
    if ident != FEATURE_IDENTITY:
        raise RuleRefused(
            f"{summary}: feature store is not {FEATURE_IDENTITY} (got {ident!r}); rebuild the "
            "features with --xasset-reference symbol_map (REL-07)"
        )
    instruments = bb.load_instruments(inst_path)
    frames = load_features(
        fdir, list(instruments), columns=lambda cols: [c for c in cols if c in COLUMNS]
    )
    rmap = load_reference_map(reference_map or DEFAULT_MAP)
    by_symbol = {inst.symbol: iid for iid, inst in instruments.items()}
    ref_of = {
        iid: by_symbol[rmap.references[inst.symbol]]
        for iid, inst in instruments.items()
        if inst.asset_class == "EQUITY"
        and inst.symbol in rmap.references
        and rmap.references[inst.symbol] in by_symbol
    }
    sector_of = {
        iid: rmap.sectors[inst.symbol]
        for iid, inst in instruments.items()
        if inst.symbol in rmap.sectors
    }
    return ScreenData(
        frames=dict(frames),
        instruments=instruments,
        cost_model=CostModel.load(exe_path),
        ref_of=ref_of,
        sector_of=sector_of,
    )


def gate(dataset_dir: Path, rule_path: Path | None, root: Path) -> tuple[Rule, str]:
    """Gates 1-3 of the module docs; :class:`RuleRefused` / ``HoldoutRefused``."""
    from iap.agents.analysis_identity import IdentityError, require_independent_runner
    from iap.research.holdout_registry import HoldoutRefused, guard
    from iap.research.rel08_rule import check_identity, load_signed_rule

    rule = load_signed_rule(rule_path, root)
    want = str(rule.doc["dataset"]["dataset_version"])
    rec = guard(dataset_dir, purpose=LEDGER_KIND, root=root)
    if rec.get("state") != "in_sample":
        raise HoldoutRefused(
            f"{dataset_dir}: state {rec.get('state')!r}; the screen reads a real in_sample dataset"
        )
    if rec.get("dataset_version") != want:
        raise HoldoutRefused(
            f"{dataset_dir}: dataset_version {rec.get('dataset_version')} is not the rule's {want}"
        )
    check_identity(rule, root, want)
    try:
        require_independent_runner(rule.doc)
    except IdentityError as exc:
        raise RuleRefused(str(exc)) from exc
    return rule, want


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dataset-dir", type=Path, required=True)
    ap.add_argument("--rule", type=Path, default=None, help="signed rule JSON")
    ap.add_argument("--ledger", type=Path, default=_REPO / "research" / "experiments.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--tag", default="ds_20190130")
    ap.add_argument("--no-timestamp", action="store_true")
    args = ap.parse_args(argv)

    from iap.research.holdout_registry import HoldoutRefused
    from iap.validation.ledger import ExperimentLedger

    try:
        rule, version = gate(args.dataset_dir, args.rule, _REPO)
        data = load_screen_data(args.dataset_dir, _REPO / rule.doc["dataset"]["reference_map"])
    except (RuleRefused, HoldoutRefused) as exc:
        print(f"rel08_screen REFUSED: {exc}", file=sys.stderr)
        return 2
    ledger = ExperimentLedger(args.ledger, dataset_version=version, data_family="real")
    report = run_screen(data, rule, ledger)
    report["dataset"] = {"dataset_version": version, "tag": args.tag}
    if not args.no_timestamp:
        report["generated"] = datetime.now(UTC).isoformat()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw_path = out / f"screen_{args.tag}.json"
    with open(raw_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dumps(report))
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    with open(out / f"SCORECARD_{args.tag}.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_scorecard(raw))
    print(f"wrote {raw_path} and SCORECARD_{args.tag}.md: {report['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

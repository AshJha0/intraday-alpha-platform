"""EN5: the E5 report builders (v2.0 U0 stage 5; E0 sections 10, 11 and 13 blocks A-E, the decision tree).

Every builder returns a :class:`Table` with **units** per column, the sample
**counts** it rests on and an **uncertainty** field where one is meaningful;
nothing here changes a decision of the engine.  Family-agnostic: a session
series belongs to one ``(family_id, horizon)`` and the builders take plain
arrays, the ledger's :class:`~iap.portfolio.ledger.SessionResult` rows and
the projection's stage books.

The session series (E0 section 10, C11) counts the three kinds of session:
a *usable session with no trades* enters R_d as zero and is counted; a
*coverage failure* is dropped under R0 section 3 and listed; an *unpriced
position at the close* is usable with its stress valuation and a flag.  The
denominator of every R_d is the registered capital C.

Prohibited Sharpe inflation (section 11) is enforced by :func:`check_no_inflation`:
no omitted no-trade or refused session, no dropped dates other than listed
coverage failures, one denominator C, refused orders uncredited (a refused
order's forecast never enters a P&L column), no annualisation over active
periods only (every statistic is per session).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from iap.core.rng import SplitMix64

__all__ = [
    "SESSION_KINDS",
    "InflationError",
    "SessionRow",
    "SessionSeries",
    "Table",
    "block_a_signal",
    "block_b_portfolio",
    "block_c_execution",
    "block_d_risk_adjusted",
    "block_e_sensitivity",
    "block_f_overfitting",
    "check_no_inflation",
    "decision_tree",
    "economic_screen",
    "retention",
    "session_series",
]

SESSION_KINDS = ("traded", "usable_no_trades", "coverage_failure", "unpriced_at_close")


class InflationError(ValueError):
    """A report violated a prohibited-inflation rule of E0 section 11."""


@dataclass(slots=True)
class Table:
    title: str
    units: dict[str, str]
    counts: dict[str, int]
    rows: list[dict[str, Any]]
    uncertainty: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "units": dict(self.units),
            "counts": dict(self.counts),
            "rows": [dict(r) for r in self.rows],
            "uncertainty": dict(self.uncertainty),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class SessionRow:
    session_id: str
    kind: str
    net_pnl: float = 0.0
    gross_pnl: float = 0.0  # midpoint-reference P&L before any cost
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in SESSION_KINDS:
            raise InflationError(f"session kind must be one of {SESSION_KINDS}, got {self.kind!r}")
        if self.kind == "usable_no_trades" and (self.net_pnl != 0.0 or self.gross_pnl != 0.0):
            raise InflationError("a usable session with no trades has zero P&L")
        if self.kind == "unpriced_at_close" and not self.flags:
            raise InflationError("an unpriced-at-close session carries a failure flag")


@dataclass(slots=True)
class SessionSeries:
    family_id: str
    horizon: str
    capital: float
    r_bps: list[float]  # one per usable session, in session order
    session_ids: list[str]
    dropped: list[str]  # coverage failures, listed
    counts: dict[str, int]
    gross_total: float
    net_total: float
    flags: dict[str, tuple[str, ...]]

    @property
    def n(self) -> int:
        return len(self.r_bps)


def session_series(
    family_id: str, horizon: str, rows: Sequence[SessionRow], capital: float
) -> SessionSeries:
    """R_d = NetPnL_d / C per usable session; no-trade sessions as zero; coverage failures dropped and listed."""
    if not math.isfinite(capital) or capital <= 0:
        raise InflationError("the denominator is the registered capital C > 0")
    ids = [r.session_id for r in rows]
    if len(set(ids)) != len(ids):
        raise InflationError("duplicate session ids")
    counts = {k: 0 for k in SESSION_KINDS}
    r_bps: list[float] = []
    kept: list[str] = []
    dropped: list[str] = []
    flags: dict[str, tuple[str, ...]] = {}
    gross = net = 0.0
    for r in rows:
        counts[r.kind] += 1
        if r.kind == "coverage_failure":
            dropped.append(r.session_id)
            continue
        r_bps.append(1e4 * r.net_pnl / capital)
        kept.append(r.session_id)
        gross += r.gross_pnl
        net += r.net_pnl
        if r.flags:
            flags[r.session_id] = r.flags
    return SessionSeries(
        family_id, horizon, capital, r_bps, kept, dropped, counts, gross, net, flags
    )


def retention(net_total: float, gross_total: float) -> float | None:
    """C7: aggregate net / aggregate gross; ``None`` (the screen fails) when the aggregate gross is not positive."""
    if gross_total <= 0:
        return None
    return net_total / gross_total


def economic_screen(
    series: SessionSeries, *, hurdle_bps: float = 1.0, min_retention: float = 0.5
) -> dict[str, Any]:
    """E0 section 10: E[R_d] >= hurdle under the conservative column with retention >= 50 %; interval reported."""
    r = np.asarray(series.r_bps, dtype=float)
    n = int(r.size)
    mean = float(r.mean()) if n else 0.0
    se = float(r.std(ddof=1) / math.sqrt(n)) if n > 1 else math.inf
    ret = retention(series.net_total, series.gross_total)
    passed = n > 0 and mean >= hurdle_bps and ret is not None and ret >= min_retention
    return {
        "mean_bps": mean,
        "se_bps": se,
        "interval_95_bps": (mean - 1.96 * se, mean + 1.96 * se) if n > 1 else None,
        "retention": ret,
        "retention_defined": ret is not None,
        "hurdle_bps": hurdle_bps,
        "min_retention": min_retention,
        "passed": bool(passed),
        "n_sessions": n,
        "denominator": "registered capital C",
        "column": "conservative",
    }


def check_no_inflation(
    series: SessionSeries,
    *,
    all_session_ids: Sequence[str],
    refused_orders_credited: Sequence[Any] = (),
    denominators: Sequence[float] = (),
    annualised_over_active_only: bool = False,
) -> None:
    """Raise :class:`InflationError` on any prohibited practice of E0 section 11."""
    seen = set(series.session_ids) | set(series.dropped)
    missing = sorted(set(all_session_ids) - seen)
    if missing:
        raise InflationError(f"sessions omitted from the series: {missing}")
    if (
        series.counts["usable_no_trades"]
        and sum(1 for s, r in zip(series.session_ids, series.r_bps, strict=True) if r == 0.0)
        < series.counts["usable_no_trades"]
    ):
        raise InflationError("a no-trade session is not present as zero")
    if refused_orders_credited:
        raise InflationError(
            f"{len(refused_orders_credited)} refused or unfilled orders credited with a forecast return"
        )
    for d in denominators:
        if abs(d - series.capital) > 1e-9 * series.capital:
            raise InflationError(f"more than one denominator: {d} != C = {series.capital}")
    if annualised_over_active_only:
        raise InflationError("annualisation over active periods only")


# --------------------------------------------------------------------------- statistics
def _ic(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 3 or a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _rank(a: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(a, kind="stable"), kind="stable").astype(float)


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """intercept, slope, slope standard error."""
    n = x.size
    if n < 3 or x.std() == 0:
        return float("nan"), float("nan"), float("nan")
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    slope = float(((x - xm) * (y - ym)).sum() / sxx)
    inter = float(ym - slope * xm)
    resid = y - inter - slope * x
    se = math.sqrt(float((resid**2).sum()) / (n - 2) / sxx)
    return inter, slope, se


def _deciles(x: np.ndarray) -> np.ndarray:
    r = _rank(x)
    return np.minimum((r * 10 // max(x.size, 1)).astype(int), 9)


# --------------------------------------------------------------------------- block A
def block_a_signal(
    z: Sequence[float],
    forward_bps: Mapping[str, Sequence[float]],
    *,
    decay_bps: Mapping[int, Sequence[float]] | None = None,
    mu_hat_bps: Mapping[str, Sequence[float]] | None = None,
    coverage: float | None = None,
    quote_age_buckets: Mapping[str, Sequence[float]] | None = None,
    z0: float = 2.0,
) -> Table:
    """Block A: IC and rank IC by horizon, decile and tail-bin conditional returns, legs, decay, calibration, coverage."""
    zz = np.asarray(z, dtype=float)
    rows: list[dict[str, Any]] = []
    counts = {"observations": int(zz.size), "horizons": len(forward_bps)}
    for h in sorted(forward_bps):
        f = np.asarray(forward_bps[h], dtype=float)
        if f.size != zz.size:
            raise ValueError(f"forward returns for {h} do not align with z")
        s = -zz  # S = -z
        row: dict[str, Any] = {
            "horizon": h,
            "ic": _ic(s, f),
            "rank_ic": _ic(_rank(s), _rank(f)),
            "n": int(f.size),
        }
        dec = _deciles(s)
        row["by_signal_decile_bps"] = [
            float(f[dec == d].mean()) if (dec == d).any() else None for d in range(10)
        ]
        bins = {"2-2.5": (2.0, 2.5), "2.5-3": (2.5, 3.0), ">3": (3.0, math.inf)}
        row["by_tail_bin_bps"] = {
            k: (
                float((np.sign(s) * f)[(abs(zz) >= lo) & (abs(zz) < hi)].mean())
                if ((abs(zz) >= lo) & (abs(zz) < hi)).any()
                else None
            )
            for k, (lo, hi) in bins.items()
        }
        row["long_leg_alpha_bps"] = float(f[zz < -z0].mean()) if (zz < -z0).any() else None
        row["short_leg_alpha_bps"] = float(-f[zz > z0].mean()) if (zz > z0).any() else None
        if mu_hat_bps and h in mu_hat_bps:
            m = np.asarray(mu_hat_bps[h], dtype=float)
            inter, slope, se = _ols(m, f)
            mdec = _deciles(m)
            row["calibration"] = {
                "bias_bps": float((f - m).mean()),
                "slope": slope,
                "slope_se": se,
                "intercept_bps": inter,
                "uncertainty_by_decile_bps": [
                    float((f - m)[mdec == d].std(ddof=1)) if (mdec == d).sum() > 1 else None
                    for d in range(10)
                ],
            }
        rows.append(row)
    unc = {"ic_se_approx": float(1 / math.sqrt(zz.size)) if zz.size else None}
    t = Table(
        "E5 block A: signal",
        {
            "ic": "correlation",
            "rank_ic": "correlation",
            "returns": "bps",
            "coverage": "share of grid minutes",
        },
        counts,
        rows,
        unc,
        [
            "S = -z; no bucket result becomes a filter, a leg restriction or a threshold (E0 section 13 rule)"
        ],
    )
    if decay_bps:
        t.rows.append(
            {
                "decay_curve_bps": {
                    int(h): float(np.mean(decay_bps[h])) for h in sorted(decay_bps)
                },
                "n": int(zz.size),
            }
        )
    if coverage is not None:
        t.rows.append({"coverage": float(coverage)})
    if quote_age_buckets:
        t.rows.append(
            {
                "quote_age_sensitivity_bps": {
                    k: float(np.mean(v)) for k, v in quote_age_buckets.items()
                }
            }
        )
    return t


# --------------------------------------------------------------------------- block B
def block_b_portfolio(
    stage_books: Mapping[str, Mapping[str, float]],
    forecasts_bps: Mapping[str, float],
    cost_bps: Mapping[str, float],
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    *,
    series: SessionSeries,
    deployed_gross: Sequence[float],
    turnover_per_rebalance: Sequence[float],
    refusals: Mapping[str, int],
    top10_pnl_share: float | None = None,
) -> Table:
    """Block B: cost-of-neutrality table, return lost to liquidity, utilisation and R_gross, concentration, turnover, retention."""
    rows: list[dict[str, Any]] = []
    order = [
        "raw",
        "dollar_neutral",
        "beta_neutral",
        "sector_constrained",
        "name_and_gross_capped",
        "participation_limited",
        "executable",
    ]
    stages = [s for s in order if s in stage_books] + [s for s in stage_books if s not in order]
    for s in stages:
        w = stage_books[s]
        sec: dict[str, float] = {}
        for n, x in w.items():
            sec[sectors[n]] = sec.get(sectors[n], 0.0) + x
        rows.append(
            {
                "stage": s,
                "expected_gross_alpha_bps_of_C": float(
                    sum(forecasts_bps.get(n, 0.0) * x for n, x in w.items())
                ),
                "modelled_conservative_cost_bps_of_C": float(
                    sum(cost_bps.get(n, 0.0) * abs(x) for n, x in w.items())
                ),
                "market_beta": float(sum(betas[n] * x for n, x in w.items())),
                "largest_sector_net": float(max((abs(v) for v in sec.values()), default=0.0)),
                "gross": float(sum(abs(x) for x in w.values())),
                "names": int(sum(1 for x in w.values() if x != 0.0)),
            }
        )
    w_cap = stage_books.get("name_and_gross_capped", {})
    w_liq = stage_books.get("participation_limited", stage_books.get("executable", {}))
    lost = float(
        sum(
            forecasts_bps.get(n, 0.0) * max(abs(w_cap.get(n, 0.0)) - abs(w_liq.get(n, 0.0)), 0.0)
            for n in set(w_cap) | set(w_liq)
        )
    )
    dg = np.asarray(deployed_gross, dtype=float)
    util = dg / series.capital if dg.size else dg
    r_gross = (series.net_total / float(dg.sum())) * 1e4 if dg.size and dg.sum() > 0 else None
    w_exec = stage_books.get("executable", w_liq)
    sec_exec: dict[str, float] = {}
    for n, x in w_exec.items():
        sec_exec[sectors[n]] = sec_exec.get(sectors[n], 0.0) + x
    tpr = np.asarray(turnover_per_rebalance, dtype=float)
    ret = retention(series.net_total, series.gross_total)
    summary = {
        "return_lost_to_liquidity_bps_of_C": lost,
        "capital_utilisation_mean": float(util.mean()) if util.size else None,
        "R_gross_bps_of_deployed": r_gross,
        "R_d_mean_bps_of_C": float(np.mean(series.r_bps)) if series.r_bps else None,
        "largest_name": float(max((abs(x) for x in w_exec.values()), default=0.0)),
        "largest_sector": float(max((abs(v) for v in sec_exec.values()), default=0.0)),
        "top10_share_of_pnl": top10_pnl_share,
        "turnover_per_rebalance_mean": float(tpr.mean()) if tpr.size else None,
        "turnover_per_session": float(tpr.sum()) / max(series.n, 1) if tpr.size else None,
        "gross_to_net_retention": ret,
        "retention_defined": ret is not None,
        "constraint_activity_and_underinvestment": dict(sorted(refusals.items())),
    }
    rows.append({"summary": summary})
    return Table(
        "E5 block B: portfolio",
        {
            "alpha": "bps of C",
            "cost": "bps of C",
            "gross": "fraction of C",
            "R_gross": "bps of deployed gross",
            "turnover": "fraction of C",
        },
        {"sessions": series.n, "rebalances": int(tpr.size), "stages": len(stages)},
        rows,
        {
            "R_d_se_bps": float(np.std(series.r_bps, ddof=1) / math.sqrt(series.n))
            if series.n > 1
            else None
        },
        [
            "R_gross is labelled beside the canonical R_d = NetPnL / C; survivors of the liquidity filters are never re-scaled"
        ],
    )


# --------------------------------------------------------------------------- block C
def block_c_execution(
    waterfalls: Sequence[Mapping[str, float]],
    *,
    capital: float,
    exit_cost: Sequence[float] = (),
    refusals: Mapping[str, tuple[int, float]] | None = None,
    cushions_bps: Sequence[float] = (),
    columns_pnl: Mapping[str, float] | None = None,
    capacity: Mapping[float, Mapping[str, float]] | None = None,
    latency_sensitivity: Mapping[str, float] | None = None,
) -> Table:
    """Block C: the three return measurements, separate waterfall lines, exit cost, rejected-trade attribution, cushion, stress, capacity."""
    keys = (
        "midpoint_reference_pnl",
        "latency_drift",
        "half_spread",
        "impact",
        "fees",
        "execution_residual",
        "financing",
    )
    tot = {k: float(sum(w.get(k, 0.0) for w in waterfalls)) for k in keys}
    net = (
        tot["midpoint_reference_pnl"]
        + tot["latency_drift"]
        - tot["half_spread"]
        - tot["impact"]
        - tot["fees"]
        + tot["execution_residual"]
        - tot["financing"]
    )
    bps = {k: 1e4 * v / capital for k, v in tot.items()}
    rows: list[dict[str, Any]] = [
        {
            "three_return_measurements_bps_of_C": {
                "mid_to_mid": bps["midpoint_reference_pnl"],
                "executable_conservative": 1e4 * net / capital,
                "statistical_alpha_lost_in_execution": bps["midpoint_reference_pnl"]
                - 1e4 * net / capital,
            }
        },
        {
            "waterfall_lines_bps_of_C": {
                **bps,
                "net": 1e4 * net / capital,
                "slippage": "no separate term (latency drift and impact only)",
            }
        },
        {"exit_cost_bps_of_C": float(1e4 * sum(exit_cost) / capital)},
    ]
    if refusals:
        rows.append(
            {
                "rejected_trade_attribution": {
                    k: {"shares": int(v[0]), "expected_gross_alpha_lost_bps_of_C": float(v[1])}
                    for k, v in sorted(refusals.items())
                }
            }
        )
    c = np.asarray(cushions_bps, dtype=float)
    rows.append(
        {
            "cost_cushion_B": {
                "n": int(c.size),
                "quantiles_bps": [float(q) for q in np.quantile(c, [0.1, 0.5, 0.9])]
                if c.size
                else None,
                "share_below_0_5_bps": float((c < 0.5).mean()) if c.size else None,
            }
        }
    )
    if columns_pnl:
        rows.append(
            {"cost_stress_bps_of_C": {k: 1e4 * v / capital for k, v in columns_pnl.items()}}
        )
    if capacity:
        rows.append({"capacity_multiples": {str(m): dict(v) for m, v in sorted(capacity.items())}})
    if latency_sensitivity:
        rows.append({"entry_latency_sensitivity_bps_of_C": dict(latency_sensitivity)})
    return Table(
        "E5 block C: execution",
        {"pnl": "bps of C", "shares": "shares", "cushion": "bps"},
        {"sessions": len(waterfalls), "trades_with_cushion": int(c.size)},
        rows,
        {
            "note": "model sensitivities, never market capacity; actual execution evidence not available (E1)"
        },
        ["every fill is simulated (C10)"],
    )


# --------------------------------------------------------------------------- block D
def _sharpe(r: np.ndarray) -> float:
    return float(r.mean() / r.std(ddof=1)) if r.size > 1 and r.std(ddof=1) > 0 else float("nan")


def block_d_risk_adjusted(
    series: SessionSeries,
    *,
    block_length: int = 5,
    bootstrap: int = 1000,
    seed: int = 20261024,
    factor_exposures: Mapping[str, float] | None = None,
    regimes: Mapping[str, Sequence[float]] | None = None,
) -> Table:
    """Block D: session Sharpe with a block-bootstrap interval, drawdown, ES 5 %, worst session, LOSO, regimes, N and N_eff."""
    r = np.asarray(series.r_bps, dtype=float)
    n = int(r.size)
    sr = _sharpe(r)
    rng = SplitMix64(seed)
    boots: list[float] = []
    if n > block_length:
        nb = math.ceil(n / block_length)
        for _ in range(bootstrap):
            idx = []
            for _b in range(nb):
                start = rng.below(n - block_length + 1)
                idx.extend(range(start, start + block_length))
            boots.append(_sharpe(r[np.asarray(idx[:n])]))
    cum = np.cumsum(r) if n else r
    dd = float((np.maximum.accumulate(cum) - cum).max()) if n else 0.0
    k = max(1, int(math.floor(0.05 * n))) if n else 0
    es = float(np.sort(r)[:k].mean()) if n else None
    loso_mean = [float(np.delete(r, i).mean()) for i in range(n)] if n > 1 else []
    loso_sr = [_sharpe(np.delete(r, i)) for i in range(n)] if n > 2 else []
    rho1 = float(np.corrcoef(r[:-1], r[1:])[0, 1]) if n > 2 and r.std() > 0 else 0.0
    rho1 = rho1 if math.isfinite(rho1) else 0.0
    n_eff = n * (1 - rho1) / (1 + rho1) if n and rho1 > -1 else n
    rows = [
        {
            "net_session_sharpe": sr,
            "sharpe_block_bootstrap_95": [
                float(np.quantile(boots, 0.025)),
                float(np.quantile(boots, 0.975)),
            ]
            if boots
            else None,
            "dependence_assumption": f"stationary block bootstrap with fixed block length {block_length} sessions; sessions treated as weakly dependent",
            "max_drawdown_bps": dd,
            "expected_shortfall_5pct_bps": es,
            "worst_session_bps": float(r.min()) if n else None,
            "worst_session_share_of_total": float(abs(r.min()) / abs(r.sum()))
            if n and r.sum() != 0
            else None,
            "leave_one_session_out_mean_range_bps": [min(loso_mean), max(loso_mean)]
            if loso_mean
            else None,
            "leave_one_session_out_sharpe_range": [min(loso_sr), max(loso_sr)] if loso_sr else None,
            "factor_exposures": dict(factor_exposures or {}),
            "regimes_mean_bps": {k: float(np.mean(v)) for k, v in (regimes or {}).items()},
            "sessions": n,
            "effective_sample_size": float(n_eff),
            "lag1_autocorrelation": rho1,
        }
    ]
    return Table(
        "E5 block D: risk-adjusted",
        {"returns": "bps of C per session", "sharpe": "per session (not annualised)"},
        {"sessions": n, "bootstrap_draws": len(boots), **series.counts},
        rows,
        {
            "sharpe_interval": "block bootstrap",
            "mean_se_bps": float(r.std(ddof=1) / math.sqrt(n)) if n > 1 else None,
        },
        ["no annualisation; every statistic is per session over every usable session"],
    )


# --------------------------------------------------------------------------- block E
def block_e_sensitivity(
    waterfalls: Sequence[Mapping[str, float]],
    *,
    capital: float,
    series: SessionSeries,
    deltas: Mapping[str, float] | None = None,
) -> Table:
    """Block E: gross-to-net waterfall in bps of C per session with uncertainty; arithmetic break-even scenarios; dSR decomposition."""
    n = len(waterfalls)
    per = {
        k: np.asarray([1e4 * w.get(k, 0.0) / capital for w in waterfalls], dtype=float)
        for k in (
            "midpoint_reference_pnl",
            "latency_drift",
            "half_spread",
            "impact",
            "fees",
            "execution_residual",
            "financing",
        )
    }
    net = (
        per["midpoint_reference_pnl"]
        + per["latency_drift"]
        - per["half_spread"]
        - per["impact"]
        - per["fees"]
        + per["execution_residual"]
        - per["financing"]
    )
    means = {k: float(v.mean()) if n else 0.0 for k, v in per.items()}
    ses = {k: float(v.std(ddof=1) / math.sqrt(n)) if n > 1 else None for k, v in per.items()}
    mu = float(net.mean()) if n else 0.0
    sigma = float(net.std(ddof=1)) if n > 1 else float("nan")
    d = dict(
        deltas
        or {
            "gross_alpha_bps": 0.5,
            "spread_and_fees_bps": 0.25,
            "impact_bps": 0.25,
            "all_costs_pct": 10.0,
        }
    )
    costs = means["half_spread"] + means["impact"] + means["fees"]
    scenarios = {
        "gross_alpha_plus": {"delta": d["gross_alpha_bps"], "net_bps": mu + d["gross_alpha_bps"]},
        "spread_and_fees_minus": {
            "delta": d["spread_and_fees_bps"],
            "net_bps": mu + d["spread_and_fees_bps"],
        },
        "impact_minus": {"delta": d["impact_bps"], "net_bps": mu + d["impact_bps"]},
        "all_costs_minus_pct": {
            "delta": d["all_costs_pct"],
            "net_bps": mu + costs * d["all_costs_pct"] / 100.0,
        },
        "break_even_gross_alpha_change_bps": -mu,
    }
    dsr = {
        "formula": "dSR ~ dmu / sigma - (mu / sigma^2) dsigma",
        "d_mu_coefficient": 1 / sigma if sigma and math.isfinite(sigma) and sigma > 0 else None,
        "d_sigma_coefficient": -mu / sigma**2
        if sigma and math.isfinite(sigma) and sigma > 0
        else None,
    }
    rows = [
        {"gross_to_net_waterfall_bps_per_session": {**means, "net": mu}},
        {"arithmetic_scenarios": scenarios},
        {"sharpe_decomposition": dsr},
    ]
    return Table(
        "E5 block E: economic sensitivity budget",
        {"all": "bps of C per session"},
        {"sessions": n, "usable_sessions": series.n},
        rows,
        {
            "standard_errors_bps": ses,
            "net_se_bps": float(net.std(ddof=1) / math.sqrt(n)) if n > 1 else None,
        },
        [
            "each scenario is an arithmetic scenario, not additive and not a forecast; no feature is assigned a Sharpe gain"
        ],
    )


# --------------------------------------------------------------------------- block F
def block_f_overfitting(
    series: SessionSeries,
    *,
    n_trials: int,
    trials_sr_variance: float | None = None,
    candidates: np.ndarray | Sequence[Sequence[float]] | None = None,
    pbo_blocks: int = 16,
) -> Table:
    """Block F: the deflated Sharpe ratio of the session series and, given a sweep, PBO by CSCV.

    The deflated Sharpe ratio (Bailey and Lopez de Prado 2014) is the probability
    that the true per-session Sharpe exceeds ``SR0``, the Sharpe the best of
    ``n_trials`` skill-less trials would show by luck; skew and kurtosis of the
    series widen the error.  ``n_trials`` is what the selection ledger records,
    never a number chosen for the report.  PBO (Bailey, Borwein, Lopez de Prado
    and Zhu 2017) needs the candidates' per-session returns as a ``T x M``
    matrix (``candidates``); without one the PBO row says so.  Both delegate to
    :mod:`iap.validation` (one implementation, its own tests).
    """
    from iap.validation.cpcv import probability_of_backtest_overfitting
    from iap.validation.deflated import deflated_sharpe_ratio, sharpe_moments

    r = np.asarray(series.r_bps, dtype=float)
    mom = sharpe_moments(r)
    n = int(mom["n"])
    dsr: dict[str, Any] | None = None
    if n > 2 and math.isfinite(mom["sharpe"]):
        dsr = deflated_sharpe_ratio(
            mom["sharpe"], n, n_trials, mom["skew"], mom["kurtosis"], trials_sr_variance
        )
    pbo: dict[str, Any] | None = None
    pbo_note = "no candidate matrix supplied: PBO needs the sweep's per-session returns"
    if candidates is not None:
        out = probability_of_backtest_overfitting(candidates, n_blocks=pbo_blocks)
        pbo = {k: out[k] for k in ("pbo", "logit_median", "n_splits", "n_blocks", "n_candidates")}
        pbo_note = "PBO = share of CSCV splits whose in-sample best ranks at or below the out-of-sample median"
    rows = [
        {
            "session_sharpe": mom["sharpe"],
            "skew": mom["skew"],
            "kurtosis": mom["kurtosis"],
            "n_trials": int(n_trials),
            "expected_max_sharpe_sr0": dsr["sr0"] if dsr else None,
            "deflated_sharpe_ratio": dsr["dsr"] if dsr else None,
            "trials_sr_variance": dsr["trials_sr_variance"] if dsr else None,
        },
        {"probability_of_backtest_overfitting": pbo},
    ]
    return Table(
        "E5 block F: selection and overfitting",
        {
            "sharpe": "per session (not annualised)",
            "deflated_sharpe_ratio": "probability",
            "pbo": "probability",
        },
        {"sessions": n, "n_trials": int(n_trials), **series.counts},
        rows,
        {"dsr": "normal approximation with skew and kurtosis (PSR at SR0)"},
        [
            "kurtosis is non-excess (3 for a normal series)",
            pbo_note,
        ],
    )


# --------------------------------------------------------------------------- the decision tree
def decision_tree(
    *,
    e3_red: bool,
    predictability: bool,
    screen_passed: bool,
    powered: bool,
    x0_eligible: bool | None,
    attribution: str = "",
) -> dict[str, Any]:
    """E0 section 13's six branches; the verdict is a development statement, never a confirmatory one."""
    if e3_red:
        return {
            "branch": 1,
            "verdict": "insufficient evidence",
            "outcome": "stop: data suitability before alpha research",
        }
    if not predictability:
        return {
            "branch": 2,
            "verdict": "not viable",
            "outcome": "H1 economically unsupported; no rescue filter",
        }
    if not screen_passed:
        return {
            "branch": 3,
            "verdict": "not viable",
            "outcome": "predictability but the fixed strategy fails the conservative-cost hurdle",
            "loss_attributed_to": attribution or "unattributed (blocks B and C name the cause)",
        }
    if not powered:
        return {
            "branch": 4,
            "verdict": "economically viable",
            "outcome": "GREEN-REGISTER-ONLY, confirmation pending",
        }
    if x0_eligible is False:
        return {
            "branch": 5,
            "verdict": "economically viable",
            "outcome": "X0 not eligible for the exact session series; the gate refuses PROMOTE",
        }
    return {
        "branch": 6,
        "verdict": "economically viable",
        "outcome": "X1 -> X2 -> X3 under their separate authorisations",
    }

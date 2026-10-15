"""Breadth report (v1.17 WS-C item C7): one document joining the book's
P&L with breadth, effective bets, break-even size, net Sharpe at size and
capacity at the rebalance horizon.

Every number here is REPORT-ONLY arithmetic on a :class:`BookResult`; no
gate reads it.  Definitions (all pinned here so the Markdown and the JSON
cannot drift):

- **breadth**: ``n_names`` (the universe with features), the mean number
  of names scored per rebalance, and the number of rebalances per session;
- **effective bets**: :func:`iap.combine.weights.effective_bets` on the
  correlation matrix of the names' rebalance-interval returns (pairwise on
  finite pairs) — the participation ratio of its eigenvalues, ``N`` for
  uncorrelated names, 1 for one common factor;
- **breadth arithmetic** (Grinold's fundamental law, as a yardstick): the
  mean per-rebalance rank IC of the score against the next interval's
  return, ``BR = N_eff * rebalances_per_day * 252`` independent bets per
  year and ``IR_implied = IC * sqrt(BR)`` — the IR the IC WOULD imply with
  no costs and no constraints; the realised net Sharpe is what it is;
- **net Sharpe at size**: the book is re-run at ``sizes x notional``
  (only the impact term and the impact cost scale; spread and fee are
  linear in size, the optimizer's weights respond), and at each size the
  Sharpe of the per-DAY net P&L (:func:`iap.validation.deflated.pnl_sharpe`,
  annualised, ``None`` with fewer than two sessions) and the per-REBALANCE
  net P&L Sharpe annualised by ``sqrt(rebalances_per_day * 252)`` (a
  single-session reading; intervals are not independent, so read it as a
  scale, not a test);
- **break-even notional**: the first size at which the net P&L crosses
  zero, by linear interpolation between the sizes of the sweep (``None``
  when the net P&L is negative already at the smallest size — then the
  smallest size is reported as ``below`` — or positive at every size);
- **capacity at horizon**: :func:`iap.validation.metrics.capacity_breakeven`
  with the book's realised round-trip edge, ``2 * gross_pnl /
  traded_notional`` (gross P&L per dollar traded, both legs), at the
  universe-median mid, half spread and ADV: the single-name size at which
  that edge equals its own spread, fee and impact; a proxy at the
  rebalance horizon, not a book capacity;
- **day-clustered SEs**: the mean per-rebalance net P&L and rank IC with
  one cluster per session (the CR1 sandwich ``G/(G-1) * sum_g S_g^2 / N^2``,
  Student-t with ``G - 1`` degrees of freedom via
  :func:`iap.validation.metrics.day_cluster_inference`); ``None`` with one
  session.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from iap.combine.weights import effective_bets
from iap.validation.deflated import DAYS_PER_YEAR, pnl_sharpe
from iap.validation.metrics import day_cluster_inference

from .breadth_book import BookResult, Instrument

DEFAULT_SIZES = (0.25, 0.5, 1.0, 2.0, 4.0)


def cluster_mean(values: Sequence[float], clusters: Sequence[int]) -> dict[str, Any]:
    """Mean with a cluster-robust (CR1) standard error, one cluster per
    distinct label; ``se``/``t``/``p`` ``None`` with fewer than 2 clusters."""
    x = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    ok = np.isfinite(x)
    x, c = x[ok], c[ok]
    n = int(x.size)
    out: dict[str, Any] = {
        "n": n,
        "n_clusters": 0,
        "mean": None,
        "se": None,
        "t": None,
        "df": None,
        "p_student_t": None,
    }
    if n == 0:
        return out
    labels, cid = np.unique(c, return_inverse=True)
    g = int(labels.size)
    mean = float(x.mean())
    out.update(n_clusters=g, mean=mean)
    if g < 2:
        return out
    s = np.bincount(cid, weights=x - mean, minlength=g)
    var = g / (g - 1) * float(np.sum(s * s)) / float(n * n)
    se = math.sqrt(var)
    out["se"] = se
    out["df"] = g - 1
    if se > 0:
        t = mean / se
        out["t"] = t
        out["p_student_t"] = day_cluster_inference(t, g)["p_student_t"]
    return out


def return_correlation(rets: np.ndarray) -> np.ndarray:
    """Pairwise-complete correlation of a (T, N) return matrix (NaN where a
    pair has fewer than 3 common rows or a zero spread)."""
    r = np.asarray(rets, dtype=float)
    n = r.shape[1]
    corr = np.full((n, n), np.nan)
    for i in range(n):
        for j in range(i, n):
            ok = np.isfinite(r[:, i]) & np.isfinite(r[:, j])
            if ok.sum() < 3:
                continue
            a = r[ok, i] - r[ok, i].mean()
            b = r[ok, j] - r[ok, j].mean()
            den = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
            if not den > 0:
                continue
            corr[i, j] = corr[j, i] = 1.0 if i == j else float(np.clip(np.sum(a * b) / den, -1, 1))
    return corr


def breakeven_notional(sizes: Sequence[float], net: Sequence[float]) -> dict[str, Any]:
    """First zero crossing of ``net`` over ``sizes`` (both increasing in
    size) by linear interpolation; see the module docstring."""
    s = [float(v) for v in sizes]
    y = [float(v) for v in net]
    if len(s) != len(y) or not s:
        raise ValueError("sizes and net must be non-empty and aligned")
    if any(b <= a for a, b in zip(s, s[1:], strict=False)):
        raise ValueError("sizes must be strictly increasing")
    if y[0] <= 0.0:
        return {"notional": None, "status": "below", "at": s[0]}
    for a, b, ya, yb in zip(s, s[1:], y, y[1:], strict=False):
        if yb <= 0.0 < ya:
            return {"notional": a + (b - a) * ya / (ya - yb), "status": "crossed", "at": None}
    return {"notional": None, "status": "above", "at": s[-1]}


def sharpe_block(result: BookResult) -> dict[str, Any]:
    daily = list(result.daily_net_pnl().values())
    per_reb = [r.net_pnl for r in result.records]
    n_days = max(len(result.days), 1)
    reb_per_day = len(result.records) / n_days
    per_reb_sharpe = pnl_sharpe(per_reb, annualize=None)
    return {
        "net_daily_annualised": pnl_sharpe(daily),
        "gross_daily_annualised": pnl_sharpe(list(result.daily_gross_pnl().values())),
        "net_per_rebalance": per_reb_sharpe,
        "net_per_rebalance_annualised": (
            None
            if per_reb_sharpe is None
            else per_reb_sharpe * math.sqrt(reb_per_day * DAYS_PER_YEAR)
        ),
        "rebalances_per_day": reb_per_day,
        "n_days": len(result.days),
    }


def breadth_report(
    base: BookResult,
    runner: Callable[[float], BookResult],
    sizes: Sequence[float],
    cost_model,
    instruments: Mapping[int, Instrument],
) -> dict[str, Any]:
    """The C7 document for ``base`` (run at ``base.config.notional``);
    ``runner(notional)`` re-runs the same book at another size."""
    cfg = base.config
    sizes = tuple(sorted(float(s) for s in sizes))
    if not sizes or any(s <= 0 for s in sizes):
        raise ValueError("sizes must be positive")
    summary = base.summary()
    n_days = max(len(base.days), 1)
    reb_per_day = len(base.records) / n_days

    corr = return_correlation(base.grid_returns[1:]) if len(base.records) > 1 else np.zeros((0, 0))
    bets = effective_bets(corr) if corr.size else effective_bets(np.zeros((0, 0)))
    ic = cluster_mean([r.rank_ic for r in base.records], [r.day for r in base.records])
    mean_ic = ic["mean"] if ic["mean"] is not None else float("nan")
    br = bets["n_effective"] * reb_per_day * DAYS_PER_YEAR
    arithmetic = {
        "mean_rank_ic": ic["mean"],
        "rank_ic_day_clustered": ic,
        "n_effective_names": bets["n_effective"],
        "bets_per_year": br,
        "ir_implied": (mean_ic * math.sqrt(br)) if np.isfinite(mean_ic) and br > 0 else None,
        "note": "IR_implied = IC * sqrt(BR): no costs, no constraints; a yardstick only",
    }

    at_size = []
    for s in sizes:
        res = base if math.isclose(s, 1.0) else runner(cfg.notional * s)
        sb = sharpe_block(res)
        at_size.append(
            {
                "size": s,
                "notional": cfg.notional * s,
                "gross_pnl": res.gross_pnl,
                "net_pnl": res.net_pnl,
                "impact_cost": res.impact_cost,
                "spread_cost": res.spread_cost,
                "fee_cost": res.fee_cost,
                "net_return_bps": 1e4 * res.net_pnl / (cfg.notional * s),
                "mean_turnover": float(np.mean([r.turnover for r in res.records])),
                "sharpe": sb,
                "n_infeasible": res.n_infeasible,
            }
        )
    be = breakeven_notional([a["size"] for a in at_size], [a["net_pnl"] for a in at_size])
    be_abs = None if be["notional"] is None else be["notional"] * cfg.notional

    traded = summary["traded_notional"]
    edge = 2.0 * base.gross_pnl / traded if traded > 0 else 0.0
    ids = base.instrument_ids
    mids = np.nanmedian(base.prices, axis=0)
    med_mid = float(np.nanmedian(mids))
    med_adv = float(np.median([instruments[i].adv_shares for i in ids]))
    # half spread as a return from the books' marks is not kept; use the cost
    # model's fee-free reading at the median mid with the universe-median
    # spread implied by the spread cost per dollar traded (both legs).
    hs_ret = base.spread_cost / traded if traded > 0 else 0.0
    cap = None
    if np.isfinite(edge) and med_mid > 0:
        from iap.validation.metrics import capacity_breakeven

        cap = capacity_breakeven(cost_model, edge, med_mid, hs_ret * med_mid, "EQUITY", med_adv, 1)
        cap = {k: (None if not np.isfinite(v) else v) for k, v in cap.items()}
    capacity = {
        "edge_return_round_trip": edge,
        "median_mid": med_mid,
        "median_adv_shares": med_adv,
        "half_spread_return": hs_ret,
        "breakeven": cap,
        "horizon_ns": cfg.rebalance_ns,
        "note": "single-name size at which the book's realised round-trip edge equals its own cost; a proxy",
    }

    pnl_cluster = cluster_mean([r.net_pnl for r in base.records], [r.day for r in base.records])
    return {
        "x-version": 1,
        "config": cfg.to_dict(),
        "book": summary,
        "identity": {
            "net_minus_gross_plus_costs": base.net_pnl - (base.gross_pnl - base.total_costs),
        },
        "neutrality": {
            "dollar": cfg.dollar_neutral,
            "beta": cfg.beta_neutral,
            "sector": cfg.sector_neutral,
            "bound": cfg.neutral_bound,
            "exposure_rows": base.exposure_labels,
            "missing_beta": base.missing_beta,
            "sector_of": {str(k): v for k, v in sorted(base.sector_of.items())},
            "max_abs_net": summary["max_abs_net_exposure"],
            "max_abs_beta": summary["max_abs_beta_exposure"],
            "max_abs_sector": summary["max_abs_sector_exposure"],
        },
        "breadth": {
            "n_names": summary["n_names"],
            "mean_names_scored": summary["mean_names_scored"],
            "rebalances_per_day": reb_per_day,
            "effective_bets": bets,
            "arithmetic": arithmetic,
        },
        "sharpe": sharpe_block(base),
        "net_pnl_day_clustered": pnl_cluster,
        "at_size": at_size,
        "breakeven": {
            "size": be["notional"],
            "notional": be_abs,
            "status": be["status"],
            "at": be["at"],
        },
        "capacity": capacity,
        "instruments": {str(i): instruments[i].symbol for i in ids},
    }


def _f(v: Any, nd: int = 2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "n/a"
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(report: Mapping[str, Any]) -> str:
    b = report["book"]
    cfg = report["config"]
    sh = report["sharpe"]
    lines = [
        f"# Breadth book — {report.get('tag', '')}",
        "",
        "In-sample, report-only. Every number is net of the cost model the optimizer saw "
        "(spread + fee + impact). No gate reads this document.",
        "",
    ]
    ds = report.get("dataset") or {}
    if ds.get("smoke_test"):
        lines += [
            f"**Smoke test: a breadth of {b['n_names']} names exercises the plumbing, not breadth.** "
            "Read the numbers as a check that the book runs, not as a result.",
            "",
        ]
    lines += [
        "## Book",
        "",
        "| | |",
        "|---|---|",
        f"| score / weights | {cfg['score']} / {cfg['weights']} |",
        f"| rebalance | every {cfg['rebalance_ns'] / 60e9:g} min, exec delay {cfg['exec_delay_ns'] / 1e9:g} s |",
        f"| notional | {cfg['notional']:,.0f} |",
        f"| names / mean scored | {b['n_names']} / {_f(b['mean_names_scored'])} |",
        f"| sessions / rebalances / traded | {b['n_days']} / {b['n_rebalances']} / {b['n_traded']} |",
        f"| gross P&L | {_f(b['gross_pnl'])} |",
        f"| spread / fee / impact | {_f(b['spread_cost'])} / {_f(b['fee_cost'])} / {_f(b['impact_cost'])} |",
        f"| net P&L | {_f(b['net_pnl'])} |",
        f"| mean turnover / rebalance (L1 weight) | {_f(b['mean_turnover'], 4)} |",
        f"| infeasible solves / frozen name-steps | {b['n_infeasible']} / {b['n_frozen_name_steps']} |",
        f"| net Sharpe (daily, annualised) | {_f(sh['net_daily_annualised'])} |",
        f"| net Sharpe (per rebalance, annualised scale) | {_f(sh['net_per_rebalance_annualised'])} |",
        "",
        "## Neutrality",
        "",
    ]
    nz = report["neutrality"]
    lines += [
        f"dollar {nz['dollar']}, beta {nz['beta']}, sector {nz['sector']} (bound {nz['bound']:g}); "
        f"rows: {', '.join(nz['exposure_rows']) or 'none'}.",
        "",
        f"max |net| {_f(nz['max_abs_net'], 6)}, max |beta| {_f(nz['max_abs_beta'], 6)}, "
        f"max |sector| {_f(nz['max_abs_sector'], 6)}; names without a beta: {nz['missing_beta'] or 'none'}.",
        "",
        "## Breadth",
        "",
    ]
    br = report["breadth"]
    ar = br["arithmetic"]
    lines += [
        f"effective bets (names) {_f(br['effective_bets']['n_effective'])} of {br['n_names']}; "
        f"mean rank IC {_f(ar['mean_rank_ic'], 4)} "
        f"(day-clustered t {_f(ar['rank_ic_day_clustered']['t'])}, df {ar['rank_ic_day_clustered']['df']}); "
        f"bets/year {_f(ar['bets_per_year'], 0)}; IR implied {_f(ar['ir_implied'])} ({ar['note']}).",
        "",
        "## Net Sharpe at size",
        "",
        "| size | notional | gross | net | impact | net bps | Sharpe daily | Sharpe per-rebalance (ann.) | turnover |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for a in report["at_size"]:
        lines.append(
            f"| {a['size']:g} | {a['notional']:,.0f} | {_f(a['gross_pnl'])} | {_f(a['net_pnl'])} | "
            f"{_f(a['impact_cost'])} | {_f(a['net_return_bps'])} | {_f(a['sharpe']['net_daily_annualised'])} | "
            f"{_f(a['sharpe']['net_per_rebalance_annualised'])} | {_f(a['mean_turnover'], 4)} |"
        )
    be = report["breakeven"]
    cap = report["capacity"]
    pc = report["net_pnl_day_clustered"]
    lines += [
        "",
        f"break-even: {be['status']}"
        + (
            f" at {be['size']:.2f}x = {be['notional']:,.0f}"
            if be["notional"] is not None
            else f" (at {be['at']}x)"
        ),
        "",
        f"capacity at horizon (single-name proxy): edge {_f(cap['edge_return_round_trip'] * 1e4)} bps round trip, "
        + (
            f"break-even size {_f(cap['breakeven']['units'], 0)} shares = {_f(cap['breakeven']['notional'], 0)} "
            f"({_f((cap['breakeven']['participation'] or 0) * 100, 3)} % ADV)"
            if cap["breakeven"]
            else "n/a"
        )
        + f"; {cap['note']}.",
        "",
        f"mean net P&L per rebalance {_f(pc['mean'])} (day-clustered se {_f(pc['se'])}, t {_f(pc['t'])}, df {pc['df']}).",
        "",
    ]
    if report.get("ledger"):
        lines += [
            f"ledger: one look of kind `{report['ledger']['kind']}` ({report['ledger']['path']}).",
            "",
        ]
    return "\n".join(lines)


def sanitise(obj: Any) -> Any:
    """NaN/inf -> None, numpy scalars -> Python, recursively (valid JSON)."""
    if isinstance(obj, dict):
        return {str(k): sanitise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitise(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return sanitise(obj.tolist())
    return obj

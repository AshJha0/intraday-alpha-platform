"""REL-08 rule v2 book-parameter dry run on SYNTHETIC data only.

No real dataset is opened here: every frame is generated in memory from a seeded RNG, so no
holdout-registry guard applies (nothing registered is read) and nothing is written to the
experiment ledger. Parameters are chosen from synthetic book behaviour and economic reasoning
(see REL08_V2_DRYRUN_REPORT.md), never from a real return.

    python rel08_v2_dryrun.py grid   --out grid.json     # stage 1: book-parameter grid, null data
    python rel08_v2_dryrun.py check  --out check.json    # stage 2: false-pass and power at the frozen setting
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from iap.backtest.costs import CostModel
from iap.research import breadth_book as bb
from iap.research import rel08_rule as rr
from iap.research import rel08_screen as rs

NS_S = 1_000_000_000
NS_MIN = 60 * NS_S
NS_DAY = 86_400 * NS_S
DAY0 = 1548858600 * NS_S  # 14:30 UTC, a regular-hours open in winter; synthetic days follow it
STEP_S = 30
SESSION_MIN = 390
N_EQ = 78
SECTORS = ("A", "B", "C", "D")
VARIANT_IDS = ("V1", "V2", "V5", "V6", "V9", "V10", "V11")


# ------------------------------------------------------------------ data --


def make_data(
    mode: str, amp: float, seed: int, days: int = 6, spread: float = 1.0
) -> rs.ScreenData:
    """80 names (78 equities on 2 reference ETFs), ``days`` regular sessions of 390 min.

    mode ``null``  : prices are martingales plus a common factor; no predictable component.
    mode ``revert``: log mid = martingale + AR(1) deviation u (half-life 10 min, sd ``amp`` bps),
                     the pattern vwap_deviation and residual_reversion are built to find.
    mode ``lead``  : a share ``amp`` (0..1) of each equity's common-factor loading is delayed by
                     3 minutes behind the reference ETF (the ETF catch-up pattern).
    """
    rng = np.random.default_rng(seed)
    rows_day = SESSION_MIN * 60 // STEP_S
    n_rows = rows_day * days
    ts = np.concatenate(
        [
            DAY0 + d * NS_DAY + np.arange(rows_day, dtype=np.int64) * STEP_S * NS_S
            for d in range(days)
        ]
    )
    common_ret = rng.normal(0.0, 1.5e-4, n_rows)
    common_lag = np.concatenate([np.zeros(6), common_ret[:-6]])
    frames: dict[int, pd.DataFrame] = {}
    instruments: dict[int, bb.Instrument] = {}
    ref_of: dict[int, int] = {}
    sector_of: dict[int, str] = {}
    phi = 0.5 ** (STEP_S / 60.0 / 10.0)

    def frame(logp: np.ndarray, beta: float, spread_lo: float, spread_hi: float) -> pd.DataFrame:
        b = np.full(n_rows, beta)
        for d in range(days):
            b[d * rows_day : d * rows_day + 10 * 60 // STEP_S] = np.nan
        vol = rng.gamma(2.0, 1000.0, n_rows)
        return pd.DataFrame(
            {
                "exchange_ts": ts,
                "mid_price_v1": 50.0 * np.exp(logp),
                "spread_bps_v1": rng.uniform(spread_lo, spread_hi, n_rows),
                "beta_w5m_v1": b,
                "traded_volume_w10s_v1": vol,
                "ret_log_1m_v1": np.zeros(n_rows),
                "ref_ret_1m_v1": np.zeros(n_rows),
            }
        )

    for iid, sym in ((100, "SPY"), (101, "QQQ")):
        etf_logp = np.cumsum(common_ret + rng.normal(0, 3e-5, n_rows))
        frames[iid] = frame(etf_logp, 1.0, 0.5, 1.5)
        instruments[iid] = bb.Instrument(iid, sym, "ETF", 5e7, 100, 0.01)
    for i in range(1, N_EQ + 1):
        beta = 0.6 + 0.8 * rng.random()
        idio = rng.normal(0.0, 3e-4, n_rows)
        if mode == "lead":
            fac = beta * ((1.0 - amp) * common_ret + amp * common_lag)
        else:
            fac = beta * common_ret
        logp = np.cumsum(fac + idio)
        if mode == "revert":
            eps = rng.normal(0.0, amp * 1e-4 * math.sqrt(1 - phi**2), n_rows)
            u = np.zeros(n_rows)
            u[0] = rng.normal(0.0, amp * 1e-4)
            for k in range(1, n_rows):
                u[k] = phi * u[k - 1] + eps[k]
            logp = logp + u
        frames[i] = frame(logp, beta, 1.0 * spread, 4.0 * spread)
        instruments[i] = bb.Instrument(i, f"N{i}", "EQUITY", 3e6, 100, 0.01)
        ref_of[i] = 101 if i % 2 else 100
        sector_of[i] = SECTORS[i % len(SECTORS)]
    return rs.ScreenData(
        frames=frames,
        instruments=instruments,
        cost_model=CostModel(2.0, 0.003, 2.5),
        ref_of=ref_of,
        sector_of=sector_of,
    )


# ------------------------------------------------------------------ rule --


def base_rule(book: dict[str, Any] | None = None, draws: int = 999) -> rr.Rule:
    """The committed v1 draft body (thresholds untouched) with ``book`` overrides.

    Sufficiency floors are the registered ones scaled to the synthetic 6-session sample
    (usable floors x 6 sessions is not applied: the v1 floors are kept as registered);
    only the cov warm-up and the bootstrap draw count differ from the rule, the latter to keep
    a Monte Carlo of hundreds of screens affordable (draws change the precision of the lower bound,
    not its definition).
    """
    doc = copy.deepcopy(rr.load_draft().doc)
    doc["statistics"]["draws"] = draws
    if book:
        doc["book"].update(book)
    rr.validate_rule(doc)
    return rr.Rule(doc=doc, signed=True, path="synthetic")


# ----------------------------------------------------------------- C1 v2 --


def within_session_max_gap_min(grid: np.ndarray, usable: np.ndarray) -> float:
    """v2 definition of ``max_gap_min`` (rule v2, section C1).

    Largest gap, in minutes, between consecutive usable rebalances that fall in the same
    session day. The leading gap and gaps across a day boundary are excluded; a day with one
    usable rebalance contributes 0; no usable rebalance at all gives ``inf``.
    """
    t = np.asarray(grid, dtype=np.int64)[np.asarray(usable, dtype=bool)]
    if t.size == 0:
        return float("inf")
    day = t // NS_DAY
    same = day[1:] == day[:-1]
    if not same.any():
        return 0.0
    return float(np.diff(t)[same].max()) / NS_MIN


def usable_mask(
    data: rs.ScreenData, rule: rr.Rule, variant: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    """(grid, usable columns) exactly as ``evaluate_variant`` builds them."""
    suff = rule.section("sufficiency")
    names_min = int(suff["names_per_rebalance_min"])
    max_age = int(round(float(suff["score_max_age_s"]) * NS_S))
    h = int(round(float(variant["horizon_min"]) * NS_MIN))
    ids = data.universe(str(variant["score"]))
    cfg = rs.book_config(rule, variant, names_min)
    grid = rs._grid(data, ids, cfg)
    scores, _ = rs.compute_scores(data, ids, str(variant["score"]), h, grid, max_age, names_min)
    labels = rs.compute_labels(data, ids, h, grid, max_age)
    both = np.isfinite(scores) & np.isfinite(labels)
    return grid, both.sum(axis=0) >= names_min


def screen_variant(data: rs.ScreenData, rule: rr.Rule, variant: dict[str, Any]) -> dict[str, Any]:
    """evaluate_variant with the v2 within-session gap substituted, then the unchanged criteria."""
    stats = rs.evaluate_variant(data, rule, variant)
    if "error" in stats and "net_bps" not in stats:
        return {"id": variant["id"], "error": stats["error"]}
    grid, usable = usable_mask(data, rule, variant)
    stats["max_gap_min_v1_leading"] = stats["max_gap_min"]
    stats["max_gap_min"] = within_session_max_gap_min(grid, usable)
    crit = rs.criteria(stats, rule)
    outcome, reason = rs.outcome(crit)
    net = stats.get("net_bps") or {}
    book = stats.get("book") or {}
    n_reb = max(1, stats["n_rebalances"])
    return {
        "id": variant["id"],
        "crit": crit,
        "outcome": outcome,
        "reason": reason,
        "n_usable": stats["n_usable"],
        "max_gap_min": stats["max_gap_min"],
        "net_mean": net.get("mean"),
        "net_lower": net.get("lower"),
        "net_se": net.get("se"),
        "mde": net.get("mde"),
        "ess": net.get("ess"),
        "ic_mean": (stats.get("rank_ic") or {}).get("mean"),
        "mean_turnover": book.get("mean_turnover"),
        "gross_pnl": book.get("gross_pnl"),
        "cost": (book.get("spread_cost", 0) or 0)
        + (book.get("fee_cost", 0) or 0)
        + (book.get("impact_cost", 0) or 0),
        "n_infeasible": book.get("n_infeasible"),
        "n_decisions_per_day": n_reb / max(1, len(data.session_close())),
    }


def run_one(args: tuple) -> dict[str, Any]:
    mode, amp, seed, book, variants, days, draws, spread = args
    t0 = time.time()
    data = make_data(mode, amp, seed, days, spread)
    rule = base_rule(book, draws)
    out = []
    for v in rule.variants:
        if v["id"] in variants:
            out.append(screen_variant(data, rule, dict(v)))
    return {
        "mode": mode,
        "amp": amp,
        "seed": seed,
        "spread": spread,
        "book": book,
        "variants": out,
        "sec": time.time() - t0,
    }


# ------------------------------------------------------------------ main --


def _pool(jobs: list[tuple], workers: int) -> list[dict[str, Any]]:
    if workers <= 1:
        return [run_one(j) for j in jobs]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(run_one, jobs))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("stage", choices=("grid", "grid2", "check", "smoke"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--draws", type=int, default=999)
    ap.add_argument("--null-reps", type=int, default=24)
    ap.add_argument("--iters", type=int, default=None)
    ap.add_argument("--spread", type=float, default=1.0)
    ap.add_argument("--alpha", type=float, default=None)
    ap.add_argument("--ra", type=float, default=None)
    ap.add_argument("--variants", default="V1,V5,V9")
    a = ap.parse_args(argv)
    variants = tuple(a.variants.split(","))
    if a.stage == "smoke":
        book = {}
        if a.alpha is not None:
            book["alpha_bps"] = a.alpha
        if a.ra is not None:
            book["risk_aversion"] = a.ra
        res = _pool([("null", 0.0, 1, book, variants, a.days, a.draws, a.spread)], 1)
    elif a.stage == "grid2":
        res = []
        for sp in (1.0, 4.0):
            for it in (20, 100):
                for al in (10.0, 20.0, 30.0):
                    for ra in (30.0, 100.0, 300.0):
                        book = {"alpha_bps": al, "risk_aversion": ra, "solver_iters": it}
                        jobs = [
                            ("null", 0.0, 9100 + s_, book, variants, a.days, a.draws, sp)
                            for s_ in range(a.reps)
                        ]
                        res.extend(_pool(jobs, a.workers))
                        print("grid2", sp, it, al, ra, "done", flush=True)
    elif a.stage == "grid":
        alphas = [5.0, 10.0, 20.0, 40.0]
        ras = [1000.0, 100.0]
        res = []
        for sp in (1.0, 4.0):
            for al in alphas:
                for ra in ras:
                    book = {"alpha_bps": al, "risk_aversion": ra}
                    jobs = [
                        ("null", 0.0, 9000 + s, book, variants, a.days, a.draws, sp)
                        for s in range(a.reps)
                    ]
                    res.extend(_pool(jobs, a.workers))
                    print("grid", sp, al, ra, "done", flush=True)
    else:
        al = a.alpha if a.alpha is not None else 20.0
        ra = a.ra if a.ra is not None else 300.0
        book = {"alpha_bps": al, "risk_aversion": ra}
        if a.iters is not None:
            book["solver_iters"] = a.iters
        res = []
        # plan: (spread, mode, amp, reps); written after every case so a long run is never all-or-nothing
        plan = [(1.0, "null", 0.0, a.null_reps), (4.0, "null", 0.0, a.null_reps)]
        plan += [
            (2.5, m_, x, a.reps)
            for m_, x in (("revert", 6.0), ("revert", 12.0), ("lead", 0.3), ("lead", 0.6))
        ]
        for sp, mode, amp, reps in plan:
            jobs = [
                (mode, amp, 5000 + s_, book, variants, a.days, a.draws, sp) for s_ in range(reps)
            ]
            res.extend(_pool(jobs, a.workers))
            a.out.write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
            print("check", sp, mode, amp, "done", flush=True)
    a.out.write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
    print("wrote", a.out, len(res), "runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

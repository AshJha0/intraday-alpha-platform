"""REL-08 screen, rule v2 evaluation path (UNSIGNED; reads no data by itself).

``iap.research.rel08_screen`` (the v1 path, used by the signed one-session run) is untouched.  This module
reuses its scores, labels, books and criteria and changes only what the v2 draft
(``research/v118/rel08/REL08_CONTINUATION_RULE_v2.md``) and the calibration plan
(``REL08_V2_CALIBRATION_PLAN.md``) specify:

* a **session** is one calendar day of the grid (``ts // NS_DAY``); a session is **qualifying** iff it has at least
  ``min_rebalances_per_session`` (2) usable rebalances; the rebalances of a non-qualifying session are excluded from
  every count and statistic (the book still trades through them);
* **coverage**: at least ``sessions_min_qualifying`` (5) of the ``sessions_total`` (6) registered sessions qualify,
  otherwise C1 fails and every variant is ``underpowered``; coverage applies to C1 only, through that condition;
* **C1 gap**: ``max_gap_min`` is the largest gap between consecutive usable rebalances *inside one qualifying session*
  (no leading gap, no gap across sessions); a variant with no qualifying session has ``inf``;
* **resampling**: the sessions are FIXED; per bootstrap draw each qualifying session is resampled independently by a
  moving block bootstrap that never leaves the session; the statistic is the pooled mean.  ESS uses a Newey-West
  inflation computed within each session and averaged with weights ``n_session``.  This is not statistically
  equivalent to the v1 bootstrap and carries no calibration until the plan's run is filed.

Thresholds, variants, book economics, K, alpha and the decision map are the rule's, unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from iap.research import breadth_book as bb
from iap.research import rel08_screen as rs
from iap.research.rel08_rule import Rule

NS_MIN = rs.NS_MIN
NS_S = rs.NS_S
NS_DAY = rs.NS_DAY

V2_DEFAULTS: dict[str, int] = {
    "min_rebalances_per_session": 2,
    "sessions_min_qualifying": 5,
    "sessions_total": 6,
}
SCREEN_LABEL_V2 = "hypothesis screen, six sessions (fixed), no edge claim"


def v2_params(rule: Rule) -> dict[str, int]:
    """The v2 session parameters: the rule's ``sufficiency_v2`` section over :data:`V2_DEFAULTS`."""
    out = dict(V2_DEFAULTS)
    out.update({k: int(v) for k, v in (rule.doc.get("sufficiency_v2") or {}).items() if k in out})
    return out


# ------------------------------------------------------------ sessions --


def session_of(ts: np.ndarray) -> np.ndarray:
    return np.asarray(ts, dtype=np.int64) // NS_DAY


def qualifying_mask(grid: np.ndarray, usable: np.ndarray, min_rebalances: int) -> np.ndarray:
    """Per grid column: usable AND in a session with >= ``min_rebalances`` usable columns."""
    grid = np.asarray(grid, dtype=np.int64)
    usable = np.asarray(usable, dtype=bool)
    out = np.zeros(grid.size, dtype=bool)
    if not usable.any():
        return out
    days = session_of(grid)
    for d in np.unique(days[usable]):
        sel = usable & (days == d)
        if int(sel.sum()) >= min_rebalances:
            out |= sel
    return out


def within_session_max_gap_min(grid: np.ndarray, qualifying: np.ndarray) -> float:
    """Largest gap (minutes) between consecutive qualifying rebalances of one session.

    ``inf`` with no qualifying rebalance.  A qualifying session has >= 2 rebalances, so every
    contributing session contributes at least one real gap (a lone rebalance never yields 0).
    """
    t = np.asarray(grid, dtype=np.int64)[np.asarray(qualifying, dtype=bool)]
    if t.size == 0:
        return float("inf")
    d = session_of(t)
    same = d[1:] == d[:-1]
    if not same.any():
        return float("inf")
    return float(np.diff(t)[same].max()) / NS_MIN


def coverage(grid: np.ndarray, qualifying: np.ndarray, params: Mapping[str, int]) -> dict[str, Any]:
    n_q = int(np.unique(session_of(np.asarray(grid)[np.asarray(qualifying, bool)])).size)
    return {
        "sessions_qualifying": n_q,
        "sessions_total": int(params["sessions_total"]),
        "sessions_min_qualifying": int(params["sessions_min_qualifying"]),
        "coverage_ok": n_q >= int(params["sessions_min_qualifying"]),
    }


# ---------------------------------------------------------- statistics --


def mbb_means_within(
    x: np.ndarray, sessions: np.ndarray, block: int, draws: int, seed: int
) -> np.ndarray:
    """Pooled-mean bootstrap distribution, each session resampled independently, in order.

    Session ``k`` (sorted session id) uses ``SeedSequence([seed, k])``; sizes are preserved; blocks
    are drawn inside the session only.
    """
    x = np.asarray(x, dtype=float)
    sessions = np.asarray(sessions)
    n = x.size
    if n == 0:
        return np.full(draws, np.nan)
    total = np.zeros(draws)
    for k, s in enumerate(np.unique(sessions)):
        xs = x[sessions == s]
        m = xs.size
        b = min(block, m)
        nb = int(math.ceil(m / b))
        rng = np.random.default_rng(np.random.SeedSequence([int(seed), k]))
        starts = rng.integers(0, m - b + 1, size=(draws, nb))
        idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(draws, -1)[:, :m]
        total += xs[idx].sum(axis=1)
    return total / n


def newey_west_inflation_within(x: np.ndarray, sessions: np.ndarray, lag: int) -> float:
    """Size-weighted mean of the per-session Newey-West inflations (1.0 for a session of < 3)."""
    x = np.asarray(x, dtype=float)
    sessions = np.asarray(sessions)
    num = 0.0
    for s in np.unique(sessions):
        xs = x[sessions == s]
        infl = rs.newey_west_inflation(xs, lag) if xs.size >= 3 else 1.0
        num += xs.size * (infl if math.isfinite(infl) else 1.0)
    return num / x.size if x.size else float("nan")


def series_stats_within(
    x: np.ndarray, sessions: np.ndarray, block: int, rule: Rule
) -> dict[str, float]:
    """As :func:`rs.series_stats` with the within-session bootstrap and Newey-West."""
    st = rule.section("statistics")
    x = np.asarray(x, dtype=float)
    sessions = np.asarray(sessions)
    keep = np.isfinite(x)
    x, sessions = x[keep], sessions[keep]
    n = int(x.size)
    nan = float("nan")
    if n < 2:
        return {"n": n, "mean": float(x.mean()) if n else nan, "se": nan, "lower": nan,
                "ess": float(n), "nw_inflation": nan, "mde": nan}  # fmt: skip
    means = mbb_means_within(x, sessions, block, int(st["draws"]), int(st["seed"]))
    se = float(np.std(means, ddof=1))
    lower = float(np.quantile(means, 1.0 - rule.level))
    infl = newey_west_inflation_within(x, sessions, block)
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


# ------------------------------------------------------------ variants --


def evaluate_variant_v2(
    data: rs.ScreenData,
    rule: Rule,
    variant: Mapping[str, Any],
    short_circuit: bool = False,
) -> dict[str, Any]:
    """Every statistic of one variant under the v2 definitions (raw; :func:`criteria_v2` judges it).

    ``short_circuit`` (pure speed, decision unchanged): when C1, C2, C4 or C3 already settles the
    outcome (``underpowered`` or ``stop`` before C5-C8 are consulted by the decision map), the two
    extra books behind C6 and C7 are not run; ``influential_removed_net_bps`` and
    ``alt_normalisation_net_bps`` are then NaN and ``robustness_books_computed`` is False.  Every
    other field is bit-identical to the full evaluation.
    """
    suff = rule.section("sufficiency")
    costs = rule.section("costs")
    th = rule.thresholds
    p2 = v2_params(rule)
    notional = float(rule.section("book")["notional"])
    names_min = int(suff["names_per_rebalance_min"])
    max_age = int(round(float(suff["score_max_age_s"]) * NS_S))
    h = int(round(float(variant["horizon_min"]) * NS_MIN))
    reb = float(variant["rebalance_min"])
    block = rs.block_length(reb, float(rule.section("statistics")["block_minutes"]))
    ids = data.universe(str(variant["score"]))
    base: dict[str, Any] = {"id": variant["id"], "variant": dict(variant), "n_names": len(ids)}
    if len(ids) < max(names_min, 2):
        return {**base, "error": f"universe has {len(ids)} names < {names_min}", "n_usable": 0}
    cfg = rs.book_config(rule, variant, names_min)
    grid = rs._grid(data, ids, cfg)
    scores, missing_beta = rs.compute_scores(
        data, ids, str(variant["score"]), h, grid, max_age, names_min
    )
    labels = rs.compute_labels(data, ids, h, grid, max_age)
    both = np.isfinite(scores) & np.isfinite(labels)
    usable_cols = both.sum(axis=0) >= names_min
    qual = qualifying_mask(grid, usable_cols, p2["min_rebalances_per_session"])
    q_ts = grid[qual]
    q_sess = session_of(q_ts)
    cov = coverage(grid, qual, p2)
    n_usable = int(qual.sum())
    frac = both[:, qual].mean(axis=1) if n_usable else np.zeros(len(ids))
    names_ok = int((frac >= float(suff["names_usable_frac"])).sum())
    ic = [bb._spearman(scores[:, g], labels[:, g]) for g in np.flatnonzero(qual)]
    out: dict[str, Any] = {
        **base,
        "block_length": block,
        "n_rebalances": len(grid),
        "n_usable_all_sessions": int(usable_cols.sum()),
        "n_usable": n_usable,
        "usable_floor": int(suff["usable_rebalances_min"][str(int(reb))]),
        "names_usable_80pct": names_ok,
        "max_gap_min": within_session_max_gap_min(grid, qual),
        "missing_beta_cells": missing_beta,
        **cov,
        "rank_ic": series_stats_within(np.array(ic), q_sess, block, rule),
    }
    try:
        res = rs._run(data, ids, cfg, scores)
    except ValueError as exc:
        return {**out, "error": f"book: {exc}"}
    to_bps = 1e4 / notional
    usable_ts = [int(t) for t in q_ts]
    net = rs.decision_series(res)
    keep = [k for k, t in enumerate(usable_ts) if t in net]
    net_u = np.array([net[usable_ts[k]] * to_bps for k in keep])
    out["net_bps"] = series_stats_within(net_u, q_sess[keep] if keep else q_sess[:0], block, rule)
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
    cons = rs.decision_series(
        res,
        float(costs["conservative_spread_mult"]),
        float(costs["conservative_fee_mult"]),
        float(costs["conservative_impact_mult"]),
    )
    out["conservative_net_bps"] = rs._mean_at(cons, usable_ts) * to_bps
    contrib = rs.name_contributions(res)
    k_rm = int(th["C6_names_removed"])
    top = sorted(contrib, key=lambda i: (-abs(contrib[i]), i))[:k_rm]
    out["influential_names"] = [
        {"instrument_id": i, "symbol": data.instruments[i].symbol, "net_pnl": contrib[i]}
        for i in top
    ]
    keep_ids = [i for i in ids if i not in top]
    out["influential_removed_net_bps"] = float("nan")
    out["alt_normalisation_net_bps"] = float("nan")
    early = criteria_v2(out, rule)
    if short_circuit and not (early["C1"] and early["C2"] and early["C4"] and early["C3"]):
        out["robustness_books_computed"] = False
        return out
    out["robustness_books_computed"] = True
    if len(keep_ids) >= names_min:
        cfg6 = rs.book_config(rule, variant, names_min)
        g6 = rs._grid(data, keep_ids, cfg6)
        s6, _ = rs.compute_scores(data, keep_ids, str(variant["score"]), h, g6, max_age, names_min)
        try:
            r6 = rs._run(data, keep_ids, cfg6, s6)
            out["influential_removed_net_bps"] = (
                rs._mean_at(rs.decision_series(r6), usable_ts) * to_bps
            )
        except ValueError:
            pass
    s7, _ = rs.compute_scores(
        data, ids, str(variant["score"]), h, grid, max_age, names_min, alt=True
    )
    try:
        r7 = rs._run(data, ids, cfg, s7)
        out["alt_normalisation_net_bps"] = rs._mean_at(rs.decision_series(r7), usable_ts) * to_bps
    except ValueError:
        out["alt_normalisation_net_bps"] = float("nan")
    return out


def criteria_v2(stats: Mapping[str, Any], rule: Rule) -> dict[str, bool]:
    """C1-C8: the v1 criteria on v2 statistics, with coverage folded into C1."""
    c = rs.criteria(stats, rule)
    c["C1"] = bool(c["C1"] and stats.get("coverage_ok", False))
    return c


def decide_v2(results: Sequence[Mapping[str, Any]], rule: Rule) -> list[dict[str, Any]]:
    """Decision map of the rule over v2 criteria (cap as v1); no evidence-status upgrade is made."""
    rows = []
    for r in results:
        c = criteria_v2(r, rule)
        o, why = rs.outcome(c)
        if o == "underpowered" and not r.get("coverage_ok", True):
            why += " (session coverage)"
        rows.append({"id": r["id"], "criteria": c, "outcome": o, "reason": why})
    passing = [k for k, row in enumerate(rows) if row["outcome"] == "continue"]
    cap = int(rule.section("decision")["max_continue"])

    def lb(k: int) -> float:
        v = (results[k].get("net_bps") or {}).get("lower")
        return float(v) if isinstance(v, int | float) else -math.inf

    for rank, k in enumerate(sorted(passing, key=lambda k: (-lb(k), k))):
        if rank >= cap:
            rows[k]["outcome"] = "passed_not_expanded"
            rows[k]["reason"] = f"passed; not among the {cap} highest net-bps lower bounds"
    return rows


# -------------------------------------------------------------- driver --


class SessionError(ValueError):
    """The data does not hold exactly the registered sessions."""


def check_sessions(data: rs.ScreenData, expected_days: Sequence[int]) -> None:
    """The frames' session days must be a subset of ``expected_days`` (no unregistered session read).

    A registered day with no rows is allowed (it just fails to qualify: a missing session).
    """
    seen = set(int(d) for d in data.session_close())
    extra = sorted(seen - set(int(d) for d in expected_days))
    if extra:
        raise SessionError(f"data holds sessions outside the registered set: {extra}")


def run_screen_v2(
    data: rs.ScreenData,
    rule: Rule,
    expected_days: Sequence[int],
    variant_ids: Sequence[str] | None = None,
    short_circuit: bool = False,
) -> dict[str, Any]:
    """Evaluate the registered variants over the fixed sessions and decide (no ledger, no I/O).

    Real-data use needs the holdout guard with the exact session list, a signed v2 rule and the
    ledger looks; those are the signer/runner PR's job and are deliberately absent here.
    """
    check_sessions(data, expected_days)
    results = [
        evaluate_variant_v2(data, rule, v, short_circuit)
        for v in rule.variants
        if variant_ids is None or v["id"] in variant_ids
    ]
    decisions = decide_v2(results, rule)
    return {
        "label": SCREEN_LABEL_V2,
        "sessions_total": v2_params(rule)["sessions_total"],
        "variants": results,
        "decisions": decisions,
        "summary": {o: [d["id"] for d in decisions if d["outcome"] == o] for o in rs.OUTCOMES},
    }

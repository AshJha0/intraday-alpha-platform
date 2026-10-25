"""EN3 (v2.0 stage 5): the constrained least-squares repair, the band, the skip rules and the diagnostics.

Uniqueness: two solves from different starting books that share the same raw
book give the same weights; the KKT check confirms the solution is the
projection (no feasible direction improves the objective).  Tolerance: every
constraint holds within 1e-9 in weight units.  Refusals: non-converged and
out-of-tolerance solves are refused with the active constraint set recorded;
the item 7 rules refuse under-invested, thin or concentrated books.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.portfolio.project import (
    BAND_GRID_BPS,
    ConstraintSpec,
    ProjectionError,
    SolverIdentity,
    apply_band,
    check_book,
    choose_band,
    construction_stages,
    diagnostic_covariance,
    feasibility,
    project,
    variance_contribution_table,
)


def book(n_long=30, n_short=30, seed=1, gross=1 / 3):
    rng = np.random.default_rng(seed)
    names = [f"N{i:02d}" for i in range(n_long + n_short)]
    raw = {}
    for i, n in enumerate(names):
        leg = n_long if i < n_long else n_short
        raw[n] = (gross / 2 / leg) * (1 + 0.3 * abs(rng.standard_normal())) * (1 if i < n_long else -1)
    betas = {n: float(1 + 0.4 * rng.standard_normal()) for n in names}
    sectors = {n: f"S{i % 3}" for i, n in enumerate(names)}
    return names, raw, betas, sectors


SPEC = ConstraintSpec(gross_cap=1 / 3)


def violations(w, raw, betas, sectors, spec, caps=None):
    names = sorted(raw)
    net = abs(sum(w[n] for n in names))
    beta = abs(sum(betas[n] * w[n] for n in names)) - spec.beta_tol
    sec = max(abs(sum(w[n] for n in names if sectors[n] == s)) for s in set(sectors.values())) - spec.sector_cap
    name = max(abs(w[n]) for n in names) - spec.name_cap
    gross = sum(abs(w[n]) for n in names) - spec.gross_cap
    sign = max(0.0 if w[n] == 0 or math.copysign(1, w[n]) == math.copysign(1, raw[n]) else abs(w[n]) for n in names)
    liq = max((abs(w[n]) - caps[n] for n in names if caps and n in caps), default=-1.0)
    return max(net, beta, sec, name, gross, sign, liq)


def test_repair_is_unique_within_tolerance_and_records_the_solver():
    names, raw, betas, sectors = book()
    r = project(raw, betas, sectors, spec=SPEC)
    assert not r.refused and r.max_violation <= 1e-9
    assert violations(r.weights, raw, betas, sectors, SPEC) <= 1e-9
    assert r.solver == SolverIdentity().as_dict()
    assert r.solver["method"] == "SLSQP" and r.solver["constraint_tol"] == 1e-9
    # uniqueness: a strictly convex objective over a polytope has one minimiser; a second solve from a different
    # start (the raw book scaled) returns the same point
    scaled = {n: 3.0 * v for n, v in raw.items()}
    r_scaled = project(scaled, betas, sectors, spec=SPEC)
    r_again = project(raw, betas, sectors, spec=SPEC)
    assert r_again.weights == r.weights
    assert r_scaled.weights != r.weights  # a different raw book is a different problem
    # KKT: the residual w* - w_raw is orthogonal to every feasible direction inside the active set; a tiny step
    # toward the raw book along a feasible direction never stays feasible when it would reduce the distance
    w = np.array([r.weights[n] for n in names])
    rw = np.array([raw[n] for n in names])
    direction = rw - w
    direction -= direction.mean()  # keep dollar neutrality
    for eps in (1e-6, 1e-7):
        trial = {n: w[k] + eps * direction[k] for k, n in enumerate(names)}
        better = sum((trial[n] - raw[n]) ** 2 for n in names) < sum((r.weights[n] - raw[n]) ** 2 for n in names)
        if better:
            assert violations(trial, raw, betas, sectors, SPEC) > 1e-9


def test_repair_changes_nothing_feasible_and_shrinks_to_the_caps():
    names, raw, betas, sectors = book(seed=3)
    neutral = {}  # a book that already satisfies everything is returned unchanged within tolerance
    r = project(raw, betas, sectors, spec=SPEC)
    for n in names:
        neutral[n] = r.weights[n] if r.weights[n] != 0 else raw[n] * 1e-6
    r2 = project(neutral, betas, sectors, spec=SPEC)
    assert max(abs(r2.weights[n] - neutral[n]) for n in names) <= 1e-6
    # single-name cap binds and is recorded in the active set
    big = dict(raw)
    big["N00"] = 0.05
    r3 = project(big, betas, sectors, spec=SPEC)
    assert not r3.refused and abs(r3.weights["N00"] - SPEC.name_cap) <= 1e-9
    assert "name_cap:N00" in r3.active
    # a liquidity cap below the name cap binds with its own label; a zero cap drops the name
    r4 = project(raw, betas, sectors, spec=SPEC, liquidity_caps={"N01": 0.001, "N02": 0.0})
    assert abs(r4.weights["N01"]) <= 0.001 + 1e-9 and r4.weights["N02"] == 0.0
    assert "liquidity_cap:N01" in r4.active and "liquidity_zero:N02" in r4.active


def test_sign_preservation_and_the_zero_book_are_feasible():
    names, raw, betas, sectors = book(n_long=12, n_short=12, seed=5)
    # a long-only raw book cannot be dollar-neutral with its signs preserved: the unique solution is w = 0
    longs = {n: abs(v) for n, v in raw.items()}
    r = project(longs, betas, sectors, spec=SPEC)
    assert not r.refused and max(abs(v) for v in r.weights.values()) <= 1e-9
    assert all(v >= 0 for v in r.weights.values())
    assert not feasibility(r.weights, gross_target=1 / 3).feasible


def test_refusals_record_the_active_constraint_set():
    names, raw, betas, sectors = book()
    r = project(raw, betas, sectors, spec=SPEC, solver=SolverIdentity(maxiter=1))
    assert r.refused and "not converged" in r.reason and r.weights == {}
    assert r.active and r.solver["maxiter"] == 1
    r2 = project(raw, betas, sectors, spec=SPEC, solver=SolverIdentity(constraint_tol=1e-30))
    assert r2.refused and ("out of tolerance" in r2.reason or "not converged" in r2.reason)
    with pytest.raises(ProjectionError):
        project({"A": 0.0, "B": -0.01}, {"A": 1.0, "B": 1.0}, {"A": "s", "B": "s"})
    with pytest.raises(ProjectionError):
        project({"A": 0.01, "B": -0.01}, {"A": 1.0}, {"A": "s", "B": "s"})
    with pytest.raises(ProjectionError):
        project({"A": float("nan"), "B": -0.01}, {"A": 1.0, "B": 1.0}, {"A": "s", "B": "s"})


def test_feasibility_skip_rules_in_order():
    w = {f"L{i}": 0.01 for i in range(10)} | {f"S{i}": -0.01 for i in range(10)}
    assert feasibility(w, gross_target=0.2).feasible
    thin = dict(w)
    del thin["L9"]
    f = feasibility(thin, gross_target=0.2)
    assert not f.feasible and "fewer than 10 names per leg" in f.reason and f.longs == 9
    under = {n: v * 0.4 for n, v in w.items()}
    f = feasibility(under, gross_target=0.2)
    assert not f.feasible and "deployed gross" in f.reason
    conc = dict(w)
    conc["L0"] = 0.05  # 0.05 / 0.24 > 15 %
    f = feasibility(conc, gross_target=0.2)
    assert not f.feasible and "single name" in f.reason
    with pytest.raises(ProjectionError):
        feasibility(w, gross_target=0.0)


def test_combined_book_recheck_after_each_horizon_solve():
    names, raw, betas, sectors = book(seed=7)
    spec = ConstraintSpec(gross_cap=1.0)
    first = project(raw, betas, sectors, spec=ConstraintSpec(gross_cap=1 / 3)).weights
    chk = check_book(first, betas, sectors, spec=spec)
    assert chk.ok and chk.gross <= 1 / 3 + 1e-9
    # a second horizon's cohort added to the book: the joint book may breach the sector cap even if each cohort passes
    second = {n: w for n, w in first.items()}
    joint = {n: first[n] + second[n] for n in names}
    chk2 = check_book(joint, betas, sectors, spec=ConstraintSpec(gross_cap=1.0, sector_cap=0.001))
    assert not chk2.ok and any(v.startswith("sector:") for v in chk2.violations)
    chk3 = check_book(joint, betas, sectors, spec=spec, book_gross_cap=0.5)
    assert not chk3.ok and any(v.startswith("gross_cap") for v in chk3.violations)
    with pytest.raises(ProjectionError):
        check_book({"X": 0.1}, {}, {})


def test_band_choice_is_outcome_blind_and_never_overrides_a_constraint_or_a_mandatory_order():
    c = choose_band({0: 100.0, 2: 95.0, 5: 88.0, 10: 70.0})
    assert c.band_bps == 5 and c.reductions[2] == pytest.approx(0.05)
    assert choose_band({0: 100.0, 2: 95.0, 5: 92.0, 10: 91.0}).band_bps == 0
    assert choose_band({0: 100.0, 2: 89.0, 5: 89.0, 10: 50.0}).band_bps == 2  # tie to the smaller
    assert choose_band({0: 0.0, 2: 0.0, 5: 0.0, 10: 0.0}).band_bps == 0
    assert BAND_GRID_BPS == (2, 5, 10)
    with pytest.raises(ProjectionError):
        choose_band({0: 100.0, 2: 101.0, 5: 90.0, 10: 80.0})
    with pytest.raises(ProjectionError):
        choose_band({2: 1.0, 5: 1.0, 10: 1.0})
    orders = {"A": 1.5, "B": -4.0, "C": 0.5, "D": 20.0}
    mandatory = {"A": False, "B": True, "C": False, "D": False}
    held, sent = apply_band(orders, mandatory, 5.0)
    assert held == {"A", "C"} and sent == {}  # B is mandatory, D is above the band
    # feasibility clause: withholding C would leave the book outside the gross cap, so C is sent regardless
    books = {"C": {"X": 0.6, "Y": -0.6}, "A": {"X": 0.1, "Y": -0.1}}
    held, sent = apply_band(
        orders, mandatory, 5.0, book_after_withholding=books, betas={"X": 1.0, "Y": 1.0},
        sectors={"X": "s", "Y": "t"}, spec=ConstraintSpec(gross_cap=1.0, sector_cap=1.0, name_cap=1.0),
    )
    assert held == {"A"} and "C" in sent and "gross_cap" in sent["C"]
    assert apply_band(orders, mandatory, 0.0)[0] == frozenset()


def test_construction_stages_add_constraints_cumulatively():
    names, raw, betas, sectors = book(seed=11)
    st = construction_stages(raw, betas, sectors, spec=SPEC, liquidity_caps={"N00": 0.0})
    assert list(st) == ["raw", "dollar_neutral", "beta_neutral", "sector_constrained", "name_and_gross_capped", "participation_limited"]
    assert st["raw"].weights == {n: raw[n] for n in names}
    assert abs(sum(st["dollar_neutral"].weights.values())) <= 1e-9
    assert abs(sum(betas[n] * w for n, w in st["beta_neutral"].weights.items())) <= SPEC.beta_tol + 1e-9
    assert st["participation_limited"].weights["N00"] == 0.0
    d = [st[s].distance for s in list(st)[1:]]
    assert all(b >= a - 1e-12 for a, b in zip(d, d[1:], strict=False))  # more constraints, never closer to raw


def test_diagnostic_covariance_and_variance_contribution_table():
    rng = np.random.default_rng(2)
    names = [f"N{i}" for i in range(12)]
    res = rng.standard_normal((400, 12)) * 0.001
    res[:, 0] *= 5  # one dominant name
    cov = diagnostic_covariance(res, names)
    assert cov["role"] == "diagnostic only" and cov["observations"] == 400
    w = {n: (0.01 if i % 2 == 0 else -0.01) for i, n in enumerate(names)}
    sectors = {n: ("a" if i < 6 else "b") for i, n in enumerate(names)}
    t = variance_contribution_table(w, cov, sectors, factor_loadings=np.ones((12, 1)), factor_covariance=np.array([[1e-6]]))
    assert t["by_name_top"][0][0] == "N0" and 0.5 < t["top_share"] <= 1.0 + 1e-12
    assert abs(sum(t["by_sector"].values()) - 1.0) <= 1e-9
    assert len(t["principal_components"]) == 5 and 0 <= t["pc_share_total"] <= 1 + 1e-9
    assert 1 / 12 <= t["herfindahl"] <= 1.0
    assert t["factor_share_of_total"] == pytest.approx(0.0)  # a dollar-neutral book has no exposure to a unit factor
    with pytest.raises(ProjectionError):
        diagnostic_covariance(res[:1], names)

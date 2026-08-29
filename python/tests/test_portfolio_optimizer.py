"""PGD optimizer: projections, constraints, KKT-style checks, determinism."""

from __future__ import annotations

import numpy as np
import pytest

from iap.core.rng import SplitMix64
from iap.portfolio.diagnostics import constraint_audit
from iap.portfolio.optimizer import (
    Constraints,
    max_violation,
    objective,
    project,
    project_l1_ball,
    solve,
)


def _problem(seed: int = 5, n: int = 8):
    rng = SplitMix64(seed)
    alpha = np.array([rng.normal() * 2e-3 for _ in range(n)])
    A = np.array([[rng.normal() for _ in range(n)] for _ in range(n)]) * 0.01
    Sigma = A @ A.T + np.eye(n) * 4e-4
    w_prev = np.array([rng.normal() * 0.05 for _ in range(n)])
    tc = np.full(n, 1e-4)
    cons = Constraints(
        w_min=np.full(n, -0.25), w_max=np.full(n, 0.25),
        gross_cap=1.0, net_cap=0.3, participation=np.full(n, 0.2),
        turnover_cap=0.8, vol_target=0.02)
    return alpha, Sigma, w_prev, tc, cons


def test_project_l1_ball_hand_cases():
    v = np.array([0.3, -0.1])
    assert np.array_equal(project_l1_ball(v, 1.0), v)  # inside: unchanged
    p = project_l1_ball(np.array([1.0, 1.0]), 1.0)
    assert abs(np.abs(p).sum() - 1.0) < 1e-12
    assert abs(p[0] - 0.5) < 1e-12 and abs(p[1] - 0.5) < 1e-12
    p = project_l1_ball(np.array([2.0, -1.0]), 1.0)
    assert abs(np.abs(p).sum() - 1.0) < 1e-12
    assert p[0] == 1.0 and p[1] == 0.0  # exact simplex projection
    assert np.array_equal(project_l1_ball(np.array([3.0]), 0.0),
                          np.array([0.0]))


def test_projection_satisfies_all_constraints():
    alpha, Sigma, w_prev, tc, cons = _problem()
    cons.validate(8)
    rng = SplitMix64(99)
    for _ in range(20):
        v = np.array([rng.normal() for _ in range(8)])
        w = project(v, cons, w_prev, Sigma, passes=12)
        assert max_violation(w, cons, w_prev, Sigma) < 1e-6


def test_objective_hand_case():
    alpha = np.array([0.01, 0.02])
    Sigma = np.array([[0.04, 0.0], [0.0, 0.01]])
    w = np.array([0.5, -0.5])
    w_prev = np.array([0.0, 0.0])
    tc = np.array([0.001, 0.002])
    # alpha'w = 0.005 - 0.01 = -0.005 ; w'Sw = 0.01 + 0.0025 = 0.0125
    # tc term = 0.001*0.5 + 0.002*0.5 = 0.0015
    got = objective(w, alpha, Sigma, w_prev, 2.0, tc)
    assert abs(got - (-0.005 - 2.0 * 0.0125 - 0.0015)) < 1e-15


def test_solve_returns_feasible_weights():
    alpha, Sigma, w_prev, tc, cons = _problem()
    res = solve(alpha, Sigma, w_prev, 5.0, tc, cons)
    assert res.max_violation < 1e-7
    assert np.all(res.weights >= cons.w_min - 1e-9)
    assert np.all(res.weights <= cons.w_max + 1e-9)
    assert np.abs(res.weights).sum() <= cons.gross_cap + 1e-7
    assert abs(res.weights.sum()) <= cons.net_cap + 1e-7


def test_solve_deterministic():
    alpha, Sigma, w_prev, tc, cons = _problem()
    r1 = solve(alpha, Sigma, w_prev, 5.0, tc, cons)
    r2 = solve(alpha, Sigma, w_prev, 5.0, tc, cons)
    assert np.array_equal(r1.weights, r2.weights)
    assert r1.objective == r2.objective
    assert r1.best_iteration == r2.best_iteration


def test_solve_beats_projected_candidates():
    """Brute-force check: no projected candidate beats the PGD solution."""
    alpha, Sigma, w_prev, tc, cons = _problem()
    res = solve(alpha, Sigma, w_prev, 5.0, tc, cons,
                iters=1500, step_decay=0.002, proj_passes=12)
    rng = SplitMix64(123)
    best_cand = -np.inf
    for _ in range(300):
        v = np.array([rng.normal() * 0.2 for _ in range(8)])
        w = project(v, cons, w_prev, Sigma, passes=12)
        if max_violation(w, cons, w_prev, Sigma) <= 1e-7:
            best_cand = max(best_cand,
                            objective(w, alpha, Sigma, w_prev, 5.0, tc))
    assert res.objective >= best_cand - 1e-9


def test_solve_matches_slsqp_reference():
    """CVX-style reference: SLSQP optimum within 1e-5 of the PGD objective."""
    from scipy.optimize import minimize
    alpha, Sigma, w_prev, tc, cons = _problem()
    res = solve(alpha, Sigma, w_prev, 5.0, tc, cons,
                iters=1500, step_decay=0.002, proj_passes=12)

    def negf(w):
        return -objective(w, alpha, Sigma, w_prev, 5.0, tc)

    cl = [
        {"type": "ineq", "fun": lambda w: cons.gross_cap - np.abs(w).sum()},
        {"type": "ineq", "fun": lambda w: cons.net_cap - abs(w.sum())},
        {"type": "ineq",
         "fun": lambda w: cons.turnover_cap - np.abs(w - w_prev).sum()},
        {"type": "ineq",
         "fun": lambda w: cons.vol_target ** 2 - w @ Sigma @ w},
    ]
    bounds = [(max(cons.w_min[i], w_prev[i] - cons.participation[i]),
               min(cons.w_max[i], w_prev[i] + cons.participation[i]))
              for i in range(8)]
    ref = -np.inf
    for x0 in (w_prev, np.zeros(8), res.weights):
        r = minimize(negf, x0, bounds=bounds, constraints=cl,
                     method="SLSQP",
                     options={"maxiter": 1000, "ftol": 1e-14})
        if r.success:
            ref = max(ref, -r.fun)
    assert ref > -np.inf
    assert res.objective >= ref - 1e-5


def test_no_improving_feasible_coordinate_move():
    """KKT-style check: projected coordinate perturbations do not improve."""
    alpha, Sigma, w_prev, tc, cons = _problem()
    res = solve(alpha, Sigma, w_prev, 5.0, tc, cons,
                iters=1500, step_decay=0.002, proj_passes=12)
    f0 = res.objective
    eps = 1e-4
    for i in range(8):
        for s in (+1.0, -1.0):
            v = res.weights.copy()
            v[i] += s * eps
            w = project(v, cons, w_prev, Sigma, passes=12)
            if max_violation(w, cons, w_prev, Sigma) <= 1e-7:
                # a genuinely suboptimal point improves linearly
                # (~eps * |grad| ~ 3e-7); PGD terminal noise is ~3e-8
                assert objective(w, alpha, Sigma, w_prev, 5.0, tc) \
                    <= f0 + 1e-7


def test_unconstrained_analytic_optimum():
    """With loose constraints and no t-costs: w* = Sigma^-1 alpha / (2 lam)."""
    n = 4
    alpha = np.array([0.002, -0.001, 0.0015, 0.0005])
    Sigma = np.diag([4e-4, 5e-4, 3e-4, 6e-4])
    lam = 5.0
    w_star = np.linalg.solve(2.0 * lam * Sigma, alpha)
    cons = Constraints(w_min=np.full(n, -10.0), w_max=np.full(n, 10.0))
    res = solve(alpha, Sigma, np.zeros(n), lam, np.zeros(n), cons,
                iters=3000, step_decay=0.0)
    assert np.max(np.abs(res.weights - w_star)) < 1e-6


def test_vol_target_binds_and_audited():
    alpha, Sigma, w_prev, tc, cons = _problem()
    cons.vol_target = 0.004  # force it to bind
    res = solve(alpha, Sigma, w_prev, 5.0, tc, cons,
                iters=1500, step_decay=0.002, proj_passes=12)
    audit = constraint_audit(res.weights, cons, w_prev, Sigma)
    vol_rows = [r for r in audit["constraints"] if r["name"] == "volatility"]
    assert len(vol_rows) == 1
    assert audit["realized_vol"] <= 0.004 + 1e-7
    assert vol_rows[0]["binding"]
    assert audit["target_vol"] == 0.004


def test_constraint_audit_reports_turnover_and_exposures():
    n = 3
    cons = Constraints(w_min=np.full(n, -1.0), w_max=np.full(n, 1.0),
                       gross_cap=0.5, net_cap=0.1)
    w = np.array([0.3, -0.2, 0.0])
    audit = constraint_audit(w, cons, np.zeros(n))
    assert abs(audit["gross"] - 0.5) < 1e-15
    assert abs(audit["net"] - 0.1) < 1e-15
    assert abs(audit["turnover"] - 0.5) < 1e-15
    names = {r["name"] for r in audit["constraints"]}
    assert {"gross_exposure", "net_exposure"} <= names
    assert audit["n_binding"] >= 2  # gross and net both at their caps


def test_solver_input_validation():
    n = 2
    cons = Constraints(w_min=np.zeros(n), w_max=np.ones(n))
    a, S, wp, tc = np.zeros(n), np.eye(n), np.zeros(n), np.zeros(n)
    with pytest.raises(ValueError):
        solve(a, np.eye(3), wp, 1.0, tc, cons)  # bad Sigma shape
    with pytest.raises(ValueError):
        solve(a, S, wp, -1.0, tc, cons)  # negative risk aversion
    with pytest.raises(ValueError):
        solve(a, S, wp, 1.0, np.array([-1e-4, 0.0]), cons)  # negative tc
    with pytest.raises(ValueError):
        solve(a, np.array([[1.0, 0.5], [0.0, 1.0]]), wp, 1.0, tc, cons)
    with pytest.raises(ValueError):
        bad = Constraints(w_min=np.ones(n), w_max=np.zeros(n))
        solve(a, S, wp, 1.0, tc, bad)  # w_min > w_max
    with pytest.raises(ValueError):
        c2 = Constraints(w_min=np.zeros(n), w_max=np.ones(n),
                         vol_target=0.01)
        c2.validate(n)
        project(np.ones(n), c2, wp, None)  # vol target without Sigma

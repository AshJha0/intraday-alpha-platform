"""Golden test: pinned 8-FX-pair portfolio problem (tests/golden, 1e-9)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from iap.portfolio.optimizer import (
    Constraints,
    max_violation,
    objective,
    solve,
)


@pytest.fixture(scope="module")
def golden(golden_dir):
    with open(golden_dir / "expected_portfolio.json") as f:
        return json.load(f)


def _build(golden):
    p = golden["problem"]
    c = p["constraints"]
    cons = Constraints(
        w_min=np.array(c["w_min"]),
        w_max=np.array(c["w_max"]),
        gross_cap=c["gross_cap"],
        net_cap=c["net_cap"],
        participation=np.array(c["participation"]),
        turnover_cap=c["turnover_cap"],
        vol_target=c["vol_target"],
        currency_matrix=np.array(p["currency_matrix"]),
        currency_bounds=np.array(c["currency_bounds"]),
    )
    return (np.array(p["alpha"]), np.array(p["sigma"]),
            np.array(p["w_prev"]), p["risk_aversion"],
            np.array(p["tc_linear"]), cons, p["solver"])


def test_golden_portfolio_weights_and_objective(golden):
    alpha, Sigma, w_prev, ra, tc, cons, solver = _build(golden)
    res = solve(alpha, Sigma, w_prev, ra, tc, cons, **solver)
    exp = golden["expected"]
    tol = exp["tolerance"]
    assert tol == 1e-9
    got = res.weights
    want = np.array(exp["weights"])
    assert np.max(np.abs(got - want)) <= tol
    assert abs(res.objective - exp["objective"]) <= tol
    assert res.best_iteration == exp["best_iteration"]


def test_golden_portfolio_feasible(golden):
    alpha, Sigma, w_prev, ra, tc, cons, solver = _build(golden)
    cons.validate(8)
    w = np.array(golden["expected"]["weights"])
    assert max_violation(w, cons, w_prev, Sigma) <= 1e-7
    # objective recomputed from the stored weights matches the stored value
    f = objective(w, alpha, Sigma, w_prev, ra, tc)
    assert abs(f - golden["expected"]["objective"]) <= 1e-9


def test_golden_portfolio_near_reference_optimum(golden):
    """CVX-style validation: SLSQP reference optimum within 1e-5."""
    from scipy.optimize import minimize
    alpha, Sigma, w_prev, ra, tc, cons, _ = _build(golden)
    cons.validate(8)
    E = cons.currency_matrix
    cb = cons.currency_bounds

    def negf(w):
        return -objective(w, alpha, Sigma, w_prev, ra, tc)

    cl = [
        {"type": "ineq", "fun": lambda w: cons.gross_cap - np.abs(w).sum()},
        {"type": "ineq", "fun": lambda w: cons.net_cap - abs(w.sum())},
        {"type": "ineq",
         "fun": lambda w: cons.turnover_cap - np.abs(w - w_prev).sum()},
        {"type": "ineq",
         "fun": lambda w: cons.vol_target ** 2 - w @ Sigma @ w},
    ]
    for i in range(E.shape[0]):
        cl.append({"type": "ineq",
                   "fun": (lambda i: lambda w: cb[i] - abs(E[i] @ w))(i)})
    bounds = [(max(cons.w_min[i], w_prev[i] - cons.participation[i]),
               min(cons.w_max[i], w_prev[i] + cons.participation[i]))
              for i in range(8)]
    w_g = np.array(golden["expected"]["weights"])
    ref = -np.inf
    for x0 in (w_prev, np.zeros(8), w_g):
        r = minimize(negf, x0, bounds=bounds, constraints=cl,
                     method="SLSQP",
                     options={"maxiter": 2000, "ftol": 1e-14})
        if r.success:
            ref = max(ref, -r.fun)
    assert ref > -np.inf
    assert golden["expected"]["objective"] >= ref - 1e-5


def test_golden_problem_is_pinned_fx_universe(golden):
    p = golden["problem"]
    assert len(p["alpha"]) == 8
    assert len(p["pair_symbols"]) == 8
    E = np.array(p["currency_matrix"])
    assert E.shape == (len(p["currencies"]), 8)
    Sigma = np.array(p["sigma"])
    assert np.allclose(Sigma, Sigma.T)
    assert np.all(np.linalg.eigvalsh(Sigma) > 0)

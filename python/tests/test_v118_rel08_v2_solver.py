"""Deterministic solver checks behind the REL-08 v2 book parameters (synthetic arrays only).

The reviewer (2026-10-24, named change 5) asked for tests of convergence, turnover caps, objective
improvement and sensitivity to the iteration count.  They characterise the optimiser; they do not say
which (alpha_bps, risk_aversion) is right, and nothing here interprets C2/C3.

Known and NOT explained (documented, not tested): on the real 2019-01-30 session the V1 and V5 books
traded about 50x apart at the registered v1 setting, while the dry run on synthetic data could not
reproduce a gap of that size (research/v118/rel08/dryrun/REL08_V2_DRYRUN_REPORT.md).
"""

from __future__ import annotations

import numpy as np
import pytest
from iap.portfolio.optimizer import Constraints, objective, solve

N = 40
CAP = 0.45


def _market(seed: int = 3):
    rng = np.random.default_rng(seed)
    F = rng.normal(size=(N, 3)) * 0.01  # rank-deficient factor covariance plus a small diagonal,
    sigma = F @ F.T + np.eye(N) * 1e-6  # like the book's EWMA estimate
    z = rng.normal(size=N)
    z -= z.mean()
    return sigma, z / np.abs(z).max()


def _cons(cap: float = CAP) -> Constraints:
    return Constraints(
        w_min=np.full(N, -0.05),
        w_max=np.full(N, 0.05),
        gross_cap=1.0,
        net_cap=0.0,
        participation=np.full(N, 1.0),
        turnover_cap=cap,
    )


def _solve(alpha_bps: float, ra: float, iters: int = 20, w_prev=None, cap: float = CAP):
    sigma, z = _market()
    alpha = alpha_bps * 1e-4 * z
    w0 = np.zeros(N) if w_prev is None else w_prev
    return (
        solve(alpha, sigma, w0, ra, np.zeros(N), _cons(cap), iters=iters),
        alpha,
        sigma,
        w0,
    )


GRID = [(a, r) for a in (5.0, 20.0, 40.0) for r in (30.0, 100.0, 1000.0)]


@pytest.mark.parametrize(("alpha_bps", "ra"), GRID)
def test_turnover_cap_and_feasibility(alpha_bps, ra):
    res, _, _, w0 = _solve(alpha_bps, ra)
    assert res.feasible
    assert np.abs(res.weights - w0).sum() <= CAP + 1e-6
    assert np.abs(res.weights).sum() <= 1.0 + 1e-6
    assert abs(res.weights.sum()) <= 1e-6


@pytest.mark.parametrize(("alpha_bps", "ra"), GRID)
def test_never_worse_than_holding(alpha_bps, ra):
    """Hold is a candidate, so the returned objective is at least the held book's."""
    rng = np.random.default_rng(11)
    w_prev = rng.normal(size=N) * 0.01
    w_prev -= w_prev.mean()
    res, alpha, sigma, _ = _solve(alpha_bps, ra, w_prev=w_prev)
    hold = objective(w_prev, alpha, sigma, w_prev, ra, np.zeros(N))
    assert res.objective >= hold - 1e-12


def test_convergence_to_analytic_optimum_when_constraints_are_slack():
    """Strictly convex, caps not binding: w* = Sigma^-1 alpha / (2 ra) (net cap slack for this alpha)."""
    rng = np.random.default_rng(5)
    A = rng.normal(size=(N, N)) * 0.01
    sigma = A @ A.T + np.eye(N) * 1e-3
    alpha = rng.normal(size=N) * 1e-6
    ra = 100.0
    cons = Constraints(
        w_min=np.full(N, -10.0),
        w_max=np.full(N, 10.0),
        gross_cap=1e6,
        net_cap=1e6,
        participation=np.full(N, 1e6),
        turnover_cap=1e6,
    )
    res = solve(alpha, sigma, np.zeros(N), ra, np.zeros(N), cons, iters=5000)
    w_star = np.linalg.solve(sigma, alpha) / (2.0 * ra)
    f_star = objective(w_star, alpha, sigma, np.zeros(N), ra, np.zeros(N))
    assert res.objective <= f_star + 1e-15  # the optimum is an upper bound
    assert f_star - res.objective <= 1e-3 * abs(f_star)
    assert np.linalg.norm(res.weights - w_star) <= 5e-2 * np.linalg.norm(w_star)


def test_objective_nondecreasing_in_iteration_count():
    """The same iterate sequence is searched for the best point: more iterations never lose."""
    for alpha_bps, ra in GRID:
        objs = [_solve(alpha_bps, ra, iters=k)[0].objective for k in (1, 5, 20, 100, 500)]
        assert all(b >= a - 1e-12 for a, b in zip(objs, objs[1:], strict=False)), (alpha_bps, ra)


def test_deterministic():
    a = _solve(20.0, 100.0)[0]
    b = _solve(20.0, 100.0)[0]
    assert np.array_equal(a.weights, b.weights) and a.objective == b.objective


def test_trade_size_sensitivity_is_documented_not_assumed_monotone():
    """Trade size (L1 distance from the previous book) for the grid at 20 vs 100 iterations.

    This only pins what the solver does today: the books are within the cap, the iteration count can
    change the traded amount, and no monotone relation to alpha or risk aversion is asserted
    (the dry-run report found there is none).
    """
    sizes = {}
    for alpha_bps, ra in GRID:
        for it in (20, 100):
            res, _, _, w0 = _solve(alpha_bps, ra, iters=it)
            sizes[(alpha_bps, ra, it)] = float(np.abs(res.weights - w0).sum())
    assert all(0.0 <= v <= CAP + 1e-6 for v in sizes.values())
    # a book with a stronger signal relative to risk is not forced to trade more: record, do not rely
    assert len({round(v, 6) for v in sizes.values()}) > 1

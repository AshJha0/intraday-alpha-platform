"""v1.16 item 14: opt-in Dykstra projection and estimated Ledoit-Wolf intensity."""

from __future__ import annotations

import numpy as np
import pytest
from iap.combine.weights import ledoit_wolf as lw_combine
from iap.core.shrinkage import ledoit_wolf
from iap.portfolio.covariance import estimated_lw_intensity, ewma_covariance
from iap.portfolio.optimizer import Constraints, project, project_dykstra, solve


def _cons(**kw):
    base = dict(w_min=np.full(2, -1.0), w_max=np.full(2, 1.0))
    base.update(kw)
    return Constraints(**base)


def test_dykstra_exact_box_and_net_where_cyclic_is_not():
    # v = (2, 0.4) onto [-1, 1]^2 ∩ {|x1 + x2| <= 1}.  Exact answer (1, 0):
    # KKT  v - x = (1, 0.4) = 0.4 * (1, 1) + 0.6 * (1, 0), multipliers >= 0.
    # Cyclic: box -> (1, 0.4), halfspace -> (0.8, 0.2), already feasible.
    cons = _cons(net_cap=1.0)
    v, w_prev = np.array([2.0, 0.4]), np.zeros(2)
    np.testing.assert_allclose(project(v, cons, w_prev, None), [0.8, 0.2], atol=1e-12)
    np.testing.assert_allclose(project_dykstra(v, cons, w_prev, None), [1.0, 0.0], atol=1e-9)


def test_dykstra_identity_on_feasible_point_and_validation():
    cons = _cons(net_cap=1.0)
    x = np.array([0.3, -0.2])
    np.testing.assert_array_equal(project_dykstra(x, cons, np.zeros(2), None), x)
    with pytest.raises(ValueError):
        project_dykstra(x, cons, np.zeros(2), None, max_iter=0)


def test_solve_projection_default_unchanged_and_dykstra_opt_in():
    rng = np.random.default_rng(0)
    n = 4
    a = rng.normal(size=(n, n))
    sigma = a @ a.T / n + np.eye(n) * 0.1
    alpha = rng.normal(size=n)
    cons = Constraints(w_min=np.full(n, -0.5), w_max=np.full(n, 0.5), net_cap=0.2, gross_cap=1.0)
    args = (alpha, sigma, np.zeros(n), 1.0, np.full(n, 0.01), cons)
    r0 = solve(*args)
    r1 = solve(*args, projection="cyclic")
    np.testing.assert_array_equal(r0.weights, r1.weights)
    rd = solve(*args, projection="dykstra")
    assert rd.feasible
    with pytest.raises(ValueError):
        solve(*args, projection="nope")


_X = np.array(
    [[1.0, 2.0, 0.5], [0.0, 1.5, -1.0], [2.0, -0.5, 0.3], [-1.0, 0.7, 1.2], [0.4, 0.1, -0.6]]
)


def test_lw_shared_and_reexported():
    assert lw_combine is ledoit_wolf


def test_lw_intensity_hand_formula():
    n, p = _X.shape
    xc = _X - _X.mean(axis=0)
    s = xc.T @ xc / n
    mu = np.trace(s) / p
    d2 = np.sum((s - mu * np.eye(p)) ** 2)
    b2 = sum(np.sum((np.outer(r, r) - s) ** 2) for r in xc) / n**2
    assert estimated_lw_intensity(_X) == pytest.approx(min(b2, d2) / d2, rel=1e-12)


def test_lw_intensity_matches_sklearn():
    sk = pytest.importorskip("sklearn.covariance")
    assert estimated_lw_intensity(_X) == pytest.approx(sk.ledoit_wolf_shrinkage(_X), rel=1e-10)


def test_ewma_estimated_shrinkage_opt_in():
    rng = np.random.default_rng(1)
    r = rng.normal(size=(60, 3)) * 1e-3
    base = ewma_covariance(r)
    np.testing.assert_array_equal(base, ewma_covariance(r, shrinkage=None))
    fixed = ewma_covariance(r, shrinkage="ledoit_wolf")
    est = ewma_covariance(r, shrinkage="ledoit_wolf_estimated", ridge=0.0)
    raw = ewma_covariance(r, ridge=0.0)
    d = estimated_lw_intensity(r)
    mu = np.trace(raw) / 3
    np.testing.assert_allclose(est, (1 - d) * raw + d * mu * np.eye(3), rtol=1e-12)
    assert not np.allclose(fixed, base)
    with pytest.raises(ValueError):
        ewma_covariance(r, shrinkage="bogus")

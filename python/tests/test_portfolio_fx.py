"""Currency-exposure translation + FX constraint enforcement (spec §15)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.portfolio.fx import currency_exposure_matrix
from iap.portfolio.optimizer import Constraints, solve


def test_currency_matrix_hand_case():
    currencies, E = currency_exposure_matrix(["EUR/USD", "EUR/GBP",
                                             "GBP/USD"])
    assert currencies == ["EUR", "GBP", "USD"]
    assert E.shape == (3, 3)
    # columns: EUR/USD, EUR/GBP, GBP/USD
    assert E[:, 0].tolist() == [1.0, 0.0, -1.0]
    assert E[:, 1].tolist() == [1.0, -1.0, 0.0]
    assert E[:, 2].tolist() == [0.0, 1.0, -1.0]
    # hand exposure: long 0.1 EUR/USD, long 0.2 EUR/GBP, short 0.05 GBP/USD
    w = np.array([0.1, 0.2, -0.05])
    expo = E @ w
    assert abs(expo[0] - 0.3) < 1e-15                # EUR: 0.1 + 0.2
    assert abs(expo[1] - (-0.2 - 0.05)) < 1e-15      # GBP: -0.2 - 0.05
    assert abs(expo[2] - (-0.1 + 0.05)) < 1e-15     # USD: -0.1 + 0.05


def test_currency_matrix_column_sums_zero():
    # every pair is long one currency, short another: columns sum to 0
    pairs = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD",
             "USD/CHF", "NZD/USD", "EUR/GBP"]
    _, E = currency_exposure_matrix(pairs)
    assert np.array_equal(E.sum(axis=0), np.zeros(len(pairs)))
    assert np.all(np.abs(E).sum(axis=0) == 2.0)


def test_currency_matrix_validation():
    for bad in ("EURUSD", "EUR/", "/USD", "EUR/EUR"):
        with pytest.raises(ValueError):
            currency_exposure_matrix([bad])


def test_currency_bound_enforced_by_solver():
    pairs = ["EUR/USD", "EUR/GBP"]
    currencies, E = currency_exposure_matrix(pairs)
    n = 2
    alpha = np.array([0.01, 0.01])  # wants max long both (EUR-heavy)
    Sigma = np.eye(n) * 1e-4
    bound = 0.15
    cons = Constraints(
        w_min=np.full(n, -0.5), w_max=np.full(n, 0.5),
        currency_matrix=E, currency_bounds=np.full(len(currencies), bound))
    res = solve(alpha, Sigma, np.zeros(n), 1.0, np.zeros(n), cons,
                iters=1000, step_decay=0.002, proj_passes=12)
    expo = np.abs(E @ res.weights)
    assert np.all(expo <= bound + 1e-7)
    # EUR exposure (w0 + w1) must actually bind: unconstrained wants 1.0
    eur = currencies.index("EUR")
    assert expo[eur] > bound - 1e-4

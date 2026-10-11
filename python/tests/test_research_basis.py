"""A2 QQQ basket / basis / lead-lag: synthetic-data tests only."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.research.basis import (
    ETF_SYMBOL,
    QQQ_TOP15_WEIGHTS_PCT,
    basis_bps,
    basket_index,
    half_life,
    lead_lag,
    log_returns,
    normalize_weights,
    residual_returns,
    run_basis_study,
    study_symbols,
)


def _synthetic(n=3000, lead=2, seed=0, noise=2e-5):
    """Constituents follow a common factor; QQQ follows the basket ``lead``
    bars later, plus an AR(1) basis."""
    rng = np.random.default_rng(seed)
    syms = list(QQQ_TOP15_WEIGHTS_PCT)
    f = rng.normal(0, 1e-4, n)
    rets = f[:, None] + rng.normal(0, 5e-5, (n, len(syms)))
    prices = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), columns=syms)
    w = normalize_weights(QQQ_TOP15_WEIGHTS_PCT)
    bret = np.log(basket_index(prices, w)).diff().fillna(0).to_numpy()
    eret = np.roll(bret, lead)
    eret[:lead] = 0
    ou = np.zeros(n)
    for t in range(1, n):
        ou[t] = 0.8 * ou[t - 1] + rng.normal(0, noise)
    prices[ETF_SYMBOL] = 400 * np.exp(np.cumsum(eret) + ou)
    return prices


def test_symbol_plan():
    syms = study_symbols()
    assert syms[0] == "QQQ" and syms[1] == "NVDA" and len(syms) == 16
    assert abs(sum(QQQ_TOP15_WEIGHTS_PCT.values()) - 61.16) < 1e-9


def test_normalize_weights():
    w = normalize_weights({"A": 2.0, "B": 6.0, "C": 2.0}, available=["A", "B"])
    assert w.to_dict() == {"A": 0.25, "B": 0.75}
    with pytest.raises(ValueError):
        normalize_weights({"A": 1.0}, available=["Z"])
    with pytest.raises(ValueError):
        normalize_weights({"A": -1.0, "B": 2.0})


def test_basket_index_buy_and_hold():
    px = pd.DataFrame({"A": [10.0, 20.0], "B": [5.0, 5.0]})
    b = basket_index(px, {"A": 1.0, "B": 3.0})
    assert b.tolist() == [1.0, 0.25 * 2 + 0.75 * 1]
    with pytest.raises(ValueError):
        basket_index(pd.DataFrame({"A": [1.0, np.nan]}), {"A": 1.0})


def test_basis_zero_when_etf_equals_basket():
    px = _synthetic(200, lead=0, noise=0.0)
    w = normalize_weights(QQQ_TOP15_WEIGHTS_PCT)
    bk = basket_index(px[list(w.index)], w)
    np.testing.assert_allclose(basis_bps(px[ETF_SYMBOL], bk), 0.0, atol=1e-8)


def test_residual_beta_and_no_lookahead():
    rng = np.random.default_rng(2)
    b = pd.Series(rng.normal(0, 1e-4, 1000))
    e = 0.9 * b + pd.Series(rng.normal(0, 1e-6, 1000))
    fit = residual_returns(e, b, train_frac=0.5)
    assert abs(fit.beta - 0.9) < 1e-2 and fit.n_train == 500
    # Changing test-slice data does not change the fitted beta.
    e2 = e.copy()
    e2.iloc[600:] *= 5
    assert residual_returns(e2, b, train_frac=0.5).beta == fit.beta


def test_lead_lag_detects_basket_lead():
    px = _synthetic(lead=2)
    w = normalize_weights(QQQ_TOP15_WEIGHTS_PCT)
    br = log_returns(basket_index(px[list(w.index)], w))
    er = log_returns(px[ETF_SYMBOL])
    ll = lead_lag(er, br, max_lag=5)
    assert ll.peak_lag == 2 and ll.ratio > 10
    rev = lead_lag(br, er, max_lag=5)
    assert rev.peak_lag == -2 and rev.ratio < 0.1
    assert ll.lags.tolist() == list(range(-5, 6))


def test_half_life():
    rng = np.random.default_rng(0)
    x = np.zeros(20000)
    for t in range(1, x.size):
        x[t] = 0.5 * x[t - 1] + rng.normal()
    assert abs(half_life(pd.Series(x)) - 1.0) < 0.1
    assert half_life(pd.Series(np.cumsum(np.ones(10)))) == float("inf")


def test_run_basis_study_deterministic():
    a = run_basis_study(_synthetic(seed=5), max_lag=4).summary()
    b = run_basis_study(_synthetic(seed=5), max_lag=4).summary()
    assert a == b
    assert a["n_constituents"] == 15 and a["peak_lag"] == 2
    assert np.isfinite(a["basis_half_life_bars"])

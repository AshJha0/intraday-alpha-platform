"""The leakage canaries run through the ML path: a future-shifted feature and a shuffled label score at chance for the fitted
models; a planted leak is caught by the contract check before any model sees it; the ML models are deterministic."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from iap.data import synthetic
from iap.data.contracts import LeakageDetected, check_causality
from iap.data.leakage import chance_band, ic, planted_leak_canary
from iap.ml import ElasticNetModel, RidgeModel

FEATURES = ("canary.signal", "canary.trailing_mean_ret")


@pytest.fixture(scope="module")
def panel() -> pd.DataFrame:
    f = synthetic.make_canary_panel(seed=20261024, n_periods=120, n_names=60, beta=0.3)
    f["canary.signal"] = synthetic.feature_canary_signal(f)
    f["canary.trailing_mean_ret"] = synthetic.feature_canary_trailing_mean_ret(f)
    return f


def _split(f: pd.DataFrame):
    train = f["period"] < 80
    valid = f["period"] >= 82  # embargo of one label period
    return train, valid


@pytest.mark.parametrize(
    "cls",
    [lambda: RidgeModel(FEATURES, 1.0), lambda: ElasticNetModel(FEATURES, 0.01, l1_ratio=0.5)],
)
def test_fitted_models_score_above_chance_and_their_future_shifted_twin_at_chance(
    panel, cls
) -> None:
    f = panel
    train, valid = _split(f)
    ok = np.all(np.isfinite(f[list(FEATURES)]), axis=1) & np.isfinite(f[synthetic.LABEL])
    x = f[list(FEATURES)].to_numpy(float)
    y = f[synthetic.LABEL].to_numpy(float)
    m = cls().fit(x[train & ok], y[train & ok])
    pred, _unc = m.predict(f.loc[valid & ok])
    r, n = ic(pred, y[valid & ok])
    assert r > chance_band(n), (r, n)  # positive control
    shifted = f.copy()
    for c in FEATURES:
        shifted[c] = shifted.groupby("name", sort=False)[c].shift(1)
    ok2 = np.all(np.isfinite(shifted[list(FEATURES)]), axis=1) & np.isfinite(y)
    m2 = cls().fit(shifted[list(FEATURES)].to_numpy(float)[train & ok2], y[train & ok2])
    pred2, _ = m2.predict(shifted.loc[valid & ok2])
    r2, n2 = ic(pred2, y[valid & ok2])
    assert abs(r2) <= chance_band(n2), (r2, n2)


def test_a_shuffled_label_fits_nothing(panel) -> None:
    f = panel
    train, valid = _split(f)
    ok = np.all(np.isfinite(f[list(FEATURES)]), axis=1) & np.isfinite(f[synthetic.LABEL])
    x = f[list(FEATURES)].to_numpy(float)
    y = f[synthetic.LABEL].to_numpy(float).copy()
    from iap.data.leakage import _permutation

    perm = _permutation(len(y), 20261024)
    ys = y[perm]
    m = RidgeModel(FEATURES, 1.0).fit(x[train & ok], ys[train & ok])
    pred, _ = m.predict(f.loc[valid & ok])
    r, n = ic(pred, ys[valid & ok])
    assert abs(r) <= chance_band(n), (r, n)


def test_the_planted_leak_is_caught_before_a_model_can_use_it(panel) -> None:
    f = synthetic.make_canary_panel(seed=20261024, n_periods=60, n_names=40)
    out = planted_leak_canary(
        synthetic.feature_canary_planted_leak,
        synthetic.feature_canary_signal,
        f,
        f[synthetic.LABEL],
    )
    assert out["passed"] and out["caught"]
    assert (
        out["ic_leaked_if_undetected"] > 0.9
    )  # what an undetected leak would have looked like to a model
    with pytest.raises(LeakageDetected):
        check_causality(synthetic.feature_canary_planted_leak, f)


def test_models_are_deterministic_and_expose_their_complexity(panel) -> None:
    f = panel
    train, _valid = _split(f)
    ok = np.all(np.isfinite(f[list(FEATURES)]), axis=1) & np.isfinite(f[synthetic.LABEL])
    x = f[list(FEATURES)].to_numpy(float)[train & ok]
    y = f[synthetic.LABEL].to_numpy(float)[train & ok]
    a = RidgeModel(FEATURES, 10.0).fit(x, y)
    b = RidgeModel(FEATURES, 10.0).fit(x, y)
    assert a.fit_hash() == b.fit_hash() and 0 < a.complexity < 2.0
    assert RidgeModel(FEATURES, 1e-9).fit(x, y).complexity == pytest.approx(2.0, abs=1e-6)
    en = ElasticNetModel(FEATURES, 5.0, l1_ratio=1.0).fit(
        x, y
    )  # a lasso strong enough to zero a coefficient
    assert en.complexity <= 1.0 and en.iterations >= 1
    assert en.model_class == "M1.elastic_net" and a.model_class == "M1.ridge"
    with pytest.raises(ValueError, match="too few"):
        RidgeModel(FEATURES, 1.0).fit(x[:5], y[:5])
    params = a.parameters()
    assert set(params) >= {
        "coef_bps_per_sd",
        "intercept_bps",
        "feature_mean",
        "feature_sd",
        "complexity",
    }

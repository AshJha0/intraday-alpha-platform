"""M2 (GAM) and M3 (bounded GBT) on synthetic panels: determinism, leakage canaries, the closed feature group, a runtime bound."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
from iap.contracts.versions import canonical_json
from iap.core.rng import SplitMix64
from iap.data import synthetic
from iap.data.contracts import LeakageDetected, check_causality
from iap.data.leakage import _permutation, chance_band, ic
from iap.ml import GAMModel, GBTModel, load_protocol
from iap.ml.ladder import build_candidate, check_feature_group
from iap.ml.nonlinear import splitmix_uniforms
from iap.ml.protocol import ProtocolError

FEATURES = ("canary.signal", "canary.trailing_mean_ret")


@pytest.fixture(scope="module")
def panel() -> pd.DataFrame:
    f = synthetic.make_canary_panel(seed=20261024, n_periods=120, n_names=60, beta=0.3)
    f["canary.signal"] = synthetic.feature_canary_signal(f)
    f["canary.trailing_mean_ret"] = synthetic.feature_canary_trailing_mean_ret(f)
    return f


def _xy(f: pd.DataFrame, feats=FEATURES, label=None):
    y = f[synthetic.LABEL].to_numpy(float) if label is None else label
    x = f[list(feats)].to_numpy(float)
    ok = np.all(np.isfinite(x), axis=1) & np.isfinite(y)
    train = (f["period"] < 80).to_numpy() & ok
    valid = (f["period"] >= 82).to_numpy() & ok  # embargo of one label period
    return x, y, train, valid


def _gam():
    return GAMModel(FEATURES, 1.0)


def _gbt():
    return GBTModel(FEATURES, 4, 0.1, 1.0, min_samples_leaf=50)


def _fit(model, x, y, train, valid):
    if isinstance(model, GBTModel):
        # early stopping on the first half of the validation window, IC on the second half
        es = valid & (np.arange(len(y)) < np.flatnonzero(valid)[valid.sum() // 2])
        return model.fit(x[train], y[train], x[es], y[es]), valid & ~es
    return model.fit(x[train], y[train]), valid


def test_splitmix_stream_is_bit_identical_to_the_pinned_generator() -> None:
    r = SplitMix64(20261025)
    ref = np.array([r.uniform() for _ in range(64)])
    assert np.array_equal(splitmix_uniforms(20261025, 0, 64), ref)
    assert np.array_equal(splitmix_uniforms(20261025, 17, 10), ref[17:27])


@pytest.mark.parametrize("make", [_gam, _gbt], ids=["M2.gam", "M3.gbt"])
def test_seeded_refits_are_byte_identical(panel, make) -> None:
    x, y, train, valid = _xy(panel)
    a, _ = _fit(make(), x, y, train, valid)
    b, _ = _fit(make(), x, y, train, valid)
    assert canonical_json(a.parameters()) == canonical_json(b.parameters())
    assert a.fit_hash() == b.fit_hash() and len(a.fit_hash()) == 64
    pa, _ = a.predict(panel)
    pb, _ = b.predict(panel)
    assert np.array_equal(pa, pb, equal_nan=True)


def test_a_different_seed_changes_the_gbt_but_not_its_contract(panel) -> None:
    x, y, train, valid = _xy(panel)
    a, _ = _fit(_gbt(), x, y, train, valid)
    b, _ = _fit(GBTModel(FEATURES, 4, 0.1, 1.0, min_samples_leaf=50, seed=7), x, y, train, valid)
    assert a.fit_hash() != b.fit_hash()


@pytest.mark.parametrize("make", [_gam, _gbt], ids=["M2.gam", "M3.gbt"])
def test_positive_control_above_chance_and_future_shifted_twin_at_chance(panel, make) -> None:
    x, y, train, valid = _xy(panel)
    m, scored = _fit(make(), x, y, train, valid)
    pred, _ = m.predict(panel.loc[scored])
    r, n = ic(pred, y[scored])
    assert r > chance_band(n), (r, n)
    shifted = panel.copy()
    for c in FEATURES:
        shifted[c] = shifted.groupby("name", sort=False)[c].shift(1)
    x2, y2, train2, valid2 = _xy(shifted)
    m2, scored2 = _fit(make(), x2, y2, train2, valid2)
    pred2, _ = m2.predict(shifted.loc[scored2])
    r2, n2 = ic(pred2, y2[scored2])
    assert abs(r2) <= chance_band(n2), (r2, n2)


@pytest.mark.parametrize("make", [_gam, _gbt], ids=["M2.gam", "M3.gbt"])
def test_a_shuffled_label_fits_nothing(panel, make) -> None:
    y = panel[synthetic.LABEL].to_numpy(float)
    ys = y[_permutation(len(y), 20261024)]
    x, ys, train, valid = _xy(panel, label=ys)
    m, scored = _fit(make(), x, ys, train, valid)
    pred, _ = m.predict(panel.loc[scored])
    r, n = ic(pred, ys[scored])
    assert abs(r) <= chance_band(n), (r, n)


def test_a_future_peeking_feature_is_caught_before_any_nonlinear_model_sees_it(panel) -> None:
    proto = load_protocol()
    group = proto["feature_groups"]["d1"]
    peek = dict(group, features=[*group["features"], "label.fwd_ret_1d"])
    # the closed group refuses it; candidates are built from the group and nothing else
    with pytest.raises(ProtocolError, match="outside the closed group"):
        check_feature_group(peek["features"], group)
    for cls in ("M2.gam", "M3.gbt"):
        m = build_candidate(cls, proto["grids"][cls][0], group, proto)
        assert m.features == tuple(group["features"])
    # the contract layer catches a planted look-ahead feature on the panel
    f = synthetic.make_canary_panel(seed=20261024, n_periods=60, n_names=40)
    with pytest.raises(LeakageDetected):
        check_causality(synthetic.feature_canary_planted_leak, f)
    # and this is what the guards prevent: a GBT handed the label itself scores far above chance
    leak = panel.assign(**{"canary.leak": panel[synthetic.LABEL]})
    x, y, train, valid = _xy(leak, feats=("canary.leak",))
    m = GBTModel(("canary.leak",), 4, 0.1, 0.0, min_samples_leaf=50)
    m.fit(x[train], y[train], x[valid], y[valid])
    r, _n = ic(m.predict(leak.loc[valid])[0], y[valid])
    assert r > 0.9


def test_complexity_measures_follow_the_protocol(panel) -> None:
    x, y, train, valid = _xy(panel)
    loose = GAMModel(FEATURES, 1e-6).fit(x[train], y[train])
    tight = GAMModel(FEATURES, 1e6).fit(x[train], y[train])
    per = 3 + loose.n_knots
    assert loose.complexity == pytest.approx(per * len(FEATURES), abs=0.05)
    assert tight.complexity == pytest.approx(
        3 * len(FEATURES), abs=0.05
    )  # only the cubic part left
    g, _ = _fit(_gbt(), x, y, train, valid)
    assert g.complexity == g.max_leaves * g.n_trees
    assert g.parameters()["realised_leaves"] <= g.complexity
    assert g.evaluations >= 1 and g.loss_curve[0][0] == 0
    with pytest.raises(ValueError, match="too few"):
        GAMModel(FEATURES, 1.0).fit(x[:20], y[:20])


def test_early_stopping_keeps_the_best_validation_tree_count() -> None:
    rng = np.random.default_rng(0)  # test data only; the models never draw from numpy's generator
    x = rng.standard_normal((6000, 2))
    y = 5.0 * np.tanh(2 * x[:, 0]) + rng.standard_normal(6000) * 4
    xv = rng.standard_normal((3000, 2))
    yv = 5.0 * np.tanh(2 * xv[:, 0]) + rng.standard_normal(3000) * 4
    m = GBTModel(("a", "b"), 8, 0.1, 1.0).fit(x, y, xv, yv)
    curve = dict(m.loss_curve)
    assert 0 < m.n_trees < m.max_trees and m.n_trees % m.evaluate_every == 0
    best = min(curve.values())
    assert curve[m.n_trees] == best
    last = max(curve)
    assert last - m.n_trees >= m.patience  # stopped by patience, not by the cap


def test_runtime_bound_on_a_development_sized_panel() -> None:
    """A full 400-tree M3 fit and an M2 fit on 50k rows x 3 features stay well inside the bound."""
    rng = np.random.default_rng(1)
    n = 50_000
    x = rng.standard_normal((n, 3))
    y = rng.standard_normal(n) * 30
    t = time.perf_counter()
    m = GBTModel(("a", "b", "c"), 8, 0.05, 1.0, patience=10_000).fit(x, y, x[:1000], y[:1000])
    gbt_s = time.perf_counter() - t
    t = time.perf_counter()
    GAMModel(("a", "b", "c"), 10.0).fit(x, y)
    gam_s = time.perf_counter() - t
    assert m.n_trees <= 400
    assert gbt_s < 120 and gam_s < 10, (gbt_s, gam_s)

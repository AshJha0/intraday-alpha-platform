"""AI4 deep LOB models: synthetic-data tests only (no real data, no training run)."""

from __future__ import annotations

import numpy as np
import pytest
from iap.models.deeplob import (
    DOWN,
    FLAT,
    UP,
    NumpyLOBSoftmax,
    build_lob_dataset,
    fit_normalizer,
    make_windows,
    mid_price,
    smoothed_labels,
    snapshot_matrix,
    synthetic_lob,
    torch_available,
)


def test_snapshot_layout():
    t, lv = 5, 3
    bp = np.full((t, lv), 1.0)
    bs = np.full((t, lv), 2.0)
    ap = np.full((t, lv), 3.0)
    asz = np.full((t, lv), 4.0)
    s = snapshot_matrix(bp, bs, ap, asz)
    assert s.shape == (t, 12)
    np.testing.assert_array_equal(s[0, :4], [3.0, 4.0, 1.0, 2.0])
    np.testing.assert_array_equal(mid_price(s), np.full(t, 2.0))
    with pytest.raises(ValueError):
        snapshot_matrix(bp, bs, ap, asz[:, :2])


def test_labels_values_and_tail():
    mid = np.array([100.0, 101.0, 101.0, 99.0, 99.0, 99.0])
    y = smoothed_labels(mid, horizon=2, threshold=1e-4)
    assert y.tolist() == [UP, DOWN, DOWN, FLAT, -1, -1]


def test_labels_no_lookahead():
    """Changing mid at or before t never changes label t; changing mid after
    t+h never changes label t."""
    rng = np.random.default_rng(1)
    mid = 100 + np.cumsum(rng.normal(0, 0.05, 200))
    h, t = 5, 100
    base = smoothed_labels(mid, h, 1e-5)
    future = mid.copy()
    future[t + h + 1 :] += 50.0
    assert smoothed_labels(future, h, 1e-5)[t] == base[t]
    # The label depends on the future window: perturbing it can move the label.
    up = mid.copy()
    up[t + 1 : t + h + 1] += 10.0
    assert smoothed_labels(up, h, 1e-5)[t] == UP


def test_windows_contain_only_past_rows():
    feats = np.arange(20, dtype=float)[:, None]
    labels = np.zeros(20, dtype=np.int64)
    labels[-3:] = -1
    x, y, ends = make_windows(feats, labels, window=4)
    assert x.shape == (14, 4, 1) and y.shape == (14,)
    assert ends[0] == 3 and ends[-1] == 16
    for xi, e in zip(x, ends, strict=True):
        assert xi[-1, 0] == e and xi[0, 0] == e - 3


def test_dataset_split_and_shapes():
    snaps = synthetic_lob(600, levels=10, seed=3)
    ds = build_lob_dataset(snaps, window=20, horizon=5, threshold=1e-5, train_frac=0.7)
    cut = int(600 * 0.7)
    assert ds["X_train"].shape[1:] == (20, 40)
    assert ds["X_test"].shape[1:] == (20, 40)
    assert (ds["end_train"] + 5 < cut).all()
    assert (ds["end_test"] - 19 >= cut).all()
    # Normaliser saw train rows only.
    norm = fit_normalizer(snaps[:cut])
    np.testing.assert_allclose(norm.transform(snaps[:cut]).mean(axis=0), 0, atol=1e-9)


def test_dataset_is_deterministic():
    a = build_lob_dataset(synthetic_lob(300, seed=7), window=10, horizon=3, threshold=1e-5)
    b = build_lob_dataset(synthetic_lob(300, seed=7), window=10, horizon=3, threshold=1e-5)
    for k in a:
        np.testing.assert_array_equal(a[k], b[k])


def test_numpy_baseline_deterministic_and_learns():
    ds = build_lob_dataset(synthetic_lob(800, seed=0), window=5, horizon=3, threshold=1e-5)
    m1 = NumpyLOBSoftmax(epochs=150, seed=4).fit(ds["X_train"], ds["y_train"])
    m2 = NumpyLOBSoftmax(epochs=150, seed=4).fit(ds["X_train"], ds["y_train"])
    np.testing.assert_array_equal(m1.predict_proba(ds["X_test"]), m2.predict_proba(ds["X_test"]))
    p = m1.predict_proba(ds["X_test"])
    assert p.shape == (len(ds["y_test"]), 3)
    np.testing.assert_allclose(p.sum(axis=1), 1.0)
    # The planted imbalance signal beats the majority class out of sample.
    majority = np.bincount(ds["y_test"], minlength=3).max() / len(ds["y_test"])
    assert (m1.predict(ds["X_test"]) == ds["y_test"]).mean() > majority


def test_numpy_baseline_overfits_tiny_set():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(12, 4, 8))
    y = np.array([DOWN, FLAT, UP] * 4)
    m = NumpyLOBSoftmax(lr=1.0, epochs=500, l2=0.0, seed=0).fit(x, y)
    assert (m.predict(x) == y).all()


def test_numpy_baseline_requires_fit():
    with pytest.raises(RuntimeError):
        NumpyLOBSoftmax().predict(np.zeros((1, 2, 4)))


needs_torch = pytest.mark.skipif(not torch_available(), reason="optional 'deep' extra (torch)")


@needs_torch
@pytest.mark.parametrize("arch", ["deeplob", "mlp"])
def test_torch_shapes(arch):
    from iap.models.deeplob_torch import DeepLOBClassifier

    x = synthetic_lob(64, levels=10, seed=0)[None].repeat(4, axis=0)[:, :20, :]
    m = DeepLOBClassifier(arch=arch, epochs=1, batch_size=4, seed=0).fit(x, np.array([0, 1, 2, 1]))
    p = m.predict_proba(x)
    assert p.shape == (4, 3)
    np.testing.assert_allclose(p.sum(axis=1), 1.0, rtol=1e-5)


@needs_torch
@pytest.mark.parametrize("arch", ["deeplob", "mlp"])
def test_torch_deterministic_and_overfits(arch):
    import torch
    from iap.models.deeplob_torch import DeepLOBClassifier

    torch.use_deterministic_algorithms(True)
    rng = np.random.default_rng(0)
    x = rng.normal(size=(16, 20, 40)).astype(np.float32)
    y = np.array([0, 1, 2, 1] * 4)
    kw = {"arch": arch, "epochs": 60, "lr": 3e-3, "batch_size": 16, "seed": 1}
    a = DeepLOBClassifier(**kw).fit(x, y)
    b = DeepLOBClassifier(**kw).fit(x, y)
    np.testing.assert_allclose(a.predict_proba(x), b.predict_proba(x), rtol=1e-6)
    assert (a.predict(x) == y).mean() >= 0.9

"""Walk-forward splitter: expanding train, embargo, purge (spec §13)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.models.splits import WalkForwardSplitter

_S = 1_000_000_000


def _ts(n: int, span_s: int = 5000) -> np.ndarray:
    return np.linspace(0, span_s * _S, n).astype(np.int64)


def test_expanding_train_and_ordering():
    ts = _ts(2000)
    folds = WalkForwardSplitter(n_folds=4, embargo_ns=60 * _S,
                                label_horizon_ns=5 * _S).split(ts)
    assert len(folds) == 4
    sizes = [len(f.train_idx) for f in folds]
    assert sizes == sorted(sizes)  # expanding
    for f in folds:
        assert ts[f.train_idx].max() < ts[f.test_idx].min()


def test_embargo_respected():
    ts = _ts(2000)
    embargo = 120 * _S
    folds = WalkForwardSplitter(n_folds=4, embargo_ns=embargo,
                                label_horizon_ns=5 * _S).split(ts)
    span = int(ts.max() - ts.min())
    for f in folds:
        boundary = ts.min() + (span * (f.fold + 1)) // 5
        assert ts[f.test_idx].min() >= boundary + embargo


def test_purge_label_overlap():
    ts = _ts(2000)
    horizon = 30 * _S
    folds = WalkForwardSplitter(n_folds=4, embargo_ns=0,
                                label_horizon_ns=horizon).split(ts)
    span = int(ts.max() - ts.min())
    for f in folds:
        boundary = ts.min() + (span * (f.fold + 1)) // 5
        # no train label window crosses the boundary into the test period
        assert (ts[f.train_idx] + horizon).max() <= boundary


def test_no_train_test_overlap():
    ts = _ts(1000)
    folds = WalkForwardSplitter(n_folds=3, embargo_ns=10 * _S,
                                label_horizon_ns=_S).split(ts)
    for f in folds:
        assert not set(f.train_idx.tolist()) & set(f.test_idx.tolist())


def test_split_input_validation():
    with pytest.raises(ValueError):
        WalkForwardSplitter(n_folds=0)
    with pytest.raises(ValueError):
        WalkForwardSplitter(embargo_ns=-1)
    sp = WalkForwardSplitter()
    with pytest.raises(ValueError):
        sp.split(np.array([], dtype=np.int64))
    with pytest.raises(ValueError):
        sp.split(np.array([5, 5, 5], dtype=np.int64))  # zero span

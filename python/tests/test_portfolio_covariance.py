"""EWMA covariance + 1m bar construction (spec §15)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.portfolio.covariance import bars_from_features, ewma_covariance


def test_ewma_hand_case():
    # T=4, N=1, lam=0.5, init_window=2 (sample cov of first 2, ddof=0)
    r = np.array([[0.01], [0.03], [0.02], [-0.01]])
    # init: mean=0.02, var = ((0.01-0.02)^2 + (0.03-0.02)^2)/2 = 1e-4
    # t=2: 0.5*1e-4 + 0.5*4e-4 = 2.5e-4
    # t=3: 0.5*2.5e-4 + 0.5*1e-4 = 1.75e-4
    S = ewma_covariance(r, lam=0.5, init_window=2, ridge=0.0)
    assert S.shape == (1, 1)
    assert abs(S[0, 0] - 1.75e-4) < 1e-15


def test_ewma_matrix_properties():
    rng = np.random.RandomState(4)
    r = rng.randn(300, 5) * 1e-3
    S = ewma_covariance(r)
    assert S.shape == (5, 5)
    assert np.allclose(S, S.T)
    assert np.all(np.linalg.eigvalsh(S) > 0)  # ridge keeps it SPD


def test_ewma_validation():
    with pytest.raises(ValueError):
        ewma_covariance(np.zeros((10, 2)), lam=1.0)
    with pytest.raises(ValueError):
        ewma_covariance(np.zeros((1, 2)))
    with pytest.raises(ValueError):
        ewma_covariance(np.zeros(10))  # 1-D


def test_bars_from_real_feature_store():
    """1m bars from the repo's bundled feature parquet (FX pairs)."""
    ids, bar_ts, rets = bars_from_features(instruments=[101, 102, 103])
    assert ids == [101, 102, 103]
    assert rets.shape[1] == 3
    assert rets.shape[0] == len(bar_ts) >= 30
    assert np.all(np.isfinite(rets))
    assert np.all(np.diff(bar_ts) > 0)
    # bar timestamps are aligned to the pinned 1-minute grid
    assert np.all(bar_ts % 60_000_000_000 == 0)
    # returns feed the EWMA estimator cleanly
    S = ewma_covariance(rets)
    assert np.all(np.diag(S) > 0)


def test_bars_deterministic():
    a = bars_from_features(instruments=[101, 102])
    b = bars_from_features(instruments=[101, 102])
    assert a[0] == b[0]
    assert np.array_equal(a[1], b[1])
    assert np.array_equal(a[2], b[2])

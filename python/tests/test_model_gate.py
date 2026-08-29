"""Gate logic (spec §14): advanced models only after positive linear OOS IC.

Uses small synthetic Dataset objects engineered so the linear baseline's OOS
IC is deterministically positive (learnable relation) or deterministically
negative (relation flips sign after the densely-sampled first segment, so
every fold trains on mostly-positive data and tests on the flipped regime).
Also hosts the automatic shift-by-one leakage test required by conventions §7.
"""

from __future__ import annotations

import numpy as np

from iap.core.rng import SplitMix64
from iap.models.dataset import Dataset
from iap.models.pipeline import (
    ic_tstat,
    information_coefficient,
    rank_ic,
    run_model_comparison,
)
from iap.models.zoo import model_names, model_tier

_S = 1_000_000_000


def _make_dataset(y: np.ndarray, X: np.ndarray,
                  ts: np.ndarray) -> Dataset:
    n = len(y)
    meta = np.zeros((n, 6))
    meta[:, 4] = 1.0  # half_spread_cost_bps = 1bp
    return Dataset(X=X, y=y, y_mid=y.copy(), ts=ts,
                   instrument_id=np.ones(n, dtype=np.int32),
                   feature_names=[f"f{i}" for i in range(X.shape[1])],
                   meta_context=meta)


def _signal_dataset(flip_after_first_segment: bool) -> Dataset:
    """Dense first time-segment with y=+x0; later segments y=-x0 if flipped."""
    rng = SplitMix64(42)
    span = 5000 * _S
    # 1500 samples in the first fifth, 60 in each later fifth
    ts_list, x_list, y_list = [], [], []
    for i in range(1500):
        ts_list.append(int(i * (span / 5) / 1500))
    for seg in range(1, 5):
        for i in range(60):
            ts_list.append(int(span * seg / 5 + i * (span / 5) / 60))
    ts = np.array(sorted(ts_list), dtype=np.int64)
    n = len(ts)
    x0 = np.array([rng.normal() for _ in range(n)])
    x1 = np.array([rng.normal() for _ in range(n)])
    sign = np.ones(n)
    if flip_after_first_segment:
        sign[ts >= span // 5] = -1.0
    y = sign * x0 * 1e-3
    X = np.column_stack([x0, x1])
    return _make_dataset(y, X, ts)


def test_gate_passes_runs_advanced_models():
    ds = _signal_dataset(flip_after_first_segment=False)
    res = run_model_comparison(ds, n_folds=4, embargo_ns=_S)
    assert res["gate"]["passed"] is True
    assert res["gate"]["best_linear_mean_oos_ic"] > 0.9
    assert res["gate"]["skipped_models"] == []
    for name in model_names(1) + model_names(2):
        assert name in res["models"], f"advanced model {name} did not run"


def test_gate_blocks_advanced_models_on_negative_ic():
    ds = _signal_dataset(flip_after_first_segment=True)
    res = run_model_comparison(ds, n_folds=4, embargo_ns=_S)
    assert res["gate"]["passed"] is False
    assert res["gate"]["best_linear_mean_oos_ic"] < 0.0
    advanced = model_names(1) + model_names(2)
    assert sorted(res["gate"]["skipped_models"]) == sorted(advanced)
    for name in advanced:
        assert name not in res["models"], \
            f"gate failed but advanced model {name} was trained"
    for name in model_names(0):  # baselines always run
        assert name in res["models"]


def test_gate_decision_is_literal_threshold():
    # the rule is > 0, not >= 0 or a soft margin: verify recorded rule text
    ds = _signal_dataset(flip_after_first_segment=False)
    res = run_model_comparison(ds, n_folds=2, embargo_ns=_S)
    assert "IC > 0" in res["gate"]["rule"]


def test_model_tiers_pinned():
    assert model_tier("ols") == 0
    assert model_tier("ridge") == 0
    assert model_tier("elasticnet") == 0
    assert model_tier("mlp") == 2
    for name in model_names(1):
        assert model_tier(name) == 1


def test_leakage_shift_by_one_destroys_ic():
    """Conventions §7: shifting features by one sample kills a real signal."""
    rng = SplitMix64(7)
    n = 4000
    x = np.array([rng.normal() for _ in range(n)])
    y = x * 1e-3  # perfect contemporaneous relation
    ic_true = information_coefficient(x, y)
    ic_shift = information_coefficient(np.roll(x, 1), y)
    assert ic_true > 0.999
    assert abs(ic_shift) < 0.05


def test_ic_functions_hand_cases():
    a = np.array([1.0, 2.0, 3.0, 4.0])
    assert information_coefficient(a, a) == 1.0
    assert information_coefficient(a, -a) == -1.0
    assert information_coefficient(np.ones(4), a) == 0.0  # degenerate
    assert rank_ic(np.array([1.0, 10.0, 100.0, 1000.0]), a) == 1.0
    assert ic_tstat([0.1, 0.1, 0.1, 0.1]) == 0.0  # zero variance
    assert ic_tstat([0.1]) == 0.0
    t = ic_tstat([0.1, 0.2, 0.3])
    assert abs(t - (0.2 / (0.1 / np.sqrt(3)))) < 1e-12


def test_rank_ic_averages_tied_ranks_vs_scipy():
    """rank_ic must equal scipy's Spearman rho exactly on heavily tied data
    (average ranks on ties — argsort-of-argsort would break ties arbitrarily
    and disagree)."""
    from scipy.stats import spearmanr

    rng = SplitMix64(2024)
    # coarsely quantized draws => many ties in both vectors
    x = np.array([float(rng.below(5)) for _ in range(400)])
    y = np.array([float(rng.below(4)) + (0.5 if rng.uniform() < 0.3 else 0.0)
                  for _ in range(400)])
    got = rank_ic(x, y)
    want = float(spearmanr(x, y).statistic)
    assert abs(got - want) <= 1e-12
    # and a hand case: perfect monotone with ties still gives rho 1
    a = np.array([1.0, 2.0, 2.0, 3.0])
    b = np.array([10.0, 20.0, 20.0, 30.0])
    assert abs(rank_ic(a, b) - 1.0) <= 1e-12

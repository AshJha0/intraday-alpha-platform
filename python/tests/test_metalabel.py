"""Meta-labeling: calibration monotonicity + economic evaluation (spec §14)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.core.rng import SplitMix64
from iap.models.dataset import Dataset
from iap.models.metalabel import (
    META_FEATURE_NAMES,
    build_meta_features,
    run_meta_labeling,
)

_S = 1_000_000_000


def _meta_dataset(n: int = 8000, seed: int = 11) -> Dataset:
    """Synthetic dataset with a planted profitability pattern.

    The primary 'prediction' is a noisy version of the outcome so the meta
    model has something real to learn: high |pred| trades are more often
    profitable than low |pred| ones.
    """
    rng = SplitMix64(seed)
    ts = np.arange(n, dtype=np.int64) * _S
    strength = np.array([abs(rng.normal()) for _ in range(n)])
    noise = np.array([rng.normal() for _ in range(n)])
    sign = np.array([1.0 if rng.uniform() < 0.5 else -1.0 for _ in range(n)])
    pred = sign * strength * 1e-3
    # realized mid return follows the prediction when strength is high
    follow = (strength + 0.8 * noise) > 1.0
    y_mid = np.where(follow, sign * 2e-4, -sign * 1e-4)
    cost = np.full(n, 5e-5)
    y_cost = y_mid - cost
    X = np.column_stack([pred, noise])
    meta = np.zeros((n, 6))
    meta[:, 0] = 2.0          # spread bps
    meta[:, 1] = 1e-4         # rvol
    meta[:, 2] = 500.0        # depth
    meta[:, 3] = np.array([rng.normal() for _ in range(n)]) * 0.1
    meta[:, 4] = 0.25         # half-spread cost bps
    meta[:, 5] = 0.5          # expected impact bps
    return Dataset(X=X, y=y_cost, y_mid=y_mid, ts=ts,
                   instrument_id=np.ones(n, dtype=np.int32),
                   feature_names=["f0", "f1"], meta_context=meta), pred


@pytest.fixture(scope="module")
def meta_result():
    ds, pred = _meta_dataset()
    return run_meta_labeling(ds, pred), ds, pred


def test_meta_labeling_output_contract(meta_result):
    res, _, _ = meta_result
    for key in ("auc_test", "brier_test", "economics_gate_off",
                "economics_gate_tau", "economics_gate_best_tau",
                "calibration_curve", "tau_sweep_calibration",
                "base_rate_test", "segments"):
        assert key in res
    seg = res["segments"]
    assert seg["train"] > seg["calibration"] > 0 and seg["test"] > 0


def test_meta_model_learns_planted_pattern(meta_result):
    res, _, _ = meta_result
    assert res["auc_test"] > 0.6  # strength drives profitability by design


def test_calibration_curve_monotone(meta_result):
    """Isotonic calibration: empirical frequency rises with predicted p."""
    res, _, _ = meta_result
    curve = res["calibration_curve"]
    assert len(curve) >= 3
    p_means = [row["p_mean"] for row in curve]
    assert p_means == sorted(p_means)
    # weak monotonicity of empirical frequencies (allow small-sample noise
    # in adjacent bins but require the overall trend)
    lo = np.mean([row["empirical"] for row in curve[: len(curve) // 2]])
    hi = np.mean([row["empirical"] for row in curve[len(curve) // 2:]])
    assert hi > lo


def test_calibrated_probabilities_bounded(meta_result):
    res, _, _ = meta_result
    for row in res["calibration_curve"]:
        assert 0.0 <= row["p_mean"] <= 1.0
        assert 0.0 <= row["empirical"] <= 1.0
        assert row["count"] > 0


def test_meta_gate_improves_mean_pnl_per_trade(meta_result):
    """The gate must raise mean P&L per trade on the planted pattern."""
    res, _, _ = meta_result
    off = res["economics_gate_off"]
    on = res["economics_gate_best_tau"]
    assert on["n_trades"] < off["n_trades"]
    assert on["n_trades"] > 0
    assert on["mean_net_bps_per_trade"] > off["mean_net_bps_per_trade"]


def test_meta_gate_economics_consistency(meta_result):
    res, _, _ = meta_result
    off = res["economics_gate_off"]
    assert off["n_trades"] == off["n_signals"]
    for key in ("economics_gate_tau", "economics_gate_best_tau"):
        assert res[key]["n_trades"] <= off["n_trades"]
    # tau sweep is evaluated on calibration data only and covers a grid
    assert len(res["tau_sweep_calibration"]) >= 9


def test_build_meta_features_contract():
    pred = np.array([1e-3, -2e-3])
    direction = np.array([1, -1])
    ctx = np.array([[2.0, 1e-4, 100.0, 0.3, 0.5, 0.2],
                    [3.0, 2e-4, np.nan, -0.4, 0.6, 0.1]])
    X = build_meta_features(pred, direction, ctx)
    assert X.shape == (2, len(META_FEATURE_NAMES))
    assert X[0, 0] == 1e-3          # |pred|
    assert X[1, 1] == -2e-3         # signed pred
    assert X[0, 5] == 0.3           # imbalance * +1
    assert X[1, 5] == 0.4           # imbalance * -1
    assert np.isfinite(X).all()     # NaN depth -> 0


def test_meta_labeling_input_validation():
    ds, pred = _meta_dataset(n=1000)
    with pytest.raises(ValueError):
        run_meta_labeling(ds, pred[:-1])
    # zero predictions -> no trades -> too few meta samples
    with pytest.raises(ValueError):
        run_meta_labeling(ds, np.zeros(len(ds)))


def test_split_purges_label_horizon_at_both_boundaries():
    """Train/calibration keep only rows whose label window closes at or
    before their segment boundary (ts + TARGET_HORIZON_NS <= boundary)."""
    import numpy as np
    from iap.models.dataset import TARGET_HORIZON_NS
    from iap.models.economics import signal_directions
    from iap.models.metalabel import _EMBARGO_NS

    ds, pred = _meta_dataset()
    res = run_meta_labeling(ds, pred)

    # reconstruct the usable meta-sample set exactly as run_meta_labeling does
    half_bps = ds.meta_context[:, 4]
    cost_est = np.maximum(
        2.0 * np.where(np.isfinite(half_bps), half_bps, 0.0) / 1e4, 0.0)
    direction = signal_directions(np.nan_to_num(pred, nan=0.0), cost_est)
    usable = np.isfinite(pred) & (direction != 0)
    ts = ds.ts[np.flatnonzero(usable)]
    t_lo, t_hi = int(ts[0]), int(ts[-1])
    span = t_hi - t_lo
    b1 = t_lo + span // 2
    b2 = t_lo + (3 * span) // 4

    exp_train = int((ts + TARGET_HORIZON_NS <= b1).sum())
    exp_cal = int(((ts > b1 + _EMBARGO_NS)
                   & (ts + TARGET_HORIZON_NS <= b2)).sum())
    exp_test = int((ts > b2 + _EMBARGO_NS).sum())
    assert res["segments"] == {"train": exp_train, "calibration": exp_cal,
                               "test": exp_test}
    # the purge really removes something vs the unpurged boundaries (rows
    # exist whose label window would straddle each boundary)
    assert exp_train < int((ts <= b1).sum())
    assert exp_cal < int(((ts > b1 + _EMBARGO_NS) & (ts <= b2)).sum())


def test_horizon_shorter_than_embargo_is_asserted():
    from iap.models.dataset import TARGET_HORIZON_NS
    from iap.models.metalabel import _EMBARGO_NS

    assert TARGET_HORIZON_NS < _EMBARGO_NS

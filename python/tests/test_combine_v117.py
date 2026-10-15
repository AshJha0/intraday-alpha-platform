"""Combiner fixes of v1.17 (plan item C3, review R-8) and ``decay_aware``.

Covers: pairwise-complete moments (a silent member is not read as zero
signal), the per-split re-standardisation of the ridge penalty search, the
decay-aware weights and their survival ratio, the own-horizon labels of the
stack with the purge mask, and the experiment identity (unchanged without a
stacking revision, new with one).  Synthetic stacks and frames only.
"""

from __future__ import annotations

import numpy as np
import pytest
from iap.combine import CombinedAlpha, fit_weights
from iap.combine import report as combine_report
from iap.combine.weights import (
    DEFAULT_METHOD,
    STACKING_REVISION,
    _restandardise,
    pairwise_moments,
    standardise,
)
from iap.core.rng import SplitMix64
from iap.validation.metrics import ic
from test_combine import HORIZON, make_frames, toy_factory

NS = 1_000_000_000


def _normal(rng: SplitMix64, n: int) -> np.ndarray:
    u1 = np.array([max(rng.uniform(), 1e-12) for _ in range(n)])
    u2 = np.array([rng.uniform() for _ in range(n)])
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


def _stack(seed: int = 3, n: int = 1200, silent_half: bool = True):
    rng = SplitMix64(seed)
    z = np.column_stack([_normal(rng, n) for _ in range(3)])
    y = 0.3 * z[:, 0] + 0.3 * z[:, 1] + _normal(rng, n)
    if silent_half:
        z[: n // 2, 1] = np.nan  # member 1 has no opinion on the first half
    folds = np.repeat([1, 2, 3], n // 3)
    ts = np.arange(n, dtype=np.int64) * NS
    return z, y, folds, ts


# ------------------------------------------------------------ R-8 fix 2


def test_pairwise_moments_ignore_absent_rows():
    z, y, _, _ = _stack()
    std = standardise(z)
    present = np.isfinite(z)
    g, gv = pairwise_moments(std.values, (y - y.mean()) / y.std(), present)
    both = present[:, 0] & present[:, 1]
    assert g[0, 1] == pytest.approx(np.mean(std.values[both, 0] * std.values[both, 1]))
    assert g[1, 1] == pytest.approx(np.mean(std.values[present[:, 1], 1] ** 2))
    assert g[1, 1] == pytest.approx(1.0, abs=1e-9)  # a standardised member: unit variance
    yc = (y - y.mean()) / y.std()
    assert gv[1] == pytest.approx(np.mean(std.values[present[:, 1], 1] * yc[present[:, 1]]))
    # the old whole-stack mean would have halved both
    assert abs(np.mean(std.values[:, 1] ** 2) - 0.5) < 0.1


@pytest.mark.parametrize("method", ["ridge", "shrinkage_mv"])
def test_silent_member_is_not_read_as_zero_signal(method):
    """Members 0 and 1 carry the same signal; member 1 is silent on half the
    rows.  Pairwise-complete moments give them (nearly) equal weight; the
    pre-v1.17 zero-filled moments halved the silent member's weight."""
    z, y, folds, ts = _stack()
    fit, _ = fit_weights(method, z, y, folds, ts, 0)
    w = np.abs(fit.weights)
    assert fit.detail["stacking_revision"] == STACKING_REVISION
    assert w[1] > 0.7 * w[0], fit.weights
    assert w[2] < 0.25 * w[0]


def test_restandardise_uses_training_rows_only():
    z, _, folds, _ = _stack(silent_half=False)
    train = folds < 3
    values, present = _restandardise(z, train)
    std = standardise(z[train])
    for j in range(z.shape[1]):
        assert values[train, j].mean() == pytest.approx(0.0, abs=1e-9)
        assert values[train, j].std() == pytest.approx(1.0, abs=1e-9)
        assert values[~train, j] == pytest.approx((z[~train, j] - std.mean[j]) / std.scale[j])
    assert present.all()


def test_ridge_penalty_search_does_not_see_the_test_fold_moments():
    """Shift the test fold's level: with per-split moments the penalty
    search sees exactly the same training-fold standardisation, so the
    chosen penalty and the per-penalty errors on the TRAINING folds are
    unchanged; the whole-stack moments would have moved both."""
    z, y, folds, ts = _stack(silent_half=False)
    a, _ = fit_weights("ridge", z, y, folds, ts, 0)
    shifted = z.copy()
    shifted[folds == 3] += 50.0  # a level shift of the last fold only
    b, _ = fit_weights("ridge", shifted, y, folds, ts, 0)
    # the first split (train fold 1, test fold 2) never touches fold 3
    assert a.detail["cv_splits"] == b.detail["cv_splits"] == 2
    assert a.detail["ridge_penalty"] > 0.0


# ------------------------------------------------------------ decay_aware


def test_decay_aware_equals_ic_weighted_without_own_labels():
    z, y, folds, ts = _stack()
    a, _ = fit_weights("ic_weighted", z, y, folds, ts, 0)
    b, _ = fit_weights("decay_aware", z, y, folds, ts, 0)
    assert b.detail["own_labels"] is False
    assert np.allclose(a.weights, b.weights)
    assert b.detail["survival"] == [1.0, 1.0, 1.0]


def test_decay_aware_survival_ratio():
    z, y, folds, ts = _stack(silent_half=False)
    own = {0: y.copy(), 1: 4.0 * z[:, 1] + 0.1 * y}  # member 1 is far better at its own horizon
    fit, _ = fit_weights("decay_aware", z, y, folds, ts, 0, own_labels=own)
    s = fit.detail["survival"]
    assert s[0] == 1.0  # own horizon == combination horizon: nothing decayed
    r1 = ic(z[:, 1], y) / ic(z[:, 1], own[1])
    assert s[1] == pytest.approx(min(1.0, max(0.0, r1)))
    assert 0.0 < s[1] < 1.0
    assert s[2] == 1.0  # no own label handed in: survival 1 (inactive anyway: IC <= 0)
    ic_w, _ = fit_weights("ic_weighted", z, y, folds, ts, 0)
    assert fit.weights[1] / fit.weights[0] < ic_w.weights[1] / ic_w.weights[0]
    assert fit.detail["member_own_ic"][1] == pytest.approx(ic(z[:, 1], own[1]))
    # a member whose own-horizon IC is not positive survives at 0
    neg = {1: -z[:, 1]}
    fit0, _ = fit_weights("decay_aware", z, y, folds, ts, 0, own_labels=neg)
    assert fit0.detail["survival"][1] == 0.0 and fit0.weights[1] == 0.0


def test_stack_carries_own_labels_with_the_purge_mask():
    frames = make_frames()
    model = CombinedAlpha(["T1", "TL"], "decay_aware", horizon=HORIZON, member_factory=toy_factory)
    z, y, folds, ts, own = model.stack(frames)
    assert own.shape == (z.shape[0], 2)
    # T1 shares the combination horizon: its own label IS the stack label
    assert np.array_equal(own[:, 0], y, equal_nan=True)
    # TL (1 m): masked on the last 1m - 5s of the window, present before
    end = max(int(df["exchange_ts"].iloc[-1]) for df in frames.values())
    late = ts > end - (60 - 5) * NS
    assert np.all(np.isnan(own[late, 1]))
    assert np.all(np.isfinite(own[~late, 1]))
    model.fit(frames)
    assert model.params()["detail"]["own_labels"] is True
    assert not model.is_dead


# ------------------------------------------------------------ identity


def test_experiment_identity_is_unchanged_without_a_stacking_revision():
    old = {
        "asset_class": "EQUITY",
        "members": ["EQ01", "EQ03"],
        "method": "ridge",
        "horizon": "5s",
        "n_folds": 4,
        "inner_folds": 3,
        "embargo_s": 60,
        "ridge_penalties": [0.01, 0.1, 1.0, 10.0, 100.0],
        "methods": "v2",
    }
    assert combine_report.experiment_identity("EQUITY", ["EQ03", "EQ01"], "ridge", "5s") == old
    new = combine_report.experiment_identity(
        "EQUITY", ["EQ03", "EQ01"], "ridge", "5s", stacking=STACKING_REVISION
    )
    assert new == {**old, "stacking": "v1.17"}
    assert combine_report.DEFAULT_STACKING_METHOD == "decay_aware"
    assert DEFAULT_METHOD == "equal_weight"

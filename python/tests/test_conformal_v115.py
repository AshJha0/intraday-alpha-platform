"""v1.15 M-P1-2: conformal intervals, confidence sizing, calibration report."""

import numpy as np
import pytest
from iap.models.conformal import (
    AdaptiveConformal,
    ConformalSizingConfig,
    SplitConformal,
    calibration_report,
    confidence_from_width,
    conformal_size,
)


def test_flag_off_by_default():
    assert ConformalSizingConfig().enabled is False


def test_split_coverage_exchangeable():
    # The guarantee is marginal over calibration draws: average many splits.
    rng = np.random.default_rng(7)
    covs = []
    for _ in range(100):
        x = rng.normal(size=1000)
        y = 0.5 * x + rng.normal(size=1000)
        pred = 0.5 * x
        cp = SplitConformal(alpha=0.1).fit(y[:500], pred[:500])
        lo, hi = cp.interval(pred[500:])
        covs.append(calibration_report(y[500:], lo, hi, ["s"] * 500, 0.1)["overall"]["coverage"])
    assert abs(np.mean(covs) - 0.9) < 0.01


def test_adaptive_recovers_coverage_under_shift():
    rng = np.random.default_rng(11)
    n = 4000
    scale = np.where(np.arange(n) < 1000, 1.0, 3.0)  # volatility triples after calibration
    y = rng.normal(size=n) * scale
    pred = np.zeros(n)
    split = SplitConformal(0.1).fit(y[:1000], pred[:1000])
    lo, hi = split.interval(pred[1000:])
    split_cov = np.mean((lo <= y[1000:]) & (y[1000:] <= hi))
    aci = AdaptiveConformal(alpha=0.1, gamma=0.01, window=300).warm_start(y[:1000], pred[:1000])
    covered = [aci.update(y[t], pred[t]) for t in range(1000, n)]
    assert split_cov < 0.6  # split conformal breaks under the shift
    assert abs(np.mean(covered) - 0.9) < 0.03  # ACI recovers nominal


@pytest.mark.parametrize("target", [-1000, -37, 0, 250, 5000])
def test_sizing_monotone_and_capped(target):
    cap, prev = 400, -1
    for c in np.linspace(0, 1, 51):
        q = conformal_size(target, c, cap, lot=10)
        assert abs(q) <= cap and abs(q) <= abs(target)
        assert q == 0 or np.sign(q) == np.sign(target)
        assert abs(q) >= prev
        prev = abs(q)


def test_confidence_from_width():
    assert confidence_from_width(0.5, 1.0) == 1.0
    assert confidence_from_width(4.0, 1.0) == 0.25
    assert confidence_from_width(float("inf"), 1.0) == 0.0
    cs = [confidence_from_width(w, 1.0) for w in [0.5, 1, 2, 3, 10]]
    assert cs == sorted(cs, reverse=True)


def test_calibration_report_by_session():
    rep = calibration_report([0, 5, 0, 0], [-1] * 4, [1] * 4, ["a", "a", "b", "b"], 0.1)
    assert rep["by_session"]["a"]["coverage"] == 0.5
    assert rep["by_session"]["b"]["coverage"] == 1.0
    assert rep["overall"]["n"] == 4

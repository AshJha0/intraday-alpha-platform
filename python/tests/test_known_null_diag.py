"""Synthetic self-test of the v3 residual-mean diagnostic (seeded, no real data).

Separately for the B style (independent trials) and the A0 style (dependent
derangement trials): the implemented calculation, bias-detection behaviour and the
valid-null false-flag rate.  Measured rates are recorded in
research/v118/NULL_PROTOCOL_v3.md section 3.3.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.research import known_null_diag as d


def test_calculation_independent_exact() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    r = d.residual_mean_flag(x, "independent")
    sd = float(np.std(x, ddof=1))
    assert r["mean"] == pytest.approx(2.5)
    assert r["se"] == pytest.approx(sd / 2.0)
    assert r["n_used"] == 4
    assert r["t"] == pytest.approx(2.5 / (sd / 2.0))
    assert r["flag"] is False


def test_calculation_dependent_uses_neff_not_n() -> None:
    x = np.arange(1854, dtype=float)
    r = d.residual_mean_flag(x, "dependent", n_eff=7)
    assert r["n_used"] == 7
    assert r["se"] == pytest.approx(float(np.std(x, ddof=1)) / math.sqrt(7))
    assert d.residual_mean_flag(x, "independent")["se"] < r["se"]


def test_calculation_flag_threshold_and_errors() -> None:
    x = np.array([-1.0, 1.0] * 50) + 0.5  # sd ~ 1.005, n=100
    assert d.residual_mean_flag(x, "independent")["flag"] is True
    assert d.residual_mean_flag(x, "independent", flag_se=100.0)["flag"] is False
    with pytest.raises(ValueError):
        d.residual_mean_flag(x, "dependent")
    with pytest.raises(ValueError):
        d.residual_mean_flag(x, "other")
    with pytest.raises(ValueError):
        d.residual_mean_flag(np.array([1.0]), "independent")


def test_derangement_count() -> None:
    assert d.derangement_index(7).shape == (1854, 7)


def test_understatement_ratio_and_gate() -> None:
    rng = np.random.default_rng(3)
    m = rng.standard_normal(5000)
    assert d.understatement_ratio(m, np.ones(5000)) == pytest.approx(1.0, abs=0.03)
    assert d.understatement_ratio(m, np.full(5000, 0.5)) == pytest.approx(2.0, abs=0.06)
    assert d.lost_difficulty(np.array([0.9, 1.0, 0.8]), np.array([1.2, 1.1, 1.3]))["lost"]
    assert not d.lost_difficulty(np.array([1.1, 1.2, 1.0]), np.array([1.2, 1.1, 1.3]))["lost"]
    assert not d.lost_difficulty(np.array([0.9, 0.9, 0.9]), np.array([0.9, 0.8, 0.9]))["lost"]


# ------------------------------------------------------------------ B style --


def test_b_valid_null_false_flag_rate_below_point_one_percent() -> None:
    # 100,000 replicates measured 1e-5 (seed 11); here a smaller seeded run.
    assert d.measure_b_style(20000, 0.0, seed=11) < 0.001


def test_b_bias_detection() -> None:
    # 4 SE at n = 2000 is a bias of 0.089 trial sd.
    assert d.measure_b_style(3000, 0.05, seed=5) < 0.15
    assert 0.3 < d.measure_b_style(3000, 0.09, seed=5) < 0.75
    assert d.measure_b_style(3000, 0.15, seed=5) > 0.98


# ----------------------------------------------------------------- A0 style --


def test_a0_bias_detection_only_large_bias() -> None:
    # n_eff = 7: a bias of 0.5 entry sd is caught <50 % of the time, 1.5 sd always.
    assert d.measure_a0_style(300, "iid", bias_sd=0.5, seed=5) < 0.7
    assert d.measure_a0_style(300, "iid", bias_sd=1.5, seed=5) > 0.99


def test_a0_valid_null_false_flag_rate_iid_dependence_is_not_below_point_one_percent() -> None:
    # Measured 0.22 % (20,000 datasets, seed 11): the "< 0.1 %" claim is NOT
    # demonstrated for A0, so the protocol does not make it.
    rate = d.measure_a0_style(3000, "iid", seed=11)
    assert rate < 0.01


def test_a0_valid_null_false_flag_rate_with_session_effects_is_not_calibrated() -> None:
    # With session effects every derangement sums every effect once, so the
    # derangement sd cannot see them: the heuristic SE is far too small.
    # Measured 34.9 % (5,000 datasets, seed 11).  The diagnostic is documented as
    # able to under-protect (false flags) under such dependence.
    rate = d.measure_a0_style(300, "effects", seed=11)
    assert rate > 0.2

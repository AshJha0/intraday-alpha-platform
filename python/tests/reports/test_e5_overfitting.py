"""E5 block F: the deflated Sharpe ratio and PBO (CSCV) on examples small enough to check by hand."""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pytest
from iap.reports import SessionRow, block_f_overfitting, session_series

C = 1_000_000.0
EULER_GAMMA = 0.5772156649015329


def series_of(bps):
    rows = [SessionRow(f"s{i}", "traded", b * C / 1e4, b * C / 1e4) for i, b in enumerate(bps)]
    return session_series("d1", "5d", rows, C)


def test_one_trial_dsr_is_the_probabilistic_sharpe_against_zero():
    """Ten sessions alternating 0 and 1 bps: mean 0.5, sample sd 0.5 * sqrt(10 / 9),
    skew 0 and (non-excess) kurtosis 1 for a symmetric two-point series, so the
    variance term 1 - skew * SR + (kurtosis - 1) / 4 * SR^2 is exactly 1 and
    DSR = Phi(SR * sqrt(n - 1)) = Phi(3 / sqrt(10 / 9)) = Phi(2.8460)."""
    t = block_f_overfitting(series_of([0.0, 1.0] * 5), n_trials=1)
    row = t.rows[0]
    sr = 0.5 / (0.5 * math.sqrt(10 / 9))
    assert row["session_sharpe"] == pytest.approx(sr)
    assert row["skew"] == pytest.approx(0.0, abs=1e-12)
    assert row["kurtosis"] == pytest.approx(1.0)
    assert row["expected_max_sharpe_sr0"] == 0.0, "one trial: no selection, no deflation"
    assert row["deflated_sharpe_ratio"] == pytest.approx(NormalDist().cdf(sr * 3.0))
    assert row["deflated_sharpe_ratio"] == pytest.approx(0.99779, abs=1e-5)


def test_more_trials_raise_the_hurdle_and_lower_the_dsr():
    """Two trials with V = 1 / (n - 1) = 1 / 9: SR0 = (1/3) * gamma * Phi^-1(1 - 1 / (2e)),
    because Phi^-1(1 - 1/2) = 0 kills the first term."""
    s = series_of([0.0, 1.0] * 5)
    one = block_f_overfitting(s, n_trials=1).rows[0]
    two = block_f_overfitting(s, n_trials=2).rows[0]
    sr0 = (1 / 3) * EULER_GAMMA * NormalDist().inv_cdf(1 - 1 / (2 * math.e))
    assert two["expected_max_sharpe_sr0"] == pytest.approx(sr0)
    assert two["expected_max_sharpe_sr0"] == pytest.approx(0.1733, abs=1e-4)
    assert two["deflated_sharpe_ratio"] == pytest.approx(
        NormalDist().cdf((two["session_sharpe"] - sr0) * 3.0)
    )
    many = block_f_overfitting(s, n_trials=1000).rows[0]
    assert (
        one["deflated_sharpe_ratio"] > two["deflated_sharpe_ratio"] > many["deflated_sharpe_ratio"]
    )


def test_without_a_sweep_pbo_is_absent_and_says_why():
    t = block_f_overfitting(series_of([0.0, 1.0] * 5), n_trials=3)
    assert t.rows[1]["probability_of_backtest_overfitting"] is None
    assert any("PBO needs" in n for n in t.notes)
    assert t.counts["n_trials"] == 3 and t.units["pbo"] == "probability"


def _blocks(means):
    """Two rows per block, m + 1 and m - 1: the block's mean is m and every block has variance."""
    return np.array([v for m in means for v in (m + 1.0, m - 1.0)])


def test_pbo_is_zero_when_one_candidate_dominates_every_split():
    """B = A + 1 everywhere: same spread, higher mean, so B wins in-sample and ranks 2 of 2
    out of sample in all C(4, 2) = 6 splits; w = 2 / 3, logit ln 2 > 0, PBO = 0."""
    a = _blocks([0.5, -0.2, 0.3, 0.1])
    t = block_f_overfitting(series_of(a), n_trials=2, candidates=np.c_[a, a + 1.0], pbo_blocks=4)
    pbo = t.rows[1]["probability_of_backtest_overfitting"]
    assert pbo["n_splits"] == 6 and pbo["n_candidates"] == 2
    assert pbo["pbo"] == 0.0
    assert pbo["logit_median"] == pytest.approx(math.log(2.0))


def test_pbo_counts_the_splits_where_the_in_sample_winner_loses():
    """A has block means 3, 1.5, -1, -2.5 and B = -A, so the sign of a block set's sum picks the
    winner.  Splits (in-sample blocks : in-sample sum, out-of-sample sum):
    01: 4.5, -3.5 (A wins, A loses OOS); 02: 2, -1 (loses); 03: 0.5, 0.5 (A wins both);
    12: 0.5, 0.5 (wins both); 13: -1, 2 (B wins, B loses); 23: -3.5, 4.5 (B wins, B loses).
    Four of six splits overfit: PBO = 4 / 6."""
    a = _blocks([3.0, 1.5, -1.0, -2.5])
    t = block_f_overfitting(series_of(a), n_trials=2, candidates=np.c_[a, -a], pbo_blocks=4)
    pbo = t.rows[1]["probability_of_backtest_overfitting"]
    assert pbo["pbo"] == pytest.approx(4 / 6)

"""Session-level P&L power (iap.research.pnl_power): the closed form against a
Monte Carlo of the test it describes, and the monotonicity the table relies on."""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.research import pnl_power as pp
from scipy import stats


def test_size_under_the_null_is_alpha():
    assert pp.power(0.0, 60, alpha=0.025) == pytest.approx(0.025, abs=1e-12)
    assert pp.power(0.0, 60, alpha=0.025, n_tests=10) == pytest.approx(0.0025, abs=1e-12)


def test_power_matches_a_simulation_of_the_t_test():
    rng = np.random.default_rng(20261118)
    sharpe, sessions, trials = 3.0, 120, 40_000
    mu = sharpe / math.sqrt(pp.SESSIONS_PER_YEAR)
    x = rng.normal(mu, 1.0, size=(trials, sessions))
    t = x.mean(axis=1) / (x.std(axis=1, ddof=1) / math.sqrt(sessions))
    rate = float((t > stats.t.ppf(0.975, sessions - 1)).mean())
    # binomial sd of the simulated rate is about 0.0025; allow four of them
    assert pp.power(sharpe, sessions) == pytest.approx(rate, abs=0.01)


def test_sessions_needed_is_the_smallest_count_that_reaches_the_target():
    for sharpe, k in ((1.0, 1), (2.0, 10), (5.0, 100)):
        s = pp.sessions_needed(sharpe, n_tests=k)
        assert s is not None
        assert pp.power(sharpe, s, n_tests=k) >= 0.8
        assert pp.power(sharpe, s - 1, n_tests=k) < 0.8


def test_normal_approximation_is_close_for_large_samples():
    # S ~ 252 * ((z_alpha + z_beta) / SR)^2 when S is large
    approx = 252 * ((stats.norm.ppf(0.975) + stats.norm.ppf(0.8)) / 1.0) ** 2
    assert pp.sessions_needed(1.0) == pytest.approx(approx, rel=0.01)


def test_mde_inverts_power_and_falls_with_sessions():
    mdes = [pp.mde_sharpe(s) for s in (7, 19, 60, 250)]
    assert mdes == sorted(mdes, reverse=True)
    assert pp.power(pp.mde_sharpe(60), 60) == pytest.approx(0.8, abs=1e-4)


def test_more_registered_tests_need_more_sessions():
    a, b, c = (pp.sessions_needed(2.0, n_tests=k) for k in (1, 10, 100))
    assert a < b < c


def test_unreachable_and_invalid_inputs():
    assert pp.sessions_needed(0.0) is None
    assert pp.sessions_needed(-1.0) is None
    assert pp.sessions_needed(0.01) is None  # beyond MAX_SESSIONS
    with pytest.raises(ValueError):
        pp.power(1.0, 1)
    with pytest.raises(ValueError):
        pp.critical_t(10, alpha=0.0)
    with pytest.raises(ValueError):
        pp.critical_t(10, n_tests=0)


def test_rendered_table_carries_every_row_and_no_placeholder():
    res = pp.table()
    md = pp.render_markdown(res)
    assert len(res["sessions_needed"]) == 6 and len(res["mde_sharpe"]) == 6
    assert "None" not in md and "nan" not in md.lower()
    assert "| 2 | " in md

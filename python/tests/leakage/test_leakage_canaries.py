"""U2 leakage canaries on the synthetic fixture (v2.0 stage 2; run in CI on every PR).

Three canaries with positive controls: a feature shifted +1 period scores at
chance, a shuffled label scores at chance, a planted leak is caught by the
contract check.  A fourth test shows that each canary FAILS when it should
(a vacuous detector would pass everything).
"""

from __future__ import annotations

import pytest
from iap.data import synthetic
from iap.data.contracts import LeakageDetected, check_causality
from iap.data.leakage import (
    CanaryFailure,
    chance_band,
    future_shift_canary,
    ic,
    planted_leak_canary,
    run_canaries,
    shuffled_label_canary,
)


@pytest.fixture(scope="module")
def panel():
    return synthetic.make_canary_panel()


def test_fixture_is_seeded_and_has_the_declared_structure(panel):
    again = synthetic.make_canary_panel()
    assert panel.equals(again)
    assert list(panel.columns) == ["period", "name", "signal", "ret", synthetic.LABEL]
    assert panel["period"].is_monotonic_increasing  # sorted by period then name
    # the label is next period's return
    nxt = panel.groupby("name")["ret"].shift(-1)
    assert (panel[synthetic.LABEL].fillna(-9) == nxt.fillna(-9)).all()


def test_future_shift_canary_passes_on_the_fixture(panel):
    feat = synthetic.feature_canary_signal(panel)
    r = future_shift_canary(feat, panel[synthetic.LABEL], group=panel["name"])
    assert r["passed"], r
    assert r["positive_control"] and abs(r["ic_unshifted"]) > 0.2
    assert abs(r["ic_shifted"]) <= r["band"] < 0.07


def test_shuffled_label_canary_passes_on_the_fixture(panel):
    feat = synthetic.feature_canary_signal(panel)
    r = shuffled_label_canary(feat, panel[synthetic.LABEL])
    assert r["passed"], r
    assert abs(r["ic_shuffled"]) <= r["band"]
    # the permutation is SplitMix64-seeded: same seed, same answer; another seed, another draw
    assert shuffled_label_canary(feat, panel[synthetic.LABEL])["ic_shuffled"] == r["ic_shuffled"]
    assert (
        shuffled_label_canary(feat, panel[synthetic.LABEL], seed=7)["ic_shuffled"]
        != r["ic_shuffled"]
    )


def test_planted_leak_is_caught_by_the_contract_check(panel):
    r = planted_leak_canary(
        synthetic.feature_canary_planted_leak,
        synthetic.feature_canary_signal,
        panel,
        panel[synthetic.LABEL],
    )
    assert r["passed"] and r["caught"], r
    assert "reads the future" in r["detail"]
    assert r["ic_leaked_if_undetected"] > 0.99  # what an undetected leak would have claimed
    with pytest.raises(LeakageDetected):
        check_causality(synthetic.feature_canary_planted_leak, panel)
    # the causal controls pass the same check (including a rolling-window feature)
    assert check_causality(synthetic.feature_canary_signal, panel)["leak"] is False
    assert check_causality(synthetic.feature_canary_trailing_mean_ret, panel)["probes"] >= 8


def test_run_canaries_is_green_on_the_fixture_and_reports_every_term(panel):
    report = run_canaries(
        panel,
        synthetic.feature_canary_signal,
        synthetic.LABEL,
        synthetic.feature_canary_planted_leak,
    )
    assert report["passed"]
    assert {report[k]["canary"] for k in ("future_shift", "shuffled_label", "planted_leak")} == {
        "LK-FUTURE-SHIFT",
        "LK-SHUFFLED-LABEL",
        "LK-PLANTED-LEAK",
    }


def test_each_canary_fails_when_it_should(panel):
    label = panel[synthetic.LABEL]
    # 1. a feature with no signal fails the positive control (a vacuous detector would pass it)
    noise = synthetic.feature_canary_trailing_mean_ret(panel)
    r = future_shift_canary(noise, label, group=panel["name"])
    assert not r["passed"] and not r["positive_control"]
    assert not shuffled_label_canary(noise, label)["passed"]
    # 2. a mis-aligned harness that pairs the feature with the PREVIOUS period's label: the shifted
    #    copy now scores above chance (the alignment itself leaks) and the canary fails
    misaligned = label.groupby(panel["name"]).shift(1)
    r2 = future_shift_canary(
        synthetic.feature_canary_signal(panel), misaligned, group=panel["name"]
    )
    assert not r2["passed"] and abs(r2["ic_shifted"]) > r2["band"]
    # 3. a planted leak that the control also commits: the control fails loudly
    with pytest.raises(LeakageDetected):
        planted_leak_canary(
            synthetic.feature_canary_planted_leak, synthetic.feature_canary_planted_leak, panel
        )
    # 4. run_canaries raises CanaryFailure when any canary fails
    with pytest.raises(CanaryFailure):
        run_canaries(
            panel,
            synthetic.feature_canary_trailing_mean_ret,
            synthetic.LABEL,
            synthetic.feature_canary_planted_leak,
        )


def test_ic_and_band_basics():
    import numpy as np

    x = np.arange(10, dtype=float)
    assert ic(x, x)[0] == pytest.approx(1.0)
    assert ic(x, -x)[0] == pytest.approx(-1.0)
    v, n = ic(np.array([1.0, np.nan, 3.0]), np.array([1.0, 2.0, 3.0]))
    assert n == 2 and v != v  # fewer than three finite pairs: nan
    assert chance_band(900) == pytest.approx(0.1)
    assert chance_band(0) == float("inf")

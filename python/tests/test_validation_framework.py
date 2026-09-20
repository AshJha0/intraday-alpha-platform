"""Validation framework tests: metrics vs hand calcs, walk-forward
purge/embargo correctness, leakage detection, ledger persistence."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from iap.alpha.base import LinearAlpha
from iap.validation import (
    MIN_TEST_PAIRS,
    ExperimentLedger,
    LeakageTester,
    WalkForwardSplitter,
    bucket_ics,
    hit_rate,
    ic,
    newey_west_tstat,
    nw_lags,
    rank_ic,
    signal_turnover,
)
from iap.validation.metrics import HORIZONS_NS as _H

NS_S = 1_000_000_000


# -- metric formulas vs hand calculations -------------------------------


def test_ic_matches_hand_pearson():
    x = np.array([1.0, 2.0, 4.0, 3.0, 5.0] * 8)
    y = np.array([2.0, 1.0, 4.0, 5.0, 3.0] * 8)
    # hand Pearson
    mx, my = x.mean(), y.mean()
    hand = float(
        np.sum((x - mx) * (y - my))
        / math.sqrt(np.sum((x - mx) ** 2) * np.sum((y - my) ** 2))
    )
    assert abs(ic(x, y) - hand) < 1e-12


def test_ic_ignores_nan_pairs_and_needs_min_obs():
    x = np.array([1.0, np.nan, 3.0, 4.0])
    y = np.array([1.0, 2.0, np.nan, 4.0])
    assert math.isnan(ic(x, y))  # only 2 pairs < min_obs
    x = np.linspace(0, 1, 100)
    y = 2 * x + 1
    assert abs(ic(x, y) - 1.0) < 1e-12


def test_rank_ic_is_spearman_with_ties():
    x = np.array([1.0, 1.0, 2.0, 3.0] * 10)
    y = np.array([10.0, 20.0, 30.0, 40.0] * 10)
    # average ranks: x -> [1.5, 1.5, 3, 4], Pearson of rank vectors
    rx = np.array([1.5, 1.5, 3.0, 4.0] * 10)
    ry = np.array([1.0, 2.0, 3.0, 4.0] * 10)
    hand = float(np.corrcoef(rx, ry)[0, 1])
    assert abs(rank_ic(x, y) - hand) < 1e-12


def test_hit_rate_hand_calc():
    x = np.array([1.0, -1.0, 1.0, -1.0] * 10)
    y = np.array([0.5, 0.5, 2.0, -3.0] * 10)
    # signs agree on 3 of 4 nonzero pairs
    assert abs(hit_rate(x, y) - 0.75) < 1e-12


def test_newey_west_tstat_hand_calc():
    s = np.array([0.02, 0.01, 0.03, 0.00, 0.02, 0.01, 0.02, 0.03, 0.01, 0.02])
    d = s - s.mean()
    g0 = float(np.mean(d * d))
    g1 = float(np.mean(d[1:] * d[:-1]))
    g2 = float(np.mean(d[2:] * d[:-2]))
    lrv = g0 + 2 * ((1 - 1 / 3) * g1 + (1 - 2 / 3) * g2)
    hand = s.mean() / math.sqrt(lrv / len(s))
    assert abs(newey_west_tstat(s, lags=2) - hand) < 1e-12


def test_bucket_ics_partitions_by_time():
    ts = np.array([0, 1, 2, 3] * 5 + [400, 401, 402, 403] * 5, dtype=np.int64) * NS_S
    x = np.tile([1.0, 2.0, 3.0, 4.0], 10)
    y1 = np.tile([1.0, 2.0, 3.0, 4.0], 5)          # perfect in bucket 0
    y2 = np.tile([4.0, 3.0, 2.0, 1.0], 5)          # perfectly inverse in bucket 1
    y = np.concatenate([y1, y2])
    b = bucket_ics(ts, x, y, bucket_ns=300 * NS_S, min_obs=4)
    assert b.shape == (2,)
    assert abs(b[0] - 1.0) < 1e-12 and abs(b[1] + 1.0) < 1e-12


def test_signal_turnover_counts_flips():
    ts = np.arange(5, dtype=np.int64) * 900 * NS_S  # 1 hour span
    er = np.array([1.0, -1.0, -1.0, 1.0, 1.0])
    conf = np.ones(5)
    # flips at rows 1 and 3 over exactly 1 hour
    assert abs(signal_turnover(ts, er, conf) - 2.0) < 1e-12


# -- walk-forward splitter ----------------------------------------------


def _frames_1s_rows(n=1000):
    ts = np.arange(n, dtype=np.int64) * NS_S + 10 * NS_S
    return {1: pd.DataFrame({"exchange_ts": ts, "x": np.arange(n, dtype=float)})}


def test_walk_forward_expanding_and_disjoint():
    frames = _frames_1s_rows()
    sp = WalkForwardSplitter(n_folds=4, embargo_ns=0)
    horizon = 5 * NS_S
    seen_tests = []
    prev_train_max = -1
    for fold, train, test in sp.split_frames(frames, horizon):
        tr, te = train[1], test[1]
        assert len(te) > 0
        # every test row inside the fold bounds, no overlap with other folds
        for a, b in seen_tests:
            assert te["exchange_ts"].iloc[0] >= b or te["exchange_ts"].iloc[-1] < a
        seen_tests.append(
            (int(te["exchange_ts"].iloc[0]), int(te["exchange_ts"].iloc[-1]) + 1)
        )
        # expanding: train grows monotonically
        assert int(tr["exchange_ts"].iloc[-1]) > prev_train_max
        prev_train_max = int(tr["exchange_ts"].iloc[-1])
        # train strictly before test
        assert int(tr["exchange_ts"].iloc[-1]) < int(te["exchange_ts"].iloc[0])


def test_purging_removes_overlapping_labels():
    """Train rows whose label window [t, t+h] reaches the test start must be
    purged — verified on constructed 1s-spaced rows with a 5s horizon."""
    frames = _frames_1s_rows()
    horizon = 5 * NS_S
    sp = WalkForwardSplitter(n_folds=4, embargo_ns=0)
    for fold, train, test in sp.split_frames(frames, horizon):
        tr_ts = train[1]["exchange_ts"].to_numpy()
        assert np.all(tr_ts + horizon < fold.test_start)
        # and purging is tight: the row just inside the boundary IS kept
        kept_max = tr_ts.max()
        assert fold.test_start - horizon - NS_S <= kept_max < fold.test_start


def test_embargo_widens_the_gap():
    frames = _frames_1s_rows()
    horizon = 5 * NS_S
    embargo = 10 * NS_S
    sp = WalkForwardSplitter(n_folds=4, embargo_ns=embargo)
    for fold, train, test in sp.split_frames(frames, horizon):
        tr_ts = train[1]["exchange_ts"].to_numpy()
        assert np.all(tr_ts + horizon + embargo < fold.test_start)


def test_splitter_validates_args():
    with pytest.raises(ValueError):
        WalkForwardSplitter(n_folds=0)
    with pytest.raises(ValueError):
        WalkForwardSplitter(embargo_ns=-1)
    with pytest.raises(ValueError):
        WalkForwardSplitter().folds(100, 100)


# -- leakage detection --------------------------------------------------


def _label_frames(n=3000, seed=7, rho=0.9):
    """Frames with a genuine (weak, PERSISTENT) signal plus labels.

    The signal is an AR(1) process, like every real book-derived feature
    sampled at 100 ms: it predicts the next second and survives a one-row
    shift with ~rho of its IC.  A white-noise "signal" would be destroyed by
    any one-row shift and is indistinguishable from lookahead — which is
    exactly what the round-3 shift-test calibration pins.
    """
    rng = np.random.default_rng(seed)  # test-only fixture data, not a shared path
    ts = np.arange(n, dtype=np.int64) * 100_000_000 + NS_S
    innov = rng.standard_normal(n)
    z = np.empty(n)
    z[0] = innov[0]
    for i in range(1, n):
        z[i] = rho * z[i - 1] + innov[i]
    z /= z.std()
    future = (0.06 * z + rng.standard_normal(n)) * 1e-4
    df = pd.DataFrame(
        {
            "exchange_ts": ts,
            "sig_feature": z,
            "ret_feature": np.concatenate(([0.0], future[:-1])),
            "label_mid_1s": future,
            "label_valid_1s": np.ones(n, dtype=bool),
        }
    )
    return {1: df}


class _HonestAlpha(LinearAlpha):
    """Honest fixture.

    Economic rationale: fixture — a weak causal signal for framework tests
    (not a real alpha; exists only inside the test suite).
    """

    alpha_id = "TST1"
    name = "honest_fixture"
    asset_class = "EQUITY"
    horizon = "1s"
    features = ("sig_feature",)

    def universe(self, ids):
        return sorted(ids)

    def raw_signal(self, df):
        return df["sig_feature"]


class _LeakyAlpha(_HonestAlpha):
    """Deliberately leaky fixture.

    Economic rationale: fixture — reads the label column inside score()
    (the canonical catastrophic leak); the label-column guard must catch it.
    """

    alpha_id = "TST2"

    def raw_signal(self, df):
        return df["label_mid_1s"] * 0.5


class _PeekAheadAlpha(_HonestAlpha):
    """Deliberately peeking fixture.

    Economic rationale: fixture — uses the NEXT row's realized return as its
    signal (lookahead without touching label columns); the shift-by-one test
    must flag it.
    """

    alpha_id = "TST3"

    def raw_signal(self, df):
        # future[t] == label, reachable via ret_feature shifted BACKWARD
        return df["ret_feature"].shift(-1)


def test_leakage_guard_catches_label_reader():
    frames = _label_frames()
    m = _LeakyAlpha()
    m.fit(frames)
    res = LeakageTester().run(m, frames)
    assert res.label_guard_ok is False
    assert res.passed is False


def test_leakage_shift_test_catches_peek_ahead():
    frames = _label_frames()
    m = _PeekAheadAlpha()
    m.fit(frames)
    res = LeakageTester().run(m, frames)
    assert res.label_guard_ok is True   # never touches label columns
    assert abs(res.ic_unshifted) > 0.9  # implausibly perfect
    assert res.shift_ok is False        # destroyed by one-event shift
    assert res.passed is False


def test_leakage_honest_alpha_passes():
    frames = _label_frames()
    m = _HonestAlpha()
    m.fit(frames)
    res = LeakageTester().run(m, frames)
    assert res.label_guard_ok is True
    assert res.truncation_ok is True
    assert res.passed is True
    assert abs(res.ic_unshifted) < 0.15
    # a persistent signal keeps most of its IC under a one-row shift
    assert abs(res.ic_shifted) >= res.required_shift_ratio * abs(res.ic_unshifted)


def test_leakage_detector_catches_sub_threshold_leak():
    """A shifted-label leak worth IC 0.12 — 12x the PROMOTE gate — must fail.

    The old detector only fired above |IC| > 0.15 and let this through.
    """
    n = 20000
    rng = np.random.default_rng(11)
    ts = np.arange(n, dtype=np.int64) * 100_000_000 + NS_S
    future = rng.standard_normal(n) * 1e-4
    leak = 0.12 * (future / 1e-4) + np.sqrt(1 - 0.12 ** 2) * rng.standard_normal(n)
    df = pd.DataFrame({
        "exchange_ts": ts,
        "sig_feature": leak,
        "ret_feature": np.concatenate(([0.0], future[:-1])),
        "label_mid_1s": future,
        "label_valid_1s": np.ones(n, dtype=bool),
    })
    m = _HonestAlpha()
    frames = {1: df}
    m.fit(frames)
    res = LeakageTester().run(m, frames)
    assert 0.10 < abs(res.ic_unshifted) < 0.15
    assert abs(res.ic_shifted) < 0.03
    assert res.shift_ok is False, "a 12x-gate leak must be flagged"
    assert res.passed is False


def test_leakage_shift_test_is_disabled_when_rows_span_the_horizon():
    """On a 15 s-spaced FX stream a 1 s alpha MUST collapse under a one-row
    shift: that is row spacing, not lookahead, so the test cannot fire."""
    frames = _label_frames()
    df = frames[1].copy()
    df["exchange_ts"] = np.arange(len(df), dtype=np.int64) * 15 * NS_S + NS_S
    m = _HonestAlpha()
    m.fit({1: df})
    res = LeakageTester().run(m, {1: df})
    assert res.required_shift_ratio == 0.0
    assert res.shift_ok is True
    assert res.median_row_gap_ns == 15 * NS_S


def test_leakage_truncation_probe_catches_future_row_reader():
    """A score() that reads the NEXT row fails the engine-level probe even
    where the shift heuristic is inconclusive."""
    frames = _label_frames()
    m = _PeekAheadAlpha()
    m.fit(frames)
    tester = LeakageTester()
    assert tester.truncation_probe(m, frames) is False
    assert tester.truncation_probe(_fitted_honest(frames), frames) is True


def _fitted_honest(frames):
    m = _HonestAlpha()
    m.fit(frames)
    return m


# -- experiments ledger --------------------------------------------------


def test_ledger_counts_persist_and_reload(tmp_path):
    path = tmp_path / "experiments.json"
    led = ExperimentLedger(path)
    assert led.total_experiments == 0
    led.record("EQ01", "unit_test", config={"a": 1}, count=3)
    led.record("EQ02", "unit_test")
    assert led.total_experiments == 4
    led.save()
    led2 = ExperimentLedger(path)
    assert led2.total_experiments == 4
    assert len(led2.entries) == 2
    led2.record("EQ03", "unit_test")
    assert led2.total_experiments == 5
    with pytest.raises(ValueError):
        led2.record("EQ04", "unit_test", count=0)


def test_ledger_bonferroni_math(tmp_path):
    led = ExperimentLedger(tmp_path / "e.json")
    led.record("X", "t", count=50)
    assert abs(led.bonferroni_threshold() - 0.05 / 50) < 1e-15
    # |t| threshold: two-sided p = 0.001 -> one tail 0.0005 -> z ~ 3.2905
    assert abs(led.bonferroni_t_threshold() - 3.2905) < 5e-3
    assert abs(led.expected_max_null_t() - math.sqrt(2 * math.log(50))) < 1e-12
    assert "50 experiments" in led.note()


def test_ledger_file_is_deterministic(tmp_path):
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    for p in (p1, p2):
        led = ExperimentLedger(p)
        led.record("EQ01", "k", config={"z": 1}, result={"ic": 0.5})
        led.save()
    assert p1.read_bytes() == p2.read_bytes()
    blob = json.loads(p1.read_text())
    assert blob["total_experiments"] == 1


# -- round-3: row-mass folds, degenerate folds, NW lag, crossed rows -------


def _two_day_frames(rows_per_day=4000, seed=3):
    """Frames shaped like the bundled equity data: 2.6 h of rows per day
    inside a 24 h calendar, plus a lone close print 4 h later."""
    rng = np.random.default_rng(seed)
    day = 86_400 * NS_S
    ts = []
    for d in (0, 1):
        base = d * day + 13 * 3600 * NS_S + 1800 * NS_S
        ts.extend(base + np.arange(rows_per_day, dtype=np.int64)
                  * (9360 * NS_S // rows_per_day))
        ts.append(d * day + 20 * 3600 * NS_S)  # 20:00 close print
    ts = np.array(sorted(ts), dtype=np.int64)
    n = ts.size
    z = rng.standard_normal(n)
    future = (0.05 * z + rng.standard_normal(n)) * 1e-4
    return {1: pd.DataFrame({
        "exchange_ts": ts,
        "sig_feature": z,
        "spread_ticks_v1": np.ones(n),
        "label_mid_1s": future,
        "label_valid_1s": np.ones(n, dtype=bool),
    })}


def test_walk_forward_splits_on_row_mass_not_wall_span():
    frames = _two_day_frames()
    rows = len(frames[1])
    sp = WalkForwardSplitter(n_folds=4, embargo_ns=NS_S)
    sizes = [len(test[1]) for _, _, test in sp.split_frames(frames, _H["1s"])]
    assert len(sizes) == 4
    assert all(s > MIN_TEST_PAIRS for s in sizes), sizes
    # every fold carries roughly a fifth of the rows
    for s in sizes:
        assert abs(s - rows / 5) < rows * 0.05, sizes

    # the old wall-span mode is exactly the failure mode we fixed
    wall = WalkForwardSplitter(n_folds=4, embargo_ns=NS_S, mode="wall_span")
    wall_sizes = [len(test[1]) for _, _, test in wall.split_frames(frames, _H["1s"])]
    assert min(wall_sizes) == 0, "wall-span folds should still show the defect"


def test_walk_forward_degenerate_fold_is_reported_and_fails_the_gate():
    """A fold with < MIN_TEST_PAIRS rows counts as a FAILED fold."""
    from iap.validation.splits import MIN_NONDEGENERATE_FOLDS

    fold_rows = [
        {"fold": 1, "ic": 0.02, "degenerate": False},
        {"fold": 2, "ic": None, "degenerate": True},
        {"fold": 3, "ic": None, "degenerate": True},
        {"fold": 4, "ic": 0.03, "degenerate": False},
    ]
    n_run = len(fold_rows)
    n_nondeg = sum(1 for r in fold_rows if not r["degenerate"])
    positive = sum(1 for r in fold_rows
                   if not r["degenerate"] and r["ic"] is not None and r["ic"] > 0)
    assert n_nondeg == 2
    assert positive / n_run == 0.5  # NOT 1.00
    assert n_nondeg < MIN_NONDEGENERATE_FOLDS  # blocks PROMOTE


def test_row_mass_split_rejects_a_sample_it_cannot_divide():
    ts = np.full(500, 1_000_000_000, dtype=np.int64)
    sp = WalkForwardSplitter(n_folds=4)
    with pytest.raises(ValueError, match="strictly increasing"):
        sp.folds_by_row_mass(ts)
    with pytest.raises(ValueError, match="too few rows"):
        sp.folds_by_row_mass(np.arange(10, dtype=np.int64))


def test_nw_lag_scales_with_horizon():
    assert nw_lags(_H["1s"]) == 2
    assert nw_lags(_H["5m"]) == 2
    assert nw_lags(_H["15m"]) >= 3
    # a longer horizon over the same buckets needs more lags
    assert nw_lags(_H["15m"]) > nw_lags(_H["1m"])
    with pytest.raises(ValueError):
        nw_lags(0)


def test_crossed_rows_are_split_out_of_the_reported_ic():
    """A signal whose IC lives entirely on crossed (stale-LP) rows must show
    a near-zero uncrossed IC — the number the PROMOTE gate reads."""
    from iap.validation.validate import _pooled_arrays

    n = 4000
    rng = np.random.default_rng(5)
    crossed = np.zeros(n, dtype=bool)
    crossed[: int(0.3 * n)] = True
    rng.shuffle(crossed)
    z = rng.standard_normal(n)
    lab = np.where(crossed, 0.5 * z, 0.0) * 1e-4 + rng.standard_normal(n) * 1e-6
    df = pd.DataFrame({
        "exchange_ts": np.arange(n, dtype=np.int64) * NS_S,
        "spread_ticks_v1": np.where(crossed, -1.0, 1.0),
        "label_mid_1s": lab,
        "label_valid_1s": np.ones(n, dtype=bool),
    })
    scores = {1: pd.DataFrame({
        "exchange_ts": df["exchange_ts"],
        "expected_return": z,
        "confidence": np.ones(n),
    })}
    ts, er, y, cr = _pooled_arrays(scores, {1: df}, "1s")
    assert abs(np.mean(cr) - 0.3) < 0.02
    pooled = ic(er, y)
    unc = ~cr
    ic_unc = ic(np.where(unc, er, np.nan), np.where(unc, y, np.nan))
    ic_cr = ic(np.where(cr, er, np.nan), np.where(cr, y, np.nan))
    assert ic_cr > 0.5
    assert abs(ic_unc) < 0.1
    assert ic_unc < pooled, "the pooled IC hides the crossed-row artefact"


def test_ledger_reruns_do_not_inflate_the_denominator(tmp_path):
    """Re-running the same script must not change the multiple-testing
    denominator every report and paper quotes."""
    path = tmp_path / "experiments.json"
    for _ in range(5):
        led = ExperimentLedger(path)
        for aid in ("EQ01", "EQ02"):
            led.record(aid, "alpha_validation", config={"folds": 4},
                       result={"ic": 0.02})
        led.save()
    led = ExperimentLedger(path)
    assert led.total_experiments == 2
    assert led.distinct_experiments == 2
    assert led.entries[0]["reruns"] >= 4
    # a genuinely different configuration IS a new experiment
    led.record("EQ01", "alpha_validation", config={"folds": 8})
    assert led.total_experiments == 3


# -- round-4: z-scored folds/stress, turnover denominator, NW weighting ---


def _backwards_frames(n=3000, seed=17):
    """Frames whose signal is ANTI-correlated with every horizon's label.

    A free-signed OLS fit lands on beta < 0 here, so ``expected_return``
    carries the opposite sign to the standardized signal ``z`` the gate is
    computed on — the exact configuration that made EQ09 report
    ``fold_sign_consistency = 1.00`` beside a negative gate IC.
    """
    rng = np.random.default_rng(seed)
    ts = np.arange(n, dtype=np.int64) * NS_S + NS_S
    z = rng.standard_normal(n)
    noise = rng.standard_normal(n)
    future = (-0.25 * z + noise) * 1e-4
    cols = {
        "exchange_ts": ts,
        "sig_feature": z,
        "mid_price_v1": 25.0 + np.cumsum(future) * 25.0,
        "spread_ticks_v1": np.ones(n),
        "vol_regime_flag_v1": np.tile([0.0, 1.0], n // 2),
    }
    for h in _H:
        cols[f"label_mid_{h}"] = future
        cols[f"label_valid_{h}"] = np.ones(n, dtype=bool)
    return {1: pd.DataFrame(cols)}


class _BackwardsAlpha(LinearAlpha):
    """Fixture whose stated direction the data contradicts.

    Economic rationale: fixture — the raw signal is oriented positively but
    the planted relationship is negative, so the free-signed fit returns
    beta < 0.  Exists only inside the test suite.
    """

    alpha_id = "TST4"
    name = "backwards_fixture"
    asset_class = "EQUITY"
    horizon = "1s"
    features = ("sig_feature",)

    def universe(self, ids):
        return [i for i in sorted(ids) if i == 1]

    def raw_signal(self, df):
        return df["sig_feature"]


def _validate_backwards():
    from iap.backtest import Backtester, BacktestConfig, CostModel
    from iap.validation.validate import validate_alpha

    frames = _backwards_frames()
    meta = {1: {"tick_size": 0.01, "lot_size": 1, "adv": 1_000_000.0,
                "asset_class": "EQUITY", "ref_price": 25.0}}
    bt = Backtester(
        CostModel(impact_coeff_bps_per_pct_adv=2.0,
                  equity_taker_fee_per_share=0.003,
                  fx_commission_per_million=2.5),
        meta, BacktestConfig(max_pos_qty=100, latency_rows=1))
    return validate_alpha(_BackwardsAlpha, frames, bt,
                          {1: {"adv": 1_000_000.0, "ref_price": 25.0}},
                          0.1, n_folds=4, embargo_ns=NS_S)


@pytest.fixture(scope="module")
def backwards_report():
    return _validate_backwards()


def test_fold_sign_consistency_is_scored_on_z_not_expected_return(backwards_report):
    """An alpha that is backwards in every fold must score 0.00, not 1.00.

    ``beta_k`` is refit free-signed per fold, so ``ic(er, y)`` is
    ``sign(beta_k) * ic(z, y)``.  Counting folds by ``ic(er, y) > 0`` made a
    uniformly-backwards alpha look perfectly consistent while the gate IC
    (computed on ``z``) was negative in the same report.
    """
    rep = backwards_report
    folds = [f for f in rep["folds"] if not f["degenerate"]]
    assert len(folds) == 4
    assert rep["gate_ic"] < 0.0
    for f in folds:
        assert f["ic"] < 0.0, "the fold IC must agree in sign with the gate"
        assert f["ic_er"] > 0.0, "the er diagnostic is the sign-flipped one"
        assert f["hit_rate"] < 0.5
    assert rep["fold_sign_consistency"] == 0.0
    # what the defect reported: every fold counted as a success
    er_positive = sum(1 for f in folds if f["ic_er"] > 0)
    assert er_positive / len(rep["folds"]) == 1.0
    assert rep["verdict"] == "REJECT"


def test_stress_and_decay_agree_in_sign_with_the_gate(backwards_report):
    """Regime and decay ICs are scored on z too: a backwards alpha must not
    publish a positive high-vol IC beside a negative gate IC."""
    rep = backwards_report
    assert rep["gate_ic"] < 0.0
    assert rep["stress"]["regime"]["ic_high_vol"] < 0.0
    assert rep["stress"]["regime"]["ic_low_vol"] < 0.0
    assert rep["stress"]["latency"]["+0ev"]["ic"] < 0.0
    assert rep["decay_ic_by_horizon"]["1s"] < 0.0


def test_decay_curve_applies_the_confidence_mask():
    """Rows whose signal was NaN must DROP OUT of the decay IC.

    Without the mask ``_pooled_arrays`` applies everywhere else, an invalid
    signal enters the correlation as an exact 0.0 — not a prediction of "no
    move" but the absence of a prediction — and shrinks the IC toward 0.
    """
    from iap.validation.metrics import decay_curve

    n = 4000
    rng = np.random.default_rng(23)
    z = rng.standard_normal(n)
    future = (0.3 * z + rng.standard_normal(n)) * 1e-4
    dead = np.zeros(n, dtype=bool)
    dead[: int(0.6 * n)] = True          # 60 % invalid, as in FX02's last fold
    rng.shuffle(dead)
    er = np.where(dead, 0.0, z)          # what score() emits at confidence 0
    frame = pd.DataFrame({
        "exchange_ts": np.arange(n, dtype=np.int64) * NS_S,
        "label_mid_1s": future,
        "label_valid_1s": np.ones(n, dtype=bool),
    })
    unmasked = decay_curve(er, frame, horizons=("1s",))["1s"]
    masked = decay_curve(np.where(dead, np.nan, er), frame,
                         horizons=("1s",))["1s"]
    honest = ic(np.where(dead, np.nan, z), np.where(dead, np.nan, future))
    assert abs(masked - honest) < 1e-12
    assert abs(unmasked) < abs(masked), "the zeros diluted the reported IC"


def test_newey_west_tstat_weights_buckets_by_pair_count():
    """One thin bucket must not move the headline t the way it used to.

    Bucket ICs are means over very different pair counts (81..2 592 here);
    weighting them equally let a single small bucket swing EQ03's reported t
    from 4.89 to 11.46.
    """
    from iap.validation.metrics import bucket_ics_with_counts

    ics = np.array([0.02] * 11 + [-0.40])       # 11 fat buckets + 1 thin one
    counts = np.array([2000] * 11 + [20])
    equal = newey_west_tstat(ics, lags=2)
    weighted = newey_west_tstat(ics, lags=2, weights=counts)
    assert equal < 0.0, "the thin bucket flips the equal-weighted mean"
    assert weighted > 0.0, "20 pairs cannot outvote 22 000"
    # equal weights must reduce EXACTLY to the pinned unweighted formula
    same = newey_west_tstat(ics, lags=2, weights=np.ones(len(ics)))
    assert abs(same - equal) < 1e-12

    # counts come out of the bucketer alongside the ICs, aligned
    ts = np.concatenate([np.zeros(40, np.int64),
                         np.full(10, 400, np.int64)]) * NS_S
    x = np.concatenate([np.arange(40.0), np.arange(10.0)])
    y = np.concatenate([np.arange(40.0), -np.arange(10.0)])
    b, c = bucket_ics_with_counts(ts, x, y, bucket_ns=300 * NS_S, min_obs=4)
    assert list(c) == [40, 10]
    assert abs(b[0] - 1.0) < 1e-12 and abs(b[1] + 1.0) < 1e-12


def test_bucket_size_distribution_is_reported(backwards_report):
    """``n_ic_buckets`` alone hides how uneven the evidence is."""
    dist = backwards_report["ic_bucket_pairs"]
    assert dist["n_buckets"] == backwards_report["n_ic_buckets"]
    assert dist["min"] <= dist["median"] <= dist["max"]
    assert dist["total_pairs"] > 0


def test_signal_turnover_denominator_excludes_session_gaps():
    """Turnover is a COST statistic: the dead hours must not dilute it.

    Dividing flips by the wall span bills the strategy for hours in which it
    cannot flip and understated equity turnover ~4.6x (EQ01: 29.42 flips/h
    reported against 134.7 of open market).
    """
    from iap.validation.metrics import signal_turnover_detail

    # two 30-minute sessions of 1-minute rows, 4 hours apart
    minute = 60 * NS_S
    s1 = np.arange(31, dtype=np.int64) * minute
    ts = np.concatenate([s1, s1 + s1[-1] + 4 * 3600 * NS_S])
    er = np.where(np.arange(len(ts)) % 2 == 0, 1.0, -1.0)
    conf = np.ones(len(ts))
    det = signal_turnover_detail(ts, er, conf)
    assert det["flips"] == len(ts) - 1
    assert abs(det["active_hours"] - 1.0) < 1e-12, "two half-hour sessions"
    assert abs(det["span_hours"] - 5.0) < 1e-12
    assert abs(det["flips_per_hour"] - det["flips"]) < 1e-9
    # the old wall-span denominator understated it fivefold
    assert abs(det["flips"] / det["span_hours"] - det["flips_per_hour"] / 5.0) < 1e-9
    assert signal_turnover(ts, er, conf) == det["flips_per_hour"]


def test_turnover_denominator_is_reported(backwards_report):
    rep = backwards_report
    assert rep["turnover_active_hours"] > 0.0
    assert rep["turnover_span_hours"] >= rep["turnover_active_hours"]

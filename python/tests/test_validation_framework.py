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
    ExperimentLedger,
    LeakageTester,
    WalkForwardSplitter,
    bucket_ics,
    hit_rate,
    ic,
    newey_west_tstat,
    rank_ic,
    signal_turnover,
)

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


def _label_frames(n=3000, seed=7):
    """Frames with a genuine (weak) signal plus labels, deterministic."""
    rng = np.random.default_rng(seed)  # test-only fixture data, not a shared path
    ts = np.arange(n, dtype=np.int64) * 100_000_000 + NS_S
    future = rng.standard_normal(n) * 1e-4
    sig = 0.05 * future / 1e-4 + rng.standard_normal(n)  # weak look-ahead-free proxy
    df = pd.DataFrame(
        {
            "exchange_ts": ts,
            "sig_feature": sig,
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
    assert res.passed is True
    assert abs(res.ic_unshifted) < 0.15


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

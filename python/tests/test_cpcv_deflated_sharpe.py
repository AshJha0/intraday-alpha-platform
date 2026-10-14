"""v1.12 (plan item R7): combinatorial purged CV, PBO, PSR / DSR / MinTRL.

Closed-form and published values: the DSR worked example of Bailey &
López de Prado (2014), PSR identities, MinTRL consistency, the CPCV split
and path counts, purge/embargo correctness, PBO on noise and on a planted
strong configuration, and the opt-in ``v4`` wiring.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd
import pytest
from iap.validation.cpcv import (
    CombinatorialPurgedSplitter,
    n_paths,
    n_splits,
    probability_of_backtest_overfitting,
)
from iap.validation.deflated import (
    daily_pnl_from_bars,
    deflated_sharpe_block,
    deflated_sharpe_ratio,
    effective_trials,
    expected_max_sharpe,
    min_track_record_length,
    norm_cdf,
    probabilistic_sharpe_ratio,
    sharpe_moments,
    study_deflated_sharpe,
)
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import METHODS_V3, METHODS_V4, methods
from iap.validation.validate import CPCV_LOOKS, DSR_LOOKS, looks_per_validation
from test_research_validity_v19 import _multi_day_frames, _TwoInstrumentAlpha, _validate

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S

# ------------------------------------------------------------- DSR / PSR


def test_dsr_matches_the_published_worked_example():
    """Bailey & López de Prado (2014), section 5: annualised SR 2.5 over
    1250 daily observations, skew -3, kurtosis 10, N = 100 trials with an
    annualised cross-trial SR variance of 0.5 -> SR0 ~ 0.1132 (daily) and
    DSR ~ 0.9004."""
    sr = 2.5 / math.sqrt(250)
    v = 0.5 / 250
    assert expected_max_sharpe(100, v) == pytest.approx(0.1132, abs=5e-4)
    d = deflated_sharpe_ratio(sr, 1250, 100, skew=-3.0, kurtosis=10.0, trials_sr_variance=v)
    assert d["dsr"] == pytest.approx(0.9004, abs=1e-3)


def test_psr_identities():
    # benchmark equal to the estimate: exactly one half
    assert probabilistic_sharpe_ratio(0.1, 0.1, 500, -1.0, 6.0) == pytest.approx(0.5)
    # normal returns: Phi((SR - SR*) sqrt(T-1) / sqrt(1 + SR^2 / 2))
    sr, t = 0.08, 400
    want = norm_cdf(sr * math.sqrt(t - 1) / math.sqrt(1 + sr * sr / 2))
    assert probabilistic_sharpe_ratio(sr, 0.0, t) == pytest.approx(want)
    # negative skew and fat tails lower the confidence in a positive SR
    assert probabilistic_sharpe_ratio(sr, 0.0, t, -2.0, 9.0) < probabilistic_sharpe_ratio(sr, 0, t)


def test_min_track_record_length_is_where_psr_reaches_one_minus_alpha():
    sr, skew, kurt = 0.12, -0.5, 5.0
    trl = min_track_record_length(sr, 0.0, skew, kurt, alpha=0.05)
    assert probabilistic_sharpe_ratio(sr, 0.0, trl, skew, kurt) == pytest.approx(0.95, abs=1e-6)
    assert min_track_record_length(0.0, 0.0) == math.inf


def test_expected_max_sharpe_grows_with_trials_and_is_zero_for_one():
    assert expected_max_sharpe(1, 0.01) == 0.0
    vals = [expected_max_sharpe(n, 0.01) for n in (2, 10, 100, 1000)]
    assert vals == sorted(vals) and vals[0] > 0


def test_sharpe_moments_of_a_normal_sample():
    x = np.random.default_rng(1).normal(0.1, 1.0, 200_000)
    m = sharpe_moments(x)
    assert m["sharpe"] == pytest.approx(0.1, abs=0.01)
    assert m["skew"] == pytest.approx(0.0, abs=0.03)
    assert m["kurtosis"] == pytest.approx(3.0, abs=0.05)


def test_dsr_block_reports_both_trial_counts_and_refuses_tiny_samples():
    pnl = np.random.default_rng(2).normal(1.0, 2.0, 60)
    b = deflated_sharpe_block(pnl, 12, n_looks=900)
    assert b["n_days"] == 60 and b["trials_basis"] == "distinct_configurations"
    assert 0 < b["dsr_at_looks"] < b["dsr"] < b["psr"] < 1
    assert b["sharpe_annualised"] == pytest.approx(b["sharpe_per_day"] * math.sqrt(252))
    tiny = deflated_sharpe_block([1.0, 2.0], 5)
    assert tiny["psr"] is None and tiny["dsr"] is None


def test_study_helper_reads_a_per_day_table_rows_or_a_series():
    pnl = [3.0, -1.0, 2.0, 0.5, 1.5, -0.5]
    a = study_deflated_sharpe(pd.DataFrame({"day": range(6), "net": pnl}), 4)
    b = study_deflated_sharpe([{"net": v} for v in pnl], 4)
    c = study_deflated_sharpe(np.asarray(pnl), 4)
    assert a == b == c and a["dsr"] is not None


def test_daily_pnl_from_bars_sums_by_utc_day():
    ts = [NS_DAY * 10 + 5, NS_DAY * 10 + 9, NS_DAY * 11 + 1]
    assert daily_pnl_from_bars(ts, [1.0, 2.0, -4.0]) == [3.0, -4.0]


def test_effective_trials_counts_distinct_configurations(tmp_path):
    led = ExperimentLedger(tmp_path / "x.json")
    assert effective_trials(led) == 1
    led.record("A", "k", {"w": 1}, {}, count=83)
    led.record("A", "k", {"w": 1}, {}, count=83)  # rerun: de-duplicated
    led.record("B", "k", {"w": 1}, {}, count=83)
    assert led.total_experiments == 166
    assert effective_trials(led) == 3 and effective_trials(led, include_new=False) == 2


# ------------------------------------------------------------------ CPCV


@pytest.mark.parametrize(
    ("n", "k", "splits", "paths"), [(6, 2, 15, 5), (10, 2, 45, 9), (5, 3, 10, 6)]
)
def test_split_and_path_counts(n, k, splits, paths):
    assert n_splits(n, k) == splits and n_paths(n, k) == paths
    assert paths == k * splits // n
    cv = CombinatorialPurgedSplitter(n, k)
    ts = np.arange(n * 100, dtype=np.int64)
    got = cv.splits(ts)
    assert len(got) == splits
    assert [s.test_groups for s in got] == list(combinations(range(n), k))
    assign = cv.path_assignment()
    assert len(assign) == paths
    # each (split, group) cell used exactly once; each path covers every group
    cells = [(i, g) for a in assign for g, i in a.items()]
    assert len(cells) == len(set(cells)) == splits * k
    assert all(sorted(a) == list(range(n)) for a in assign)
    assert all(g in got[i].test_groups for a in assign for g, i in a.items())


def test_groups_are_day_aligned_when_there_are_enough_days():
    ts = np.concatenate(
        [d * NS_DAY + np.arange(100, dtype=np.int64) * NS_S for d in range(100, 106)]
    )
    cv = CombinatorialPurgedSplitter(3, 1)
    g = cv.groups(ts)
    assert cv.grouping == "day_aligned"
    assert [s // NS_DAY for s, _ in g] == [100, 102, 104]
    assert all(s % NS_DAY == 0 for s, _ in g)
    cv7 = CombinatorialPurgedSplitter(7, 2)
    cv7.groups(ts)
    assert cv7.grouping == "row_mass"


def test_purge_and_embargo_on_both_sides_no_leakage():
    ts = np.arange(6000, dtype=np.int64) * NS_S
    horizon, embargo = 30 * NS_S, 10 * NS_S
    cv = CombinatorialPurgedSplitter(6, 2, embargo_ns=embargo)
    for split in cv.splits(ts):
        train = split.train_mask(ts, horizon, embargo)
        test = split.test_mask(ts)
        assert not np.any(train & test)
        tr = ts[train]
        for s, e in split.intervals():
            # no train label window reaches into a test interval
            assert not np.any((tr + horizon + embargo >= s) & (tr < e))
            # nothing trains within the embargo after it
            assert not np.any((tr >= e) & (tr < e + embargo))
        # and everything else IS trained on (the purge is not over-eager)
        lost = ~train & ~test
        assert lost.sum() <= len(split.intervals()) * (horizon + 2 * embargo) // NS_S + 2


def test_adjacent_test_groups_merge_into_one_interval():
    cv = CombinatorialPurgedSplitter(4, 2)
    ts = np.arange(400, dtype=np.int64)
    s = cv.splits(ts)[0]  # groups (0, 1)
    assert s.test_groups == (0, 1) and len(s.intervals()) == 1


# ------------------------------------------------------------------- PBO


def test_pbo_is_about_one_half_on_pure_noise():
    rng = np.random.default_rng(7)
    pbos = [
        probability_of_backtest_overfitting(rng.standard_normal((480, 20)), n_blocks=10)["pbo"]
        for _ in range(8)
    ]
    assert 0.3 < float(np.mean(pbos)) < 0.7


def test_pbo_is_about_zero_with_a_planted_strong_configuration():
    rng = np.random.default_rng(8)
    m = rng.standard_normal((480, 20))
    m[:, 13] += 0.6  # one genuinely better configuration
    out = probability_of_backtest_overfitting(m, n_blocks=10)
    assert out["pbo"] < 0.05
    assert out["n_splits"] == math.comb(10, 5) and out["n_candidates"] == 20


def test_pbo_rejects_bad_inputs():
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.zeros((100, 1)))
    with pytest.raises(ValueError):
        probability_of_backtest_overfitting(np.zeros((100, 3)), n_blocks=5)


# ----------------------------------------------------------- v4 wiring


def test_v4_bundle_is_v3_plus_report_only_blocks():
    v3, v4 = methods(METHODS_V3), methods(METHODS_V4)
    kw = v4.validate_kwargs()
    assert kw == {**v3.validate_kwargs(), "cpcv": True, "deflated_sharpe": True}
    # v1.14.0 (R-P2-4): the report-only blocks make no gate decision
    assert v4.looks(4) == v3.looks(4) == 2
    v3_old, v4_old = methods("v3_v113"), methods("v4_v113")
    assert v4_old.looks(4) == v3_old.looks(4) + CPCV_LOOKS + DSR_LOOKS
    assert looks_per_validation(4, looks_rule="statistics") == 83


def test_validate_with_cpcv_and_dsr_keeps_the_v3_verdict():
    frames = _multi_day_frames(per_day=500)
    v3 = _validate(frames, **methods(METHODS_V3).validate_kwargs())
    with pytest.raises(ValueError, match="deflated_sharpe_trials"):
        _validate(frames, **methods(METHODS_V4).validate_kwargs())
    v4 = _validate(
        frames,
        **methods(METHODS_V4).validate_kwargs(),
        cpcv_groups=5,
        deflated_sharpe_trials=7,
    )
    assert v4["verdict"] == v3["verdict"] and v4["gate_ic"] == v3["gate_ic"]
    assert v4["methods"]["cpcv"] is True and v4["methods"]["deflated_sharpe"] is True
    c = v4["cpcv"]
    assert c["grouping"] == "day_aligned" and c["n_splits"] == 10 and c["n_paths"] == 4
    assert len(c["splits"]) == 10 and len(c["path_ics"]) == 4
    # the backwards fixture is backwards on every path
    assert all(v is not None and v < 0 for v in c["path_ics"])
    d = v4["deflated_sharpe"]
    assert d["n_trials"] == 7 and d["n_looks"] == 88 and d["n_days"] >= 1
    assert "cpcv" not in v3 and "deflated_sharpe" not in v3


# ----------------------------------------------- v1.15 memory: narrowed frames


def _v4(frames):
    return _validate(
        frames, **methods(METHODS_V4).validate_kwargs(), cpcv_groups=5, deflated_sharpe_trials=7
    )


def test_cpcv_block_on_narrowed_frames_is_identical(monkeypatch):
    """The cpcv block narrows the frames to the columns it reads (R7 OOM fix);
    the report must equal the one computed on the full frames, also when the
    frames carry many unrelated columns."""
    import iap.validation.validate as v

    frames = _multi_day_frames(per_day=500)
    rng = np.random.default_rng(7)
    for df in frames.values():
        for j in range(30):
            df[f"junk_{j}_v1"] = rng.standard_normal(len(df))
    seen = []
    real = v._cpcv_columns
    monkeypatch.setattr(v, "_cpcv_columns", lambda *a: seen.append(real(*a)) or seen[-1])
    narrowed = _v4(frames)["cpcv"]
    assert seen and seen[0] is not None and not any(c.startswith("junk_") for c in seen[0])
    monkeypatch.setattr(v, "_cpcv_columns", lambda *a, **k: None)
    full = _v4(frames)["cpcv"]
    assert narrowed == full


def test_cpcv_block_fails_loudly_on_missing_column(monkeypatch):
    """v1.16: an alpha that reads a column outside the narrowed set raises
    (it used to rerun silently on full-width frames, the R7 OOM path)."""
    import iap.validation.validate as v

    frames = _multi_day_frames(per_day=500)
    real = v._cpcv_columns
    calls = []

    def narrow_cpcv_only(*a, **k):
        calls.append(1)
        return real(*a, **k) if len(calls) == 1 else {"exchange_ts"}

    monkeypatch.setattr(v, "_cpcv_columns", narrow_cpcv_only)
    with pytest.raises(ValueError, match="cpcv: the alpha read column"):
        _v4(frames)


def test_whole_v4_report_is_identical_with_narrowed_walk_forward_train(monkeypatch):
    """v1.15 R7 (EQ05 OOM): walk-forward train frames keep only the columns a
    fit reads.  The full v4 report must not change, with unrelated columns."""
    import iap.validation.validate as v

    frames = _multi_day_frames(per_day=500)
    rng = np.random.default_rng(11)
    for df in frames.values():
        for j in range(30):
            df[f"junk_{j}_v1"] = rng.standard_normal(len(df))
    narrowed = _v4(frames)
    monkeypatch.setattr(v, "_cpcv_columns", lambda *a, **k: None)
    monkeypatch.setattr(v, "_test_columns", lambda *a, **k: None)
    full = _v4(frames)
    assert narrowed == full


def test_walk_forward_test_columns_drop_unread_features():
    """v1.16: walk-forward test frames keep ids, every label, the market
    columns and the alpha's features - never the unrelated feature columns."""
    import iap.validation.validate as v

    frames = _multi_day_frames(per_day=300)
    for df in frames.values():
        df["junk_0_v1"] = 0.0
    model = _TwoInstrumentAlpha()
    cols = v._test_columns(lambda: model, frames)
    assert "junk_0_v1" not in cols
    assert set(model.features) <= cols
    assert {c for df in frames.values() for c in df.columns if c.startswith("label_")} <= cols


def test_narrowed_frames_keep_labels_of_other_horizons(monkeypatch):
    """PR #148 CI regression: a combined alpha fits each member at the member's
    own horizon, so train/CPCV frames must keep the labels of every horizon,
    not only the validated alpha's."""
    import iap.validation.validate as v

    frames = _multi_day_frames(per_day=500)
    for df in frames.values():
        df["label_mid_1m"] = 0.0
    seen = []
    fit = _TwoInstrumentAlpha.fit

    def fit_reading_other_horizon(self, train):
        seen.append(all("label_mid_1m" in df.columns for df in train.values()))
        return fit(self, train)

    monkeypatch.setattr(_TwoInstrumentAlpha, "fit", fit_reading_other_horizon)
    _v4(frames)
    assert seen and all(seen)
    assert "label_mid_1m" in v._cpcv_columns(_TwoInstrumentAlpha, frames)


def test_walk_forward_fails_loudly_when_a_column_is_missing(monkeypatch):
    """v1.16: no silent full-width refit when a fit reads an undeclared column."""
    import iap.validation.validate as v

    frames = _multi_day_frames(per_day=500)
    monkeypatch.setattr(v, "_cpcv_columns", lambda *a, **k: {"exchange_ts"})
    with pytest.raises(ValueError, match="walk-forward fit: the alpha read column"):
        _v4(frames)


def test_split_frames_train_columns_keep_rows_and_order():
    from iap.validation.splits import WalkForwardSplitter

    frames = _multi_day_frames(per_day=300)
    sp = WalkForwardSplitter(n_folds=3, embargo_ns=0)
    cols = {
        "exchange_ts",
        next(c for c in next(iter(frames.values())).columns if c != "exchange_ts"),
    }
    full = list(sp.split_frames(frames, NS_S))
    narrow = list(sp.split_frames(frames, NS_S, cols))
    for (f1, tr1, te1), (f2, tr2, te2) in zip(full, narrow, strict=True):
        assert f1 == f2
        for iid in tr1:
            assert list(tr2[iid].columns) == [c for c in tr1[iid].columns if c in cols]
            pd.testing.assert_frame_equal(tr2[iid], tr1[iid][list(tr2[iid].columns)])
            pd.testing.assert_frame_equal(te2[iid], te1[iid])
        assert sp.train_frames(frames, f1, NS_S).keys() == tr1.keys()


@pytest.mark.parametrize("shuffle", [False, True])
def test_restrict_frames_slice_equals_mask(shuffle):
    """v1.15 R7 memory: the sorted-path slice returns exactly the masked rows."""
    from iap.research.runner import restrict_frames

    rng = np.random.default_rng(3)
    ts = np.sort(rng.integers(0, 10_000, 2_000)).astype(np.int64)
    df = pd.DataFrame({"exchange_ts": ts, "x": rng.standard_normal(ts.size)})
    if shuffle:
        df = df.sample(frac=1.0, random_state=1).reset_index(drop=True)
    for lo, hi in ((0, 10_000), (2_500, 7_500), (int(ts[100]), int(ts[100])), (-5, 3)):
        got = restrict_frames({1: df}, lo, hi)[1]
        m = (df["exchange_ts"] >= lo) & (df["exchange_ts"] < hi)
        pd.testing.assert_frame_equal(got, df[m].reset_index(drop=True))

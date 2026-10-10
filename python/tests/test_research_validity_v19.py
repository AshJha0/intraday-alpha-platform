"""v1.9 research-validity items R1-R6: seeded stratified day sampling and the
event calendar, day-clustered / day-block-bootstrap t-stats and day-aligned
folds, the equal-weight per-instrument gate IC, single-venue book scope,
the causal label freshness bound, and the validity properties (HAC t
invariant to row duplication, no adjacency across day boundaries, pinned
dataset versions in reports).

Every option is opt-in: the last tests pin that the v2 default report is
unchanged by the new code paths."""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest
from iap.backtest import BacktestConfig, Backtester, CostModel
from iap.labels.labels import (
    LABEL_MAX_AGE_FLOOR_NS,
    LabelReason,
    MidSeries,
    compute_labels,
    max_sample_age,
    trailing_max_sample_age,
)
from iap.validation.methods import METHODS_V2, METHODS_V3, methods
from iap.validation.metrics import (
    bucket_ics_with_counts,
    day_block_bootstrap_tstat,
    day_cluster_tstat,
    newey_west_tstat,
    pooled_slope_hac_tstat,
)
from iap.validation.sessions import (
    book_scope_for_dataset,
    day_tags,
    event_day_mask,
    record_day_sampling,
    stratified_day_sample,
)
from iap.validation.splits import WalkForwardSplitter
from iap.validation.validate import VALIDITY_LOOKS, looks_per_validation, validate_alpha
from test_validation_framework import _H, _BackwardsAlpha

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S
OPEN_NS = (14 * 3600 + 30 * 60) * NS_S  # 14:30 UTC

#: Five consecutive sessions; 2019-01-30 is an FOMC day.
DAYS = ["2019-01-28", "2019-01-29", "2019-01-30", "2019-01-31", "2019-02-01"]


def _day_index(iso: str) -> int:
    return (dt.date.fromisoformat(iso) - dt.date(1970, 1, 1)).days


def _multi_day_frames(per_day=700, n_instruments=2, seed=23):
    """Backwards-fixture frames spread over :data:`DAYS`, one row per second
    from 14:30 UTC, ``n_instruments`` instruments (the second 10x busier in
    label scale so the pooled IC is dominated by it)."""
    rng = np.random.default_rng(seed)
    ts = np.concatenate(
        [_day_index(d) * NS_DAY + OPEN_NS + np.arange(per_day, dtype=np.int64) * NS_S for d in DAYS]
    )
    n = ts.size
    frames = {}
    for iid in range(1, n_instruments + 1):
        z = rng.standard_normal(n)
        scale = 1e-4 if iid == 1 else 1e-3
        future = (-0.25 * z + rng.standard_normal(n)) * scale
        cols = {
            "exchange_ts": ts,
            "sig_feature": z,
            "mid_price_v1": 25.0 + np.cumsum(future) * 25.0,
            "spread_ticks_v1": np.where(np.arange(n) % 10 == 0, -1.0, 1.0),
            "vol_regime_flag_v1": np.tile([0.0, 1.0], n // 2),
            "depth_bid_l1_v1": np.full(n, 10_000.0),
            "depth_ask_l1_v1": np.full(n, 10_000.0),
        }
        for h in _H:
            cols[f"label_mid_{h}"] = future
            cols[f"label_valid_{h}"] = np.ones(n, dtype=bool)
        frames[iid] = pd.DataFrame(cols)
    return frames


def _cost_model() -> CostModel:
    return CostModel(
        impact_coeff_bps_per_pct_adv=2.0,
        equity_taker_fee_per_share=0.003,
        fx_commission_per_million=2.5,
    )


class _TwoInstrumentAlpha(_BackwardsAlpha):
    """Fixture: the backwards signal on a two-instrument universe.

    Economic rationale: fixture only — exists inside the test suite.
    """

    alpha_id = "TST9"

    def universe(self, instruments):
        return sorted(instruments)


def _validate(frames, **kw):
    meta = {
        i: {
            "tick_size": 0.01,
            "lot_size": 1,
            "adv": 1_000_000.0,
            "asset_class": "EQUITY",
            "ref_price": 25.0,
        }
        for i in frames
    }
    bt = Backtester(_cost_model(), meta, BacktestConfig(max_pos_qty=100, latency_rows=1))
    return json.loads(
        json.dumps(
            validate_alpha(
                _TwoInstrumentAlpha,
                frames,
                bt,
                {i: {"adv": 1_000_000.0, "ref_price": 25.0} for i in frames},
                0.1,
                n_folds=4,
                embargo_ns=NS_S,
                ledger_t_threshold=3.5,
                ledger_looks=88,
                n_boot=200,
                **kw,
            ),
            sort_keys=True,
        )
    )


@pytest.fixture(scope="module")
def frames():
    return _multi_day_frames()


@pytest.fixture(scope="module")
def v2_report(frames):
    return _validate(frames)


# ----------------------------------------------------------------- R1


def test_event_calendar_tags_the_known_sessions():
    assert day_tags("2019-01-30") == {"fomc"}
    assert day_tags("2019-10-30") == {"fomc"}
    assert day_tags("2019-12-30") == {"holiday_thin"}
    assert day_tags("2019-05-30") == frozenset()
    ts = np.array([_day_index(d) * NS_DAY + OPEN_NS for d in DAYS])
    assert event_day_mask(ts).tolist() == [False, False, True, False, False]
    with pytest.raises(ValueError):
        event_day_mask(ts, tags=("earnings",))


def test_stratified_day_sample_is_seeded_stratified_and_recorded():
    cands = [
        f"2019-{m:02d}-{d:02d}"
        for m in (1, 2, 3, 4)
        for d in (7, 8, 9, 10, 11, 30)
        if not (m == 2 and d == 30)
    ]
    a = stratified_day_sample(cands, 8, seed=7)
    b = stratified_day_sample(list(reversed(cands)) + cands[:3], 8, seed=7)
    assert a == b  # depends on the candidate SET only
    assert a["method"] == "stratified_v1" and a["seed"] == 7 and len(a["selected"]) == 8
    assert sum(a["allocation"].values()) == 8
    assert all(v == 2 for v in a["allocation"].values())  # 4 months, equal share
    assert a["selected"] == sorted(a["selected"])
    assert stratified_day_sample(cands, 8, seed=8)["selected"] != a["selected"]
    ex = stratified_day_sample(cands, 5, seed=1, strata="event", exclude_tags=("fomc",))
    assert "2019-01-30" in ex["excluded"] and "2019-01-30" not in ex["selected"]
    with pytest.raises(ValueError):
        stratified_day_sample(cands, 100, seed=1)
    with pytest.raises(ValueError):
        stratified_day_sample(cands, 2, seed=1, strata="hour")


def test_day_sampling_is_written_into_dataset_json(tmp_path):
    manifest = {
        "x-version": 1,
        "kind": "real",
        "source": {"format": "itch50", "venue": "XNAS", "venue_id": 101},
        "sessions": {d: {} for d in DAYS},
        "dataset_version": "abc",
    }
    (tmp_path / "dataset.json").write_text(json.dumps(manifest), encoding="utf-8")
    rec = stratified_day_sample(DAYS, 3, seed=11)
    out = record_day_sampling(tmp_path, rec)
    on_disk = json.loads((tmp_path / "dataset.json").read_text(encoding="utf-8"))
    assert on_disk["sampling"] == rec == out["sampling"]
    assert on_disk["dataset_version"] == "abc"  # the version is not the manifest's hash
    with pytest.raises(ValueError, match="does not hold"):
        record_day_sampling(tmp_path, {**rec, "selected": ["2020-01-02"]})
    assert book_scope_for_dataset(tmp_path) == "single_venue"
    assert book_scope_for_dataset(tmp_path / "nope") == "consolidated"
    assert book_scope_for_dataset(None) == "consolidated"


def test_validity_block_reports_results_with_and_without_event_days(frames):
    r = _validate(frames, validity_diagnostics=True)
    v = r["validity"]
    assert v["n_days"] == 4  # day_aligned not set: row-mass folds test the later days
    tags = {row["day"]: row["tags"] for row in v["days"]}
    assert tags.get("2019-01-30") == ["fomc"]
    assert v["ex_event"]["n_days_excluded"] == 1
    assert v["ex_event"]["n_pairs"] < sum(row["n_pairs"] for row in v["days"])
    assert v["ex_event"]["gate_ic"] is not None
    assert r["methods"]["validity_diagnostics"] is True


# ----------------------------------------------------------------- R2


def _days_xy(n_days=6, per=400, seed=3, beta=0.2):
    rng = np.random.default_rng(seed)
    ts = np.concatenate(
        [k * NS_DAY + OPEN_NS + np.arange(per, dtype=np.int64) * 10 * NS_S for k in range(n_days)]
    )
    x = rng.standard_normal(ts.size)
    y = beta * x + rng.standard_normal(ts.size)
    return ts, x, y


def test_day_cluster_and_day_block_bootstrap_tstats():
    ts, x, y = _days_xy()
    tc = day_cluster_tstat(ts, x, y)
    boot = day_block_bootstrap_tstat(ts, x, y, seed=5, n_boot=500)
    assert tc > 3.0 and boot["tstat"] > 3.0 and boot["n_days"] == 6
    assert boot == day_block_bootstrap_tstat(ts, x, y, seed=5, n_boot=500)  # seeded
    assert boot["p05"] < boot["ic"] < boot["p95"]
    assert day_cluster_tstat(ts, -x, y) == pytest.approx(-tc, rel=1e-9)
    # one day: no clusters to compare
    one = ts < NS_DAY
    assert np.isnan(day_cluster_tstat(ts[one], x[one], y[one]))
    assert np.isnan(day_block_bootstrap_tstat(ts[one], x[one], y[one])["tstat"])


def test_day_cluster_t_sees_a_day_level_effect_the_bucket_hac_misses():
    """A slope that is a per-DAY draw (a regime that holds all session) is
    one observation per day: the 5-minute-bucket HAC t treats each day's
    ~80 buckets as near-independent evidence, the day-clustered t does
    not."""
    rng = np.random.default_rng(12)
    n_days, per = 8, 2400
    ts = np.concatenate(
        [k * NS_DAY + OPEN_NS + np.arange(per, dtype=np.int64) * 10 * NS_S for k in range(n_days)]
    )
    slope = np.repeat(0.1 + 0.5 * rng.standard_normal(n_days), per)
    x = rng.standard_normal(ts.size)
    y = slope * x + rng.standard_normal(ts.size)
    hac = pooled_slope_hac_tstat(ts, x, y, lags=2)
    clustered = day_cluster_tstat(ts, x, y)
    assert abs(clustered) < abs(hac) / 3


def test_day_aligned_folds_start_at_session_starts(frames):
    pooled = np.concatenate([df["exchange_ts"].to_numpy() for df in frames.values()])
    starts = {_day_index(d) * NS_DAY + OPEN_NS for d in DAYS}
    rm = WalkForwardSplitter(n_folds=3).folds_by_row_mass(pooled)
    assert any(f.test_start not in starts for f in rm)  # the defect R2 fixes
    da = WalkForwardSplitter(n_folds=4, mode="day_aligned").folds_by_day(pooled)
    assert [f.test_start for f in da] == sorted(starts)[1:]
    lodo = WalkForwardSplitter(n_folds=4, mode="leave_one_day_out")
    folds = lodo.folds_by_day(pooled)
    assert len(folds) == 5 and all(f.two_sided for f in folds)
    for fold, train, test in lodo.split_frames(frames, horizon_ns=5 * NS_S):
        tr = train[1]["exchange_ts"].to_numpy()
        te = test[1]["exchange_ts"].to_numpy()
        assert np.unique(te // NS_DAY).size == 1
        assert not np.isin(tr // NS_DAY, te // NS_DAY).any()
        if fold.index not in (1, 5):
            assert (tr < fold.test_start).any() and (tr >= fold.test_end).any()
    with pytest.raises(ValueError, match="at least 2 session days"):
        WalkForwardSplitter(mode="day_aligned").folds_by_day(np.arange(100) * NS_S)
    with pytest.raises(ValueError):
        WalkForwardSplitter(mode="random")


def test_validate_runs_on_day_aligned_folds(frames):
    r = _validate(frames, split_mode="day_aligned")
    assert r["methods"]["split_mode"] == "day_aligned"
    assert r["n_folds_run"] == 4
    starts = {_day_index(d) * NS_DAY + OPEN_NS for d in DAYS}
    assert all(f["test_start"] in starts for f in r["folds"])


# ----------------------------------------------------------------- R3


def test_gate_can_read_the_equal_weight_instrument_ic(frames, v2_report):
    r = _validate(frames, gate_ic_source="instrument_mean")
    assert r["gate_ic_source"] == "instrument_mean"
    assert r["gate_ic"] == pytest.approx(r["oos_ic_instrument_mean_uncrossed"], rel=1e-12)
    assert r["gate_ic_pooled"] == pytest.approx(v2_report["gate_ic"], rel=1e-12)
    assert v2_report["gate_ic_source"] == "pooled"
    assert v2_report["gate_ic"] == v2_report["gate_ic_pooled"]
    with pytest.raises(ValueError, match="gate_ic_source"):
        _validate(frames, gate_ic_source="median")


# ----------------------------------------------------------------- R4


def test_single_venue_scope_switches_off_crossed_book_logic(frames, v2_report):
    assert v2_report["crossed_frac"] == pytest.approx(0.1, abs=0.01)
    assert v2_report["price_reference"] == "consolidated_mid"
    r = _validate(frames, book_scope="single_venue")
    assert r["book_scope"] == "single_venue" and r["price_reference"] == "nasdaq_bbo"
    assert r["crossed_frac"] is None and r["oos_ic_crossed"] is None
    assert r["oos_ic_uncrossed"] == pytest.approx(r["oos_ic"], rel=1e-12)
    assert r["methods"]["book_scope"] == "single_venue"


# ----------------------------------------------------------------- R5


def _series(ts, tradable=None):
    s = MidSeries()
    for i, t in enumerate(ts):
        s.append(int(t), 100.0, 0.01, True if tradable is None else tradable[i])
    return s


def test_trailing_freshness_bound_is_causal():
    # dense 1 s quotes in the morning, sparse 20 s quotes in the afternoon
    dense = list(range(0, 600 * NS_S, NS_S))
    sparse = list(range(600 * NS_S, 600 * NS_S + 400 * 20 * NS_S, 20 * NS_S))
    s = _series(dense + sparse)
    bounds = trailing_max_sample_age(s, window=64)
    assert bounds[0] == LABEL_MAX_AGE_FLOOR_NS
    assert bounds[300] == LABEL_MAX_AGE_FLOOR_NS  # morning: floor binds
    assert bounds[-1] == 40 * NS_S  # afternoon: 2 x 20 s
    # causality: bounds up to k do not change when later samples change
    s2 = _series(dense + [t + 7 * NS_S for t in sparse])
    assert trailing_max_sample_age(s2, window=64)[:600] == bounds[:600]
    # the whole-day bound is a single number set by both regimes
    assert max_sample_age(s) == LABEL_MAX_AGE_FLOOR_NS


def test_trailing_freshness_changes_only_the_stale_bit():
    # a sparse morning (30 s quotes) then a long dense afternoon (1 s): the
    # whole-day median gap is 1 s, so the morning is judged by the floor
    ts = list(range(0, 3000 * NS_S, 30 * NS_S)) + list(range(3000 * NS_S, 6000 * NS_S, NS_S))
    s = _series(ts)
    anchors = [t + NS_S // 2 for t in ts[:-5]]
    whole = compute_labels(anchors, s, ts[-1], horizons=("10s",))["10s"]
    trail = compute_labels(anchors, s, ts[-1], horizons=("10s",), freshness="trailing")["10s"]
    stale = LabelReason.FORWARD_STALE
    n_whole = sum(1 for r in whole.reason if r & stale)
    n_trail = sum(1 for r in trail.reason if r & stale)
    assert n_trail < n_whole  # the sparse afternoon is judged by its own cadence
    for a, b in zip(whole.reason, trail.reason, strict=True):
        assert a & ~stale == b & ~stale
    with pytest.raises(ValueError):
        compute_labels(anchors, s, ts[-1], horizons=("10s",), freshness="trailing", max_age_ns=1)
    with pytest.raises(ValueError):
        compute_labels(anchors, s, ts[-1], horizons=("10s",), freshness="future")


# ----------------------------------------------------------------- R6


def test_hac_tstats_are_invariant_to_row_duplication():
    ts, x, y = _days_xy(n_days=3, per=500, seed=4)
    for k in (2, 5):
        tsk, xk, yk = (np.repeat(a, k) for a in (ts, x, y))
        assert pooled_slope_hac_tstat(tsk, xk, yk, lags=3) == pytest.approx(
            pooled_slope_hac_tstat(ts, x, y, lags=3), rel=1e-9
        )
        assert pooled_slope_hac_tstat(tsk, xk, yk, lags=3, day_ns=NS_DAY) == pytest.approx(
            pooled_slope_hac_tstat(ts, x, y, lags=3, day_ns=NS_DAY), rel=1e-9
        )
        b, c = bucket_ics_with_counts(ts, x, y)
        bk, ck = bucket_ics_with_counts(tsk, xk, yk)
        assert newey_west_tstat(bk, lags=3, weights=ck) == pytest.approx(
            newey_west_tstat(b, lags=3, weights=c), rel=1e-9
        )
        assert day_cluster_tstat(tsk, xk, yk) == pytest.approx(
            day_cluster_tstat(ts, x, y), rel=1e-9
        )


def test_no_lag_product_crosses_a_day_boundary():
    """With ``day_ns`` the statistic of several days equals the one built
    from per-day long-run variances: changing the ORDER of the days, or the
    overnight gap, cannot move it, because no last-bucket x first-bucket
    product is ever formed."""
    ts, x, y = _days_xy(n_days=4, per=300, seed=9)
    t = pooled_slope_hac_tstat(ts, x, y, lags=4, day_ns=NS_DAY)
    day = ts // NS_DAY
    # move day 3 in front of day 0 (different neighbours across midnight)
    perm_ts = np.where(day == 3, ts - 10 * NS_DAY, ts)
    assert pooled_slope_hac_tstat(perm_ts, x, y, lags=4, day_ns=NS_DAY) == pytest.approx(
        t, rel=1e-9
    )
    # build an adversarial pair: end-of-day and start-of-next-day moments
    # with the same sign inflate the old (adjacent) lrv but not the new one
    ts2, x2, y2 = _days_xy(n_days=2, per=300, seed=1)
    old = pooled_slope_hac_tstat(ts2, x2, y2, lags=8)
    new = pooled_slope_hac_tstat(ts2, x2, y2, lags=8, day_ns=NS_DAY)
    assert np.isfinite(old) and np.isfinite(new) and old != new


def test_reports_pin_their_dataset_versions(frames):
    r = _validate(frames, dataset_version="a" * 64)
    assert r["dataset_versions"] == {"dataset": "a" * 64}
    per_day = {d: f"{i:064x}" for i, d in enumerate(DAYS)}
    r2 = _validate(frames, dataset_version=per_day)
    assert r2["dataset_versions"] == per_day


# ------------------------------------------------- defaults unchanged


def test_v2_defaults_are_unchanged_and_v3_is_opt_in(frames, v2_report):
    assert "validity" not in v2_report and "dataset_versions" not in v2_report
    assert set(v2_report["methods"]) == {
        "block_rows",
        "cap_fills_at_l1",
        "capacity",
        "fold_diagnostics",
        "ic_rows",
        "impact_model",
        "position_policy",
        "recompute_probe",
        "significance",
        "stress_version",
        "tstat_threshold",
    }
    assert methods(METHODS_V2).validate_kwargs().keys() == {
        "ic_rows",
        "tstat_threshold",
        "stress_version",
        "significance",
        "capacity",
        "fold_diagnostics",
    }
    v3 = methods(METHODS_V3)
    assert v3.validate_kwargs()["gate_ic_source"] == "instrument_mean"
    assert v3.validate_kwargs()["split_mode"] == "day_aligned"
    assert v3.looks(4) == methods(METHODS_V2).looks(4) + VALIDITY_LOOKS == 88
    assert looks_per_validation(4) == 83
    r = _validate(frames, **v3.validate_kwargs())
    assert r["gate_ic_source"] == "instrument_mean" and "validity" in r
    assert r["validity"]["n_days"] == 4

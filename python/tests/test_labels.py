"""Event-time forward label tests: correctness, no-lookahead, two-pointer."""

from __future__ import annotations

import math
from bisect import bisect_right

import pytest

from conftest import GOLDEN_DIR, REPO_ROOT
from bruteforce_features import book_frames
from iap.core.codec import read_jsonl
from iap.labels.labels import (
    HORIZON_ORDER,
    HORIZONS_NS,
    LabelReason,
    MidSeries,
    compute_labels,
    max_sample_age,
)

NS = 1_000_000_000


def _series(samples, max_age=None):
    """MidSeries from (ts, mid, half_spread[, tradable]) tuples."""
    s = MidSeries()
    for row in samples:
        ts, mid, hs = row[0], row[1], row[2]
        tradable = row[3] if len(row) > 3 else True
        s.append(ts, mid, hs, tradable)
    return s


#: generous freshness bound for the small hand-built series below
BIG_AGE = 10_000 * NS


@pytest.fixture(scope="module")
def eq_series():
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    book_df, _ = book_frames(events)
    both = book_df.dropna(subset=["bp", "ap"])
    s = MidSeries()
    for row in both.itertuples():
        mid = (row.bp + row.ap) * 0.01 / 2
        hs = (row.ap - row.bp) * 0.01 / 2
        s.append(row.ts, mid, hs)
    return s, events[-1].exchange_ts


def test_pinned_horizons():
    assert list(HORIZON_ORDER) == [
        "10ms", "50ms", "100ms", "500ms", "1s", "5s", "10s", "30s",
        "1m", "5m", "15m"]
    assert HORIZONS_NS["10ms"] == 10_000_000
    assert HORIZONS_NS["15m"] == 900 * NS


def test_basic_mid_to_mid():
    s = _series([(0, 100.0, 0.5), (5 * NS, 101.0, 0.5), (12 * NS, 99.0, 0.5)])
    out = compute_labels([0], s, last_event_ts=12 * NS, horizons=("1s", "5s", "10s"))
    # 1s ahead: still 100 -> 0 return
    assert out["1s"].valid[0] and out["1s"].mid[0] == 0.0
    # 5s ahead: sample at exactly t+h counts (ts <= t+h)
    assert out["5s"].valid[0]
    assert math.isclose(out["5s"].mid[0], 0.01, rel_tol=1e-12)
    # 10s ahead: prevailing state is still the 5s sample
    assert math.isclose(out["10s"].mid[0], 0.01, rel_tol=1e-12)


def test_cost_adjusted_formula():
    s = _series([(0, 100.0, 0.5), (NS, 102.0, 0.4)])
    out = compute_labels([0], s, last_event_ts=NS, horizons=("1s",))
    lab = out["1s"]
    assert lab.valid[0]
    # buy at 100.5, sell at 101.6
    assert math.isclose(lab.cost[0], ((102.0 - 0.4) - (100.0 + 0.5)) / 100.0,
                        rel_tol=1e-12)
    assert math.isclose(lab.mid[0], 0.02, rel_tol=1e-12)
    # cost-adjusted is always <= mid-to-mid by the two half-spreads
    assert lab.cost[0] < lab.mid[0]


def test_horizon_past_stream_end_is_invalid():
    s = _series([(0, 100.0, 0.5), (2 * NS, 101.0, 0.5)])
    out = compute_labels([0, 2 * NS], s, last_event_ts=2 * NS,
                         horizons=("1s", "5s"))
    assert out["1s"].valid[0]          # 0 + 1s <= 2s observed
    assert not out["5s"].valid[0]      # 0 + 5s > last event -> invalid
    assert not out["1s"].valid[1]      # 2s + 1s > last event -> invalid
    assert math.isnan(out["5s"].mid[0])


def test_no_lookahead_truncation_invariance(eq_series):
    """Labels depend ONLY on events with ts <= t+h: truncating the series
    just after t+h leaves every label at anchor t unchanged."""
    series, last_ts = eq_series
    anchors = [series.ts[300]]
    t = anchors[0]
    for h in ("1s", "10s", "1m"):
        h_ns = HORIZONS_NS[h]
        full = compute_labels(anchors, series, last_ts, horizons=(h,))[h]
        cut = bisect_right(series.ts, t + h_ns)
        assert cut < len(series.ts), "probe anchor too close to stream end"
        trunc = MidSeries(series.ts[:cut], series.mid[:cut],
                          series.half_spread[:cut], series.tradable[:cut])
        age = max_sample_age(series)
        full = compute_labels(anchors, series, last_ts, horizons=(h,),
                              max_age_ns=age)[h]
        got = compute_labels(anchors, trunc, t + h_ns, horizons=(h,),
                             max_age_ns=age)[h]
        assert full.valid[0] == got.valid[0]
        assert full.reason[0] == got.reason[0]
        if full.valid[0]:
            assert full.mid[0] == got.mid[0]
            assert full.cost[0] == got.cost[0]


def test_shifted_series_breaks_alignment(eq_series):
    """Leakage canary: shifting mids one event forward (a classic off-by-one
    lookahead bug) must change the labels."""
    series, last_ts = eq_series
    # deduplicate to mid-change samples so every index shift moves the price
    ts, mid, hs = [], [], []
    for i in range(len(series.ts)):
        if not mid or series.mid[i] != mid[-1]:
            ts.append(series.ts[i])
            mid.append(series.mid[i])
            hs.append(series.half_spread[i])
    series = MidSeries(ts, mid, hs, [True] * len(ts))
    anchors = series.ts[20:-20:2]
    age = max_sample_age(series)
    good = compute_labels(anchors, series, last_ts, horizons=("30s",),
                          max_age_ns=age)["30s"]
    shifted = MidSeries(series.ts[:-1], series.mid[1:], series.half_spread[1:],
                        [True] * (len(ts) - 1))
    bad = compute_labels(anchors, shifted, last_ts, horizons=("30s",),
                         max_age_ns=age)["30s"]
    diffs = sum(
        1 for g, b, gv, bv in zip(good.mid, bad.mid, good.valid, bad.valid)
        if gv and bv and g != b)
    valid_both = sum(1 for gv, bv in zip(good.valid, bad.valid) if gv and bv)
    assert valid_both >= 20
    assert diffs / valid_both > 0.2, (
        "shifting the mid series barely changed labels — alignment test "
        "cannot detect leaks")


def test_two_pointer_matches_bisect_bruteforce(eq_series):
    """The O(n) sweep equals an independent per-anchor bisect implementation
    for every horizon (exact float equality)."""
    series, last_ts = eq_series
    anchors = series.ts[::7]
    age = max_sample_age(series)
    out = compute_labels(anchors, series, last_ts, max_age_ns=age)
    for h in HORIZON_ORDER:
        h_ns = HORIZONS_NS[h]
        lab = out[h]
        for i, t in enumerate(anchors):
            b = bisect_right(series.ts, t) - 1
            k = bisect_right(series.ts, t + h_ns) - 1
            valid = (b >= 0 and k >= 0 and last_ts >= t + h_ns
                     and t + h_ns - series.ts[k] <= age)
            assert lab.valid[i] == valid, (h, i)
            if valid:
                m0, hs0 = series.mid[b], series.half_spread[b]
                m1, hs1 = series.mid[k], series.half_spread[k]
                assert lab.mid[i] == m1 / m0 - 1.0
                assert lab.cost[i] == ((m1 - hs1) - (m0 + hs0)) / m0


def test_unsorted_anchors_raise(eq_series):
    series, last_ts = eq_series
    with pytest.raises(ValueError, match="non-decreasing"):
        compute_labels([10 * NS, 5 * NS], series, last_ts)


def test_unknown_horizon_raises(eq_series):
    series, last_ts = eq_series
    with pytest.raises(ValueError, match="unknown horizon"):
        compute_labels([0], series, last_ts, horizons=("2s",))


def test_series_rejects_decreasing_timestamps():
    s = MidSeries()
    s.append(10, 1.0, 0.1)
    with pytest.raises(ValueError, match="non-decreasing"):
        s.append(9, 1.0, 0.1)


# ---------------------------------------------------------------------------
# Tradability / freshness rules (API_FEATURES §6, round-3 RESEARCH)
# ---------------------------------------------------------------------------


def test_scenario_labels_invalid_across_halt():
    """LSE halt 09:03 -> reopen auction 09:08 with the mid 30 bp higher.

    Every anchor whose (t, t+h] window touches the halt must be invalid, so
    the pre-halt OFI rows are never credited with the reopen jump.
    """
    halt_ts = 300 * NS
    reopen_ts = 600 * NS
    samples = [(k * NS, 100.0, 0.5) for k in range(0, 300)]
    # halt: refreshes stop being tradable
    samples.append((halt_ts, float("nan"), float("nan"), False))
    samples.append((halt_ts + 60 * NS, float("nan"), float("nan"), False))
    # auction print then normal trading at the higher mid
    samples.append((reopen_ts, float("nan"), float("nan"), False))
    samples += [(reopen_ts + k * NS, 100.3, 0.5) for k in range(1, 200)]
    s = _series(samples)
    anchors = [t for t in s.ts if t <= halt_ts] + [reopen_ts + 100 * NS]
    out = compute_labels(anchors, s, last_event_ts=s.ts[-1],
                         horizons=("1m", "5m"), max_age_ns=BIG_AGE)
    for h in ("1m", "5m"):
        h_ns = HORIZONS_NS[h]
        lab = out[h]
        for i, t in enumerate(anchors):
            spans_halt = t < halt_ts <= t + h_ns
            if spans_halt:
                assert not lab.valid[i], (h, t)
                assert lab.reason[i] & LabelReason.BLACKOUT
            elif t == halt_ts:
                assert not lab.valid[i]
                assert lab.reason[i] & LabelReason.ANCHOR_NOT_TRADABLE
    # an anchor safely after the reopen is valid again
    assert out["1m"].valid[-1]
    assert out["1m"].reason[-1] == LabelReason.OK


def test_scenario_labels_invalid_when_prevailing_mid_is_stale():
    """Equity quotes stop at 16:05, a single close print lands at 20:00.

    A 15 m label anchored at 16:00 must be INVALID (its forward mid is the
    frozen 16:05 quote), while a 1 s label at 16:04 is valid.
    """
    end_quote = 3600 * NS          # "16:05"
    close_print = end_quote + 4 * 3600 * NS  # "20:00"
    samples = [(k * NS, 100.0 + k * 0.001, 0.5)
               for k in range(0, 3601)]
    samples.append((close_print, 101.0, 0.5))
    s = _series(samples)
    max_age = 5 * NS
    anchors = [end_quote - 900 * NS, end_quote - 300 * NS, end_quote - NS]
    out = compute_labels(anchors, s, last_event_ts=close_print,
                         horizons=("1s", "15m"), max_age_ns=max_age)
    # 15m from 16:00 lands in the dead zone -> stale forward mid
    assert not out["15m"].valid[1]
    assert out["15m"].reason[1] & LabelReason.FORWARD_STALE
    # exactly-15m-before-the-last-quote anchor is still fine
    assert out["15m"].valid[0]
    # a 1s label just before the last quote is valid
    assert out["1s"].valid[2]


def test_labels_invalid_across_a_stale_venue_gap():
    samples = [(k * NS, 100.0, 0.5) for k in range(0, 60)]
    samples.append((60 * NS, float("nan"), float("nan"), False))  # venue stale
    samples += [(k * NS, 100.5, 0.5) for k in range(180, 260)]     # recovered
    s = _series(samples)
    anchors = [30 * NS, 190 * NS]
    out = compute_labels(anchors, s, last_event_ts=s.ts[-1],
                         horizons=("1m",), max_age_ns=BIG_AGE)
    assert not out["1m"].valid[0]
    assert out["1m"].reason[0] & LabelReason.BLACKOUT
    assert out["1m"].valid[1]


def test_max_sample_age_scales_with_the_median_gap():
    dense = _series([(k * NS, 100.0, 0.5) for k in range(100)])
    sparse = _series([(k * 15 * NS, 100.0, 0.5) for k in range(100)])
    assert max_sample_age(dense) == 5 * NS       # floor
    assert max_sample_age(sparse) == 30 * NS     # 2 x median gap
    assert max_sample_age(MidSeries()) == 5 * NS


def test_reason_bits_describe_names():
    assert LabelReason.describe(0) == []
    mask = LabelReason.BLACKOUT | LabelReason.NOT_OBSERVED
    assert LabelReason.describe(mask) == ["not_observed", "blackout"]

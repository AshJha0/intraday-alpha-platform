"""v1.14 feature-engine correctness: same-timestamp coalescing (D-P1-1) and
side-less cross prints (D-P2-6).

The ITCH mapper turns one exchange message into several canonical events
that share an ``exchange_ts`` (an ``E`` into EXECUTE + TRADE, a ``U`` into
CANCEL + ADD).  Refreshing the merged book after each of them sampled books
that never existed (an ask level gone before the replacement arrived, a
trade measured against the book its own execution already moved).  The
coalesced engine applies the whole group, then refreshes, samples and emits
once.
"""

from __future__ import annotations

import pytest
from conftest import GOLDEN_DIR, REPO_ROOT
from iap.core.codec import read_jsonl
from iap.core.events import EventType, MarketEvent
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine, group_ends
from iap.features.extended import ExtendedFeatureEngine
from iap.features.native import compute_native
from iap.features.registry import feature_index

NS = 1_000_000_000
T0 = 1_787_578_200 * NS
IID, VENUE = 1, 1


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


class _Feed:
    def __init__(self) -> None:
        self.n = 0

    def ev(self, ts, et, side=0, price=0, qty=0, oid=0, tid=0, iid=IID) -> MarketEvent:
        self.n += 1
        return MarketEvent(self.n, iid, VENUE, ts, ts, self.n, int(et), side, price, qty, oid, tid)


def _replace_and_execute(feed: _Feed) -> list[MarketEvent]:
    """A two-sided book, a minute later: the ask is executed by a buyer
    (EXECUTE + TRADE) and a new ask is posted, all in ONE nanosecond."""
    t1 = T0 + 61 * NS
    return [
        feed.ev(T0, EventType.ADD, 0, 1000, 100, 1),
        feed.ev(T0, EventType.ADD, 1, 1002, 100, 2),
        feed.ev(T0 + NS, EventType.ADD, 0, 999, 50, 3),
        feed.ev(t1, EventType.EXECUTE, 1, 1002, 100, 2, 7),
        feed.ev(t1, EventType.TRADE, 0, 1002, 100, 0, 7),
        feed.ev(t1, EventType.ADD, 1, 1004, 100, 4),
    ]


def _run(events, contexts, coalesce: bool, cls=FeatureEngine, **kw):
    eng = cls(contexts, cadence_ns=0, **kw)
    ends = group_ends(events) if coalesce else [True] * len(events)
    rows = [eng.apply(ev, end) for ev, end in zip(events, ends, strict=True)]
    return eng, rows


def test_group_ends_follow_each_instrument_substream():
    feed = _Feed()
    evs = [
        feed.ev(T0, EventType.ADD, 0, 1000, 1, 1),
        feed.ev(T0, EventType.ADD, 0, 1000, 1, 2, iid=2),  # other instrument, same ts
        feed.ev(T0, EventType.ADD, 1, 1002, 1, 3),
        feed.ev(T0 + 1, EventType.ADD, 1, 1003, 1, 4, iid=2),
        feed.ev(T0 - 5, EventType.ADD, 1, 1003, 1, 5),  # a regression: its own group
    ]
    assert group_ends(evs) == [False, True, True, True, True]
    assert group_ends([]) == []


def test_coalesced_group_is_one_refresh_one_row_and_no_phantom_book(contexts):
    events = _replace_and_execute(_Feed())
    idx = feature_index()
    legacy, lrows = _run(events, contexts, coalesce=False)
    coal, crows = _run(events, contexts, coalesce=True)
    assert all(r is not None for r in lrows)
    assert [r is not None for r in crows] == [False, True, True, False, False, True]
    # legacy refreshed after the EXECUTE (ask side empty: a phantom one-sided
    # book) and again after the ADD; coalesced refreshes once per group
    assert legacy.states[IID].refresh_seq == 5 and coal.states[IID].refresh_seq == 3
    assert not lrows[3].validity[idx["mid_price_v1"]]  # the phantom state was emitted
    # the end-of-group state is the same book either way
    for name in ("mid_price_v1", "spread_ticks_v1", "depth_ask_l1_v1", "depth_bid_l10_v1"):
        assert crows[-1].values[idx[name]] == lrows[-1].values[idx[name]]
    # effective spread: legacy measured the trade against the phantom
    # one-sided book (no mid: not counted); coalesced against the PRE-group
    # mid 10.01 -> 2 * |10.02 - 10.01| / 10.01 * 1e4 bps
    eff = idx["effective_spread_bps_w1m_v1"]
    assert not lrows[-1].validity[eff]
    assert crows[-1].validity[eff]
    assert crows[-1].values[eff] == pytest.approx(2 * 0.01 / 10.01 * 1e4, rel=1e-12)
    # the trade flow is the same: one 100-share buy
    assert crows[-1].values[idx["signed_volume_w1m_v1"]] == 100.0


def test_apply_without_group_flag_is_the_legacy_engine(contexts):
    """Every event closing its own group IS the per-event engine (the MVP and
    the alpha golden frames still drive it this way)."""
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")[:600]
    a, ra = _run(events, contexts, coalesce=False)
    b = FeatureEngine(contexts, cadence_ns=0)
    rb = [b.apply(ev) for ev in events]
    assert [r.values for r in ra] == [r.values for r in rb]
    assert [r.validity for r in ra] == [r.validity for r in rb]


def test_same_timestamp_gap_and_snapshot_is_still_a_recovery(contexts):
    """A sequence gap repaired by a SNAPSHOT in the same nanosecond lost a
    message: the coalesced engine still resets rolling state (the venue was
    stale inside the group), exactly like the per-event engine."""
    events = read_jsonl(GOLDEN_DIR / "events_fx_anomalies.jsonl")
    legacy, _ = _run(events, contexts, coalesce=False)
    coal, _ = _run(events, contexts, coalesce=True)
    assert coal.states[101].recoveries == legacy.states[101].recoveries == 2


def test_native_frames_coalesce_by_default(contexts):
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    co = compute_native(events, contexts, 0, "python")
    legacy = compute_native(events, contexts, 0, "python", coalesce=False)
    ends = group_ends(events)
    assert len(legacy.timestamp) == len(events)
    assert len(co.timestamp) == sum(ends) < len(events)
    assert all(ends[i] for i in co.event_index)


def test_extended_engine_observes_every_event_of_a_group(contexts):
    """The volume clock counts every TRADE of a coalesced group."""
    events = _replace_and_execute(_Feed())
    eng, rows = _run(
        events, contexts, coalesce=True, cls=ExtendedFeatureEngine, sampling="volume", sample_n=50
    )
    assert rows[-1] is not None  # the 100-share trade inside the group reached the clock


def test_cross_print_counts_in_volume_not_in_signed_flow(contexts):
    """D-P2-6: a side-less cross print folds into traded volume,
    participation and the volume clock; signed volume, trade imbalance and
    trade counts never see it."""
    idx = feature_index()
    events = _replace_and_execute(_Feed())
    plain, prow = _run(events, contexts, coalesce=True)
    eng = FeatureEngine(contexts, cadence_ns=0)
    ends = group_ends(events)
    rows = []
    for i, (ev, end) in enumerate(zip(events, ends, strict=True)):
        if i == 3:  # the opening cross printed just before the minute-61 group
            assert eng.on_cross_print(IID, ev.exchange_ts - 1, 5_000)
        rows.append(eng.apply(ev, end))
    a, b = prow[-1], rows[-1]
    assert b.values[idx["traded_volume_w1m_v1"]] == a.values[idx["traded_volume_w1m_v1"]] + 5_000
    assert b.values[idx["participation_w1m_v1"]] > a.values[idx["participation_w1m_v1"]]
    for name in (
        "signed_volume_w1m_v1",
        "trade_imbalance_w1m_v1",
        "trade_count_w1m_v1",
        "effective_spread_bps_w1m_v1",
    ):
        assert b.values[idx[name]] == a.values[idx[name]], name
    assert eng.cross_prints_folded == 1
    # a print behind the instrument's clock, or of no size, is dropped + counted
    assert not eng.on_cross_print(IID, T0, 10)
    assert not eng.on_cross_print(IID, events[-1].exchange_ts, 0)
    assert eng.cross_prints_dropped == 2

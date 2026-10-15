"""``TRADE_BUST`` (event type 10; v1.17 A5, review X-11) through the
canonical validator, the book, the normaliser (IAP1 + Parquet) and the
registered feature engine (which never sees it)."""

from __future__ import annotations

import math

import pytest
from conftest import GOLDEN_DIR, REPO_ROOT, add, mkev
from iap.core.codec import read_iap1, read_jsonl, write_jsonl
from iap.core.events import EventType, MarketEvent, Side, validation_error
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine, group_ends
from iap.features.extended import ExtendedFeatureEngine
from iap.marketdata.normalize import normalize_run
from iap.orderbook.book import ApplyStatus, OrderBook

NS = 1_000_000_000


def bust(seq: int, trade_id: int, **kw) -> MarketEvent:
    return mkev(seq, EventType.TRADE_BUST, 0, 0, 0, 0, trade_id, **kw)


# ------------------------------------------------------------- validator


def test_validator_accepts_a_bust_naming_a_print_and_rejects_payload():
    assert EventType.TRADE_BUST == 10
    assert validation_error(bust(1, 77)) is None
    assert "trade_id required" in validation_error(bust(1, 0))
    assert "only trade_id" in validation_error(mkev(1, EventType.TRADE_BUST, 0, 5, 0, 0, 77))
    assert "only trade_id" in validation_error(mkev(1, EventType.TRADE_BUST, 0, 0, 10, 0, 77))
    assert "only trade_id" in validation_error(mkev(1, EventType.TRADE_BUST, 0, 0, 0, 3, 77))
    assert "unknown event_type" in validation_error(mkev(1, 250))
    assert "unknown event_type" in validation_error(mkev(1, 11))


# ------------------------------------------------------------------ book


def test_book_applies_a_bust_as_a_no_op_also_while_stale():
    b = OrderBook(1, 1)
    assert b.apply(add(1, Side.BID, 100, 10, 1)) == ApplyStatus.APPLIED
    before = b.state_summary()
    assert b.apply(bust(2, 77)) == ApplyStatus.APPLIED
    after = b.state_summary()
    assert after.pop("sequence") == 2 and before.pop("sequence") == 1  # consumed, like a HEARTBEAT
    assert after == before
    assert b.unknown_type_dropped == 0 and b.invalid_payload_dropped == 0
    # a gap marks the book stale (the ADD that reveals it is dropped under the
    # stale rule): the bust still applies, like a HEARTBEAT
    assert b.apply(add(5, Side.ASK, 102, 10, 2)) == ApplyStatus.DROPPED and b.stale
    assert b.dropped_while_stale == 1
    assert b.apply(bust(6, 78)) == ApplyStatus.APPLIED
    assert b.dropped_while_stale == 1
    assert b.apply(add(7, Side.ASK, 103, 10, 3)) == ApplyStatus.DROPPED
    assert b.dropped_while_stale == 2


# ------------------------------------------------------------ normaliser


def test_normaliser_carries_a_bust_into_iap1_and_parquet(tmp_path):
    evs = [
        add(1, Side.BID, 100, 10, 1, ts=10 * NS),
        mkev(2, EventType.TRADE, 1, 100, 5, 0, 501, ts=11 * NS),
        bust(3, 501, ts=12 * NS),
        add(4, Side.ASK, 102, 10, 2, ts=13 * NS),
    ]
    for i, e in enumerate(evs):
        e.event_id = i + 1
        e.receive_ts = e.exchange_ts
    raw = tmp_path / "raw"
    raw.mkdir()
    write_jsonl(raw / "eq_x.jsonl", evs)
    report = normalize_run(raw, tmp_path / "norm")
    assert report["per_stream"]["1:1"]["invalid"] == 0
    out = list(read_iap1(tmp_path / "norm" / "eq_x.normalized.iap1"))
    assert [e.event_type for e in out] == [1, 5, 10, 1]
    assert out[2].trade_id == 501 and out[2].qty == 0 and out[2].price_ticks == 0
    twin = list(read_jsonl(tmp_path / "norm" / "eq_x.normalized.jsonl"))
    assert [e.event_type for e in twin] == [1, 5, 10, 1]
    pq = pytest.importorskip("pyarrow.parquet")
    table = pq.read_table(tmp_path / "norm" / "events.parquet")
    assert table.column("event_type").to_pylist() == [1, 5, 10, 1]


# -------------------------------------------------------- feature engine


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


def _vectors(engine, events):
    ends = group_ends(events)
    out = []
    for ev, end in zip(events, ends, strict=True):
        v = engine.apply(ev, end)
        if v is not None:
            out.append(v)
    return out


def _same(a, b):
    assert len(a) == len(b)
    for x, y in zip(a, b, strict=True):
        assert x.timestamp == y.timestamp and x.validity == y.validity
        for p, q in zip(x.values, y.values, strict=True):
            assert p == q or (math.isnan(p) and math.isnan(q))


@pytest.mark.parametrize("engine_cls", [FeatureEngine, ExtendedFeatureEngine])
def test_registered_engine_never_sees_a_bust(contexts, engine_cls):
    """The golden MBO stream with busts spliced in (alone at their time,
    sharing a timestamp with the first / middle / last event of a group)
    yields exactly the vectors of the stream without them, and the busts
    are counted."""
    base = list(read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl"))[:600]
    with_busts: list[MarketEvent] = []
    n_busts = 0
    lone = next(
        i for i in range(40, len(base)) if base[i].exchange_ts > base[i - 1].exchange_ts + 1
    )
    for i, ev in enumerate(base):
        if i == lone:  # bust alone at its own time, strictly between two groups
            with_busts.append(mkev(0, EventType.TRADE_BUST, 0, 0, 0, 0, 5, ts=ev.exchange_ts - 1))
            n_busts += 1
        if i % 97 == 3:  # bust BEFORE an event at that event's time
            with_busts.append(mkev(0, EventType.TRADE_BUST, 0, 0, 0, 0, 7, ts=ev.exchange_ts))
            n_busts += 1
        with_busts.append(ev)
        if i % 89 == 5:  # bust AFTER an event at that event's time
            with_busts.append(mkev(0, EventType.TRADE_BUST, 0, 0, 0, 0, 9, ts=ev.exchange_ts))
            n_busts += 1
    plain = engine_cls(contexts, cadence_ns=0)
    busted = engine_cls(contexts, cadence_ns=0)
    want = _vectors(plain, base)
    got = _vectors(busted, with_busts)
    _same(want, got)
    assert busted.trade_busts_skipped == n_busts
    assert busted.events_processed == plain.events_processed
    assert busted.events_dropped == plain.events_dropped
    assert busted.vectors_emitted == plain.vectors_emitted
    assert len(busted.states) == len(plain.states)

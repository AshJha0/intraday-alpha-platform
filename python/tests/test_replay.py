"""Deterministic replay + checkpoint/restart equivalence tests."""

import json

import pytest

from iap.core.codec import read_jsonl
from iap.replay.replay import ReplayEngine


@pytest.fixture(scope="module")
def eq_events(golden_dir):
    return read_jsonl(golden_dir / "events_eq_mbo.jsonl")


@pytest.fixture(scope="module")
def fx_events(golden_dir):
    return read_jsonl(golden_dir / "events_fx_quote.jsonl")


def test_replay_deterministic(eq_events):
    a, b = ReplayEngine(), ReplayEngine()
    ra = a.run(eq_events)
    rb = b.run(eq_events)
    assert ra == rb
    assert a.checkpoint() == b.checkpoint()
    assert a.book_states() == b.book_states()


def test_replay_stats(eq_events):
    engine = ReplayEngine()
    stats = engine.run(eq_events)
    assert stats["events_processed"] == 2000
    assert stats["instruments"] == 1
    assert stats["time_regressions"] == 0


@pytest.mark.parametrize("split_at", [1, 100, 1234, 1999])
def test_checkpoint_restart_identical_continuation(eq_events, split_at):
    full = ReplayEngine()
    full.run(eq_events)

    part = ReplayEngine()
    part.run(eq_events[:split_at])
    cp = part.checkpoint()
    cp_json = json.loads(json.dumps(cp))  # survives serialization round-trip
    resumed = ReplayEngine.restore(cp_json)
    resumed.run(eq_events[split_at:])

    assert resumed.events_processed == full.events_processed
    assert resumed.checkpoint() == full.checkpoint()
    assert resumed.book_states() == full.book_states()


def test_periodic_checkpoints_are_replayable(eq_events):
    engine = ReplayEngine(checkpoint_every=500, keep_checkpoints=10)
    engine.run(eq_events)
    assert [cp["events_processed"] for cp in engine.checkpoints] == [500, 1000, 1500, 2000]
    # Resume from the 1000-event checkpoint and reach identical final state.
    resumed = ReplayEngine.restore(engine.checkpoints[1])
    resumed.run(eq_events[1000:])
    assert resumed.checkpoint() == engine.checkpoint()


def test_restore_carries_keep_checkpoints(eq_events):
    engine = ReplayEngine(checkpoint_every=100, keep_checkpoints=2)
    engine.run(eq_events[:600])
    assert [cp["events_processed"] for cp in engine.checkpoints] == [500, 600]
    cp = json.loads(json.dumps(engine.checkpoint()))
    resumed = ReplayEngine.restore(cp)
    assert resumed.keep_checkpoints == 2
    # the restored engine keeps enforcing the SAME retention policy
    resumed.run(eq_events[600:1100])
    assert [c["events_processed"] for c in resumed.checkpoints] == [1000, 1100]


def test_restore_round_trips_resting_order_arrival_order(eq_events):
    """resting_orders() must iterate identically after checkpoint/restore."""
    from iap.orderbook.book import OrderBook

    book = OrderBook(1, 1)
    for ev in eq_events[:800]:
        book.apply(ev)
    restored = OrderBook.restore(json.loads(json.dumps(book.checkpoint())))
    assert restored.resting_orders() == book.resting_orders()
    assert restored.resting_orders(0) == book.resting_orders(0)
    assert restored.resting_orders(1) == book.resting_orders(1)


def test_snapshot_emission(eq_events):
    seen = []
    engine = ReplayEngine(snapshot_every=250)
    engine.run(eq_events, on_snapshot=lambda i, snap: seen.append(i))
    assert seen == list(range(250, 2001, 250))
    assert len(engine.snapshots) == 8
    snap = engine.snapshots[-1]
    assert snap["index"] == 2000
    state = snap["instruments"]["1"]["1"]
    assert state["sequence"] == 2000
    assert state["best_bid_ticks"] < state["best_ask_ticks"]


def test_replay_multi_venue_fx(fx_events):
    engine = ReplayEngine()
    stats = engine.run(fx_events)
    assert stats["events_processed"] == 800
    cons = engine.instrument_book(101)
    assert sorted(cons.books) == [10, 11, 12]
    for vid, book in cons.books.items():
        assert not book.stale
        assert book.duplicates_dropped == 0 and book.gaps_detected == 0
    # Consolidated best must be at least as good as any single venue's.
    bb = cons.best_bid()
    assert bb is not None
    assert bb[0] == max(b.best_bid()[0] for b in cons.books.values() if b.best_bid())


def test_replay_golden_states_match_expected(eq_events, golden_dir):
    with open(golden_dir / "expected_book_states.json") as f:
        expected = json.load(f)
    engine = ReplayEngine(snapshot_every=100)
    engine.run(eq_events)
    for idx, exp_state in expected["states"].items():
        snap = next(s for s in engine.snapshots if s["index"] == int(idx))
        assert snap["instruments"]["1"]["1"] == exp_state


def test_invalid_cadence_rejected():
    with pytest.raises(ValueError):
        ReplayEngine(checkpoint_every=-1)

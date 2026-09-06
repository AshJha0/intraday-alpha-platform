"""Anomaly golden vectors + checkpoint interchange golden (golden group).

``tests/golden/events_{eq,fx}_anomalies.jsonl`` + ``expected_anomaly_states.json``
pin the book's sequencing / snapshot / quote / malformed-event semantics
across all four languages; ``expected_checkpoint_eq_1000.json`` pins the
cross-language checkpoint JSON shape.
"""

import json

import pytest

from bruteforce_book import BruteForceBook
from iap.core.codec import read_jsonl, write_jsonl
from iap.marketdata.golden_anomalies import anomaly_vector
from iap.orderbook.book import ConsolidatedBook, OrderBook
from iap.replay.replay import ReplayEngine

VECTORS = ["events_eq_anomalies.jsonl", "events_fx_anomalies.jsonl"]


@pytest.fixture(scope="module")
def expected(golden_dir):
    with open(golden_dir / "expected_anomaly_states.json") as f:
        return json.load(f)


def _venue_state(book: OrderBook) -> dict:
    return {
        "summary": book.state_summary(),
        "counters": book.counters(),
        "stale": book.stale,
        "status": book.status,
        "has_sequence": book.has_sequence,
        "sequence_epoch": book.sequence_epoch,
        "pending_count": book.pending_count(),
    }


@pytest.mark.parametrize("name", VECTORS)
def test_golden_anomaly_vector_reproducible_and_byte_stable(golden_dir, refdata, name, tmp_path):
    events = read_jsonl(golden_dir / name)
    assert anomaly_vector(refdata, name) == events
    out = tmp_path / name
    write_jsonl(out, events)
    assert out.read_bytes() == (golden_dir / name).read_bytes()


@pytest.mark.parametrize("name", VECTORS)
def test_golden_anomaly_states_reference_book(golden_dir, expected, name):
    events = read_jsonl(golden_dir / name)
    spec = expected["vectors"][name]
    assert spec["events"] == len(events)
    for run in spec["runs"]:
        cons = ConsolidatedBook(spec["instrument_id"], run["reorder_window"])
        states = run["states"]
        for i, ev in enumerate(events, start=1):
            cons.apply(ev)
            exp = states.get(str(i))
            if exp is None:
                continue
            got = {
                "venues": {str(vid): _venue_state(cons.books[vid]) for vid in sorted(cons.books)},
                "consolidated": cons.consolidated_summary(),
            }
            assert got == exp, f"{name} window={run['reorder_window']} index {i}"


@pytest.mark.parametrize("name", VECTORS)
def test_golden_anomaly_states_brute_force_independent(golden_dir, expected, name):
    """Independent naive rebuild agrees with the pinned window-0 states."""
    events = read_jsonl(golden_dir / name)
    spec = expected["vectors"][name]
    run = next(r for r in spec["runs"] if r["reorder_window"] == 0)
    brutes = {}
    for i, ev in enumerate(events, start=1):
        brute = brutes.setdefault(ev.venue_id, BruteForceBook())
        brute.apply(ev)
        exp = run["states"].get(str(i))
        if exp is None:
            continue
        for vid, vexp in exp["venues"].items():
            b = brutes[int(vid)]
            assert b.state_summary() == vexp["summary"], f"{name} index {i} venue {vid}"
            assert b.counters() == vexp["counters"], f"{name} index {i} venue {vid}"
            assert b.stale == vexp["stale"] and b.status == vexp["status"]


@pytest.mark.parametrize("name", VECTORS)
def test_golden_anomaly_accounting_invariant(golden_dir, name):
    """Every event ends in events_applied, exactly one drop counter, or the buffer."""
    events = read_jsonl(golden_dir / name)
    for window in (0, 4):
        cons = ConsolidatedBook(events[0].instrument_id, window)
        fed = {}
        for ev in events:
            cons.apply(ev)
            fed[ev.venue_id] = fed.get(ev.venue_id, 0) + 1
        for vid, book in cons.books.items():
            c = book.counters()
            drops = sum(c[k] for k in (
                "duplicates_dropped", "dropped_while_stale", "unknown_order_events",
                "invalid_side_dropped", "invalid_payload_dropped", "unknown_type_dropped",
                "modify_price_mismatch"))
            assert c["events_applied"] + drops + book.pending_count() == fed[vid]


@pytest.mark.parametrize("name", VECTORS)
def test_golden_anomaly_checkpoint_round_trip_mid_vector(golden_dir, name):
    """Checkpoint/restore at every pinned index continues identically (both windows)."""
    events = read_jsonl(golden_dir / name)
    for window in (0, 4):
        full = ConsolidatedBook(events[0].instrument_id, window)
        for ev in events:
            full.apply(ev)
        for split in (137, 500, 900, len(events) - 20):
            part = ConsolidatedBook(events[0].instrument_id, window)
            for ev in events[:split]:
                part.apply(ev)
            resumed = ConsolidatedBook.restore(json.loads(json.dumps(part.checkpoint())))
            for ev in events[split:]:
                resumed.apply(ev)
            assert resumed.checkpoint() == full.checkpoint(), f"{name} w={window} split={split}"


def test_golden_checkpoint_eq_1000_interchange(golden_dir):
    """The pinned engine checkpoint restores and replays to the golden final state."""
    eq = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    with open(golden_dir / "expected_checkpoint_eq_1000.json") as f:
        cp = json.load(f)
    with open(golden_dir / "expected_book_states.json") as f:
        expected = json.load(f)
    engine = ReplayEngine()
    engine.run(eq[:1000])
    assert engine.checkpoint() == cp  # Python still writes exactly this file
    resumed = ReplayEngine.restore(cp)
    resumed.run(eq[1000:])
    assert resumed.book_states()["instruments"]["1"]["1"] == expected["states"]["2000"]
    full = ReplayEngine()
    full.run(eq)
    assert resumed.checkpoint() == full.checkpoint()

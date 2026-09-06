#!/usr/bin/env python3
"""(Re)generate the anomaly golden vectors + expected states in tests/golden/.

Run from python/ with PYTHONPATH=src:

    PYTHONPATH=src python3 tools/make_golden_anomalies.py

Writes:
  events_eq_anomalies.jsonl, events_fx_anomalies.jsonl
      pinned anomaly vectors (iap.marketdata.golden_anomalies), arrival order
  expected_anomaly_states.json
      for every vector and for reorder_window in {0, 4}: at pinned indices
      (every 100 events + the last), per venue the exact state_summary(), ALL
      QC counters, stale/status/has_sequence/sequence_epoch/pending_count,
      and the consolidated (non-stale) view
  expected_checkpoint_eq_1000.json
      ReplayEngine.checkpoint() after the first 1000 events of
      events_eq_mbo.jsonl (cross-language checkpoint interchange golden)
  jsonl_reject_cases.txt
      JSONL lines every decoder must REJECT (one per line; '#' comments) plus
      the ACCEPT block (lines every decoder must accept)

Expected anomaly states are written only after the reference OrderBook and
the INDEPENDENT brute-force rebuild (tests/bruteforce_book.py) agree exactly
at every event for reorder_window == 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))
sys.path.insert(0, str(REPO / "python" / "tests"))

from bruteforce_book import BruteForceBook  # noqa: E402
from iap.core.codec import encode_jsonl_line, read_jsonl, write_jsonl  # noqa: E402
from iap.core.events import MarketEvent  # noqa: E402
from iap.marketdata.golden_anomalies import (  # noqa: E402
    GOLDEN_EQ_ANOMALY_SEED,
    GOLDEN_FX_ANOMALY_SEED,
    generate_golden_eq_anomalies,
    generate_golden_fx_anomalies,
)
from iap.orderbook.book import ConsolidatedBook, OrderBook  # noqa: E402
from iap.reference.refdata import ReferenceData  # noqa: E402
from iap.replay.replay import ReplayEngine  # noqa: E402

GOLDEN_DIR = REPO / "tests" / "golden"
REORDER_WINDOWS = (0, 4)
PIN_EVERY = 100
CHECKPOINT_AT = 1000


def venue_state(book: OrderBook) -> dict:
    return {
        "summary": book.state_summary(),
        "counters": book.counters(),
        "stale": book.stale,
        "status": book.status,
        "has_sequence": book.has_sequence,
        "sequence_epoch": book.sequence_epoch,
        "pending_count": book.pending_count(),
    }


def expected_states(events, reorder_window: int) -> dict:
    cons = ConsolidatedBook(events[0].instrument_id, reorder_window)
    states = {}
    n = len(events)
    for i, ev in enumerate(events, start=1):
        cons.apply(ev)
        if i % PIN_EVERY == 0 or i == n:
            states[str(i)] = {
                "venues": {str(vid): venue_state(cons.books[vid]) for vid in sorted(cons.books)},
                "consolidated": cons.consolidated_summary(),
            }
    return states


def cross_validate(events) -> None:
    """Reference book vs independent brute force at EVERY event (window 0)."""
    books, brutes = {}, {}
    for i, ev in enumerate(events, start=1):
        book = books.setdefault(ev.venue_id, OrderBook(ev.instrument_id, ev.venue_id))
        brute = brutes.setdefault(ev.venue_id, BruteForceBook())
        book.apply(ev)
        brute.apply(ev)
        if (book.state_summary() != brute.state_summary()
                or book.counters() != brute.counters()
                or book.stale != brute.stale or book.status != brute.status):
            raise SystemExit(
                f"VALIDATION FAILED at event {i} ({ev}):\n"
                f"reference:   {book.state_summary()} {book.counters()}\n"
                f"brute force: {brute.state_summary()} {brute.counters()}"
            )


def reject_cases() -> str:
    base = encode_jsonl_line(MarketEvent(1, 2, 3, 4, 5, 6, 1, 0, 2450, 100, 10, 0))
    rej = [
        ("# JSONL lines every decoder must REJECT (tests/golden, pinned; consumed by all four suites)", None),
        ("# blank lines and '#' comments are ignored; the ACCEPT block lists lines every decoder must accept", None),
        ("# out-of-range unsigned", base.replace('"event_id":1', '"event_id":18446744073709551616')),
        ("# u32 overflow", base.replace('"instrument_id":2', '"instrument_id":4294967296')),
        ("# u16 overflow", base.replace('"venue_id":3', '"venue_id":70000')),
        ("# u8 overflow", base.replace('"event_type":1', '"event_type":256')),
        ("# u8 overflow (side)", base.replace('"side":0', '"side":300')),
        ("# i64 overflow", base.replace('"qty":100', '"qty":9223372036854775808')),
        ("# i64 underflow", base.replace('"price_ticks":2450', '"price_ticks":-9223372036854775809')),
        ("# negative unsigned", base.replace('"order_id":10', '"order_id":-1')),
        ("# negative zero on unsigned", base.replace('"sequence":6', '"sequence":-0')),
        ("# float", base.replace('"qty":100', '"qty":100.0')),
        ("# exponent", base.replace('"qty":100', '"qty":1e2')),
        ("# leading plus", base.replace('"qty":100', '"qty":+100')),
        ("# leading zero", base.replace('"qty":100', '"qty":0100')),
        ("# bool", base.replace('"qty":100', '"qty":true')),
        ("# string", base.replace('"qty":100', '"qty":"100"')),
        ("# null", base.replace('"qty":100', '"qty":null')),
        ("# duplicate key", base.replace('"trade_id":0', '"trade_id":0,"trade_id":77')),
        ("# missing key", base.replace(',"trade_id":0', '')),
        ("# extra key", base.replace('"trade_id":0}', '"trade_id":0,"extra":1}')),
        ("# misordered keys", base.replace('"event_id":1,"instrument_id":2', '"instrument_id":2,"event_id":1')),
        ("# renamed key", base.replace('"qty"', '"quantity"')),
        ("# trailing content", base + " x"),
        ("# trailing comma", base.replace('"trade_id":0}', '"trade_id":0,}')),
        ("# not an object", "[1,2,3]"),
        ("# empty object", "{}"),
        ("# garbage", "not json at all {"),
        ("# hex", base.replace('"qty":100', '"qty":0x64')),
        ("# empty value", base.replace('"qty":100', '"qty":')),
        ("# unquoted key", base.replace('"qty":100', 'qty:100')),
    ]
    acc = [
        ("# ACCEPT", None),
        ("# canonical", base),
        ("# whitespace between tokens is tolerated", base.replace(",", ", ").replace(":", ": ")),
        ("# negative zero on signed", base.replace('"price_ticks":2450', '"price_ticks":-0')),
        ("# u64 max", base.replace('"order_id":10', '"order_id":18446744073709551615')),
        ("# i64 extremes", base.replace('"qty":100', '"qty":-9223372036854775808').replace('"price_ticks":2450', '"price_ticks":9223372036854775807')),
        ("# u8 max event_type/side (domain, not semantics)", base.replace('"event_type":1', '"event_type":255').replace('"side":0', '"side":255')),
    ]
    lines = []
    for comment, line in rej + acc:
        lines.append(comment)
        if line is not None:
            lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    ref = ReferenceData.load(REPO / "configs")
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    vectors = {
        "events_eq_anomalies.jsonl": (generate_golden_eq_anomalies(ref), GOLDEN_EQ_ANOMALY_SEED),
        "events_fx_anomalies.jsonl": (generate_golden_fx_anomalies(ref), GOLDEN_FX_ANOMALY_SEED),
    }
    out = {
        "description": (
            "Anomaly goldens (conventions section 4, API_CORE section 4). For each vector "
            "(applied through ConsolidatedBook(instrument, reorder_window) in FILE order) "
            "and each reorder_window: at pinned 1-based indices, per venue the exact "
            "state_summary(), every QC counter, stale/status/has_sequence/sequence_epoch/"
            "pending_count, and the consolidated non-stale view. Validated against an "
            "independent brute-force rebuild (window 0) before writing. Tolerance: exact."
        ),
        "x-version": 1,
        "vectors": {},
    }
    for name, (events, seed) in vectors.items():
        cross_validate(events)
        write_jsonl(GOLDEN_DIR / name, events)
        runs = []
        for w in REORDER_WINDOWS:
            runs.append({"reorder_window": w, "states": expected_states(events, w)})
        out["vectors"][name] = {
            "seed": seed,
            "events": len(events),
            "instrument_id": events[0].instrument_id,
            "venues": sorted({ev.venue_id for ev in events}),
            "runs": runs,
        }
        print(f"  {name}: {len(events)} events, validated (reference == brute force)")
    with open(GOLDEN_DIR / "expected_anomaly_states.json", "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")

    # Cross-language checkpoint interchange golden.
    eq = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    engine = ReplayEngine(checkpoint_every=0, snapshot_every=0, keep_checkpoints=4)
    engine.run(eq[:CHECKPOINT_AT])
    cp = engine.checkpoint()
    with open(GOLDEN_DIR / "expected_checkpoint_eq_1000.json", "w") as f:
        json.dump(cp, f, indent=2)
        f.write("\n")
    # Prove it: restore + replay the rest == replay everything.
    resumed = ReplayEngine.restore(json.loads(json.dumps(cp)))
    resumed.run(eq[CHECKPOINT_AT:])
    full = ReplayEngine()
    full.run(eq)
    if resumed.checkpoint() != full.checkpoint():
        raise SystemExit("checkpoint interchange golden is not replay-equivalent")

    (GOLDEN_DIR / "jsonl_reject_cases.txt").write_text(reject_cases(), encoding="utf-8")
    print(f"anomaly goldens written to {GOLDEN_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Golden generators of the v1.17 F7 C++ parity ports (part 1: the rules on main).

One module per ported rule (``r01_sor_sweep`` .. ``r10_markout_decomposition``),
driven by ``python/tools/make_golden_f7.py``. Every module exposes
``NAME`` (the golden file name under ``tests/golden``) and ``document()``
(the whole golden: ``inputs`` consumed by both the Python reference and the
C++ port, ``expected`` produced by the Python reference).

Parity contract (cpp/tests/test_f7_*.cpp): the C++ port rebuilds the
``expected`` block as a ``contracts::Value`` tree and compares its
canonical JSON bytes (``iap.contracts.versions.canonical_json`` =
``cpp/include/iap/contracts/canonical_json.hpp``) with the canonical bytes
of the golden's ``expected`` block: byte-for-byte, every integer exact,
every double the same IEEE value (Python ``repr`` = the C++ writer).
Blocks the Python derives from SciPy (Student-t readings of B6) are kept
apart under ``expected_tol`` and compared to a tolerance (the generator
says so in each file's description).

The committed bytes are the ones the CI runner's Linux Python (glibc libm,
numpy / OpenBLAS Haswell) writes, taken from the `f7-goldens-linux` artifact
of the python job: a Windows regeneration differs in the last bit of a few
libm results (pow, exp, acosh) and in the seeded hazard fit, which is why
`--check` on a laptop may report differences that CI does not.

Two kinds of vectors per rule: fixed vectors (small synthetic books and
scalar cases) and a sampled slice of the committed real session
``benchmarks/slices/itch50_20190130_AAPL`` (F4; provenance in its
SLICE.md): every ``SAMPLE_EVERY``-th event after the book-reconstruction
prefix, the rule evaluated on the consolidated book as the C++ book
builds it from the same IAP1 bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from iap.core.codec import read_iap1
from iap.core.events import EventType, MarketEvent
from iap.orderbook.book import ConsolidatedBook

REPO = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO / "tests" / "golden"
SLICE_DIR = REPO / "benchmarks" / "slices" / "itch50_20190130_AAPL"
SLICE_FILE = "normalized/eq_20190130.normalized.iap1"
SLICE_INSTRUMENT = 1
SLICE_VENUE = 101
SLICE_TICK = 0.01
SLICE_ADV = 14_451_444.0
SLICE_PREFIX = 1132  #: book-reconstruction ADDs before the window (SLICE.md)
SAMPLE_EVERY = 500  #: every 500th event (1-based ordinal) after the prefix
SEC = 1_000_000_000
X_VERSION = 1

SLICE_PROVENANCE = {
    "path": "benchmarks/slices/" + SLICE_DIR.name + "/" + SLICE_FILE,
    "instrument_id": SLICE_INSTRUMENT,
    "venue_id": SLICE_VENUE,
    "tick_size": SLICE_TICK,
    "adv": SLICE_ADV,
    "prefix_events": SLICE_PREFIX,
    "sample_every": SAMPLE_EVERY,
    "rule": (
        "a sample is taken after applying the k-th event (1-based) whenever "
        "k > prefix_events and k % sample_every == 0"
    ),
}


def load_slice() -> list[MarketEvent]:
    return read_iap1(SLICE_DIR / SLICE_FILE)


def sample_ordinals(n_events: int) -> list[int]:
    """1-based ordinals of the sampled events (``SLICE_PROVENANCE['rule']``)."""
    return [k for k in range(SLICE_PREFIX + 1, n_events + 1) if k % SAMPLE_EVERY == 0]


def is_sample(k: int) -> bool:
    return k > SLICE_PREFIX and k % SAMPLE_EVERY == 0


def event_row(ev: MarketEvent) -> list:
    """The 12 MarketEvent fields in ``MarketEvent::of`` order."""
    return [
        ev.event_id,
        ev.instrument_id,
        ev.venue_id,
        ev.exchange_ts,
        ev.receive_ts,
        ev.sequence,
        ev.event_type,
        ev.side,
        ev.price_ticks,
        ev.qty,
        ev.order_id,
        ev.trade_id,
    ]


def event_from_row(row: list) -> MarketEvent:
    return MarketEvent(
        event_id=row[0],
        instrument_id=row[1],
        venue_id=row[2],
        exchange_ts=row[3],
        receive_ts=row[4],
        sequence=row[5],
        event_type=row[6],
        side=row[7],
        price_ticks=row[8],
        qty=row[9],
        order_id=row[10],
        trade_id=row[11],
    )


class EventBuilder:
    """Synthetic multi-venue streams for the fixed vectors (per-venue sequences)."""

    def __init__(self, instrument_id: int, t0: int) -> None:
        self.instrument_id = instrument_id
        self.t0 = t0
        self.rows: list[list] = []
        self._seq: dict[int, int] = {}
        self._eid = 0

    def push(
        self,
        venue: int,
        dt_ns: int,
        etype: EventType,
        side: int,
        px: int,
        qty: int,
        oid: int = 0,
        tid: int = 0,
        *,
        skip_sequence: int = 0,
    ) -> None:
        self._eid += 1
        seq = self._seq.get(venue, 0) + 1 + skip_sequence
        self._seq[venue] = seq
        ts = self.t0 + dt_ns
        self.rows.append(
            [
                self._eid,
                self.instrument_id,
                venue,
                ts,
                ts,
                seq,
                int(etype),
                side,
                px,
                qty,
                oid,
                tid,
            ]
        )

    def add(self, venue, dt_ns, side, px, qty, oid, **kw):
        self.push(venue, dt_ns, EventType.ADD, side, px, qty, oid, **kw)

    def status(self, venue, dt_ns, status):
        self.push(venue, dt_ns, EventType.STATUS, 0, 0, int(status), 0)

    def trade(self, venue, dt_ns, side, px, qty, tid):
        self.push(venue, dt_ns, EventType.TRADE, side, px, qty, 0, tid)

    def events(self) -> list[MarketEvent]:
        return [event_from_row(r) for r in self.rows]


def build_book(instrument_id: int, events) -> ConsolidatedBook:
    book = ConsolidatedBook(instrument_id)
    for ev in events:
        book.apply(ev)
    return book


def venue_spec_row(v) -> dict:
    """``VenueSpec`` as the C++ ``VenueSpec`` fields (regulatory opt-ins at 0)."""
    return {
        "venue_id": v.venue_id,
        "name": v.name,
        "is_fx": v.is_fx,
        "taker_fee_per_share": v.taker_fee_per_share,
        "maker_rebate_per_share": v.maker_rebate_per_share,
        "commission_per_million": v.commission_per_million,
        "latency_mean_ns": v.latency_mean_ns,
        "latency_jitter_ns": v.latency_jitter_ns,
    }


def instrument_row(ins) -> dict:
    return {
        "instrument_id": ins.instrument_id,
        "tick_size": ins.tick_size,
        "qty_unit": ins.qty_unit,
        "adv": ins.adv,
        "quote_ccy": ins.quote_ccy,
    }


def dumps(doc: dict) -> bytes:
    """The committed bytes of a golden (readable layout; the C++ test
    re-canonicalises the ``expected`` block, so the layout is free)."""
    return (json.dumps(doc, indent=1, allow_nan=False) + "\n").encode("utf-8")


def base_document(description: str, generator: str, tolerance: float | None = None) -> dict:
    doc = {
        "x-version": X_VERSION,
        "description": description,
        "generator": generator,
        "parity": (
            "canonical JSON bytes of `expected` (sorted keys, no whitespace, Python float "
            "repr) must equal the C++ port's canonical bytes; `expected_tol` (when present) "
            "is compared to `tolerance`"
        ),
    }
    if tolerance is not None:
        doc["tolerance"] = tolerance
    return doc

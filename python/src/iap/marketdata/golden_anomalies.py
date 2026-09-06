"""Pinned anomaly golden vectors (cross-language book/replay contract).

``generate_golden_eq_anomalies`` / ``generate_golden_fx_anomalies`` build the
vectors behind ``tests/golden/events_eq_anomalies.jsonl`` and
``events_fx_anomalies.jsonl``: a seeded generator session with the QC anomaly
classes ON (sequence gaps with SNAPSHOT recovery bursts, exact duplicates,
out-of-order arrivals, invalid events, receive_ts violations, a HALT with a
re-opening auction) followed by SCRIPTED scenario blocks that exercise every
pinned malformed-event and sequencing rule of PLATFORM_CONVENTIONS.md §4:

EQ (SYN.EQ.001 @ XV1 + XV2; XV2's stream numbers from sequence 0):
  payload-domain drops (EXECUTE qty 0 / negative, ADD price 0, ADD order_id
  0, reserved ids, STATUS with a bogus code, unknown event types), MODIFY
  price mismatch, duplicate ADD id, i64 overflow guards (ADD qty i64::MAX
  twice at one level, TRADE qty i64::MAX twice), ids >= 2^63, an interrupted
  SNAPSHOT burst restarted without a gap, an id-less (L2) SNAPSHOT burst with
  a repeated explicit id inside it, a gap landing on the first record of a
  burst, a duplicate and HEARTBEAT/TRADE interleaved inside a burst, STATUS
  transitions while stale, ADD after CLOSE, a crossed AUCTION call phase
  uncrossed by EXECUTEs, a venue sequence reset (burst restarting at
  sequence 1) with continuing flow, and a late retransmission block
  (sequences 1,2,3,6,4,5,7 pattern) whose outcome depends on the book's
  reorder_window.
FX (EUR/USD @ LP1/LP2/PRI): id-less QUOTEs (order_id 0) on every venue, an
  explicit QUOTE id reused across sides, a QUOTE replacing the resting order
  with the same id, QUOTE qty 0 / price 0, an id-less SNAPSHOT burst, a gap
  on LP2 that stays unrecovered for a while (consolidated view excludes the
  venue) and a recovery burst later.

Vectors are written in ARRIVAL order with event_id 1..N (they are not
normalized streams — duplicates and late arrivals are part of the test).
Expected states are produced by ``python/tools/make_golden_anomalies.py``
after cross-validation against ``tests/bruteforce_book.py``.
"""

from __future__ import annotations

from typing import List, Optional

from iap.core.events import (
    I64_MAX,
    SYNTHETIC_ID_BASE,
    EventType,
    MarketEvent,
    SessionStatus,
    Side,
)
from iap.core.rng import SplitMix64
from iap.marketdata.generator import (
    NS,
    MarketDataGenerator,
    _EffPrice,
    _Stream,
)
from iap.reference.refdata import ReferenceData

#: Pinned seeds (tests/golden depends on them).
GOLDEN_EQ_ANOMALY_SEED = 1717171717
GOLDEN_FX_ANOMALY_SEED = 2929292929

_ANOMALIES = {
    "gap_prob": 0.01,
    "gap_max_events": 3,
    "dup_prob": 0.01,
    "ooo_prob": 0.006,
    "invalid_prob": 0.004,
    "ts_violation_prob": 0.004,
}


class _Script:
    """Scripted emitter on one stream (advances time by 1 ms per event)."""

    def __init__(self, gen: MarketDataGenerator, stream: _Stream,
                 rng: SplitMix64, t: int) -> None:
        self.gen = gen
        self.stream = stream
        self.rng = rng
        self.t = t
        self.out: List[MarketEvent] = []

    def emit(self, event_type, side=0, price=0, qty=0, order_id=0,
             trade_id=0) -> MarketEvent:
        self.t += 1_000_000
        return self.gen._emit(self.stream, self.out, self.rng, self.t, event_type,
                              side=side, price=price, qty=qty, order_id=order_id,
                              trade_id=trade_id)

    def raw(self, sequence: int, event_type: int, side=0, price=0, qty=0,
            order_id=0, trade_id=0) -> MarketEvent:
        """Emit an event with an explicit sequence (not applied to the internal book)."""
        self.t += 1_000_000
        venue = self.stream.venue
        receive = self.t + venue.latency_mean_ns
        if receive <= self.stream.last_receive:
            receive = self.stream.last_receive + 1
        self.stream.last_receive = receive
        ev = MarketEvent(0, self.stream.inst.instrument_id, venue.venue_id, self.t,
                         receive, sequence, int(event_type), int(side), price, qty,
                         order_id, trade_id)
        self.out.append(ev)
        return ev

    def dup(self, ev: MarketEvent) -> None:
        """Append an exact duplicate (same sequence) arriving 1 us later."""
        d = MarketEvent(**ev.to_dict())
        d.receive_ts = ev.receive_ts + 1_000
        self.out.append(d)

    def snapshot_burst(self, records, zero_ids: bool = False) -> None:
        """records: [(side, price, qty, oid)] -> full burst with countdown."""
        n = len(records)
        for i, (side, price, qty, oid) in enumerate(records):
            self.emit(EventType.SNAPSHOT, side=side, price=price, qty=qty,
                      order_id=0 if zero_ids else oid, trade_id=n - 1 - i)

    def book_records(self):
        """Current internal book as SNAPSHOT records (bids then asks)."""
        recs = []
        for side in (int(Side.BID), int(Side.ASK)):
            for level in self.stream.book._sorted_levels(side):
                for oid, q in level.orders.items():
                    recs.append((side, level.price, q,
                                 oid if oid < SYNTHETIC_ID_BASE else 0))
        return recs

    def head(self, side: int):
        level = self.stream.book._best_level(side)
        if level is None:
            return None
        oid, qty = next(iter(level.orders.items()))
        return oid, level.price, qty


def _eq_scripted(script: _Script, venue_index: int) -> None:
    """Scenario blocks for one equity stream (see module docstring)."""
    st = script.stream
    book = st.book
    lot = st.inst.lot_size
    BID, ASK = int(Side.BID), int(Side.ASK)

    # Make sure both sides exist (a complete burst also clears any staleness
    # left by the natural anomalies, so the blocks start from a fresh book).
    recs = script.book_records()
    if not recs:
        recs = [(BID, 2440, lot, st.new_order_id()), (ASK, 2450, lot, st.new_order_id())]
    script.snapshot_burst(recs)
    if book.best_bid() is None:
        script.emit(EventType.ADD, side=BID, price=book.best_ask()[0] - 2, qty=lot,
                    order_id=st.new_order_id())
    if book.best_ask() is None:
        script.emit(EventType.ADD, side=ASK, price=book.best_bid()[0] + 2, qty=lot,
                    order_id=st.new_order_id())
    bb = book.best_bid()[0]
    ba = book.best_ask()[0]

    # --- payload-domain malformed events (sequence consumed, state unchanged)
    head_bid = script.head(BID)
    script.emit(EventType.EXECUTE, side=BID, price=head_bid[1], qty=0, order_id=head_bid[0])
    script.emit(EventType.EXECUTE, side=BID, price=head_bid[1], qty=-50, order_id=head_bid[0])
    script.emit(EventType.ADD, side=ASK, price=0, qty=lot, order_id=st.new_order_id())
    script.emit(EventType.ADD, side=ASK, price=ba + 1, qty=-lot, order_id=st.new_order_id())
    script.emit(EventType.ADD, side=ASK, price=ba + 1, qty=lot, order_id=0)
    script.emit(EventType.ADD, side=ASK, price=ba + 1, qty=lot,
                order_id=SYNTHETIC_ID_BASE | 7)
    script.emit(EventType.TRADE, side=BID, price=ba, qty=0, trade_id=st.new_trade_id())
    script.emit(EventType.QUOTE, side=BID, price=bb, qty=0, order_id=st.new_order_id())
    script.emit(EventType.STATUS, qty=9)
    script.emit(0, side=BID, price=bb, qty=lot, order_id=st.new_order_id())   # unknown type
    script.emit(10, side=BID, price=bb, qty=lot, order_id=st.new_order_id())  # unknown type
    script.emit(EventType.ADD, side=9, price=bb, qty=lot, order_id=st.new_order_id())

    # --- MODIFY price mismatch, duplicate ADD id, unknown ids
    hb = script.head(BID)
    script.emit(EventType.MODIFY, side=BID, price=hb[1] + 5, qty=hb[2] + lot, order_id=hb[0])
    script.emit(EventType.MODIFY, side=BID, price=0, qty=hb[2] + lot, order_id=hb[0])  # ok: price 0 = unchanged
    script.emit(EventType.ADD, side=BID, price=bb - 1, qty=lot, order_id=hb[0])  # duplicate id
    script.emit(EventType.CANCEL, side=BID, price=bb, qty=0, order_id=st.next_order + 500_000)

    # --- i64 overflow guards
    big = st.new_order_id()
    script.emit(EventType.ADD, side=BID, price=bb - 3, qty=I64_MAX, order_id=big)
    script.emit(EventType.ADD, side=BID, price=bb - 3, qty=I64_MAX, order_id=st.new_order_id())
    script.emit(EventType.MODIFY, side=BID, price=bb - 3, qty=I64_MAX, order_id=big)  # no-op decrease
    script.emit(EventType.CANCEL, side=BID, price=bb - 3, qty=0, order_id=big)
    flow = book.trade_flow
    if flow < 0:
        script.emit(EventType.TRADE, side=BID, price=ba, qty=-flow, trade_id=st.new_trade_id())
    elif flow > 0:
        script.emit(EventType.TRADE, side=ASK, price=bb, qty=flow, trade_id=st.new_trade_id())
    # trade_flow is now 0: +i64::MAX fits, one more unit would overflow (dropped).
    script.emit(EventType.TRADE, side=BID, price=ba, qty=I64_MAX, trade_id=st.new_trade_id())
    script.emit(EventType.TRADE, side=BID, price=ba, qty=1, trade_id=st.new_trade_id())
    script.emit(EventType.TRADE, side=ASK, price=bb, qty=I64_MAX, trade_id=st.new_trade_id())
    script.emit(EventType.TRADE, side=ASK, price=bb, qty=7, trade_id=st.new_trade_id())

    # --- ids >= 2^63 through the full MBO lifecycle
    huge = (1 << 63) + 11 + venue_index
    script.emit(EventType.ADD, side=ASK, price=ba + 2, qty=3 * lot, order_id=huge)
    script.emit(EventType.MODIFY, side=ASK, price=ba + 2, qty=5 * lot, order_id=huge)
    script.emit(EventType.EXECUTE, side=ASK, price=ba + 2, qty=lot, order_id=huge)
    script.emit(EventType.CANCEL, side=ASK, price=ba + 2, qty=0, order_id=huge)

    # --- interrupted SNAPSHOT burst restarted without a gap
    script.emit(EventType.SNAPSHOT, side=BID, price=bb, qty=lot, order_id=st.new_order_id(), trade_id=3)
    script.emit(EventType.SNAPSHOT, side=BID, price=bb - 1, qty=lot, order_id=st.new_order_id(), trade_id=2)
    recs = [(BID, bb, 2 * lot, st.new_order_id()), (BID, bb - 1, lot, st.new_order_id()),
            (ASK, ba, 2 * lot, st.new_order_id()), (ASK, ba + 1, lot, st.new_order_id())]
    script.snapshot_burst(recs)  # trade_id 3.. restarts the burst

    # --- id-less (L2) burst with a repeated explicit id inside it
    dup_id = st.new_order_id()
    n = 5
    script.emit(EventType.SNAPSHOT, side=BID, price=bb, qty=lot, order_id=0, trade_id=n - 1)
    script.emit(EventType.SNAPSHOT, side=BID, price=bb - 1, qty=2 * lot, order_id=0, trade_id=n - 2)
    script.emit(EventType.SNAPSHOT, side=ASK, price=ba, qty=lot, order_id=dup_id, trade_id=n - 3)
    script.emit(EventType.SNAPSHOT, side=ASK, price=ba + 1, qty=lot, order_id=dup_id, trade_id=n - 4)
    script.emit(EventType.SNAPSHOT, side=ASK, price=ba + 2, qty=3 * lot, order_id=0, trade_id=0)
    # MBO events on the synthetic ids: EXECUTE against the synthetic bid head.
    hb = script.head(BID)
    script.emit(EventType.EXECUTE, side=BID, price=hb[1], qty=lot // 2, order_id=hb[0])

    # --- gap landing on the first record of a burst (recovers, not broken)
    st.seq += 2
    recs = script.book_records()
    script.snapshot_burst(recs)

    # --- duplicate + HEARTBEAT/TRADE interleaved inside a burst
    recs = script.book_records()
    n = len(recs)
    first = script.emit(EventType.SNAPSHOT, side=recs[0][0], price=recs[0][1],
                        qty=recs[0][2], order_id=recs[0][3], trade_id=n - 1)
    script.dup(first)
    script.emit(EventType.HEARTBEAT)
    script.emit(EventType.TRADE, side=ASK, price=bb, qty=lot, trade_id=st.new_trade_id())
    for i, (side, price, qty, oid) in enumerate(recs[1:], start=1):
        script.emit(EventType.SNAPSHOT, side=side, price=price, qty=qty, order_id=oid,
                    trade_id=n - 1 - i)

    # --- STATUS transitions while stale, ADD after CLOSE
    st.seq += 1  # gap -> stale
    script.emit(EventType.STATUS, qty=int(SessionStatus.HALT))
    script.emit(EventType.ADD, side=BID, price=bb, qty=lot, order_id=st.new_order_id())  # dropped (stale)
    script.emit(EventType.STATUS, qty=int(SessionStatus.AUCTION))
    script.snapshot_burst(script.book_records())  # recovers; status stays AUCTION
    script.emit(EventType.STATUS, qty=int(SessionStatus.CLOSE))
    script.emit(EventType.ADD, side=BID, price=bb - 2, qty=lot, order_id=st.new_order_id())  # applies

    # --- AUCTION call phase: crossed book, uncrossed by EXECUTEs
    script.emit(EventType.STATUS, qty=int(SessionStatus.AUCTION))
    bb = book.best_bid()[0]
    ba = book.best_ask()[0]
    cross_bid = st.new_order_id()
    cross_ask = st.new_order_id()
    script.emit(EventType.ADD, side=BID, price=ba + 1, qty=lot, order_id=cross_bid)  # rests (crossed)
    script.emit(EventType.ADD, side=ASK, price=bb - 1, qty=lot, order_id=cross_ask)  # rests (crossed)
    script.emit(EventType.EXECUTE, side=BID, price=ba + 1, qty=lot, order_id=cross_bid)
    script.emit(EventType.EXECUTE, side=ASK, price=bb - 1, qty=lot, order_id=cross_ask)
    script.emit(EventType.TRADE, side=BID, price=ba, qty=lot, trade_id=st.new_trade_id())
    script.emit(EventType.STATUS, qty=int(SessionStatus.TRADING))
    # the same crossing ADD during TRADING executes immediately
    script.emit(EventType.ADD, side=BID, price=ba, qty=lot // 2, order_id=st.new_order_id())

    # --- venue sequence reset: burst restarting at sequence 1, then flow
    recs = script.book_records()
    st.seq = 0
    script.snapshot_burst(recs)
    script.emit(EventType.ADD, side=BID, price=book.best_bid()[0], qty=lot, order_id=st.new_order_id())
    script.emit(EventType.ADD, side=ASK, price=book.best_ask()[0], qty=lot, order_id=st.new_order_id())

    # --- late retransmission block: sequences s+1, s+2, s+3, s+6, s+4, s+5, s+7
    base = st.seq
    bb = book.best_bid()[0]
    ids = [st.new_order_id() for _ in range(7)]
    order = [1, 2, 3, 6, 4, 5, 7]
    for k in order:
        script.raw(base + k, EventType.ADD, side=BID, price=bb - k, qty=lot, order_id=ids[k - 1])
    st.seq = base + 7
    # Apply the block to the internal book in sequence order so the script
    # can keep emitting consistent events afterwards (the golden expectation
    # is computed by the reference book from the file, not from here).
    for k in range(1, 8):
        ev = next(e for e in script.out[-7:] if e.sequence == base + k)
        book.apply(ev)
    script.emit(EventType.CANCEL, side=BID, price=bb - 7, qty=0, order_id=ids[6])
    script.emit(EventType.HEARTBEAT)


def _fx_scripted(scripts: List[_Script]) -> None:
    """Scenario blocks for the FX pair across LP1/LP2/PRI (venue order)."""
    BID, ASK = int(Side.BID), int(Side.ASK)
    lp1, lp2, pri = scripts
    mid = 108650

    # id-less QUOTE stream on every venue (one synthetic order per side).
    for k in range(6):
        for sc, spread in ((lp1, 3), (lp2, 5), (pri, 2)):
            sc.emit(EventType.QUOTE, side=BID, price=mid - spread + k, qty=5 + k, order_id=0)
            sc.emit(EventType.QUOTE, side=ASK, price=mid + spread + k, qty=7 + k, order_id=0)
    # explicit id reused across sides -> the ASK quote is dropped
    shared = lp1.stream.new_order_id()
    lp1.emit(EventType.QUOTE, side=BID, price=mid, qty=9, order_id=shared)
    lp1.emit(EventType.QUOTE, side=ASK, price=mid + 3, qty=9, order_id=shared)
    # same id on the same side replaces the resting quote
    lp1.emit(EventType.QUOTE, side=BID, price=mid - 1, qty=4, order_id=shared)
    # QUOTE qty 0 / price 0 -> invalid payload
    pri.emit(EventType.QUOTE, side=ASK, price=mid + 1, qty=0, order_id=pri.stream.new_order_id())
    pri.emit(EventType.QUOTE, side=ASK, price=0, qty=3, order_id=pri.stream.new_order_id())
    # id-less SNAPSHOT burst on PRI (L2 snapshot)
    pri.snapshot_burst([(BID, mid - 2, 10, 0), (BID, mid - 3, 20, 0),
                        (ASK, mid + 2, 12, 0), (ASK, mid + 4, 30, 0)], zero_ids=True)
    # gap on LP2: stays stale (excluded from the consolidated view) ...
    lp2.stream.seq += 2
    lp2.emit(EventType.QUOTE, side=BID, price=mid + 5, qty=50, order_id=0)  # dropped while stale
    lp2.emit(EventType.TRADE, side=BID, price=mid + 5, qty=3, trade_id=lp2.stream.new_trade_id())
    for k in range(4):
        lp1.emit(EventType.QUOTE, side=BID, price=mid - 3 + k, qty=6, order_id=0)
        pri.emit(EventType.QUOTE, side=ASK, price=mid + 2 + k, qty=8, order_id=0)
    # ... until a recovery burst re-admits it
    lp2.snapshot_burst([(BID, mid - 4, 11, 0), (ASK, mid + 6, 13, 0)], zero_ids=True)
    lp2.emit(EventType.QUOTE, side=ASK, price=mid + 5, qty=14, order_id=0)


def _merge_arrival(parts: List[List[MarketEvent]]) -> List[MarketEvent]:
    """Concatenate scripted blocks (already in per-stream time order) by receive_ts."""
    out = [ev for part in parts for ev in part]
    out.sort(key=lambda e: (e.receive_ts, e.venue_id, e.sequence, e.event_type))
    return out


def generate_golden_eq_anomalies(
    refdata: ReferenceData, seed: int = GOLDEN_EQ_ANOMALY_SEED, natural: int = 600
) -> List[MarketEvent]:
    """SYN.EQ.001 @ XV1+XV2 anomaly vector (arrival order, event_id 1..N)."""
    gen = MarketDataGenerator(refdata, {"seed": seed, "anomalies": _ANOMALIES,
                                        "equities": {"halt": {"reopen_auction": True,
                                                              "reopen_call_s": 30,
                                                              "duration_s": 120}}})
    inst = refdata.instrument("SYN.EQ.001")
    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", date)
    price = _EffPrice(inst, gen._rng("eqmid", inst.instrument_id, 0))
    price.new_session(open_ns, close_ns, gen.cfg["equities"]["vol_regimes"])
    halt_at = open_ns + 20 * NS
    halt_window = (halt_at, halt_at + gen.cfg["equities"]["halt"]["duration_s"] * NS)
    streams = []
    natural_events: List[MarketEvent] = []
    for vi, vname in enumerate(("XV1", "XV2")):
        stream = _Stream(inst, refdata.venue(vname))
        if vi == 1:
            stream.seq = -1  # XV2 numbers its stream from sequence 0
        rng = SplitMix64(seed + vi)
        natural_events.extend(
            gen._eq_session_stream(stream, price, rng, open_ns, close_ns, slots=natural + 200,
                                   halt_window=halt_window, anomalies=_ANOMALIES,
                                   max_events=natural)
        )
        streams.append(stream)
    natural_events = gen._inject_file_anomalies(natural_events, gen._rng("anom-golden", 0, 0))
    t0 = max(max(ev.exchange_ts, ev.receive_ts) for ev in natural_events) + NS
    scripted = []
    for vi, stream in enumerate(streams):
        script = _Script(gen, stream, SplitMix64(seed + 100 + vi), t0 + vi * 100_000)
        _eq_scripted(script, vi)
        scripted.append(script.out)
    events = natural_events + _merge_arrival(scripted)
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    return events


def generate_golden_fx_anomalies(
    refdata: ReferenceData, seed: int = GOLDEN_FX_ANOMALY_SEED, natural: int = 500
) -> List[MarketEvent]:
    """EUR/USD @ LP1/LP2/PRI anomaly vector (arrival order, event_id 1..N)."""
    gen = MarketDataGenerator(refdata, {"seed": seed, "anomalies": _ANOMALIES})
    inst = refdata.instrument("EUR/USD")
    streams = [_Stream(inst, refdata.venue(v)) for v in ("LP1", "LP2", "PRI")]
    rng = SplitMix64(seed)
    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("FX", date)
    natural_events = gen._fx_pair_session(inst, streams, rng, open_ns, close_ns,
                                          slots=natural + 200, anomalies=_ANOMALIES,
                                          max_events=natural)
    natural_events = gen._inject_file_anomalies(natural_events, gen._rng("anom-golden", 1, 0))
    t0 = max(max(ev.exchange_ts, ev.receive_ts) for ev in natural_events) + NS
    scripts = [_Script(gen, s, SplitMix64(seed + 100 + i), t0 + i * 100_000)
               for i, s in enumerate(streams)]
    _fx_scripted(scripts)
    events = natural_events + _merge_arrival([s.out for s in scripts])
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    return events


def anomaly_vector(refdata: ReferenceData, name: str) -> Optional[List[MarketEvent]]:
    """Regenerate a pinned anomaly vector by golden file name."""
    if name == "events_eq_anomalies.jsonl":
        return generate_golden_eq_anomalies(refdata)
    if name == "events_fx_anomalies.jsonl":
        return generate_golden_fx_anomalies(refdata)
    return None

"""Seeded synthetic market-data generator (equities MBO + FX quotes).

Design (all randomness is SplitMix64 — conventions section 3; identical seed =>
bit-identical output files):

- Equities (SYN.EQ.001..010 + SYN.ETF.IDX, venues XV1/XV2): full MBO streams.
  * Efficient price: ONE shared per-INSTRUMENT price process (2-state
    regime-switching volatility, Markov switching per 1s step), precomputed
    per session on a 1-second grid from a dedicated per-instrument SplitMix64
    stream ("eqmid"). Both venues of an instrument quote around the SAME
    efficient price, so the consolidated multi-venue book is essentially
    never crossed (target < 2% of event states; venue-independent mids in an
    earlier design left it crossed ~95% of the time — a research-validity
    bug, fixed and pinned here).
  * Per-venue microstructure: each venue stream adds a small mean-reverting
    AR(1) noise to the efficient price (sigma/rho/bound from config, bounded
    well below half the typical spread) and its own latency on receive_ts.
  * Order flow: clustered via a self-exciting intensity (Hawkes-style):
    executions kick excitation up, exponential decay per slot; inter-arrival
    times are exponential in the current intensity.
  * Queue dynamics: the generator maintains a real internal ``OrderBook`` per
    stream, so ADD/MODIFY/CANCEL/EXECUTE reference live FIFO state — EXECUTE
    always fills the FIFO head of the best opposite level, occasional
    aggressive ADDs cross the book (marketable), far orders are pruned with
    real CANCELs.
  * Session structure: open auction (STATUS AUCTION + auction TRADE prints +
    STATUS TRADING), one intraday HALT for a configured instrument/session
    (optionally followed by a re-opening auction: STATUS AUCTION, crossing
    ADDs that rest during the call, EXECUTEs that uncross them, auction
    TRADE prints, STATUS TRADING — ``halt.reopen_auction``, off by default so
    the pinned dataset is unchanged), close auction (STATUS AUCTION + prints
    + STATUS CLOSE). Multi-day capable; books and sequences persist across
    sessions.
- FX (8 G10 pairs, venues LP1/LP2/PRI): QUOTE (full L1 side replace) + TRADE
  streams; one shared regime-switching mid per pair, venue-specific spreads
  and venue-specific latency in receive_ts.
- QC anomalies (disabled for golden vectors): sequence gaps (with full
  SNAPSHOT recovery bursts so books can recover), exact duplicates, out-of-
  order arrivals (latency spikes), invalid events, receive_ts < exchange_ts
  violations. The generator reports exactly what it injected so the QC
  pipeline can be tested against ground truth.

Raw files are written in *arrival order* (sorted by receive_ts) with
``event_id`` assigned 1..N per file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from iap.core.codec import write_jsonl
from iap.core.events import SYNTHETIC_ID_BASE, EventType, MarketEvent, SessionStatus, Side
from iap.core.rng import SplitMix64
from iap.orderbook.book import OrderBook
from iap.reference.refdata import Instrument, ReferenceData, Venue

NS = 1_000_000_000

#: Golden-vector seeds (pinned; tests/golden depends on them).
GOLDEN_EQ_SEED = 4242424242
GOLDEN_FX_SEED = 8484848484

_DEFAULT_CONFIG = {
    "seed": 20260829,
    "sessions": 2,
    "equities": {
        "slots_per_stream": 3850,
        "vol_regimes": {"sigma_ticks_per_s": [0.15, 0.4], "switch_prob_per_s": 0.0007},
        "venue_noise": {"rho": 0.9, "sigma_ticks": 0.12, "max_ticks": 0.45},
        "flow": {"excitation_kick": 1.4, "excitation_decay": 0.82, "max_excitation": 8.0},
        "book": {"max_resting_orders": 160, "qty_lots_max": 10},
        "mix": {"add": 0.46, "cancel": 0.26, "modify": 0.12, "execute": 0.16},
        "halt": {"instrument": "SYN.EQ.007", "session_index": 0, "duration_s": 300,
                 "reopen_auction": False, "reopen_call_s": 60},
        "auction_prints": 3,
    },
    "fx": {
        "slots_per_pair": 3300,
        "vol_regimes": {"sigma_ticks": [0.8, 3.0], "switch_prob": 0.003},
        "spread_ticks": {"PRI": 2, "LP1": 3, "LP2": 5},
        "trade_prob": 0.12,
        "qty_units_max": 20,
    },
    "anomalies": {
        "gap_prob": 0.0006,
        "gap_max_events": 3,
        "dup_prob": 0.0015,
        "ooo_prob": 0.001,
        "invalid_prob": 0.0005,
        "ts_violation_prob": 0.0005,
    },
}


def _merge_config(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_config(out[k], v)
        else:
            out[k] = v
    return out


def load_generator_config(path=None) -> dict:
    """Load configs/generator.json merged over built-in defaults."""
    cfg = dict(_DEFAULT_CONFIG)
    if path is not None:
        with open(path) as f:
            file_cfg = json.load(f)
        file_cfg.pop("x-version", None)
        file_cfg.pop("description", None)
        cfg = _merge_config(cfg, file_cfg)
    return cfg


class _EffPrice:
    """Shared per-INSTRUMENT efficient price (regime-switching vol, pinned).

    The path is precomputed per session on a fixed 1-second grid from a
    dedicated per-instrument SplitMix64 stream, so every venue of the
    instrument reads the SAME efficient price at any event time regardless
    of how venue streams interleave their own draws.  Level and regime
    persist across sessions.
    """

    __slots__ = ("inst", "rng", "mid_f", "regime", "open_ns", "path")

    STEP_NS = 1_000_000_000  # 1-second efficient-price grid

    def __init__(self, inst: Instrument, rng: SplitMix64) -> None:
        self.inst = inst
        self.rng = rng
        self.mid_f = float(inst.ref_price_ticks)
        self.regime = 0
        self.open_ns = 0
        self.path: List[float] = [self.mid_f]

    def new_session(self, open_ns: int, close_ns: int, vol_cfg: dict) -> None:
        """Precompute this session's path, continuing from the prior level."""
        steps = int((close_ns - open_ns) // self.STEP_NS) + 2
        sigma = vol_cfg["sigma_ticks_per_s"]
        switch = vol_cfg["switch_prob_per_s"]
        lo = 0.5 * self.inst.ref_price_ticks
        hi = 2.0 * self.inst.ref_price_ticks
        rng = self.rng
        mid = self.mid_f
        regime = self.regime
        path = [mid]
        for _ in range(steps):
            if rng.uniform() < switch:
                regime ^= 1
            mid += rng.normal() * sigma[regime]
            mid = min(max(mid, lo), hi)
            path.append(mid)
        self.open_ns = open_ns
        self.path = path
        self.mid_f = path[-1]  # carried into the next session
        self.regime = regime

    def mid_at(self, ts: int) -> float:
        """Efficient price at event time ts (grid floor; clamped to session)."""
        k = (ts - self.open_ns) // self.STEP_NS
        if k < 0:
            k = 0
        last = len(self.path) - 1
        if k > last:
            k = last
        return self.path[int(k)]


class _Stream:
    """Per venue+instrument stream state (sequence, latency, internal book)."""

    __slots__ = (
        "inst",
        "venue",
        "book",
        "seq",
        "last_receive",
        "next_order",
        "next_trade",
        "mid_f",
        "regime",
        "noise",
        "excitation",
    )

    def __init__(self, inst: Instrument, venue: Venue) -> None:
        self.inst = inst
        self.venue = venue
        self.book = OrderBook(inst.instrument_id, venue.venue_id)
        self.seq = 0
        self.last_receive = 0
        base = (venue.venue_id << 56) | (inst.instrument_id << 40)
        self.next_order = base + 1
        self.next_trade = base + 1
        # mid_f/regime: FX pair-level state (equities read the shared
        # _EffPrice instead; for them mid_f caches the venue-effective mid).
        self.mid_f = float(inst.ref_price_ticks)
        self.regime = 0
        self.noise = 0.0  # equities: venue-specific AR(1) microstructure
        self.excitation = 0.0

    def new_order_id(self) -> int:
        oid = self.next_order
        self.next_order += 1
        return oid

    def new_trade_id(self) -> int:
        tid = self.next_trade
        self.next_trade += 1
        return tid


class MarketDataGenerator:
    """Deterministic synthetic generator over the configured universe."""

    def __init__(self, refdata: ReferenceData, config: Optional[dict] = None) -> None:
        self.ref = refdata
        self.cfg = _merge_config(_DEFAULT_CONFIG, config or {})
        self.seed: int = self.cfg["seed"]
        self.injected: Dict[str, int] = {
            "gaps": 0,
            "gap_missing_events": 0,
            "duplicates": 0,
            "out_of_order": 0,
            "invalid": 0,
            "ts_violations": 0,
        }
        # Persistent stream state across sessions.
        self._eq_streams: Dict[Tuple[int, int], _Stream] = {}
        self._fx_streams: Dict[Tuple[int, int], _Stream] = {}
        self._eq_prices: Dict[int, _EffPrice] = {}  # per-instrument shared mid
        self._rngs: Dict[Tuple[str, int, int], SplitMix64] = {}

    # ----------------------------------------------------------------- utils

    def _rng(self, kind: str, a: int, b: int) -> SplitMix64:
        """Deterministic per-(kind, instrument, venue) RNG, stable across calls."""
        key = (kind, a, b)
        rng = self._rngs.get(key)
        if rng is None:
            rng = SplitMix64(self.seed ^ (a << 32) ^ (b << 16) ^ len(kind))
            self._rngs[key] = rng
        return rng

    @staticmethod
    def _emit(
        stream: _Stream,
        out: List[MarketEvent],
        rng: SplitMix64,
        ts: int,
        event_type: int,
        side: int = 0,
        price: int = 0,
        qty: int = 0,
        order_id: int = 0,
        trade_id: int = 0,
    ) -> MarketEvent:
        """Append one event with the next sequence and a latency-modelled receive_ts."""
        stream.seq += 1
        venue = stream.venue
        latency = venue.latency_mean_ns + rng.below(venue.latency_jitter_ns + 1)
        receive = ts + latency
        if receive <= stream.last_receive:
            receive = stream.last_receive + 1
        stream.last_receive = receive
        ev = MarketEvent(
            0,
            stream.inst.instrument_id,
            venue.venue_id,
            ts,
            receive,
            stream.seq,
            int(event_type),
            int(side),
            price,
            qty,
            order_id,
            trade_id,
        )
        stream.book.apply(ev)
        out.append(ev)
        return ev

    def _snapshot_burst(self, stream: _Stream, out: List[MarketEvent],
                        rng: SplitMix64, ts: int) -> None:
        """Emit a full-book SNAPSHOT burst from the stream's internal state."""
        records: List[Tuple[int, int, int, int]] = []  # (side, price, qty, oid)
        for side in (int(Side.BID), int(Side.ASK)):
            for level in stream.book._sorted_levels(side):
                for oid, q in level.orders.items():
                    # Synthetic (id-less) resting orders are re-emitted id-less.
                    records.append((side, level.price, q,
                                    oid if oid < SYNTHETIC_ID_BASE else 0))
        if not records:  # nothing to recover; emit a heartbeat to advance
            self._emit(stream, out, rng, ts, EventType.HEARTBEAT)
            return
        n = len(records)
        for i, (side, price, q, oid) in enumerate(records):
            self._emit(
                stream, out, rng, ts, EventType.SNAPSHOT,
                side=side, price=price, qty=q, order_id=oid, trade_id=n - 1 - i,
            )

    # -------------------------------------------------------------- equities

    def _eq_add(self, stream: _Stream, out: List[MarketEvent], rng: SplitMix64,
                ts: int, forced_side: Optional[int] = None) -> None:
        cfg = self.cfg["equities"]
        mid = max(11, int(round(stream.mid_f)))
        side = (
            forced_side
            if forced_side is not None
            else (int(Side.BID) if rng.uniform() < 0.5 else int(Side.ASK))
        )
        offset = rng.below(6)
        if offset == 0 and forced_side is None:
            # Aggressive marketable ADD: cross the venue's own touch, sized
            # to be fully consumed so no crossing remainder ever rests (the
            # consolidated multi-venue book must stay uncrossed).
            opp = int(Side.ASK) if side == Side.BID else int(Side.BID)
            level = stream.book._best_level(opp)
            if level is not None:
                head_qty = next(iter(level.orders.values()))
                qty = min(head_qty,
                          stream.inst.lot_size * rng.randint(1, 3))
                self._emit(stream, out, rng, ts, EventType.ADD, side=side,
                           price=level.price, qty=qty,
                           order_id=stream.new_order_id())
                return
            offset = 1  # empty opposite side: fall through to a passive ADD
        offset = max(offset, 1)
        price = mid - offset if side == Side.BID else mid + offset
        if price < 1:
            price = 1
        qty = stream.inst.lot_size * rng.randint(1, cfg["book"]["qty_lots_max"])
        self._emit(stream, out, rng, ts, EventType.ADD, side=side, price=price,
                   qty=qty, order_id=stream.new_order_id())

    def _eq_slot(self, stream: _Stream, price: _EffPrice,
                 out: List[MarketEvent], rng: SplitMix64, ts: int) -> None:
        """Generate one flow slot (1-2 events) for an equity stream."""
        cfg = self.cfg["equities"]
        book = stream.book
        # Venue-effective mid = shared efficient price + bounded AR(1)
        # venue microstructure noise (well below half the typical spread).
        ncfg = cfg["venue_noise"]
        noise = ncfg["rho"] * stream.noise + ncfg["sigma_ticks"] * rng.normal()
        bound = ncfg["max_ticks"]
        stream.noise = min(max(noise, -bound), bound)
        stream.mid_f = price.mid_at(ts) + stream.noise
        stream.excitation *= cfg["flow"]["excitation_decay"]

        # Reprice: cancel this venue's resting orders that the efficient
        # price has moved through (bid above / ask below the venue mid).
        # Market makers pull stale quotes; without this the consolidated
        # multi-venue book stays crossed until random flow cleans up.
        vmid = int(round(stream.mid_f))
        stale = [
            o for o in book.resting_orders()
            if (o[1] == Side.BID and o[2] > vmid)
            or (o[1] == Side.ASK and o[2] < vmid)
        ]
        for oid, side, p, q in stale:
            self._emit(stream, out, rng, ts, EventType.CANCEL, side=side,
                       price=p, qty=q, order_id=oid)

        # Keep both sides populated.
        if book.best_bid() is None:
            self._eq_add(stream, out, rng, ts, forced_side=int(Side.BID))
            return
        if book.best_ask() is None:
            self._eq_add(stream, out, rng, ts, forced_side=int(Side.ASK))
            return

        mix = cfg["mix"]
        u = rng.uniform()
        if u < mix["add"]:
            self._eq_add(stream, out, rng, ts)
        elif u < mix["add"] + mix["cancel"]:
            orders = book.resting_orders()
            if not orders:
                self._eq_add(stream, out, rng, ts)
                return
            oid, side, price, qty = orders[rng.below(len(orders))]
            self._emit(stream, out, rng, ts, EventType.CANCEL, side=side,
                       price=price, qty=qty, order_id=oid)
        elif u < mix["add"] + mix["cancel"] + mix["modify"]:
            orders = book.resting_orders()
            if not orders:
                self._eq_add(stream, out, rng, ts)
                return
            oid, side, price, qty = orders[rng.below(len(orders))]
            new_qty = stream.inst.lot_size * rng.randint(1, cfg["book"]["qty_lots_max"])
            if new_qty == qty:
                new_qty += stream.inst.lot_size
            self._emit(stream, out, rng, ts, EventType.MODIFY, side=side,
                       price=price, qty=new_qty, order_id=oid)
        else:
            # Aggression: EXECUTE against the FIFO head of the best opposite
            # level, plus the tape TRADE print.
            aggressor = int(Side.BID) if rng.uniform() < 0.5 else int(Side.ASK)
            resting = int(Side.ASK) if aggressor == Side.BID else int(Side.BID)
            level = book._best_level(resting)
            if level is None:
                self._eq_add(stream, out, rng, ts)
                return
            head_id, head_qty = next(iter(level.orders.items()))
            price = level.price
            fill = min(head_qty, stream.inst.lot_size * rng.randint(1, 3))
            self._emit(stream, out, rng, ts, EventType.EXECUTE, side=resting,
                       price=price, qty=fill, order_id=head_id)
            self._emit(stream, out, rng, ts, EventType.TRADE, side=aggressor,
                       price=price, qty=fill, trade_id=stream.new_trade_id())
            stream.excitation = min(
                stream.excitation + cfg["flow"]["excitation_kick"],
                cfg["flow"]["max_excitation"],
            )
        # Depth pruning with a real CANCEL of the order farthest from mid.
        if book.order_count_total() > cfg["book"]["max_resting_orders"]:
            mid = int(round(stream.mid_f))
            far = max(book.resting_orders(), key=lambda o: (abs(o[2] - mid), o[0]))
            self._emit(stream, out, rng, ts, EventType.CANCEL, side=far[1],
                       price=far[2], qty=far[3], order_id=far[0])

    def _reopen_auction(self, stream: _Stream, out: List[MarketEvent],
                        rng: SplitMix64, t_end: int, price: _EffPrice) -> None:
        """Re-opening auction after a halt (pinned synthetic model).

        STATUS AUCTION opens the call ``reopen_call_s`` before ``t_end``;
        during the call two crossing ADDs (a bid at the best ask, an ask at
        the best bid) REST — the book is crossed, nothing matches while the
        status is AUCTION — then the venue uncrosses them with two EXECUTEs,
        prints the auction TRADEs at the mid, and STATUS TRADING resumes
        continuous matching exactly at ``t_end``.
        """
        cfg = self.cfg["equities"]
        book = stream.book
        lot = stream.inst.lot_size
        call_ns = int(cfg["halt"]["reopen_call_s"]) * NS
        if call_ns < (cfg["auction_prints"] + 5) * 1_000_000:
            raise ValueError("halt.reopen_call_s too short for the auction messages")
        t = t_end - call_ns
        self._emit(stream, out, rng, t, EventType.STATUS,
                   qty=int(SessionStatus.AUCTION))
        bb, ba = book.best_bid(), book.best_ask()
        if bb is not None and ba is not None:
            qty = lot * rng.randint(1, 3)
            t += 1_000_000
            bid_id = stream.new_order_id()
            self._emit(stream, out, rng, t, EventType.ADD, side=int(Side.BID),
                       price=ba[0], qty=qty, order_id=bid_id)
            t += 1_000_000
            ask_id = stream.new_order_id()
            self._emit(stream, out, rng, t, EventType.ADD, side=int(Side.ASK),
                       price=bb[0], qty=qty, order_id=ask_id)
            # Uncross: the venue matches the two auction orders together.
            t += 1_000_000
            self._emit(stream, out, rng, t, EventType.EXECUTE, side=int(Side.BID),
                       price=ba[0], qty=qty, order_id=bid_id)
            t += 1_000_000
            self._emit(stream, out, rng, t, EventType.EXECUTE, side=int(Side.ASK),
                       price=bb[0], qty=qty, order_id=ask_id)
            mid = max(1, int(round(price.mid_at(t))))
            for i in range(cfg["auction_prints"]):
                t += 1_000_000
                self._emit(stream, out, rng, t, EventType.TRADE, side=i % 2,
                           price=mid, qty=lot * rng.randint(1, 20),
                           trade_id=stream.new_trade_id())
        self._emit(stream, out, rng, t_end, EventType.STATUS,
                   qty=int(SessionStatus.TRADING))

    def _eq_session_stream(
        self,
        stream: _Stream,
        price: _EffPrice,
        rng: SplitMix64,
        open_ns: int,
        close_ns: int,
        slots: int,
        halt_window: Optional[Tuple[int, int]],
        anomalies: Optional[dict],
        max_events: Optional[int] = None,
    ) -> List[MarketEvent]:
        """One session of MBO flow for one equity stream.

        ``price`` is the instrument's SHARED efficient price (already
        advanced to this session via ``new_session``); the stream only adds
        its venue noise on top.
        """
        cfg = self.cfg["equities"]
        out: List[MarketEvent] = []
        t = open_ns
        stream.mid_f = price.mid_at(t)
        mid = max(11, int(round(stream.mid_f)))
        lot = stream.inst.lot_size

        # Open auction.
        self._emit(stream, out, rng, t, EventType.STATUS, qty=int(SessionStatus.AUCTION))
        for i in range(cfg["auction_prints"]):
            t += 1_000_000
            self._emit(stream, out, rng, t, EventType.TRADE,
                       side=i % 2, price=mid, qty=lot * rng.randint(1, 20),
                       trade_id=stream.new_trade_id())
        t += 1_000_000
        self._emit(stream, out, rng, t, EventType.STATUS, qty=int(SessionStatus.TRADING))

        # Seed the book on the very first session.
        if stream.book.order_count_total() == 0:
            for offset in range(1, 7):
                for _ in range(2):
                    t += 1_000
                    for side, px in (
                        (int(Side.BID), mid - offset),
                        (int(Side.ASK), mid + offset),
                    ):
                        self._emit(stream, out, rng, t, EventType.ADD, side=side,
                                   price=max(px, 1),
                                   qty=lot * rng.randint(1, cfg["book"]["qty_lots_max"]),
                                   order_id=stream.new_order_id())

        duration_s = max((close_ns - t) / NS, 1.0)
        lambda0 = slots / duration_s * 1.30
        halted = halt_window is None
        for _ in range(slots):
            rate = lambda0 * (1.0 + stream.excitation)
            t += int(rng.exponential(rate) * NS) + 1
            if not halted and t >= halt_window[0]:
                self._emit(stream, out, rng, halt_window[0], EventType.STATUS,
                           qty=int(SessionStatus.HALT))
                t = halt_window[1]
                if cfg["halt"].get("reopen_auction"):
                    self._reopen_auction(stream, out, rng, t, price)
                else:
                    self._emit(stream, out, rng, t, EventType.STATUS,
                               qty=int(SessionStatus.TRADING))
                halted = True
            if t >= close_ns - 2_000_000:
                break
            if anomalies is not None and rng.uniform() < anomalies["gap_prob"]:
                missing = rng.randint(1, anomalies["gap_max_events"])
                stream.seq += missing
                self.injected["gaps"] += 1
                self.injected["gap_missing_events"] += missing
                self._snapshot_burst(stream, out, rng, t)
                continue
            self._eq_slot(stream, price, out, rng, t)
            if max_events is not None and len(out) >= max_events:
                break

        # Close auction (printed at the shared efficient price so both
        # venues of the instrument print the same close).
        close_mid = max(1, int(round(price.mid_at(close_ns))))
        self._emit(stream, out, rng, close_ns, EventType.STATUS,
                   qty=int(SessionStatus.AUCTION))
        for i in range(cfg["auction_prints"]):
            self._emit(stream, out, rng, close_ns + (i + 1) * 1_000_000,
                       EventType.TRADE, side=i % 2, price=close_mid,
                       qty=lot * rng.randint(1, 20), trade_id=stream.new_trade_id())
        self._emit(stream, out, rng,
                   close_ns + (cfg["auction_prints"] + 1) * 1_000_000,
                   EventType.STATUS, qty=int(SessionStatus.CLOSE))
        return out

    # -------------------------------------------------------------------- FX

    def _fx_pair_session(
        self,
        inst: Instrument,
        streams: List[_Stream],
        rng: SplitMix64,
        open_ns: int,
        close_ns: int,
        slots: int,
        anomalies: Optional[dict],
        max_events: Optional[int] = None,
    ) -> List[MarketEvent]:
        """One session of QUOTE+TRADE flow for one FX pair across venues."""
        cfg = self.cfg["fx"]
        out: List[MarketEvent] = []
        pair_mid = streams[0].mid_f
        regime = streams[0].regime
        vol = cfg["vol_regimes"]
        t = open_ns
        duration_s = max((close_ns - open_ns) / NS, 1.0)
        lambda0 = slots / duration_s * 1.05

        for _ in range(slots):
            t += int(rng.exponential(lambda0) * NS) + 1
            if t >= close_ns:
                break
            if rng.uniform() < vol["switch_prob"]:
                regime ^= 1
            pair_mid += rng.normal() * vol["sigma_ticks"][regime]
            lo = 0.5 * inst.ref_price_ticks
            hi = 2.0 * inst.ref_price_ticks
            pair_mid = min(max(pair_mid, lo), hi)
            stream = streams[rng.below(len(streams))]
            if anomalies is not None and rng.uniform() < anomalies["gap_prob"]:
                if stream.book.order_count_total() > 0:
                    missing = rng.randint(1, anomalies["gap_max_events"])
                    stream.seq += missing
                    self.injected["gaps"] += 1
                    self.injected["gap_missing_events"] += missing
                    self._snapshot_burst(stream, out, rng, t)
                    continue
            mid = int(round(pair_mid))
            bb, ba = stream.book.best_bid(), stream.book.best_ask()
            if bb is not None and ba is not None and rng.uniform() < cfg["trade_prob"]:
                aggressor = int(Side.BID) if rng.uniform() < 0.5 else int(Side.ASK)
                price = ba[0] if aggressor == Side.BID else bb[0]
                self._emit(stream, out, rng, t, EventType.TRADE, side=aggressor,
                           price=price, qty=rng.randint(1, cfg["qty_units_max"]),
                           trade_id=stream.new_trade_id())
            else:
                spread = cfg["spread_ticks"][stream.venue.venue]
                half = spread // 2
                bid = mid - half
                ask = bid + spread + rng.below(2)
                self._emit(stream, out, rng, t, EventType.QUOTE, side=int(Side.BID),
                           price=max(bid, 1), qty=rng.randint(1, cfg["qty_units_max"]),
                           order_id=stream.new_order_id())
                self._emit(stream, out, rng, t, EventType.QUOTE, side=int(Side.ASK),
                           price=max(ask, 2), qty=rng.randint(1, cfg["qty_units_max"]),
                           order_id=stream.new_order_id())
            if max_events is not None and len(out) >= max_events:
                break
        streams[0].mid_f = pair_mid
        streams[0].regime = regime
        return out

    # ------------------------------------------------------- anomaly injection

    def _inject_file_anomalies(
        self, events: List[MarketEvent], rng: SplitMix64
    ) -> List[MarketEvent]:
        """Duplicate / out-of-order / invalid / ts-violation injection.

        Operates on one raw file's events; returns the final arrival-ordered
        list. Gap injection happens at generation time (with SNAPSHOT
        recovery); this pass handles the remaining anomaly classes.
        """
        an = self.cfg["anomalies"]
        clones: List[MarketEvent] = []
        last_exch: Dict[Tuple[int, int], int] = {}
        for ev in events:
            stream_key = (ev.venue_id, ev.instrument_id)
            prev_exch = last_exch.get(stream_key, 0)
            last_exch[stream_key] = ev.exchange_ts
            if ev.event_type in (EventType.SNAPSHOT, EventType.STATUS):
                continue  # keep recovery/status paths clean
            u = rng.uniform()
            if u < an["dup_prob"]:
                dup = MarketEvent(**ev.to_dict())
                dup.receive_ts = ev.receive_ts + 1_500
                clones.append(dup)
                self.injected["duplicates"] += 1
            elif u < an["dup_prob"] + an["ooo_prob"]:
                ev.receive_ts += 2_000_000_000  # 2s latency spike => late arrival
                self.injected["out_of_order"] += 1
            elif u < an["dup_prob"] + an["ooo_prob"] + an["invalid_prob"]:
                bad = MarketEvent(**ev.to_dict())
                bad.receive_ts = ev.receive_ts + 800
                if rng.below(2) == 0:
                    bad.event_type = 0  # unknown type
                else:
                    bad.side = 9  # impossible side
                clones.append(bad)
                self.injected["invalid"] += 1
            elif u < (an["dup_prob"] + an["ooo_prob"] + an["invalid_prob"]
                      + an["ts_violation_prob"]):
                # Guard: only on events spaced >= 1ms from their stream
                # predecessor, so the violation never also inverts arrival
                # order (which would contaminate the out-of-order counts).
                if ev.exchange_ts - prev_exch >= 1_000_000:
                    ev.receive_ts = ev.exchange_ts - 50_000
                    self.injected["ts_violations"] += 1
        events.extend(clones)
        return self._finalize_file(events)

    @staticmethod
    def _finalize_file(events: List[MarketEvent]) -> List[MarketEvent]:
        """Sort into arrival order and assign event_id 1..N."""
        events.sort(
            key=lambda e: (e.receive_ts, e.exchange_ts, e.venue_id,
                           e.instrument_id, e.sequence, e.event_type)
        )
        for i, ev in enumerate(events):
            ev.event_id = i + 1
        return events

    # ------------------------------------------------------------ run driver

    def generate_run(self, raw_dir) -> dict:
        """Generate the full configured run into ``raw_dir``; return stats."""
        raw_dir = Path(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        sessions = self.cfg["sessions"]
        dates = self.ref.trading_days[:sessions]
        if len(dates) < sessions:
            raise ValueError(
                f"calendar has {len(dates)} trading days, need {sessions}"
            )
        an = self.cfg["anomalies"]
        halt_cfg = self.cfg["equities"]["halt"]
        eq_slots = self.cfg["equities"]["slots_per_stream"]
        fx_slots = self.cfg["fx"]["slots_per_pair"]

        eq_venues = self.ref.venues("EQUITY")
        fx_venues = self.ref.venues("FX")
        stats = {"files": {}, "sessions": len(dates), "total_events": 0}

        for si, date in enumerate(dates):
            eq_open, eq_close = self.ref.session_bounds_ns("EQUITY", date)
            fx_open, fx_close = self.ref.session_bounds_ns("FX", date)

            eq_events: List[MarketEvent] = []
            for inst in self.ref.instruments("EQUITY"):
                # Advance the instrument's SHARED efficient price for this
                # session (dedicated per-instrument RNG stream, so the path
                # is identical for every venue regardless of interleaving).
                price = self._eq_prices.get(inst.instrument_id)
                if price is None:
                    price = _EffPrice(
                        inst, self._rng("eqmid", inst.instrument_id, 0)
                    )
                    self._eq_prices[inst.instrument_id] = price
                price.new_session(
                    eq_open, eq_close, self.cfg["equities"]["vol_regimes"]
                )
                for venue in eq_venues:
                    key = (inst.instrument_id, venue.venue_id)
                    stream = self._eq_streams.get(key)
                    if stream is None:
                        stream = _Stream(inst, venue)
                        self._eq_streams[key] = stream
                    rng = self._rng("eq", inst.instrument_id, venue.venue_id)
                    halt_window = None
                    if (
                        inst.symbol == halt_cfg["instrument"]
                        and si == halt_cfg["session_index"]
                    ):
                        halt_at = eq_open + (eq_close - eq_open) * 3 // 10
                        halt_window = (
                            halt_at, halt_at + halt_cfg["duration_s"] * NS
                        )
                    eq_events.extend(
                        self._eq_session_stream(
                            stream, price, rng, eq_open, eq_close, eq_slots,
                            halt_window, an,
                        )
                    )
            eq_events = self._inject_file_anomalies(
                eq_events, self._rng("anom-eq", si, 0)
            )
            eq_path = raw_dir / f"eq_{date.replace('-', '')}.jsonl"
            write_jsonl(eq_path, eq_events)
            stats["files"][eq_path.name] = len(eq_events)
            stats["total_events"] += len(eq_events)

            fx_events: List[MarketEvent] = []
            for inst in self.ref.instruments("FX"):
                streams = []
                for venue in fx_venues:
                    key = (inst.instrument_id, venue.venue_id)
                    stream = self._fx_streams.get(key)
                    if stream is None:
                        stream = _Stream(inst, venue)
                        self._fx_streams[key] = stream
                    streams.append(stream)
                rng = self._rng("fx", inst.instrument_id, 0)
                fx_events.extend(
                    self._fx_pair_session(
                        inst, streams, rng, fx_open, fx_close, fx_slots, an
                    )
                )
            fx_events = self._inject_file_anomalies(
                fx_events, self._rng("anom-fx", si, 0)
            )
            fx_path = raw_dir / f"fx_{date.replace('-', '')}.jsonl"
            write_jsonl(fx_path, fx_events)
            stats["files"][fx_path.name] = len(fx_events)
            stats["total_events"] += len(fx_events)

        stats["injected_anomalies"] = dict(self.injected)
        return stats


# ------------------------------------------------------------- golden vectors


def generate_golden_eq(
    refdata: ReferenceData, seed: int = GOLDEN_EQ_SEED, n: int = 2000
) -> List[MarketEvent]:
    """Pinned single-instrument (SYN.EQ.001 @ XV1) clean MBO golden vector.

    No anomalies, no halt; exactly ``n`` events in exchange-time order with
    event_id 1..n and contiguous sequences.
    """
    gen = MarketDataGenerator(refdata, {"seed": seed})
    inst = refdata.instrument("SYN.EQ.001")
    venue = refdata.venue("XV1")
    stream = _Stream(inst, venue)
    rng = SplitMix64(seed)
    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", date)
    price = _EffPrice(inst, gen._rng("eqmid", inst.instrument_id, 0))
    price.new_session(open_ns, close_ns, gen.cfg["equities"]["vol_regimes"])
    events = gen._eq_session_stream(
        stream, price, rng, open_ns, close_ns, slots=n + 200,
        halt_window=None, anomalies=None, max_events=n + 20,
    )
    events = events[:n]
    if len(events) != n:
        raise RuntimeError(f"golden EQ vector produced {len(events)} < {n} events")
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    return events


def generate_golden_fx(
    refdata: ReferenceData, seed: int = GOLDEN_FX_SEED, n: int = 800
) -> List[MarketEvent]:
    """Pinned EUR/USD QUOTE+TRADE golden vector across LP1/LP2/PRI.

    No anomalies; exactly ``n`` events in exchange-time order with event_id
    1..n; per-venue sequences contiguous.
    """
    gen = MarketDataGenerator(refdata, {"seed": seed})
    inst = refdata.instrument("EUR/USD")
    streams = [_Stream(inst, refdata.venue(v)) for v in ("LP1", "LP2", "PRI")]
    rng = SplitMix64(seed)
    date = refdata.trading_days[0]
    open_ns, close_ns = refdata.session_bounds_ns("FX", date)
    events = gen._fx_pair_session(
        inst, streams, rng, open_ns, close_ns, slots=n + 200,
        anomalies=None, max_events=n + 4,
    )
    events = events[:n]
    if len(events) != n:
        raise RuntimeError(f"golden FX vector produced {len(events)} < {n} events")
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    return events

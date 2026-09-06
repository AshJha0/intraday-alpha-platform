"""Event-driven incremental feature engine (conventions §6, spec §10).

The engine is driven by the same normalized event stream the replay layer
consumes (globally ordered by exchange_ts).  Per instrument it maintains a
ConsolidatedBook (reference order book, reused — not reimplemented) plus
rolling event-time state, and emits :class:`FeatureVector` rows — values in
registry order with a parallel validity bitset — at a configurable cadence:
every event (``cadence_ns=0``) or at most once per ``cadence_ns`` per
instrument, evaluated when that instrument's events arrive (pure event time,
no wall clock).

Pinned state-update semantics (family modules document the formulas):

- **Timestamp regression**: an event whose ``exchange_ts`` is below the
  instrument's last seen ``exchange_ts`` (cross-venue clock skew) is dropped
  and counted (``ts_regressions_dropped``) BEFORE the book sees it — the
  engine never raises mid-stream and never re-orders the stream.
- **Applied events only** (API_FEATURES §2 step 1): every event is handed to
  the ConsolidatedBook first; an event the book DROPS (duplicate sequence,
  invalid side, malformed payload, dropped-while-stale, unknown type) or
  HOLDS contributes to NO rolling state — no trade window, no event-rate
  window, no venue-update count, no book refresh.
- The book state is refreshed after every applied book-touching event: ADD,
  MODIFY, CANCEL, EXECUTE, QUOTE, and the final record of a SNAPSHOT burst
  (interior burst records leave derived state untouched so a half-built book
  never contaminates rolling statistics).
- The consolidated view merges only non-stale venue books; ``book_ok`` means
  both sides are quoted after the merge.
- Rolling windows are half-open event-time intervals ``(t - w, t]``.
- Mid-derived samples (returns, realized vol, extrema, mid stats, cross-
  asset pairs) are recorded whenever the merged mid differs from the last
  RECORDED mid sample (so a one-sided flicker that moves the mid still
  yields a vol sample); depth/imbalance/spread samples at every two-sided
  refresh; OFI and L1 queue deltas at every refresh.
- **Stale recovery** (API_FEATURES §2.1): when the instrument's venue set
  goes from "at least one venue stale" back to "no venue stale", every
  rolling window and history of that instrument is CLEARED and the warmup
  anchor moves to the recovery timestamp (``warmup_after_recovery``).  A
  window therefore never spans an interval the platform did not observe.
- Validity: a feature is invalid during warmup, when the book is not ok
  (for book-derived features), or when its inputs are undefined — including
  every ``x / (y + EPS)`` ratio whose denominator ``y`` is <= 0 (an
  unreliable denominator makes the ratio undefined, never huge).  NaN never
  appears with valid=True (enforced by the single value funnel).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log
from typing import Callable, Dict, Iterable, List, Optional

from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.features import crossasset, timeofday
from iap.features.context import InstrumentContext
from iap.features.registry import build_registry, family_module, registry_hash
from iap.features.rolling import (
    RollingExtrema,
    RollingKeyCount,
    RollingSum,
    SessionProfile,
    TimeSeries,
)
from iap.features.spec import FAMILY_ORDER, WINDOW_NS
from iap.features.volatility import JUMP_K, JUMP_MIN_OBS
from iap.orderbook.book import ApplyStatus, ConsolidatedBook

_NS = 1_000_000_000
#: History retention: 2x the longest lookback needed (accel_1m needs 2m,
#: window stats need 5m) plus margin.
_HIST_KEEP_NS = 660 * _NS

#: Largest quantity the feature engine folds into a rolling window
#: (pinned, API_FEATURES §2.2).  Beyond this a feed is malformed, not a
#: market: window sums must stay exactly representable as int64 in every
#: port, and 2^40 units leaves >= 2^23 samples of headroom per window.
FEATURE_MAX_QTY = 1 << 40

_BOOK_TOUCH = frozenset({
    EventType.ADD, EventType.MODIFY, EventType.CANCEL,
    EventType.EXECUTE, EventType.QUOTE,
})


@dataclass
class FeatureVector:
    """FeatureVector contract (schemas/feature_vector.schema.json)."""

    instrument_id: int
    timestamp: int
    feature_version: str
    values: List[float]
    validity: List[bool]

    def validity_bits(self) -> bytes:
        """Validity as a little-endian-bit-packed bitset (bit i = feature i)."""
        n = len(self.validity)
        out = bytearray((n + 7) // 8)
        for i, v in enumerate(self.validity):
            if v:
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)

    @staticmethod
    def unpack_bits(bits: bytes, n: int) -> List[bool]:
        """Inverse of :meth:`validity_bits`."""
        return [bool(bits[i >> 3] >> (i & 7) & 1) for i in range(n)]


class _InstState:
    """All rolling per-instrument state (the ``st`` family modules read)."""

    def __init__(self, ctx: InstrumentContext, profile: SessionProfile) -> None:
        self.ctx = ctx
        self.tick = ctx.tick_size
        self.cons = ConsolidatedBook(ctx.instrument_id)
        self.ref: "_InstState" = self  # rewired by the engine
        self.profile = profile
        self.first_ts: Optional[int] = None
        #: warmup anchor: max(first event ts, last stale->fresh recovery ts)
        self.warm_ts: Optional[int] = None
        #: event time of the last stale->fresh recovery (None: never stale)
        self.recovered_ts: Optional[int] = None
        #: number of stale->fresh recoveries (rolling state resets)
        self.recoveries = 0
        #: sorted ids of this instrument's venues whose book is stale
        self.stale_venues: tuple = ()
        #: monotone counter of merged-view refreshes (label sampling hook)
        self.refresh_seq = 0
        #: events the book dropped/held (never folded into rolling state)
        self.events_dropped = 0
        #: events dropped by the engine for an exchange_ts regression
        self.ts_regressions_dropped = 0
        #: applied events whose qty exceeded FEATURE_MAX_QTY (not folded)
        self.oversized_qty_dropped = 0
        #: refreshes whose merged depth exceeded FEATURE_MAX_QTY (view unusable)
        self.oversized_depth_skipped = 0
        self.last_ts = 0
        self.t = 0
        self.last_emit: Optional[int] = None
        # current merged book view (refreshed on book-touching events)
        self.book_ok = False
        self.depth_bid: List = []
        self.depth_ask: List = []
        self.bid_p = self.bid_q = self.ask_p = self.ask_q = 0
        self.db1 = self.db3 = self.db5 = self.db10 = 0
        self.da1 = self.da3 = self.da5 = self.da10 = 0
        self.mid2 = 0
        self.mid = 0.0
        self.logmid = 0.0
        self.spread_ticks = 0
        self.spread_bps = 0.0
        self.oc_bid1 = 0
        self.oc_ask1 = 0
        self.halt = False
        self.auction = False
        self.venue_rows: List = []  # [(vid, bid10, ask10, stale)] sorted
        self.venue_cache: Dict[int, tuple] = {}  # vid -> (bid10, ask10)
        self.venue_last_ts: Dict[int, int] = {}
        # rolling structures
        self.hist2 = TimeSeries()   # mid2 (int) at mid changes
        self.histlog = TimeSeries()  # ln(mid2) at mid changes
        self.rv = {w: RollingSum(WINDOW_NS[w], 3) for w in ("10s", "1m", "5m")}
        self.ext_mid = {w: RollingExtrema(WINDOW_NS[w]) for w in ("10s", "1m", "5m")}
        self.ext_absdlm = RollingExtrema(WINDOW_NS["1m"])
        self.jumps = RollingSum(WINDOW_NS["5m"], 1)
        self.midstat = {w: RollingSum(WINDOW_NS[w], 2) for w in ("10s", "1m", "5m")}
        self.ofi = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "5s", "30s")}
        self.depthavg = {w: RollingSum(WINDOW_NS[w], 12) for w in ("1s", "10s", "1m")}
        self.trades = {w: RollingSum(WINDOW_NS[w], 6) for w in ("1s", "10s", "1m")}
        self.evstats = {w: RollingSum(WINDOW_NS[w], 7) for w in ("1s", "10s", "1m")}
        self.queue = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "10s")}
        self.xc = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.xl = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.venue_updates = RollingKeyCount(WINDOW_NS["10s"])

    # ------------------------------------------------------ family accessors

    def warm(self, w_ns: int) -> bool:
        """True when the window w has fully elapsed since the warmup anchor.

        The anchor is the instrument's first event, or the last stale->fresh
        recovery when one happened (``warmup_after_recovery``, API_FEATURES
        §2.1): a window is never reported over an unobserved interval.
        """
        return self.warm_ts is not None and self.t - self.warm_ts >= w_ns

    @property
    def label_tradable(self) -> bool:
        """True when this refresh is a tradable market state (API_FEATURES §6).

        Two-sided merged book, no stale venue, no venue in HALT or AUCTION.
        The label layer marks every other refresh as a blackout sample.
        """
        return (
            self.book_ok
            and not self.stale_venues
            and not self.halt
            and not self.auction
        )

    def mid2_at(self, ts: int) -> Optional[int]:
        """Latest mid2 sample at-or-before ts (None during warmup)."""
        return self.hist2.at_or_before(ts)

    def logmid_at(self, ts: int) -> Optional[float]:
        """Latest ln(mid2) sample at-or-before ts."""
        return self.histlog.at_or_before(ts)

    def rvol(self, w: str) -> Optional[float]:
        """Realized vol over window w (None until the window is warm)."""
        if not self.warm(WINDOW_NS[w]):
            return None
        # max() guards against tiny negative float drift in a drained window
        return (max(self.rv[w].sums[0], 0.0) / (WINDOW_NS[w] / 1e9)) ** 0.5

    def ref_ret_log(self, h_ns: int) -> Optional[float]:
        """Reference-instrument log mid return over h (at-or-before reads)."""
        ref = self.ref
        now = ref.logmid_at(self.t)
        past = ref.logmid_at(self.t - h_ns)
        if now is None or past is None:
            return None
        return now - past

    def beta_w5m(self) -> Optional[float]:
        """OLS beta vs the reference over 5m of contemporaneous 1s pairs."""
        if not self.warm(WINDOW_NS["5m"]):
            return None
        m = crossasset._moments(self.xc["5m"])
        if m is None or m[1] <= crossasset.MIN_VAR:
            return None
        return m[2] / m[1]

    # ----------------------------------------------------------- maintenance

    def reset_rolling(self, t: int) -> None:
        """Clear every rolling window / history and re-anchor warmup at t.

        Called on a stale->fresh recovery (pinned, API_FEATURES §2.1): the
        platform did not observe the market during the stale period, so no
        window may span it and no history lookup may reach across it.
        """
        self.warm_ts = t
        self.recovered_ts = t
        self.recoveries += 1
        self.hist2 = TimeSeries()
        self.histlog = TimeSeries()
        self.rv = {w: RollingSum(WINDOW_NS[w], 3) for w in ("10s", "1m", "5m")}
        self.ext_mid = {w: RollingExtrema(WINDOW_NS[w]) for w in ("10s", "1m", "5m")}
        self.ext_absdlm = RollingExtrema(WINDOW_NS["1m"])
        self.jumps = RollingSum(WINDOW_NS["5m"], 1)
        self.midstat = {w: RollingSum(WINDOW_NS[w], 2) for w in ("10s", "1m", "5m")}
        self.ofi = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "5s", "30s")}
        self.depthavg = {w: RollingSum(WINDOW_NS[w], 12) for w in ("1s", "10s", "1m")}
        self.trades = {w: RollingSum(WINDOW_NS[w], 6) for w in ("1s", "10s", "1m")}
        self.evstats = {w: RollingSum(WINDOW_NS[w], 7) for w in ("1s", "10s", "1m")}
        self.queue = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "10s")}
        self.xc = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.xl = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.venue_updates = RollingKeyCount(WINDOW_NS["10s"])

    def trim_all(self, t: int) -> None:
        """Evict expired samples from every rolling structure at time t."""
        for group in (self.rv, self.midstat, self.ofi, self.depthavg,
                      self.trades, self.evstats, self.queue, self.xc, self.xl):
            for win in group.values():
                win.trim(t)
        for ext in self.ext_mid.values():
            ext.trim(t)
        self.ext_absdlm.trim(t)
        self.jumps.trim(t)
        self.venue_updates.trim(t)
        self.hist2.trim(t - _HIST_KEEP_NS)
        self.histlog.trim(t - _HIST_KEEP_NS)


class FeatureEngine:
    """Incremental event-driven feature computation over a replay stream."""

    def __init__(
        self,
        contexts: Dict[int, InstrumentContext],
        cadence_ns: int = 0,
        on_vector: Optional[Callable[[FeatureVector], None]] = None,
        profiles: Optional[Dict[int, SessionProfile]] = None,
    ) -> None:
        """``cadence_ns=0`` emits on every event; else at most once per
        cadence per instrument.  ``profiles`` lets a pipeline carry session
        profiles across engine instances (e.g. across trading days)."""
        if cadence_ns < 0:
            raise ValueError("cadence_ns must be >= 0")
        self.contexts = contexts
        self.cadence_ns = cadence_ns
        self.on_vector = on_vector
        self.registry = build_registry()
        self.feature_names = [s.name for s in self.registry]
        self.feature_version = registry_hash()
        self.families = [(fam, family_module(fam).compute) for fam in FAMILY_ORDER]
        self._fam_sizes = {
            fam: len(family_module(fam).specs()) for fam in FAMILY_ORDER
        }
        self.states: Dict[int, _InstState] = {}
        self._profiles = profiles if profiles is not None else {}
        self.events_processed = 0
        #: events the book dropped/held — never folded into rolling state
        self.events_dropped = 0
        #: events dropped for an exchange_ts regression (fail closed)
        self.ts_regressions_dropped = 0
        #: applied events whose qty exceeded FEATURE_MAX_QTY
        self.oversized_qty_dropped = 0
        #: refreshes whose merged depth exceeded FEATURE_MAX_QTY
        self.oversized_depth_skipped = 0
        self.vectors_emitted = 0

    # ---------------------------------------------------------------- states

    def _state(self, instrument_id: int) -> _InstState:
        st = self.states.get(instrument_id)
        if st is None:
            ctx = self.contexts.get(instrument_id)
            if ctx is None:
                raise ValueError(
                    f"no InstrumentContext for instrument {instrument_id}"
                )
            profile = self._profiles.get(instrument_id)
            if profile is None:
                profile = SessionProfile(timeofday.PROFILE_METRICS)
                self._profiles[instrument_id] = profile
            st = _InstState(ctx, profile)
            self.states[instrument_id] = st
            if ctx.ref_instrument_id != instrument_id:
                st.ref = self._state(ctx.ref_instrument_id)
        return st

    # ----------------------------------------------------------------- apply

    def apply(self, ev: MarketEvent) -> Optional[FeatureVector]:
        """Apply one event; returns the emitted FeatureVector, if any.

        Events the book DROPS or HOLDS (API_CORE §4 / API_FEATURES §2) are
        counted and then ignored: they contribute to no rolling sample.  A
        *staleness refresh* still runs when the drop changed the set of stale
        venues (a gap detected on a dropped event still removes that venue
        from the merged view) — it updates the merged view and ``book_ok``
        but records no flow/mid samples.  An emission still happens on the
        engine's cadence so the vector stream stays aligned with the events.
        """
        st = self._state(ev.instrument_id)
        t = ev.exchange_ts
        if st.first_ts is not None and t < st.last_ts:
            # Cross-venue exchange_ts regression (clock skew between venue
            # gateways).  Pinned (API_FEATURES §2): dropped + counted before
            # the book sees it — never raised mid-stream, never re-ordered.
            st.ts_regressions_dropped += 1
            self.ts_regressions_dropped += 1
            st.events_dropped += 1
            self.events_processed += 1
            self.events_dropped += 1
            return None
        status = st.cons.apply(ev)
        if st.first_ts is None:
            st.first_ts = t
            st.warm_ts = t
        st.last_ts = t
        st.t = t
        self.events_processed += 1

        # The merged view is a function of WHICH venues are stale, so the
        # trigger is a change of the stale SET (a second venue going stale
        # must remove it from the view too), not of "any venue is stale".
        stale_now = tuple(
            vid for vid in sorted(st.cons.books) if st.cons.books[vid].stale
        )
        stale_changed = stale_now != st.stale_venues
        just_recovered = bool(st.stale_venues) and not stale_now
        st.stale_venues = stale_now

        if status != ApplyStatus.APPLIED:
            st.events_dropped += 1
            self.events_dropped += 1
            if stale_changed:
                self._refresh_book(st, ev.venue_id, t, just_recovered,
                                   samples=False)
            return self._maybe_emit(st, t)
        st.venue_last_ts[ev.venue_id] = t
        st.venue_updates.add(t, ev.venue_id)

        et = ev.event_type
        oversized = ev.qty > FEATURE_MAX_QTY
        if oversized:
            # Oversized quantity (pinned §2.2): the book may hold it, but no
            # rolling window folds it in — an i64 window sum must stay exact.
            # The merged view is still refreshed (the book state changed).
            st.oversized_qty_dropped += 1
            self.oversized_qty_dropped += 1
            et = None
        if et == EventType.TRADE:
            self._on_trade(st, ev)
        elif et == EventType.ADD:
            self._add_evstats(st, t, (1, ev.qty, 0, 0, 0, 0, 0))
        elif et == EventType.CANCEL:
            self._add_evstats(st, t, (0, 0, 1, ev.qty, 0, 0, 0))
        elif et == EventType.MODIFY:
            self._add_evstats(st, t, (0, 0, 0, 0, 1, 0, 0))
        elif et == EventType.EXECUTE:
            self._add_evstats(st, t, (0, 0, 0, 0, 0, 1, ev.qty))

        et = ev.event_type
        if et in _BOOK_TOUCH or (
            et == EventType.SNAPSHOT and ev.trade_id == 0
        ):
            self._refresh_book(st, ev.venue_id, t, just_recovered)
        elif stale_changed:
            self._refresh_book(st, ev.venue_id, t, just_recovered,
                               samples=False)

        return self._maybe_emit(st, t)

    def _maybe_emit(self, st: _InstState, t: int) -> Optional[FeatureVector]:
        """Cadence check (pure event time); emits at most one vector."""
        if (
            self.cadence_ns == 0
            or st.last_emit is None
            or t - st.last_emit >= self.cadence_ns
        ):
            vec = self._emit(st, t)
            st.last_emit = t
            return vec
        return None

    def run(self, events: Iterable[MarketEvent]) -> dict:
        """Apply an event stream; returns summary stats."""
        for ev in events:
            self.apply(ev)
        return {
            "events_processed": self.events_processed,
            "events_dropped": self.events_dropped,
            "ts_regressions_dropped": self.ts_regressions_dropped,
            "oversized_qty_dropped": self.oversized_qty_dropped,
            "oversized_depth_skipped": self.oversized_depth_skipped,
            "vectors_emitted": self.vectors_emitted,
            "instruments": len(self.states),
        }

    # --------------------------------------------------------- state updates

    @staticmethod
    def _add_evstats(st: _InstState, t: int, vals: tuple) -> None:
        for win in st.evstats.values():
            win.add(t, vals)

    def _on_trade(self, st: _InstState, ev: MarketEvent) -> None:
        qty = ev.qty
        buy = qty if ev.side == 0 else 0
        sell = qty - buy
        eff = 0.0
        eff_n = 0
        if st.book_ok:
            price = ev.price_ticks * st.tick
            eff = 2.0 * abs(price - st.mid) / st.mid * 1e4
            eff_n = 1
        vals = (buy - sell, qty, buy, sell, eff, eff_n)
        for win in st.trades.values():
            win.add(ev.exchange_ts, vals)

    def _refresh_book(self, st: _InstState, venue_id: int, t: int,
                      just_recovered: bool = False,
                      samples: bool = True) -> None:
        """Recompute the merged view (pinned, API_FEATURES §2).

        ``samples=False`` is a *staleness refresh*: the merged view and
        ``book_ok`` are recomputed because the set of stale venues changed,
        but no OFI / queue / depth / mid sample is recorded — a venue
        appearing in or disappearing from the merged view is a data
        availability event, not order flow.
        """
        cons = st.cons
        st.refresh_seq += 1
        vb = cons.books.get(venue_id)
        if vb is not None:
            st.venue_cache[venue_id] = (vb.depth(0, 10), vb.depth(1, 10))
        # stale->fresh recovery: clear all rolling state before this refresh
        # contributes anything (pinned, API_FEATURES §2.1).
        if just_recovered:
            st.reset_rolling(t)
            st.depth_bid = []
            st.depth_ask = []
            st.book_ok = False
        # merged non-stale depth (sorted venue iteration — deterministic)
        agg_b: Dict[int, int] = {}
        agg_a: Dict[int, int] = {}
        rows = []
        for vid in sorted(st.venue_cache):
            b10, a10 = st.venue_cache[vid]
            stale = cons.books[vid].stale
            rows.append((vid, b10, a10, stale))
            if stale:
                continue
            for p, q in b10:
                agg_b[p] = agg_b.get(p, 0) + q
            for p, q in a10:
                agg_a[p] = agg_a.get(p, 0) + q
        st.venue_rows = rows
        prev_bid, prev_ask = st.depth_bid, st.depth_ask
        bid = sorted(agg_b.items(), key=lambda x: -x[0])[:10]
        ask = sorted(agg_a.items())[:10]

        # Oversized merged depth (pinned §2.2): a level above FEATURE_MAX_QTY
        # makes the merged view unusable — clear it, record nothing, and let
        # the next clean refresh re-baseline (like the first refresh).
        oversized = any(q > FEATURE_MAX_QTY for _, q in bid) or any(
            q > FEATURE_MAX_QTY for _, q in ask
        )
        if oversized:
            st.oversized_depth_skipped += 1
            self.oversized_depth_skipped += 1
            st.depth_bid, st.depth_ask = [], []
            st.book_ok = False
            self._status_flags(st)
            return
        st.depth_bid, st.depth_ask = bid, ask

        # OFI contributions + L1 queue deltas (defined per side, book_ok or
        # not).  The first refresh after a recovery has no previous depth
        # and contributes nothing (same rule as the very first refresh).
        sample_flow = samples and (not just_recovered) and bool(
            prev_bid or prev_ask or bid or ask
        )
        if sample_flow:
            contribs = [0, 0, 0, 0]
            for i, k in enumerate((1, 3, 5, 10)):
                db = self._delta(prev_bid, bid, k)
                da = self._delta(prev_ask, ask, k)
                contribs[i] = db - da
            ct = tuple(contribs)
            for win in st.ofi.values():
                win.add(t, ct)
            dep_b, rep_b = self._queue_delta(prev_bid, bid, True)
            dep_a, rep_a = self._queue_delta(prev_ask, ask, False)
            qt = (dep_b, rep_b, dep_a, rep_a)
            for win in st.queue.values():
                win.add(t, qt)

        st.book_ok = bool(bid) and bool(ask)
        self._status_flags(st)
        if not st.book_ok:
            return
        st.bid_p, st.bid_q = bid[0]
        st.ask_p, st.ask_q = ask[0]
        st.db1 = bid[0][1]
        st.da1 = ask[0][1]
        st.db3 = sum(q for _, q in bid[:3])
        st.da3 = sum(q for _, q in ask[:3])
        st.db5 = sum(q for _, q in bid[:5])
        st.da5 = sum(q for _, q in ask[:5])
        st.db10 = sum(q for _, q in bid)
        st.da10 = sum(q for _, q in ask)
        st.mid2 = st.bid_p + st.ask_p
        st.mid = st.mid2 * st.tick / 2.0
        st.logmid = log(st.mid2)
        st.spread_ticks = st.ask_p - st.bid_p
        st.spread_bps = st.spread_ticks * st.tick / st.mid * 1e4

        if not samples:
            return  # staleness refresh: view updated, nothing recorded

        # depth/imbalance/spread sample (every two-sided refresh)
        imb = []
        for bk, ak in ((st.db1, st.da1), (st.db3, st.da3),
                       (st.db5, st.da5), (st.db10, st.da10)):
            imb.append((bk - ak) / (bk + ak) if bk + ak > 0 else 0.0)
        dvals = (st.db1, st.da1, st.db5, st.da5, st.db10, st.da10,
                 imb[0], imb[1], imb[2], imb[3],
                 st.spread_ticks, st.db10 + st.da10)
        for win in st.depthavg.values():
            win.add(t, dvals)

        # Mid-change samples.  The comparison is against the last RECORDED
        # mid sample (pinned): a one-sided flicker that moves the mid still
        # produces a vol sample, and a flicker back to the same mid produces
        # none.  History is empty right after a stale recovery, so no sample
        # ever spans an unobserved interval.
        last_mid2 = st.hist2.last()
        if last_mid2 is None or st.mid2 != last_mid2:
            if last_mid2 is not None:
                dlm = st.logmid - st.histlog.last()
                adlm = abs(dlm)
                # jump detection against the *prior* 1m mean (no lookahead).
                # Trim the window to the current time first: with a coarse
                # emission cadence trim_all() may not have run since the last
                # mid change, and stale samples older than 1m would otherwise
                # contaminate the comparison baseline.
                rv1m = st.rv["1m"]
                rv1m.trim(t)
                if (
                    st.warm(WINDOW_NS["1m"])
                    and rv1m.count >= JUMP_MIN_OBS
                    and adlm > JUMP_K * (rv1m.sums[1] / rv1m.count)
                ):
                    st.jumps.add(t, (1,))
                sq = dlm * dlm
                rvals = (sq, adlm, sq * sq)
                for win in st.rv.values():
                    win.add(t, rvals)
                st.ext_absdlm.add(t, adlm)
            st.hist2.append(t, st.mid2)
            st.histlog.append(t, st.logmid)
            for ext in st.ext_mid.values():
                ext.add(t, st.mid2)
            ms = (st.mid2, st.mid2 * st.mid2)
            for win in st.midstat.values():
                win.add(t, ms)
            # cross-asset 1s-return pairs
            ref = st.ref
            x0 = st.logmid_at(t - WINDOW_NS["1s"])
            if x0 is not None:
                x = st.logmid - x0
                y1 = ref.logmid_at(t)
                y0 = ref.logmid_at(t - WINDOW_NS["1s"])
                if y1 is not None and y0 is not None:
                    pair = (x, y1 - y0, x * x,
                            (y1 - y0) * (y1 - y0), x * (y1 - y0))
                    for win in st.xc.values():
                        win.add(t, pair)
                    yl0 = ref.logmid_at(t - 2 * WINDOW_NS["1s"])
                    if yl0 is not None:
                        yl = y0 - yl0
                        lpair = (x, yl, x * x, yl * yl, x * yl)
                        for win in st.xl.values():
                            win.add(t, lpair)

    @staticmethod
    def _delta(prev: List, curr: List, k: int) -> int:
        """Signed depth change within the best-k levels (OFI building block)."""
        d = 0
        pk = {p: q for p, q in prev[:k]}
        seen = set()
        for p, q in curr[:k]:
            d += q - pk.get(p, 0)
            seen.add(p)
        for p, q in prev[:k]:
            if p not in seen:
                d -= q
        return d

    @staticmethod
    def _queue_delta(prev: List, curr: List, is_bid: bool) -> tuple:
        """(depleted, replenished) L1 qty per the pinned five-case rule."""
        if not prev and not curr:
            return 0, 0
        if not prev:
            return 0, curr[0][1]
        if not curr:
            return prev[0][1], 0
        p0, q0 = prev[0]
        p1, q1 = curr[0]
        if p1 == p0:
            d = q1 - q0
            return (-d, 0) if d < 0 else (0, d)
        improved = p1 > p0 if is_bid else p1 < p0
        return (0, q1) if improved else (q0, 0)

    # ------------------------------------------------------------- emissions

    @staticmethod
    def _status_flags(st: _InstState) -> None:
        """Consolidated session status flags (sorted venue order)."""
        halt = auction = False
        for vid in sorted(st.cons.books):
            status = st.cons.books[vid].status
            halt = halt or status == SessionStatus.HALT
            auction = auction or status == SessionStatus.AUCTION
        st.halt, st.auction = halt, auction

    def _emit(self, st: _InstState, t: int) -> FeatureVector:
        st.t = t
        st.trim_all(t)
        self._status_flags(st)
        # L1 order counts across non-stale venues quoting the best price
        st.oc_bid1 = st.oc_ask1 = 0
        if st.book_ok:
            for vid, _, _, stale in st.venue_rows:
                if stale:
                    continue
                book = st.cons.books[vid]
                for p, c in book.order_count(0, 10):
                    if p == st.bid_p:
                        st.oc_bid1 += c
                        break
                for p, c in book.order_count(1, 10):
                    if p == st.ask_p:
                        st.oc_ask1 += c
                        break

        values: List[float] = []
        valid: List[bool] = []
        for fam, compute in self.families:
            before = len(values)
            compute(st, values, valid)
            got = len(values) - before
            if got != self._fam_sizes[fam]:
                raise RuntimeError(
                    f"family {fam} appended {got} values, "
                    f"registry says {self._fam_sizes[fam]}"
                )
        vec = FeatureVector(
            instrument_id=st.ctx.instrument_id,
            timestamp=t,
            feature_version=self.feature_version,
            values=values,
            validity=valid,
        )
        # fold this emission's observations into the session profile
        # (after all reads — normalizations never see their own value)
        bucket = SessionProfile.bucket_of(t, st.ctx.clock.offset_seconds(t))
        for m in timeofday.PROFILE_METRICS:
            cur = timeofday._metric_value(st, m)
            if cur is not None:
                st.profile.update(m, bucket, float(cur))
        self.vectors_emitted += 1
        if self.on_vector is not None:
            self.on_vector(vec)
        return vec

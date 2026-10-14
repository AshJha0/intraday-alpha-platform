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
- **Same-timestamp coalescing** (v1.14, D-P1-1; API_FEATURES §2.3): the
  events of one instrument that share an ``exchange_ts`` are ONE market
  state change.  ``apply(ev, group_end=False)`` applies the event to the
  book and folds its flow (trades, event counts, venue updates) but does
  not refresh, sample or emit; the event with ``group_end=True`` closes the
  group: one refresh over every venue the group touched, one set of
  samples, one emission check.  The effective spread of a trade in the
  group is measured against the merged mid BEFORE the group (the refresh
  has not run), not against a book the trade's own EXECUTE already moved.
  :func:`group_ends` marks the last event of each group of a stream;
  :meth:`FeatureEngine.run` coalesces by default.  ``apply(ev)`` alone
  (``group_end=True`` on every event) is the per-event legacy semantics
  the streaming MVP and the alpha golden frames still use.
- Validity: a feature is invalid during warmup, when the book is not ok
  (for book-derived features), or when its inputs are undefined — including
  every ``x / (y + EPS)`` ratio whose denominator ``y`` is <= 0 (an
  unreliable denominator makes the ratio undefined, never huge).  NaN never
  appears with valid=True (enforced by the single value funnel).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from math import log

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


def group_ends(events: Iterable[MarketEvent]) -> list[bool]:
    """``group_end`` flag of every event of a stream (pinned, D-P1-1).

    An event closes its group when the NEXT event of the same instrument
    has a different ``exchange_ts`` (or there is none): consecutive events
    of one instrument's sub-stream sharing a timestamp form one group,
    whatever other instruments' events are interleaved between them.
    """
    evs = events if isinstance(events, list) else list(events)
    out = [True] * len(evs)
    nxt: dict[int, int] = {}
    for i in range(len(evs) - 1, -1, -1):
        ev = evs[i]
        iid = ev.instrument_id
        nts = nxt.get(iid)
        out[i] = nts is None or nts != ev.exchange_ts
        nxt[iid] = ev.exchange_ts
    return out


def iter_group_ends(events: Iterable[MarketEvent]) -> Iterator[tuple[MarketEvent, bool]]:
    """Streaming :func:`group_ends`: yields ``(event, group_end)`` in order
    while holding only the events of the current ``exchange_ts``.

    Exact for a stream in canonical order (``exchange_ts`` non-decreasing,
    as every normalized file is): an instrument's group then cannot resume
    once the timestamp has moved on, so within one timestamp block an event
    closes its group iff it is that instrument's last event of the block."""
    block: list[MarketEvent] = []
    for ev in events:
        if block and ev.exchange_ts != block[0].exchange_ts:
            yield from _flush_block(block)
            block = []
        block.append(ev)
    if block:
        yield from _flush_block(block)


def _flush_block(block: list[MarketEvent]) -> Iterator[tuple[MarketEvent, bool]]:
    last: dict[int, int] = {}
    for i, ev in enumerate(block):
        last[ev.instrument_id] = i
    for i, ev in enumerate(block):
        yield ev, last[ev.instrument_id] == i


_BOOK_TOUCH = frozenset(
    {
        EventType.ADD,
        EventType.MODIFY,
        EventType.CANCEL,
        EventType.EXECUTE,
        EventType.QUOTE,
    }
)


@dataclass
class FeatureVector:
    """FeatureVector contract (schemas/features/feature_vector.schema.json)."""

    instrument_id: int
    timestamp: int
    feature_version: str
    values: list[float]
    validity: list[bool]

    def validity_bits(self) -> bytes:
        """Validity as a little-endian-bit-packed bitset (bit i = feature i)."""
        n = len(self.validity)
        out = bytearray((n + 7) // 8)
        for i, v in enumerate(self.validity):
            if v:
                out[i >> 3] |= 1 << (i & 7)
        return bytes(out)

    @staticmethod
    def unpack_bits(bits: bytes, n: int) -> list[bool]:
        """Inverse of :meth:`validity_bits`."""
        return [bool(bits[i >> 3] >> (i & 7) & 1) for i in range(n)]


class _InstState:
    """All rolling per-instrument state (the ``st`` family modules read)."""

    def __init__(self, ctx: InstrumentContext, profile: SessionProfile) -> None:
        self.ctx = ctx
        self.tick = ctx.tick_size
        self.cons = ConsolidatedBook(ctx.instrument_id)
        self.ref: _InstState = self  # rewired by the engine
        self.profile = profile
        self.first_ts: int | None = None
        #: warmup anchor: max(first event ts, last stale->fresh recovery ts)
        self.warm_ts: int | None = None
        #: event time of the last stale->fresh recovery (None: never stale)
        self.recovered_ts: int | None = None
        #: number of stale->fresh recoveries (rolling state resets)
        self.recoveries = 0
        #: sorted ids of this instrument's venues whose book is stale, as of
        #: the last group close (refresh decisions compare against it)
        self.stale_venues: tuple = ()
        #: the same set after the latest event (inside an open group)
        self.stale_running: tuple = ()
        #: an event of the open group changed the stale set / left a venue
        #: stale: a venue that goes stale and recovers within one timestamp
        #: (a sequence gap repaired by a same-timestamp SNAPSHOT) is still a
        #: stale->fresh recovery — a message was lost (D-P1-1)
        self.stale_touched = False
        self.stale_seen = False
        #: open same-timestamp group (D-P1-1): a sampling refresh is due /
        #: venues whose depth cache the closing refresh must re-read
        self.pending_refresh = False
        self.pending_venues: set[int] = set()
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
        self.last_emit: int | None = None
        # current merged book view (refreshed on book-touching events)
        self.book_ok = False
        self.depth_bid: list = []
        self.depth_ask: list = []
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
        self.venue_rows: list = []  # [(vid, bid10, ask10, stale)] sorted
        self.venue_cache: dict[int, tuple] = {}  # vid -> (bid10, ask10)
        self.venue_last_ts: dict[int, int] = {}
        # rolling structures
        self.hist2 = TimeSeries()  # mid2 (int) at mid changes
        self.histlog = TimeSeries()  # ln(mid2) at mid changes
        self.rv = {w: RollingSum(WINDOW_NS[w], 3) for w in ("10s", "1m", "5m")}
        self.ext_mid = {w: RollingExtrema(WINDOW_NS[w]) for w in ("10s", "1m", "5m")}
        self.ext_absdlm = RollingExtrema(WINDOW_NS["1m"])
        self.jumps = RollingSum(WINDOW_NS["5m"], 1)
        self.midstat = {w: RollingSum(WINDOW_NS[w], 2) for w in ("10s", "1m", "5m")}
        self.ofi = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "5s", "30s")}
        self.depthavg = {w: RollingSum(WINDOW_NS[w], 12) for w in ("1s", "10s", "1m")}
        self.trades = {w: RollingSum(WINDOW_NS[w], 6) for w in ("1s", "10s", "1m")}
        #: side-less cross prints (opening / closing / halt crosses, D-P2-6)
        self.xprints = {w: RollingSum(WINDOW_NS[w], 1) for w in ("1s", "10s", "1m")}
        self.evstats = {w: RollingSum(WINDOW_NS[w], 7) for w in ("1s", "10s", "1m")}
        self.queue = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "10s")}
        self.xc = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.xl = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.venue_updates = RollingKeyCount(WINDOW_NS["10s"])

    # ------------------------------------------------------ family accessors

    def traded_volume(self, w: str) -> int:
        """Traded quantity over window w: continuous TRADE prints plus the
        side-less cross prints folded in by :meth:`FeatureEngine.on_cross_print`
        (D-P2-6; zero on a stream without them, e.g. every synthetic one).
        Volume, participation, the volume clock and the volume profile read
        this; signed flow (signed volume, trade imbalance) never sees a cross."""
        return self.trades[w].sums[1] + self.xprints[w].sums[0]

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
        return self.book_ok and not self.stale_venues and not self.halt and not self.auction

    def mid2_at(self, ts: int) -> int | None:
        """Latest mid2 sample at-or-before ts (None during warmup)."""
        return self.hist2.at_or_before(ts)

    def logmid_at(self, ts: int) -> float | None:
        """Latest ln(mid2) sample at-or-before ts."""
        return self.histlog.at_or_before(ts)

    def rvol(self, w: str) -> float | None:
        """Realized vol over window w (None until the window is warm)."""
        if not self.warm(WINDOW_NS[w]):
            return None
        # max() guards against tiny negative float drift in a drained window
        return (max(self.rv[w].sums[0], 0.0) / (WINDOW_NS[w] / 1e9)) ** 0.5

    def ref_ret_log(self, h_ns: int) -> float | None:
        """Reference-instrument log mid return over h (at-or-before reads)."""
        ref = self.ref
        now = ref.logmid_at(self.t)
        past = ref.logmid_at(self.t - h_ns)
        if now is None or past is None:
            return None
        return now - past

    def ref_ret_log_strict(self, h_ns: int) -> float | None:
        """SENSITIVITY ONLY (not the default, never wired into a feature):
        :meth:`ref_ret_log` with strictly-before reads, so a reference
        update stamped at the SAME nanosecond as this instrument's event is
        excluded.  The pinned feature keeps at-or-before (ties included)."""
        ref = self.ref
        now = ref.histlog.at_or_before(self.t - 1)
        past = ref.histlog.at_or_before(self.t - h_ns - 1)
        if now is None or past is None:
            return None
        return now - past

    def beta_w5m(self) -> float | None:
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
        self.xprints = {w: RollingSum(WINDOW_NS[w], 1) for w in ("1s", "10s", "1m")}
        self.evstats = {w: RollingSum(WINDOW_NS[w], 7) for w in ("1s", "10s", "1m")}
        self.queue = {w: RollingSum(WINDOW_NS[w], 4) for w in ("1s", "10s")}
        self.xc = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.xl = {w: RollingSum(WINDOW_NS[w], 5) for w in ("1m", "5m")}
        self.venue_updates = RollingKeyCount(WINDOW_NS["10s"])

    def trim_all(self, t: int) -> None:
        """Evict expired samples from every rolling structure at time t."""
        for group in (
            self.rv,
            self.midstat,
            self.ofi,
            self.depthavg,
            self.trades,
            self.xprints,
            self.evstats,
            self.queue,
            self.xc,
            self.xl,
        ):
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
        contexts: dict[int, InstrumentContext],
        cadence_ns: int = 0,
        on_vector: Callable[[FeatureVector], None] | None = None,
        profiles: dict[int, SessionProfile] | None = None,
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
        self._fam_sizes = {fam: len(family_module(fam).specs()) for fam in FAMILY_ORDER}
        self.states: dict[int, _InstState] = {}
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
        #: side-less cross prints folded into volume (D-P2-6) / dropped
        self.cross_prints_folded = 0
        self.cross_prints_dropped = 0

    # ---------------------------------------------------------------- states

    def _state(self, instrument_id: int) -> _InstState:
        st = self.states.get(instrument_id)
        if st is None:
            ctx = self.contexts.get(instrument_id)
            if ctx is None:
                raise ValueError(f"no InstrumentContext for instrument {instrument_id}")
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

    def apply(self, ev: MarketEvent, group_end: bool = True) -> FeatureVector | None:
        """Apply one event; returns the emitted FeatureVector, if any.

        ``group_end=False`` (D-P1-1, module docs) leaves the instrument's
        same-timestamp group open: the event reaches the book and the flow
        windows, but the refresh, the samples and the emission wait for the
        event that closes the group (:func:`group_ends`).

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
        # The venue whose event changed the set is re-read by the refresh.
        stale_now = tuple(vid for vid in sorted(st.cons.books) if st.cons.books[vid].stale)
        if stale_now != st.stale_running:
            st.pending_venues.add(ev.venue_id)
            st.stale_running = stale_now
            st.stale_touched = True
        if stale_now:
            st.stale_seen = True

        if status != ApplyStatus.APPLIED:
            st.events_dropped += 1
            self.events_dropped += 1
            return self._close_group(st, t, group_end)
        st.venue_last_ts[ev.venue_id] = t
        st.venue_updates.add(t, ev.venue_id)

        et: int | None = ev.event_type
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
        if et in _BOOK_TOUCH or (et == EventType.SNAPSHOT and ev.trade_id == 0):
            st.pending_refresh = True
            st.pending_venues.add(ev.venue_id)
        return self._close_group(st, t, group_end)

    def on_cross_print(self, instrument_id: int, ts: int, qty: int) -> bool:
        """Fold a side-less cross print (opening / closing / halt cross,
        ITCH ``Q``) into the instrument's VOLUME windows (D-P2-6).

        A cross print is not an event of the stream (it has no aggressor and
        rests nowhere, so the book and the native engines never see it); the
        feature pipeline calls this at its exchange_ts, before the first
        event after it.  It counts in ``traded_volume_*``, ``participation_*``,
        the volume profile and the volume clock, never in signed flow, trade
        counts or the effective spread.  A print before the instrument's last
        timestamp, or with a quantity outside (0, FEATURE_MAX_QTY], is dropped
        and counted.  Returns True when folded."""
        st = self._state(instrument_id)
        if qty <= 0 or qty > FEATURE_MAX_QTY or (st.first_ts is not None and ts < st.last_ts):
            self.cross_prints_dropped += 1
            return False
        for win in st.xprints.values():
            win.add(ts, (qty,))
        self.cross_prints_folded += 1
        return True

    def _group_event(self, st: _InstState) -> None:
        """Hook: an event that leaves its group open (subclasses observe it)."""

    def _close_group(self, st: _InstState, t: int, group_end: bool) -> FeatureVector | None:
        """Close the instrument's same-timestamp group (D-P1-1): one refresh
        over the venues the group touched, then the emission check.  With
        ``group_end=False`` the group stays open and nothing is emitted."""
        if not group_end:
            self._group_event(st)
            return None
        stale_now = st.stale_running
        stale_changed = st.stale_touched
        just_recovered = (bool(st.stale_venues) or st.stale_seen) and not stale_now
        st.stale_venues = stale_now
        st.stale_touched = st.stale_seen = False
        venues = sorted(st.pending_venues)
        if st.pending_refresh:
            self._refresh_book(st, venues, t, just_recovered)
        elif stale_changed:
            self._refresh_book(st, venues, t, just_recovered, samples=False)
        st.pending_refresh = False
        st.pending_venues.clear()
        return self._maybe_emit(st, t)

    def _maybe_emit(self, st: _InstState, t: int) -> FeatureVector | None:
        """Cadence check (pure event time); emits at most one vector."""
        if self.cadence_ns == 0 or st.last_emit is None or t - st.last_emit >= self.cadence_ns:
            vec = self._emit(st, t)
            st.last_emit = t
            return vec
        return None

    def run(self, events: Iterable[MarketEvent], coalesce: bool = True) -> dict:
        """Apply an event stream; returns summary stats.  ``coalesce``
        (default) closes same-timestamp groups at their last event (D-P1-1);
        ``False`` is the per-event legacy semantics."""
        evs = list(events)
        ends = group_ends(evs) if coalesce else [True] * len(evs)
        for ev, end in zip(evs, ends, strict=True):
            self.apply(ev, end)
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
        # st.mid / st.book_ok are the merged view as of the last group close:
        # inside a coalesced group that is the PRE-group mid (D-P1-1), so a
        # trade is not measured against the book its own EXECUTE moved.
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

    def _refresh_book(
        self,
        st: _InstState,
        venue_ids: Iterable[int],
        t: int,
        just_recovered: bool = False,
        samples: bool = True,
    ) -> None:
        """Recompute the merged view (pinned, API_FEATURES §2).

        ``samples=False`` is a *staleness refresh*: the merged view and
        ``book_ok`` are recomputed because the set of stale venues changed,
        but no OFI / queue / depth / mid sample is recorded — a venue
        appearing in or disappearing from the merged view is a data
        availability event, not order flow.
        """
        cons = st.cons
        st.refresh_seq += 1
        for venue_id in venue_ids:  # ascending: the venues the group touched
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
        agg_b: dict[int, int] = {}
        agg_a: dict[int, int] = {}
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
        sample_flow = samples and (not just_recovered) and bool(prev_bid or prev_ask or bid or ask)
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
        for bk, ak in ((st.db1, st.da1), (st.db3, st.da3), (st.db5, st.da5), (st.db10, st.da10)):
            imb.append((bk - ak) / (bk + ak) if bk + ak > 0 else 0.0)
        dvals = (
            st.db1,
            st.da1,
            st.db5,
            st.da5,
            st.db10,
            st.da10,
            imb[0],
            imb[1],
            imb[2],
            imb[3],
            st.spread_ticks,
            st.db10 + st.da10,
        )
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
                    pair = (x, y1 - y0, x * x, (y1 - y0) * (y1 - y0), x * (y1 - y0))
                    for win in st.xc.values():
                        win.add(t, pair)
                    yl0 = ref.logmid_at(t - 2 * WINDOW_NS["1s"])
                    if yl0 is not None:
                        yl = y0 - yl0
                        lpair = (x, yl, x * x, yl * yl, x * yl)
                        for win in st.xl.values():
                            win.add(t, lpair)

    @staticmethod
    def _delta(prev: list, curr: list, k: int) -> int:
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
    def _queue_delta(prev: list, curr: list, is_bid: bool) -> tuple:
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

        values: list[float] = []
        valid: list[bool] = []
        for fam, compute in self.families:
            before = len(values)
            compute(st, values, valid)
            got = len(values) - before
            if got != self._fam_sizes[fam]:
                raise RuntimeError(
                    f"family {fam} appended {got} values, registry says {self._fam_sizes[fam]}"
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

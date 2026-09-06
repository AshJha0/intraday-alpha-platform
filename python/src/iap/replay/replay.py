"""Deterministic event-time replay driving order books (spec sections 9, 18).

The engine consumes normalized events in event-time order (exchange_ts,
event_id — the order normalized files are written in), routes each event to
its per-venue book inside a per-instrument ``ConsolidatedBook``, and:

- emits book-state snapshots every ``snapshot_every`` events (delivered to an
  ``on_snapshot`` callback; the latest ``keep_snapshots`` are retained in
  ``snapshots`` — bounded, pinned);
- can checkpoint its complete state every ``checkpoint_every`` events
  (``checkpoints`` keeps the latest ``keep_checkpoints``) or on demand via
  ``checkpoint()``;
- can be restored from any checkpoint with ``ReplayEngine.restore`` and,
  fed the remaining events, reaches bit-identical state (verified by the
  test suite): replay(all) == replay(prefix) -> checkpoint -> replay(rest);
- optionally validates instrument/venue ids against reference data
  (``refdata`` / ``universe``): events for unknown instruments or for venues
  not listed for the instrument are dropped + counted
  (``unknown_instrument_dropped`` / ``unknown_venue_dropped``) instead of
  silently creating books;
- ``reset_sequences()`` starts a new sequence epoch on every book (session
  roll known out of band); in-stream resets are handled by the books.

Determinism: no wall clock, no unordered iteration — all maps are walked in
sorted key order when serializing. Checkpoint JSON is cross-language
(x-version 2; see API_CORE.md section 5).
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Set

from iap.core.events import MarketEvent
from iap.orderbook.book import ConsolidatedBook

SnapshotCallback = Callable[[int, dict], None]

#: Engine checkpoint schema version.
ENGINE_CHECKPOINT_VERSION = 2


class ReplayEngine:
    """Event-time replay across all instruments/venues in a stream."""

    def __init__(
        self,
        checkpoint_every: int = 0,
        snapshot_every: int = 0,
        keep_checkpoints: int = 4,
        keep_snapshots: int = 4,
        reorder_window: int = 0,
        refdata=None,
        universe: Optional[Dict[int, Set[int]]] = None,
    ) -> None:
        if checkpoint_every < 0 or snapshot_every < 0:
            raise ValueError("checkpoint_every/snapshot_every must be >= 0")
        if keep_checkpoints < 0 or keep_snapshots < 0:
            raise ValueError("keep_checkpoints/keep_snapshots must be >= 0")
        if refdata is not None and universe is not None:
            raise ValueError("pass either refdata or universe, not both")
        self.books: Dict[int, ConsolidatedBook] = {}
        self.events_processed = 0
        self.checkpoint_every = checkpoint_every
        self.snapshot_every = snapshot_every
        self.keep_checkpoints = keep_checkpoints
        self.keep_snapshots = keep_snapshots
        self.reorder_window = reorder_window
        self.checkpoints: List[dict] = []
        self.snapshots: List[dict] = []
        self.snapshots_emitted = 0
        self.time_regressions = 0
        self.unknown_instrument_dropped = 0
        self.unknown_venue_dropped = 0
        self._last_exchange_ts = 0
        # instrument_id -> allowed venue ids (None: accept everything).
        self.universe: Optional[Dict[int, Set[int]]] = None
        if refdata is not None:
            self.universe = {
                inst.instrument_id: {refdata.venue(v).venue_id for v in inst.venues}
                for inst in refdata.instruments()
            }
        elif universe is not None:
            self.universe = {int(k): set(v) for k, v in universe.items()}

    # -------------------------------------------------------------- applying

    def instrument_book(self, instrument_id: int) -> ConsolidatedBook:
        book = self.books.get(instrument_id)
        if book is None:
            book = ConsolidatedBook(instrument_id, self.reorder_window)
            self.books[instrument_id] = book
        return book

    def apply(self, ev: MarketEvent) -> None:
        """Apply one event; tracks event-time monotonicity and the universe."""
        if ev.exchange_ts < self._last_exchange_ts:
            self.time_regressions += 1
        self._last_exchange_ts = ev.exchange_ts
        self.events_processed += 1
        if self.universe is not None:
            venues = self.universe.get(ev.instrument_id)
            if venues is None:
                self.unknown_instrument_dropped += 1
                return
            if ev.venue_id not in venues:
                self.unknown_venue_dropped += 1
                return
        self.instrument_book(ev.instrument_id).apply(ev)

    def reset_sequences(self) -> None:
        """Start a new sequence epoch on every book (session roll / restart)."""
        for iid in sorted(self.books):
            self.books[iid].reset_sequences()

    def run(
        self,
        events: Iterable[MarketEvent],
        on_snapshot: Optional[SnapshotCallback] = None,
    ) -> dict:
        """Replay an event stream; returns summary stats."""
        for ev in events:
            self.apply(ev)
            if (
                self.snapshot_every
                and self.events_processed % self.snapshot_every == 0
            ):
                snap = self.book_states()
                snap["index"] = self.events_processed
                self.snapshots_emitted += 1
                self.snapshots.append(snap)
                if len(self.snapshots) > self.keep_snapshots:
                    self.snapshots.pop(0)
                if on_snapshot is not None:
                    on_snapshot(self.events_processed, snap)
            if (
                self.checkpoint_every
                and self.events_processed % self.checkpoint_every == 0
            ):
                self.checkpoints.append(self.checkpoint())
                if len(self.checkpoints) > self.keep_checkpoints:
                    self.checkpoints.pop(0)
        return {
            "events_processed": self.events_processed,
            "instruments": len(self.books),
            "time_regressions": self.time_regressions,
            "snapshots": self.snapshots_emitted,
            "unknown_instrument_dropped": self.unknown_instrument_dropped,
            "unknown_venue_dropped": self.unknown_venue_dropped,
        }

    # ---------------------------------------------------------- serialization

    def book_states(self) -> dict:
        """Exact-integer per-venue state summaries (deterministic key order)."""
        out: dict = {"instruments": {}}
        for iid in sorted(self.books):
            cons = self.books[iid]
            out["instruments"][str(iid)] = {
                str(vid): cons.books[vid].state_summary()
                for vid in sorted(cons.books)
            }
        return out

    def checkpoint(self) -> dict:
        """Full JSON-able engine state; restore() rebuilds it identically."""
        return {
            "x-version": ENGINE_CHECKPOINT_VERSION,
            "events_processed": self.events_processed,
            "last_exchange_ts": self._last_exchange_ts,
            "time_regressions": self.time_regressions,
            "unknown_instrument_dropped": self.unknown_instrument_dropped,
            "unknown_venue_dropped": self.unknown_venue_dropped,
            "checkpoint_every": self.checkpoint_every,
            "snapshot_every": self.snapshot_every,
            "keep_checkpoints": self.keep_checkpoints,
            "keep_snapshots": self.keep_snapshots,
            "snapshots_emitted": self.snapshots_emitted,
            "reorder_window": self.reorder_window,
            "universe": (
                None
                if self.universe is None
                else {str(k): sorted(v) for k, v in sorted(self.universe.items())}
            ),
            "books": {
                str(iid): self.books[iid].checkpoint() for iid in sorted(self.books)
            },
        }

    @classmethod
    def restore(cls, cp: dict) -> "ReplayEngine":
        """Rebuild an engine from ``checkpoint()`` output."""
        if cp.get("x-version") != ENGINE_CHECKPOINT_VERSION:
            raise ValueError(
                f"unsupported engine checkpoint x-version: {cp.get('x-version')!r}"
            )
        universe = cp["universe"]
        engine = cls(
            checkpoint_every=cp["checkpoint_every"],
            snapshot_every=cp["snapshot_every"],
            keep_checkpoints=cp["keep_checkpoints"],
            keep_snapshots=cp["keep_snapshots"],
            reorder_window=cp["reorder_window"],
            universe=None if universe is None else {int(k): set(v) for k, v in universe.items()},
        )
        engine.events_processed = cp["events_processed"]
        engine._last_exchange_ts = cp["last_exchange_ts"]
        engine.time_regressions = cp["time_regressions"]
        engine.unknown_instrument_dropped = cp["unknown_instrument_dropped"]
        engine.unknown_venue_dropped = cp["unknown_venue_dropped"]
        engine.snapshots_emitted = cp["snapshots_emitted"]
        for iid, bcp in cp["books"].items():
            engine.books[int(iid)] = ConsolidatedBook.restore(bcp)
        return engine

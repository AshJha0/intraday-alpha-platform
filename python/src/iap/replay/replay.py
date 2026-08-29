"""Deterministic event-time replay driving order books (spec sections 9, 18).

The engine consumes normalized events in event-time order (exchange_ts,
event_id — the order normalized files are written in), routes each event to
its per-venue book inside a per-instrument ``ConsolidatedBook``, and:

- emits book-state snapshots every ``snapshot_every`` events (collected in
  ``snapshots`` and/or delivered to an ``on_snapshot`` callback);
- can checkpoint its complete state every ``checkpoint_every`` events
  (``checkpoints`` keeps the latest few) or on demand via ``checkpoint()``;
- can be restored from any checkpoint with ``ReplayEngine.restore`` and,
  fed the remaining events, reaches bit-identical state (verified by the
  test suite): replay(all) == replay(prefix) -> checkpoint -> replay(rest).

Determinism: no wall clock, no unordered iteration — all maps are walked in
sorted key order when serializing.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from iap.core.events import MarketEvent
from iap.orderbook.book import ConsolidatedBook

SnapshotCallback = Callable[[int, dict], None]


class ReplayEngine:
    """Event-time replay across all instruments/venues in a stream."""

    def __init__(
        self,
        checkpoint_every: int = 0,
        snapshot_every: int = 0,
        keep_checkpoints: int = 4,
    ) -> None:
        if checkpoint_every < 0 or snapshot_every < 0:
            raise ValueError("checkpoint_every/snapshot_every must be >= 0")
        self.books: Dict[int, ConsolidatedBook] = {}
        self.events_processed = 0
        self.checkpoint_every = checkpoint_every
        self.snapshot_every = snapshot_every
        self.keep_checkpoints = keep_checkpoints
        self.checkpoints: List[dict] = []
        self.snapshots: List[dict] = []
        self.time_regressions = 0
        self._last_exchange_ts = 0

    # -------------------------------------------------------------- applying

    def instrument_book(self, instrument_id: int) -> ConsolidatedBook:
        book = self.books.get(instrument_id)
        if book is None:
            book = ConsolidatedBook(instrument_id)
            self.books[instrument_id] = book
        return book

    def apply(self, ev: MarketEvent) -> None:
        """Apply one event; tracks event-time monotonicity."""
        if ev.exchange_ts < self._last_exchange_ts:
            self.time_regressions += 1
        self._last_exchange_ts = ev.exchange_ts
        self.instrument_book(ev.instrument_id).apply(ev)
        self.events_processed += 1

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
                self.snapshots.append(snap)
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
            "snapshots": len(self.snapshots),
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
            "events_processed": self.events_processed,
            "last_exchange_ts": self._last_exchange_ts,
            "time_regressions": self.time_regressions,
            "checkpoint_every": self.checkpoint_every,
            "snapshot_every": self.snapshot_every,
            "keep_checkpoints": self.keep_checkpoints,
            "books": {
                str(iid): self.books[iid].checkpoint() for iid in sorted(self.books)
            },
        }

    @classmethod
    def restore(cls, cp: dict) -> "ReplayEngine":
        """Rebuild an engine from ``checkpoint()`` output."""
        engine = cls(
            checkpoint_every=cp["checkpoint_every"],
            snapshot_every=cp["snapshot_every"],
            keep_checkpoints=cp["keep_checkpoints"],
        )
        engine.events_processed = cp["events_processed"]
        engine._last_exchange_ts = cp["last_exchange_ts"]
        engine.time_regressions = cp["time_regressions"]
        for iid, bcp in cp["books"].items():
            engine.books[int(iid)] = ConsolidatedBook.restore(bcp)
        return engine

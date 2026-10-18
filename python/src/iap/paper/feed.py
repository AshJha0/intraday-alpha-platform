"""Feed resilience for the paper recorder and paper loop (REL-18).

:class:`FeedGuard` sits between a sequenced market-data feed and its consumer
(:class:`iap.paper.recorder.PaperRecorder` via :class:`GuardedRecorder`, or the
paper loop).  For each message ``(seq, payload, mono_ns)`` it:

* drops a **duplicate** (``seq`` already delivered or already buffered);
* buffers an **out-of-order** message up to ``reorder_window`` sequence numbers
  ahead and releases it in order once the hole fills;
* declares a **gap** when the hole is still open after the window, and applies
  the declared :class:`GapPolicy`:

  - ``HALT``   raise :class:`FeedGapError` (the run stops; default),
  - ``RESYNC`` report UNKNOWN health, call ``on_gap(expected, got)`` (request a
    snapshot) and hold messages until :meth:`FeedGuard.resync` names the next
    sequence number; held messages the snapshot covers are dropped,
  - ``SKIP``   record the gap and continue from the first buffered message.

* checks the **clock** (:class:`ClockGuard`): the monotonic clock may not run
  backwards and the wall clock may not drift from the start-time mapping by
  more than ``tolerance_ns``; either raises :class:`ClockDriftError`;
* tracks **staleness**: :meth:`FeedGuard.check_stale` reports UNKNOWN to the
  health sink when no message arrived for ``stale_after_ns``; the next message
  reports OK again.

The health interface is deliberately minimal (:class:`HealthSink`,
``set_state(component, state, reason)`` with states ``OK`` / ``UNKNOWN``) so the
REL-16 monitor can implement it without importing this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from iap.paper.recorder import ClockMapping

OK, UNKNOWN = "OK", "UNKNOWN"


class FeedError(RuntimeError):
    """Base class of feed-resilience errors."""


class FeedGapError(FeedError):
    def __init__(self, expected: int, got: int) -> None:
        super().__init__(f"sequence gap: expected {expected}, got {got}")
        self.expected, self.got = expected, got


class ClockDriftError(FeedError):
    """Monotonic clock backwards, or wall clock drift beyond tolerance."""


class GapPolicy(str, Enum):
    HALT = "HALT"
    RESYNC = "RESYNC"
    SKIP = "SKIP"


class HealthSink(Protocol):
    """What REL-16 monitoring implements to receive feed health."""

    def set_state(self, component: str, state: str, reason: str) -> None: ...


class ClockGuard:
    """Checks ``wall - mapping.to_wall(mono)`` stays within ``tolerance_ns``."""

    def __init__(
        self,
        mapping: ClockMapping,
        tolerance_ns: int,
        wall: Callable[[], int] = time.time_ns,
    ) -> None:
        self.mapping = mapping
        self.tolerance_ns = int(tolerance_ns)
        self.wall = wall
        self.last_mono = -(1 << 62)
        self.max_abs_drift_ns = 0

    def check(self, mono_ns: int, wall_ns: int | None = None) -> int:
        """Return the drift in ns; raise :class:`ClockDriftError` past tolerance."""
        if mono_ns < self.last_mono:
            raise ClockDriftError(f"monotonic clock went backwards ({mono_ns} < {self.last_mono})")
        self.last_mono = mono_ns
        w = self.wall() if wall_ns is None else wall_ns
        drift = w - self.mapping.to_wall(mono_ns)
        self.max_abs_drift_ns = max(self.max_abs_drift_ns, abs(drift))
        if abs(drift) > self.tolerance_ns + self.mapping.uncertainty_ns:
            raise ClockDriftError(
                f"wall clock drift {drift} ns exceeds tolerance {self.tolerance_ns} ns"
            )
        return drift


@dataclass
class FeedStats:
    delivered: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)  # [first missing, last missing]
    dropped_resync: int = 0
    stale_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "gaps": [list(g) for g in self.gaps],
            "missing": sum(b - a + 1 for a, b in self.gaps),
            "dropped_resync": self.dropped_resync,
            "stale_events": self.stale_events,
        }


class FeedGuard:
    def __init__(
        self,
        *,
        policy: GapPolicy = GapPolicy.HALT,
        reorder_window: int = 0,
        first_seq: int | None = None,
        stale_after_ns: int | None = None,
        clock: ClockGuard | None = None,
        health: HealthSink | None = None,
        on_gap: Callable[[int, int], None] | None = None,
        component: str = "feed",
    ) -> None:
        self.policy = GapPolicy(policy)
        self.reorder_window = int(reorder_window)
        self.next_seq = first_seq
        self.stale_after_ns = stale_after_ns
        self.clock = clock
        self.health = health
        self.on_gap = on_gap
        self.component = component
        self.buffer: dict[int, Any] = {}
        self.stats = FeedStats()
        self.state = OK
        self.resyncing = False
        self.last_msg_mono: int | None = None

    def _set(self, state: str, reason: str) -> None:
        if state != self.state:
            self.state = state
            if self.health is not None:
                self.health.set_state(self.component, state, reason)

    # ----------------------------------------------------------------- input
    def on_message(self, seq: int, payload: Any, mono_ns: int) -> list[Any]:
        """Payloads now deliverable, in sequence order."""
        if self.clock is not None:
            self.clock.check(mono_ns)
        self.last_msg_mono = mono_ns
        if self.resyncing:  # hold until the snapshot says where to resume
            if seq in self.buffer:
                self.stats.duplicates += 1
            else:
                self.buffer[seq] = payload
            return []
        if self.state == UNKNOWN:
            self._set(OK, "feed resumed")
        if self.next_seq is None:
            self.next_seq = seq
        if seq < self.next_seq or seq in self.buffer:
            self.stats.duplicates += 1
            return []
        if seq > self.next_seq:
            self.stats.out_of_order += 1
            self.buffer[seq] = payload
            if seq - self.next_seq > self.reorder_window:
                return self._gap()
            return []
        out = [payload]
        self.next_seq += 1
        out += self._drain()
        self.stats.delivered += len(out)
        return out

    def _drain(self) -> list[Any]:
        out = []
        assert self.next_seq is not None
        while self.next_seq in self.buffer:
            out.append(self.buffer.pop(self.next_seq))
            self.next_seq += 1
        return out

    def _gap(self) -> list[Any]:
        assert self.next_seq is not None
        first = min(self.buffer)
        expected = self.next_seq
        self.stats.gaps.append((expected, first - 1))
        if self.policy is GapPolicy.HALT:
            raise FeedGapError(expected, first)
        if self.policy is GapPolicy.RESYNC:
            self.resyncing = True
            self._set(UNKNOWN, f"sequence gap {expected}..{first - 1}; resync requested")
            if self.on_gap is not None:
                self.on_gap(expected, first)
            return []
        self.next_seq = first
        out = self._drain()
        self.stats.delivered += len(out)
        return out

    def resync(self, next_seq: int) -> list[Any]:
        """Snapshot applied up to ``next_seq - 1``: drop what it covers and
        deliver the held messages from ``next_seq`` on."""
        self.next_seq = int(next_seq)
        self.resyncing = False
        for s in [s for s in self.buffer if s < self.next_seq]:
            del self.buffer[s]
            self.stats.dropped_resync += 1
        self._set(OK, f"resynced at {next_seq}")
        out = self._drain()
        self.stats.delivered += len(out)
        return out

    def flush(self) -> list[Any]:
        """End of feed: an open hole with buffered messages is a gap."""
        return self._gap() if self.buffer and not self.resyncing else []

    def check_stale(self, mono_ns: int) -> bool:
        """``True`` (and UNKNOWN health) when the feed is silent past ``stale_after_ns``."""
        if self.stale_after_ns is None or self.last_msg_mono is None:
            return False
        if mono_ns - self.last_msg_mono > self.stale_after_ns:
            if self.state != UNKNOWN:
                self.stats.stale_events += 1
            self._set(UNKNOWN, f"no message for {(mono_ns - self.last_msg_mono) // 1_000_000} ms")
            return True
        return False


class GuardedRecorder:
    """Feed a :class:`~iap.paper.recorder.PaperRecorder` through a :class:`FeedGuard`.

    Delivered events are recorded with the arrival ``mono_ns`` of the message
    that released them (a released out-of-order event is stamped when it became
    deliverable, never earlier).
    """

    def __init__(self, recorder: Any, guard: FeedGuard) -> None:
        self.recorder = recorder
        self.guard = guard

    def on_message(self, seq: int, event: Any, mono_ns: int) -> list[Any]:
        return [
            self.recorder.record_market_event(ev, mono_ns=mono_ns)
            for ev in self.guard.on_message(seq, event, mono_ns)
        ]

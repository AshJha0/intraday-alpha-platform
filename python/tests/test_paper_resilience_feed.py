"""REL-18 feed resilience: duplicate, out-of-order and gap injection, clock checks,
stale-feed health, and the guarded recorder."""

from __future__ import annotations

import random

import pytest
from iap.core.events import MarketEvent
from iap.paper.feed import (
    OK,
    UNKNOWN,
    ClockDriftError,
    ClockGuard,
    FeedGapError,
    FeedGuard,
    GapPolicy,
    GuardedRecorder,
)
from iap.paper.recorder import ClockMapping, PaperRecorder

MS = 1_000_000


class Health:
    def __init__(self):
        self.log = []

    def set_state(self, component, state, reason):
        self.log.append((component, state))


def _feed(g, seqs):
    out = []
    for i, s in enumerate(seqs):
        out += g.on_message(s, s, mono_ns=i * MS)
    return out


def test_in_order_passthrough():
    g = FeedGuard()
    assert _feed(g, [5, 6, 7]) == [5, 6, 7]


def test_duplicates_dropped():
    g = FeedGuard(reorder_window=2)
    assert _feed(g, [1, 2, 2, 1, 4, 4, 3]) == [1, 2, 3, 4]
    assert g.stats.duplicates == 3


def test_out_of_order_within_window_reordered():
    g = FeedGuard(reorder_window=3)
    assert _feed(g, [1, 3, 4, 2, 5]) == [1, 2, 3, 4, 5]
    assert g.stats.out_of_order == 2 and not g.stats.gaps


def test_gap_halt_raises():
    g = FeedGuard(reorder_window=1)
    with pytest.raises(FeedGapError) as e:
        _feed(g, [1, 2, 5])
    assert (e.value.expected, e.value.got) == (3, 5)


def test_gap_skip_records_and_continues():
    g = FeedGuard(policy=GapPolicy.SKIP, reorder_window=1)
    assert _feed(g, [1, 2, 5, 6]) == [1, 2, 5, 6]
    assert g.stats.to_dict()["gaps"] == [[3, 4]] and g.stats.to_dict()["missing"] == 2


def test_gap_resync_unknown_then_ok():
    h, asked = Health(), []
    g = FeedGuard(
        policy=GapPolicy.RESYNC,
        reorder_window=0,
        health=h,
        on_gap=lambda exp, got: asked.append((exp, got)),
    )
    assert _feed(g, [1, 2, 6, 7, 8]) == [1, 2]
    assert asked == [(3, 6)] and g.state == UNKNOWN
    # snapshot covers up to 6; resume at 7
    assert g.resync(7) == [7, 8]
    assert g.state == OK and h.log == [("feed", UNKNOWN), ("feed", OK)]
    assert _feed(g, [9]) == [9]


def test_flush_reports_trailing_hole():
    g = FeedGuard(policy=GapPolicy.SKIP, reorder_window=5)
    assert _feed(g, [1, 3]) == [1]
    assert g.flush() == [3] and g.stats.gaps == [(2, 2)]


def test_random_injection_delivers_every_message_once_in_order():
    rng = random.Random(3)
    seqs = list(range(1, 2001))
    stream = []
    for s in seqs:
        stream.append(s)
        if rng.random() < 0.05:
            stream.append(rng.choice(stream))  # duplicate
    for i in range(0, len(stream) - 1):
        if rng.random() < 0.05:
            stream[i], stream[i + 1] = stream[i + 1], stream[i]  # swap
    g = FeedGuard(reorder_window=4, first_seq=1)
    out = _feed(g, stream) + g.flush()
    assert out == seqs and not g.stats.gaps


def test_random_gap_injection_skip_accounts_for_every_seq():
    rng = random.Random(4)
    stream = [s for s in range(1, 3001) if rng.random() > 0.01]
    g = FeedGuard(policy=GapPolicy.SKIP, reorder_window=2, first_seq=1)
    out = _feed(g, stream) + g.flush()
    assert out == stream
    assert len(stream) + g.stats.to_dict()["missing"] == stream[-1]


# ----------------------------------------------------------------- clock
def test_clock_drift_within_tolerance_and_beyond():
    m = ClockMapping(wall_ns0=1_000 * MS, mono_ns0=0, uncertainty_ns=0)
    c = ClockGuard(m, tolerance_ns=5 * MS)
    assert c.check(10 * MS, wall_ns=1_010 * MS + 4 * MS) == 4 * MS
    with pytest.raises(ClockDriftError):
        c.check(20 * MS, wall_ns=1_020 * MS - 6 * MS)


def test_clock_monotonic_backwards_raises():
    c = ClockGuard(ClockMapping(0, 0, 0), tolerance_ns=MS, wall=lambda: 0)
    c.check(0)
    with pytest.raises(ClockDriftError):
        c.check(-1)


def test_feed_guard_applies_clock_check():
    wall = [0]
    c = ClockGuard(ClockMapping(0, 0, 0), tolerance_ns=MS, wall=lambda: wall[0])
    g = FeedGuard(clock=c)
    g.on_message(1, "a", 0)
    wall[0] = 5 * MS  # wall jumped 5 ms, monotonic did not move
    with pytest.raises(ClockDriftError):
        g.on_message(2, "b", 0)


# ----------------------------------------------------------------- stale
def test_stale_feed_reports_unknown_then_recovers():
    h = Health()
    g = FeedGuard(stale_after_ns=100 * MS, health=h)
    g.on_message(1, "a", 0)
    assert not g.check_stale(50 * MS)
    assert g.check_stale(200 * MS) and g.state == UNKNOWN
    assert g.check_stale(300 * MS) and g.stats.stale_events == 1
    g.on_message(2, "b", 310 * MS)
    assert g.state == OK and h.log == [("feed", UNKNOWN), ("feed", OK)]


# ----------------------------------------------------------------- recorder
def _ev(seq):
    return MarketEvent(seq, 1, 1, seq * 10, 0, seq, 1, 1, 100, 5, seq, 0)


def test_guarded_recorder_records_in_sequence_once(tmp_path):
    rec = PaperRecorder(mapping=ClockMapping(0, 0, 0), mono=lambda: 0)
    gr = GuardedRecorder(rec, FeedGuard(reorder_window=2))
    for i, s in enumerate([1, 3, 2, 2, 4]):
        gr.on_message(s, _ev(s), mono_ns=i * MS)
    assert [e.sequence for e in rec.events] == [1, 2, 3, 4]
    assert [e.receive_ts for e in rec.events] == [0, 2 * MS, 2 * MS, 4 * MS]
    rec.write(tmp_path)

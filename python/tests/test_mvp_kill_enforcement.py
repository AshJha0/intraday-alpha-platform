"""v1.15: MVP kill-switch enforcement — a latched kill cancels the working
children of the killed scope, and any fill that races the cancel is counted
and alerted, never silently absorbed."""

from __future__ import annotations

import pytest
from iap.mvp.config import REPO_ROOT, load_config
from iap.mvp.engine import MvpEngine
from iap.mvp.feed import generate_feed, load_feed
from iap.risk.events import Scope
from iap.trace.sinks import MemoryTraceSink

# runs the tiny MVP session several times: slow job, like test_mvp.py
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    cfg = load_config(REPO_ROOT / "configs" / "mvp" / "mvp_tiny.json")
    out = tmp_path_factory.mktemp("mvp-kill") / "run"
    generate_feed(cfg, out)
    return cfg, load_feed(out)


def _until_working(eng: MvpEngine, events: list) -> int | None:  # type: ignore[type-arg]
    for i, ev in enumerate(events):
        eng.on_event(ev)
        live = eng._live
        if live is not None and any(eng.sim.open_qty(c) > 0 for c in live.children):
            return i + 1
    return None


def test_loss_kill_cancels_working_children(tiny) -> None:  # type: ignore[no-untyped-def]
    cfg, feed = tiny
    eng = MvpEngine(cfg, feed, MemoryTraceSink())
    events = list(feed.events)
    cut = _until_working(eng, events)
    assert cut is not None, "no working child in the tiny feed"
    working = [c for c in eng._live.children if eng.sim.open_qty(c) > 0]
    eng.risk_engine.engage_kill(
        Scope.STRATEGY, eng.strategy_id, events[cut - 1].exchange_ts, "injected loss kill"
    )
    fills_before = eng.counters.fills
    submitted_before = eng.counters.child_orders_submitted
    eng.on_event(events[cut])
    # each child is either kill-cancelled or already done (filled in step 1
    # of the same event, before the cancel could be sent: a raced fill)
    assert all(c in eng._kill_cancelled or eng.sim.open_qty(c) == 0 for c in working)
    assert eng.kill_cancels == len(eng._kill_cancelled)
    for ev in events[cut + 1 :]:
        eng.on_event(ev)
    eng.finish()
    assert eng.counters.child_orders_submitted == submitted_before
    raced = eng.counters.fills - fills_before
    # every fill after the kill raced an in-flight cancel: counted + alerted
    assert eng.post_kill_fills == raced
    assert len(eng.kill_alerts) == raced
    assert eng.parent_count == eng.counters.parent_orders


def test_raced_post_kill_fill_is_counted_and_alerted(tiny) -> None:  # type: ignore[no-untyped-def]
    """Force the race: suppress the kill cancel so working children keep
    filling after the latch; every such fill is booked, counted and alerted."""
    cfg, feed = tiny
    eng = MvpEngine(cfg, feed, MemoryTraceSink())
    events = list(feed.events)
    cut = _until_working(eng, events)
    assert cut is not None
    eng._enforce_kills = lambda ts: None  # type: ignore[method-assign]
    eng.risk_engine.engage_kill(Scope.GLOBAL, "", events[cut - 1].exchange_ts, "injected")
    fills_before = eng.counters.fills
    for ev in events[cut:]:
        eng.on_event(ev)
    eng.finish()
    raced = eng.counters.fills - fills_before
    assert raced > 0, "the forced race produced no fill on the tiny feed"
    assert eng.post_kill_fills == raced
    assert all(m.startswith("post-kill fill") for m in eng.kill_alerts)


def test_kill_blocks_scopes(tiny) -> None:  # type: ignore[no-untyped-def]
    cfg, feed = tiny
    eng = MvpEngine(cfg, feed, MemoryTraceSink())
    re = eng.risk_engine
    assert not re.kill_blocks(eng.strategy_id, eng.iid, 1)
    re.engage_kill(Scope.VENUE, "1", 0, "t")
    assert re.kill_blocks(eng.strategy_id, eng.iid, 1)
    assert not re.kill_blocks(eng.strategy_id, eng.iid, 2)
    re.engage_kill(Scope.INSTRUMENT, str(eng.iid), 0, "t")
    assert re.kill_blocks("other", eng.iid, 2)

"""Dry kill-switch drill on the day's feed (v1.15 #115 enforcement).

A SEPARATE engine (its own trace sink, nothing written) replays the day's
feed for one symbol until a child order is resting at a venue, engages the
strategy kill switch at that event, and keeps feeding up to
``max_events_after_kill`` events.  The drill passes when every child that
was working at the kill is cancelled (or had already completed in the same
event: a raced fill, counted and alerted by the engine) and no child is
submitted after the kill.  The day's real run is untouched.
"""

from __future__ import annotations

from typing import Any

from iap.mvp.engine import MvpEngine
from iap.mvp.real import PreparedSymbol
from iap.risk.events import Scope
from iap.trace.digest import TraceDigest


class _NullSink:
    """Counts traces; keeps none (the drill writes nothing)."""

    def __init__(self) -> None:
        self.n = 0
        self.digest = TraceDigest()

    def emit(self, trace: Any) -> None:
        self.n += 1


def kill_drill(prep: PreparedSymbol, max_events_after_kill: int) -> dict[str, Any]:
    """Run the drill (module docstring) and return its record."""
    eng = MvpEngine(prep.cfg, prep.feed, _NullSink(), realism=prep.realism)
    it = iter(prep.feed.events)
    seen = 0
    working: list[int] = []
    kill_ts = None
    for ev in it:
        eng.on_event(ev)
        seen += 1
        live = eng._live
        if live is not None:
            working = [c for c in live.children if eng.sim.open_qty(c) > 0]
            if working:
                kill_ts = ev.exchange_ts
                break
    base = {
        "symbol": prep.symbol,
        "scope": "STRATEGY",
        "strategy_id": eng.strategy_id,
        "dry_run": True,
    }
    if kill_ts is None:
        return {
            **base,
            "status": "inconclusive",
            "reason": "no child order was ever resting at a venue",
            "events_replayed": seen,
        }
    eng.risk_engine.engage_kill(Scope.STRATEGY, eng.strategy_id, kill_ts, "paper-trade drill")
    fills_before = eng.counters.fills
    submitted_before = eng.counters.child_orders_submitted
    after = 0
    for ev in it:
        if after >= max_events_after_kill:
            break
        eng.on_event(ev)
        after += 1
    not_cancelled = [c for c in working if c not in eng._kill_cancelled and eng.sim.open_qty(c) > 0]
    submitted_after = eng.counters.child_orders_submitted - submitted_before
    ok = not not_cancelled and submitted_after == 0 and after > 0
    return {
        **base,
        "status": "pass" if ok else "fail",
        "kill_event_index": seen,
        "kill_ts": kill_ts,
        "working_children_at_kill": len(working),
        "kill_cancels": eng.kill_cancels,
        "children_still_open": len(not_cancelled),
        "children_submitted_after_kill": submitted_after,
        "fills_after_kill": eng.counters.fills - fills_before,
        "post_kill_fills_alerted": eng.post_kill_fills,
        "kill_alerts": list(eng.kill_alerts),
        "events_after_kill": after,
    }

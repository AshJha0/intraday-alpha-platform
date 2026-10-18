"""Property tests (v1.18 REL-12): ExecutionReplay invariants on generated
event streams (tests only; ``iap.execution`` is not changed).

For a random one-venue MBO stream and a random parent (TWAP / VWAP / IS /
POV; NATIVE / AGGRESSIVE / PASSIVE policy; buy or sell):

* quantity conservation: per parent, the fills sum to ``filled_qty``,
  ``filled_qty + unfilled_qty == qty``, and every child's fills sum to
  ``qty - remaining`` and never exceed the child's quantity;
* fills are a subset of the book: taker quantity at a price never exceeds
  the opposite-side quantity that was ever displayed at that price, and
  maker quantity never exceeds the venue's printed (EXECUTE + TRADE) volume;
* every fill has positive quantity, our side, and lies in the parent window;
* the run is deterministic (same stream + config + seed => same fills).

Hypothesis runs are small and derandomized; ``IAP_HYPOTHESIS_EXAMPLES``
raises the count for a local soak.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import timedelta

import pytest
from iap.core.events import EventType, MarketEvent
from iap.execution import (
    AlgoType,
    ExecConfig,
    ExecutionReplay,
    InstrumentSpec,
    Liquidity,
    ParentOrder,
    VenueSpec,
)
from iap.execution.passive import ExecPolicy

pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

EXAMPLES = int(os.environ.get("IAP_HYPOTHESIS_EXAMPLES", "30"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=timedelta(seconds=10),
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

T0 = 1_700_000_000_000_000_000
SEC = 1_000_000_000
INST, VENUE = 7, 1


def _config() -> ExecConfig:
    venue = VenueSpec(
        venue_id=VENUE,
        name="TST",
        taker_fee_per_share=0.003,
        maker_rebate_per_share=0.002,
        latency_mean_ns=100_000,
        latency_jitter_ns=0,
    )
    return ExecConfig(
        seed=11, venues={VENUE: venue}, instruments={INST: InstrumentSpec(INST, 0.01, 1.0, 1e6)}
    )


@st.composite
def market(draw) -> list[MarketEvent]:
    """A clean, in-sequence stream around 100 ticks over ~100 s: a seeded
    two-sided book, then adds, cancels, executes of live orders and trades."""
    events: list[MarketEvent] = []
    live: dict[int, list[int]] = {}  # oid -> [side, price, qty]
    ts = T0
    next_oid = 1

    def emit(etype, side, price, qty, oid, tid=0):
        events.append(
            MarketEvent(
                event_id=len(events),
                instrument_id=INST,
                venue_id=VENUE,
                exchange_ts=ts,
                receive_ts=ts,
                sequence=len(events) + 1,
                event_type=int(etype),
                side=side,
                price_ticks=price,
                qty=qty,
                order_id=oid,
                trade_id=tid,
            )
        )

    for side, price in ((0, 99), (0, 98), (1, 101), (1, 102)):
        emit(EventType.ADD, side, price, 500, next_oid)
        live[next_oid] = [side, price, 500]
        next_oid += 1
    for _ in range(draw(st.integers(5, 60))):
        ts += draw(st.integers(1, 4 * SEC))
        op = draw(st.sampled_from(["add", "add", "cancel", "exec", "trade"]))
        if op == "add" or not live:
            side = draw(st.integers(0, 1))
            price = draw(st.integers(96, 99)) if side == 0 else draw(st.integers(101, 104))
            qty = draw(st.integers(1, 600))
            emit(EventType.ADD, side, price, qty, next_oid)
            live[next_oid] = [side, price, qty]
            next_oid += 1
            continue
        oid = draw(st.sampled_from(sorted(live)))
        side, price, qty = live[oid]
        if op == "cancel":
            emit(EventType.CANCEL, side, price, qty, oid)
            del live[oid]
        elif op == "exec":
            q = draw(st.integers(1, qty))
            emit(EventType.EXECUTE, side, price, q, oid, tid=len(events) + 1)
            live[oid][2] -= q
            if live[oid][2] == 0:
                del live[oid]
        else:
            emit(EventType.TRADE, 1 - side, price, draw(st.integers(1, 300)), 0, len(events) + 1)
    return events


@st.composite
def parents(draw) -> ParentOrder:
    algo = draw(st.sampled_from(list(AlgoType)))
    return ParentOrder(
        parent_id=1,
        instrument_id=INST,
        venue_id=VENUE,
        side=draw(st.integers(0, 1)),
        qty=draw(st.integers(1, 3_000)),
        algo=algo,
        start_ts=T0,
        end_ts=T0 + draw(st.integers(10, 120)) * SEC,
        slices=draw(st.integers(1, 8)),
        max_child_qty=draw(st.sampled_from([50, 200, 1_000, 10_000])),
        policy=draw(st.sampled_from(list(ExecPolicy))),
    )


def _displayed(events: list[MarketEvent]) -> tuple[dict, int]:
    """Quantity ever displayed per (side, price), and printed volume."""
    shown: dict[tuple[int, int], int] = defaultdict(int)
    printed = 0
    for ev in events:
        if ev.event_type == EventType.ADD:
            shown[(ev.side, ev.price_ticks)] += ev.qty
        elif ev.event_type in (EventType.EXECUTE, EventType.TRADE):
            printed += ev.qty
    return shown, printed


@SETTINGS
@given(events=market(), parent=parents())
def test_replay_conserves_quantity_and_fills_from_the_book(events, parent):
    result = ExecutionReplay(_config(), [parent]).run(events)
    report = result.parents[parent.parent_id]
    fills = [f for f in result.fills if f.parent_id == parent.parent_id]

    assert sum(f.qty for f in fills) == report.filled_qty
    assert report.filled_qty + report.unfilled_qty == parent.qty
    assert 0 <= report.filled_qty <= parent.qty

    by_child: dict[int, int] = defaultdict(int)
    for f in fills:
        assert f.qty > 0
        assert f.side == parent.side
        assert f.venue_id == VENUE and f.instrument_id == INST
        assert parent.start_ts <= f.ts <= parent.end_ts
        by_child[f.order_id] += f.qty
    for child in result.children.get(parent.parent_id, []):
        got = by_child.get(child.order_id, 0)
        assert got <= child.qty
        assert got == child.qty - child.remaining

    shown, printed = _displayed(events)
    taker: dict[tuple[int, int], int] = defaultdict(int)
    maker = 0
    for f in fills:
        if f.liquidity == Liquidity.TAKER:
            taker[(1 - f.side, f.price_ticks)] += f.qty
        else:
            maker += f.qty
    for key, q in taker.items():
        assert q <= shown.get(key, 0), f"taker fill {key} not backed by displayed depth"
    assert maker <= printed, "maker fills exceed the printed volume"


@SETTINGS
@given(events=market(), parent=parents())
def test_replay_is_deterministic(events, parent):
    a = ExecutionReplay(_config(), [parent]).run(events)
    b = ExecutionReplay(_config(), [parent]).run(events)
    assert a.fills == b.fills
    assert a.parents[1].to_dict() == b.parents[1].to_dict()

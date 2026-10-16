"""v1.17.1 B8 follow-up: the opt-in ``sor_sweep`` route (``ExecutionReplay(...,
router_sweep=True)``). Synthetic two-venue books only.

Venue 1 (taker 0.003 $/sh) asks 10_001 x50, 10_002 x50, 10_003 x50.
Venue 2 (taker 0.001 $/sh, the cheaper fee) asks 10_001 x10, 10_005 x1000.
The touches tie, so the single-venue router (``sor``) sends a 100-lot buy
to venue 2 on its fee and the child walks to 10_005; the sweep takes venue
2's 10 at the touch and the rest on venue 1 at 10_001 / 10_002.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from iap.core.events import EventType
from iap.execution import AlgoType, ExecPolicy, ExecutionReplay, ParentOrder
from iap.execution.sor_v2 import CostAwareRouter, RouterOptions
from iap.execution.types import OrderType
from iap.execution.venues_model import VenueModel
from iap.research import execution_benchmark as bench
from test_exec_algos import SEC, T0, _ev, algo_config

BOOK = (
    (1, 10_001, 50),
    (1, 10_002, 50),
    (1, 10_003, 50),
    (2, 10_001, 10),
    (2, 10_005, 1_000),
)


def stream() -> list:
    evs = []
    seq: dict[int, int] = {}  # per-venue sequence (a gap marks the book stale)

    def push(ts, etype, side, px, qty, oid, venue):
        seq[venue] = seq.get(venue, 0) + 1
        ev = _ev(seq[venue], ts, etype, side, px, qty, oid, venue=venue)
        evs.append(replace(ev, event_id=len(evs) + 1))

    for k, (vid, px, q) in enumerate(BOOK):
        push(T0 - SEC, EventType.ADD, 1, px, q, 1 + k, vid)
    push(T0 - SEC, EventType.ADD, 0, 9_999, 100, 50, 1)
    push(T0 - SEC, EventType.ADD, 0, 9_999, 100, 51, 2)
    # heartbeat bids (away from the asks) so arrivals are processed
    for k in range(1, 20):
        push(T0 + k * SEC // 10, EventType.ADD, 0, 9_990, 1, 100 + k, 1)
    return evs


def config():
    cfg = algo_config()
    # no latency: the decision book is the arrival book
    return replace(cfg, venues={v: replace(s, latency_mean_ns=0) for v, s in cfg.venues.items()})


def router(cfg, rank: str = "all_in") -> CostAwareRouter:
    return CostAwareRouter(
        {v: VenueModel(spec=s) for v, s in cfg.venues.items()}, RouterOptions(sweep_rank=rank)
    )


def parent(qty: int = 100, venue_id: int = 0) -> ParentOrder:
    return ParentOrder(
        parent_id=1,
        instrument_id=7,
        venue_id=venue_id,
        side=0,
        qty=qty,
        algo=AlgoType.TWAP,
        start_ts=T0,
        end_ts=T0 + 100 * SEC,
        slices=1,
        max_child_qty=10_000,
        policy=ExecPolicy.AGGRESSIVE,  # marketable children (NATIVE TWAP joins the bid)
    )


def fills_by_venue_px(res) -> dict[tuple[int, int], int]:
    out: dict[tuple[int, int], int] = {}
    for f in res.fills:
        out[(f.venue_id, f.price_ticks)] = out.get((f.venue_id, f.price_ticks), 0) + f.qty
    return out


def test_single_venue_router_walks_the_cheap_fee_venue():
    cfg = config()
    res = ExecutionReplay(cfg, [parent()], router=router(cfg)).run(stream())
    assert fills_by_venue_px(res) == {(2, 10_001): 10, (2, 10_005): 90}


def test_sweep_takes_the_best_all_in_levels_across_both_books():
    cfg = config()
    res = ExecutionReplay(cfg, [parent()], router=router(cfg), router_sweep=True).run(stream())
    assert fills_by_venue_px(res) == {(2, 10_001): 10, (1, 10_001): 50, (1, 10_002): 40}
    kids = res.children[1]
    assert {k.type for k in kids} == {OrderType.IOC}
    # one child per venue, limit at its deepest planned level, qty capped there
    assert sorted((k.venue_id, k.limit_ticks, k.qty) for k in kids) == [
        (1, 10_002, 90),
        (2, 10_001, 10),
    ]
    assert res.sweep_unsent_qty == 0


def test_sweep_caps_each_venue_at_its_displayed_depth():
    cfg = config()
    res = ExecutionReplay(cfg, [parent(qty=2_000)], router=router(cfg), router_sweep=True).run(
        stream()
    )
    displayed = {1: 150, 2: 1_010}
    sent = {k.venue_id: k.qty for k in res.children[1]}
    assert sent == displayed
    assert res.sweep_unsent_qty == 2_000 - sum(displayed.values())
    assert sum(f.qty for f in res.fills) == sum(displayed.values())


def test_sweep_leaves_pinned_parents_alone():
    cfg = config()
    a = ExecutionReplay(cfg, [parent(venue_id=2)]).run(stream())
    b = ExecutionReplay(cfg, [parent(venue_id=2)], router=router(cfg), router_sweep=True).run(
        stream()
    )
    assert [f.to_dict() for f in a.fills] == [f.to_dict() for f in b.fills]


def test_sweep_needs_a_router():
    with pytest.raises(ValueError, match="router_sweep needs a router"):
        ExecutionReplay(config(), [parent()], router_sweep=True)


def test_sor_sweep_is_a_route_policy():
    assert bench.split_route("sor_sweep") == ("native", "sor_sweep")
    assert bench.split_route("passive@sor_sweep") == ("passive", "sor_sweep")

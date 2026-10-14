"""v1.16 P0-4: all-in sweep ranking and queue-aware passive routing (iap.execution.sor_v2).

Both are opt-ins on the v1.12 cost-aware router; the pinned
``SmartOrderRouter`` routes (and the C++ parity) are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from iap.core.events import SessionStatus
from iap.execution import NO_ROUTE, SmartOrderRouter
from iap.execution.sor_v2 import CostAwareRouter, RouterOptions, hazard_fill_prob
from iap.execution.venues_model import VenueModel
from test_sor import Book, sor_venues

TICK = 0.01
TS = 0
ALL_IN = RouterOptions(sweep_rank="all_in")


def router(venues=None, options=None):
    venues = venues or sor_venues()  # taker fee: v1 0.003, v2 0.001
    return CostAwareRouter({v: VenueModel(spec=s) for v, s in venues.items()}, options)


def split(kids):
    out: dict[int, int] = {}
    for k in kids:
        out[k.venue_id] = out.get(k.venue_id, 0) + k.qty
    return sorted(out.items())


def test_all_in_sweep_walks_depth_net_of_fees():
    r = router(options=ALL_IN)
    b = Book()
    b.add(1, 1, 100, 300, 11)  # 1.003 / share
    b.add(1, 1, 101, 500, 12)  # 1.013
    b.add(2, 1, 100, 200, 21)  # 1.001  (cheapest)
    b.add(2, 1, 102, 500, 22)  # 1.021
    kids = r.plan_sweep(b.book, 0, 400, TS, tick=TICK)
    assert [(k.venue_id, k.price_ticks, k.qty) for k in kids] == [(2, 100, 200), (1, 100, 200)]
    assert split(r.plan_sweep(b.book, 0, 900, TS, tick=TICK)) == [(1, 700), (2, 200)]
    # Depth runs out: take what is displayed.
    assert split(r.plan_sweep(b.book, 0, 5000, TS, tick=TICK)) == [(1, 800), (2, 700)]
    assert split(r.plan_sweep(b.book, 0, 100, TS, tick=TICK, candidates=[1])) == [(1, 100)]


def test_fee_can_outweigh_a_tick_only_when_ranking_all_in():
    venues = sor_venues()
    venues[1] = replace(venues[1], taker_fee_per_share=0.02)  # 2 ticks of fee
    b = Book()
    b.add(1, 1, 100, 500, 11)  # 1.02
    b.add(2, 1, 101, 500, 21)  # 1.011: worse price, cheaper all-in
    assert split(router(venues, ALL_IN).plan_sweep(b.book, 0, 100, TS, tick=TICK)) == [(2, 100)]
    # The default price-first sweep and the pinned route take the better displayed price.
    assert split(router(venues).plan_sweep(b.book, 0, 100, TS)) == [(1, 100)]
    assert SmartOrderRouter(venues).route_aggressive(b.book, 0, [1, 2]) == 1


def test_all_in_sell_sweep_takes_best_proceeds_first():
    b = Book()
    b.add(1, 0, 99, 100, 13)  # 0.987
    b.add(2, 0, 99, 100, 23)  # 0.989 (lower fee)
    b.add(2, 0, 98, 100, 24)
    kids = router(options=ALL_IN).plan_sweep(b.book, 1, 150, TS, tick=TICK)
    assert [(k.venue_id, k.price_ticks, k.qty) for k in kids] == [(2, 99, 100), (1, 99, 50)]


def test_all_in_sweep_skips_ineligible_venues_and_needs_tick():
    r = router(options=ALL_IN)
    b = Book()
    b.add(1, 1, 100, 300, 11)
    b.add(2, 1, 99, 300, 21)
    b.status(2, SessionStatus.HALT)
    assert split(r.plan_sweep(b.book, 0, 100, TS, tick=TICK)) == [(1, 100)]
    assert r.plan_sweep(b.book, 1, 100, TS, tick=TICK) == []
    with pytest.raises(ValueError):
        r.plan_sweep(b.book, 0, 100, TS)
    with pytest.raises(ValueError):
        RouterOptions(sweep_rank="fee")


def queue_book() -> Book:
    b = Book()
    b.add(1, 0, 100, 100, 11)  # short queue
    b.add(2, 0, 100, 5000, 21)  # long queue, better rebate (v2 0.0025 vs v1 0.002)
    b.add(1, 1, 101, 100, 12)
    b.add(2, 1, 101, 100, 22)
    return b


def test_passive_fill_prob_from_queue_hazard():
    b = queue_book()
    r = router(options=RouterOptions(miss_cost_bps=10.0))
    fp = hazard_fill_prob(lambda v, s, q: 10.0 / q, 1.0)  # depletes faster when short
    assert fp(1, 0, 100) == pytest.approx(1 - math.exp(-0.1))
    # Missing a fill is costly: the short queue wins.
    assert r.route_passive(b.book, 0, TS, TICK, fill_prob=fp) == 1
    # Certain fills, no miss cost: the rebate decides.
    free = router(options=RouterOptions(miss_cost_bps=0.0))
    assert free.route_passive(b.book, 0, TS, TICK, fill_prob=lambda v, s, q: 1.0) == 2
    with pytest.raises(ValueError):
        hazard_fill_prob(lambda v, s, q: 1.0, 0.0)


def test_passive_unknown_fill_prob_skips_venue():
    b = queue_book()
    r = router(options=RouterOptions(miss_cost_bps=0.0))
    only2 = hazard_fill_prob(lambda v, s, q: None if v == 1 else 0.5, 1.0)
    assert r.route_passive(b.book, 0, TS, TICK, fill_prob=only2) == 2
    assert r.route_passive(b.book, 0, TS, TICK, fill_prob=lambda v, s, q: None) == NO_ROUTE

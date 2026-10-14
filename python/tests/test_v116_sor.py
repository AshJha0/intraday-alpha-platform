"""v1.16 P0-4: depth-for-size aggressive allocation and queue-aware passive routing."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from iap.core.events import SessionStatus
from iap.execution import NO_ROUTE, SmartOrderRouter
from test_sor import Book, sor_venues

TICK = 0.01


def test_allocation_walks_depth_net_of_fees():
    sor = SmartOrderRouter(sor_venues())  # taker fee: v1 0.003, v2 0.001
    b = Book()
    b.add(1, 1, 100, 300, 11)  # 1.003 / share
    b.add(1, 1, 101, 500, 12)  # 1.013
    b.add(2, 1, 100, 200, 21)  # 1.001  (cheapest)
    b.add(2, 1, 102, 500, 22)  # 1.021
    assert sor.allocate_aggressive(b.book, 0, 400, [1, 2], TICK) == [(1, 200), (2, 200)]
    assert sor.allocate_aggressive(b.book, 0, 900, [1, 2], TICK) == [(1, 700), (2, 200)]
    # Depth runs out: allocate what is displayed.
    assert sor.allocate_aggressive(b.book, 0, 5000, [1, 2], TICK) == [(1, 800), (2, 700)]
    # One venue only.
    assert sor.allocate_aggressive(b.book, 0, 100, [1], TICK) == [(1, 100)]


def test_fee_can_outweigh_a_tick():
    venues = sor_venues()
    venues[1] = replace(venues[1], taker_fee_per_share=0.02)  # 2 ticks of fee
    sor = SmartOrderRouter(venues)
    b = Book()
    b.add(1, 1, 100, 500, 11)  # 1.02
    b.add(2, 1, 101, 500, 21)  # 1.011: worse price, cheaper all-in
    assert sor.allocate_aggressive(b.book, 0, 100, [1, 2], TICK) == [(2, 100)]
    # The pinned price-first route still picks the better displayed price.
    assert sor.route_aggressive(b.book, 0, [1, 2]) == 1


def test_sell_allocation_takes_best_proceeds_first():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    b.add(1, 0, 99, 100, 13)  # 0.987
    b.add(2, 0, 99, 100, 23)  # 0.989 (lower fee)
    b.add(2, 0, 98, 100, 24)
    assert sor.allocate_aggressive(b.book, 1, 150, [1, 2], TICK) == [(1, 50), (2, 100)]


def test_allocation_skips_ineligible_venues_and_rejects_bad_qty():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    b.add(1, 1, 100, 300, 11)
    b.add(2, 1, 99, 300, 21)
    b.status(2, SessionStatus.HALT)
    assert sor.allocate_aggressive(b.book, 0, 100, [1, 2], TICK) == [(1, 100)]
    assert sor.allocate_aggressive(b.book, 1, 100, [1, 2], TICK) == []
    with pytest.raises(ValueError):
        sor.allocate_aggressive(b.book, 0, 0, [1, 2], TICK)


def test_passive_queue_prefers_fill_probability_over_rebate():
    sor = SmartOrderRouter(sor_venues())  # rebate: v1 0.002, v2 0.0025
    b = Book()
    b.add(1, 0, 100, 100, 11)  # short queue
    b.add(2, 0, 100, 5000, 21)  # long queue, better rebate

    def hazard(vid, side, q):
        return 10.0 / q  # depletes faster when the queue is short

    # Missing a fill is costly: the short queue wins.
    assert sor.route_passive_queue(b.book, 0, [1, 2], hazard, 1.0, 0.01) == 1
    # No miss cost and certain fills: the rebate decides.
    assert sor.route_passive_queue(b.book, 0, [1, 2], lambda v, s, q: 1e9, 1.0, 0.0) == 2
    # Score hand calc for venue 1.
    p1 = 1 - math.exp(-0.1)
    p2 = 1 - math.exp(-0.002)
    assert p1 * 0.002 - (1 - p1) * 0.01 > p2 * 0.0025 - (1 - p2) * 0.01


def test_passive_queue_unknown_hazard_and_no_route():
    sor = SmartOrderRouter(sor_venues())
    b = Book()
    b.add(1, 0, 100, 100, 11)
    b.add(2, 0, 100, 100, 21)
    only2 = lambda v, s, q: None if v == 1 else 0.5  # noqa: E731
    assert sor.route_passive_queue(b.book, 0, [1, 2], only2, 1.0, 0.0) == 2
    assert sor.route_passive_queue(b.book, 0, [1, 2], lambda v, s, q: None, 1.0, 0.0) == NO_ROUTE
    assert sor.route_passive_queue(b.book, 1, [1, 2], lambda v, s, q: 1.0, 1.0, 0.0) == NO_ROUTE
    with pytest.raises(ValueError):
        sor.route_passive_queue(b.book, 0, [1, 2], only2, 0.0, 0.0)

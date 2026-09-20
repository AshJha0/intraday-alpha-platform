"""``OrderBook.restore`` must reject what ``apply`` rejects.

A checkpoint is a trust boundary like the wire.  restore() validated side,
duplicate ids and arrival-order consistency but NOT qty or price_ticks, while
``_payload_ok`` requires both > 0 on the live path: a hand-written checkpoint
was accepted and ``best_bid()`` returned ``(-5, -1000000)`` — a state
unreachable through ``apply()``, which then feeds negative sizes into the
feature engine's merged depth and rolling windows.
"""

from __future__ import annotations

import pytest

from iap.core.events import EventType, MarketEvent
from iap.orderbook.book import OrderBook


def _seed() -> dict:
    book = OrderBook(1, 1)
    book.apply(
        MarketEvent(1, 1, 1, 1_000, 1_000, 1, EventType.ADD, 0, 100, 10, 7, 0)
    )
    return book.checkpoint()


def test_clean_checkpoint_still_round_trips():
    book = OrderBook.restore(_seed())
    assert book.best_bid() == (100, 10)


@pytest.mark.parametrize("price", [-5, 0])
def test_restore_rejects_non_positive_price_ticks(price):
    cp = _seed()
    cp["levels"][0]["price_ticks"] = price
    with pytest.raises(ValueError, match="non-positive price_ticks"):
        OrderBook.restore(cp)


@pytest.mark.parametrize("qty", [-1_000_000, 0])
def test_restore_rejects_non_positive_qty(qty):
    cp = _seed()
    cp["levels"][0]["orders"][0][1] = qty
    with pytest.raises(ValueError, match="non-positive qty"):
        OrderBook.restore(cp)


def test_restore_rejects_the_reported_unreachable_state():
    cp = _seed()
    cp["levels"][0]["price_ticks"] = -5
    cp["levels"][0]["orders"][0][1] = -1_000_000
    with pytest.raises(ValueError):
        OrderBook.restore(cp)

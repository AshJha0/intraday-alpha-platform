"""Shared fixtures for the iap reference test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
CONFIGS_DIR = REPO_ROOT / "configs"

sys.path.insert(0, str(TESTS_DIR))  # for bruteforce_book

from iap.core.events import EventType, MarketEvent  # noqa: E402
from iap.reference.refdata import ReferenceData  # noqa: E402


@pytest.fixture(scope="session")
def refdata() -> ReferenceData:
    return ReferenceData.load(CONFIGS_DIR)


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    assert GOLDEN_DIR.is_dir(), f"missing {GOLDEN_DIR}"
    return GOLDEN_DIR


def mkev(
    seq: int,
    event_type: int,
    side: int = 0,
    price: int = 0,
    qty: int = 0,
    order_id: int = 0,
    trade_id: int = 0,
    instrument_id: int = 1,
    venue_id: int = 1,
    ts: int = 0,
) -> MarketEvent:
    """Convenience MarketEvent factory for unit tests."""
    ts = ts or 1_700_000_000_000_000_000 + seq * 1_000_000
    return MarketEvent(
        event_id=seq,
        instrument_id=instrument_id,
        venue_id=venue_id,
        exchange_ts=ts,
        receive_ts=ts + 150_000,
        sequence=seq,
        event_type=int(event_type),
        side=int(side),
        price_ticks=price,
        qty=qty,
        order_id=order_id,
        trade_id=trade_id,
    )


def add(seq, side, price, qty, oid, **kw):
    return mkev(seq, EventType.ADD, side, price, qty, oid, **kw)

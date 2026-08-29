"""MarketEvent contract and validation tests (conventions section 1)."""

import pytest

from conftest import mkev
from iap.core.events import (
    FIELDS,
    EventType,
    MarketEvent,
    SessionStatus,
    Side,
    validate,
    validation_error,
)


def test_enum_values_pinned():
    assert (Side.BID, Side.ASK) == (0, 1)
    assert [
        EventType.ADD, EventType.MODIFY, EventType.CANCEL, EventType.EXECUTE,
        EventType.TRADE, EventType.QUOTE, EventType.SNAPSHOT, EventType.STATUS,
        EventType.HEARTBEAT,
    ] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert [
        SessionStatus.TRADING, SessionStatus.HALT,
        SessionStatus.AUCTION, SessionStatus.CLOSE,
    ] == [1, 2, 3, 4]


def test_field_order_is_canonical():
    assert FIELDS == (
        "event_id", "instrument_id", "venue_id", "exchange_ts", "receive_ts",
        "sequence", "event_type", "side", "price_ticks", "qty", "order_id",
        "trade_id",
    )


def test_dataclass_uses_slots():
    ev = mkev(1, EventType.HEARTBEAT)
    assert not hasattr(ev, "__dict__")
    with pytest.raises(AttributeError):
        ev.bogus = 1


def test_valid_add_passes():
    validate(mkev(1, EventType.ADD, Side.BID, 2450, 100, order_id=7))


def test_receive_before_exchange_rejected():
    ev = mkev(1, EventType.ADD, Side.BID, 2450, 100, order_id=7)
    ev.receive_ts = ev.exchange_ts - 1
    assert "receive_ts" in validation_error(ev)
    with pytest.raises(ValueError):
        validate(ev)


def test_unknown_event_type_rejected():
    ev = mkev(1, EventType.ADD, Side.BID, 2450, 100, order_id=7)
    ev.event_type = 0
    assert "event_type" in validation_error(ev)
    ev.event_type = 42
    assert validation_error(ev) is not None


def test_bad_side_rejected():
    ev = mkev(1, EventType.ADD, Side.BID, 2450, 100, order_id=7)
    ev.side = 9
    assert "side" in validation_error(ev)


def test_nonpositive_qty_rejected_for_add_trade_quote():
    for et in (EventType.ADD, EventType.TRADE, EventType.QUOTE):
        ev = mkev(1, et, Side.BID, 2450, 0, order_id=7, trade_id=7)
        assert "qty" in validation_error(ev)
        ev.qty = -5
        assert validation_error(ev) is not None


def test_add_requires_order_id_trade_requires_trade_id():
    assert "order_id" in validation_error(mkev(1, EventType.ADD, 0, 2450, 100))
    assert "trade_id" in validation_error(mkev(1, EventType.TRADE, 0, 2450, 100))


def test_unsigned_range_checks():
    ev = mkev(1, EventType.HEARTBEAT)
    ev.instrument_id = 1 << 32
    assert "u32" in validation_error(ev)
    ev = mkev(1, EventType.HEARTBEAT)
    ev.venue_id = 1 << 16
    assert "u16" in validation_error(ev)
    ev = mkev(1, EventType.HEARTBEAT)
    ev.sequence = -1
    assert "u64" in validation_error(ev)


def test_status_payload_must_be_session_status():
    ev = mkev(1, EventType.STATUS, qty=int(SessionStatus.HALT))
    assert validation_error(ev) is None
    ev.qty = 99
    assert "SessionStatus" in validation_error(ev)


def test_cancel_allows_zero_payload():
    assert validation_error(mkev(1, EventType.CANCEL, Side.BID, 0, 0, order_id=5)) is None


def test_to_dict_round_trip():
    ev = mkev(3, EventType.ADD, Side.ASK, 2455, 300, order_id=9)
    assert MarketEvent(**ev.to_dict()) == ev
    assert list(ev.to_dict()) == list(FIELDS)

"""Canonical MarketEvent contract and enums (PLATFORM_CONVENTIONS.md sections 1-2).

All prices are int64 ticks, quantities int64 base units, timestamps int64 ns since
Unix epoch. Field order below is the canonical JSONL key order and the IAP1 record
field order — do not reorder.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import IntEnum
from typing import Optional

U64_MAX = (1 << 64) - 1
U32_MAX = (1 << 32) - 1
U16_MAX = (1 << 16) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


class Side(IntEnum):
    """Order/quote/aggressor side."""

    BID = 0
    ASK = 1


class EventType(IntEnum):
    """Canonical event types (u8)."""

    ADD = 1
    MODIFY = 2
    CANCEL = 3
    EXECUTE = 4
    TRADE = 5
    QUOTE = 6
    SNAPSHOT = 7
    STATUS = 8
    HEARTBEAT = 9


class SessionStatus(IntEnum):
    """STATUS event payload, carried in the qty field."""

    TRADING = 1
    HALT = 2
    AUCTION = 3
    CLOSE = 4


#: Event types whose payload carries a live price/qty/order_id.
_BOOK_TYPES = frozenset(
    {EventType.ADD, EventType.MODIFY, EventType.CANCEL, EventType.EXECUTE}
)


@dataclass(slots=True)
class MarketEvent:
    """One canonical market event. Field order is normative (JSONL/IAP1)."""

    event_id: int
    instrument_id: int
    venue_id: int
    exchange_ts: int
    receive_ts: int
    sequence: int
    event_type: int
    side: int
    price_ticks: int
    qty: int
    order_id: int
    trade_id: int

    def to_dict(self) -> dict:
        """Return an insertion-ordered dict in canonical key order."""
        return {
            "event_id": self.event_id,
            "instrument_id": self.instrument_id,
            "venue_id": self.venue_id,
            "exchange_ts": self.exchange_ts,
            "receive_ts": self.receive_ts,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "side": self.side,
            "price_ticks": self.price_ticks,
            "qty": self.qty,
            "order_id": self.order_id,
            "trade_id": self.trade_id,
        }


#: Canonical field/key order (normative for JSONL and IAP1).
FIELDS = tuple(f.name for f in fields(MarketEvent))


def validation_error(ev: MarketEvent) -> Optional[str]:
    """Return a reason string if ``ev`` violates the contract, else None.

    Checked: integer domains (u64/u32/u16/u8 enums), receive_ts >= exchange_ts,
    and per-event-type payload rules from schemas/FORMAT.md section 4.
    """
    if not (0 <= ev.event_id <= U64_MAX):
        return f"event_id out of u64 range: {ev.event_id}"
    if not (0 <= ev.instrument_id <= U32_MAX):
        return f"instrument_id out of u32 range: {ev.instrument_id}"
    if not (0 <= ev.venue_id <= U16_MAX):
        return f"venue_id out of u16 range: {ev.venue_id}"
    if not (I64_MIN <= ev.exchange_ts <= I64_MAX):
        return f"exchange_ts out of i64 range: {ev.exchange_ts}"
    if not (I64_MIN <= ev.receive_ts <= I64_MAX):
        return f"receive_ts out of i64 range: {ev.receive_ts}"
    if ev.receive_ts < ev.exchange_ts:
        return f"receive_ts {ev.receive_ts} < exchange_ts {ev.exchange_ts}"
    if not (0 <= ev.sequence <= U64_MAX):
        return f"sequence out of u64 range: {ev.sequence}"
    if ev.event_type not in EventType._value2member_map_:
        return f"unknown event_type: {ev.event_type}"
    if ev.side not in (0, 1):
        return f"side must be 0 (BID) or 1 (ASK): {ev.side}"
    if not (I64_MIN <= ev.price_ticks <= I64_MAX):
        return f"price_ticks out of i64 range: {ev.price_ticks}"
    if not (I64_MIN <= ev.qty <= I64_MAX):
        return f"qty out of i64 range: {ev.qty}"
    if not (0 <= ev.order_id <= U64_MAX):
        return f"order_id out of u64 range: {ev.order_id}"
    if not (0 <= ev.trade_id <= U64_MAX):
        return f"trade_id out of u64 range: {ev.trade_id}"

    et = ev.event_type
    if et in _BOOK_TYPES:
        if ev.order_id == 0:
            return f"order_id required for event_type {et}"
        if ev.qty <= 0 and et != EventType.CANCEL:
            return f"qty must be > 0 for event_type {et}: {ev.qty}"
        if ev.price_ticks <= 0 and et != EventType.CANCEL:
            return f"price_ticks must be > 0 for event_type {et}: {ev.price_ticks}"
    elif et in (EventType.TRADE, EventType.QUOTE, EventType.SNAPSHOT):
        if ev.qty <= 0:
            return f"qty must be > 0 for event_type {et}: {ev.qty}"
        if ev.price_ticks <= 0:
            return f"price_ticks must be > 0 for event_type {et}: {ev.price_ticks}"
        if et == EventType.TRADE and ev.trade_id == 0:
            return "trade_id required for TRADE"
    elif et == EventType.STATUS:
        if ev.qty not in SessionStatus._value2member_map_:
            return f"STATUS qty must be a SessionStatus code: {ev.qty}"
    # HEARTBEAT: no payload constraints.
    return None


def validate(ev: MarketEvent) -> None:
    """Raise ValueError if ``ev`` violates the canonical contract."""
    reason = validation_error(ev)
    if reason is not None:
        raise ValueError(f"invalid MarketEvent (event_id={ev.event_id}): {reason}")

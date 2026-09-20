"""Order and fill contracts consumed by the risk engine.

``OrderRequest`` mirrors the Rust ``venue::OrderRequest``
(``schemas/order/order_request.schema.json``) field-for-field, including the
wire domains Rust enforces through its types (``u64 order_id``,
``u32 instrument_id``, ``u8 side / order_type``, ``u16 venue_id``,
``i64 qty / price_ticks / timestamp``): a value outside its domain cannot be
constructed at all (``ValueError``), exactly as it cannot be represented in
Rust. :func:`order_validation_error` is the schema-level check
(``venue::order_validation_error``) whose reason text becomes the
``MALFORMED_ORDER`` decision reason — byte-identical.

``Fill`` mirrors ``risk::Fill``: ``order_id == 0`` is an external
adjustment with no open order. Its *content* (qty > 0, side in {0, 1},
price > 0, known instrument) is validated by ``RiskEngine.on_fill`` — a
malformed fill is audited as ``MALFORMED_FILL`` and dropped, never raised —
so only the type domains are enforced here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from iap.risk.serialize import rust_display_f64

__all__ = ["OrderType", "OrderRequest", "Fill", "order_validation_error"]

_U64_MAX = (1 << 64) - 1
_U32_MAX = (1 << 32) - 1
_U16_MAX = (1 << 16) - 1
_U8_MAX = (1 << 8) - 1
_I64_MIN = -(1 << 63)
_I64_MAX = (1 << 63) - 1


class OrderType(IntEnum):
    """Order type codes (``venue::OrderType``)."""

    #: Immediate execution at the prevailing book.
    MARKET = 1
    #: Priced order; unfilled remainder rests.
    LIMIT = 2
    #: Immediate-or-cancel: marketable part fills, remainder cancels.
    IOC = 3
    #: Fill-or-kill: fills completely or not at all.
    FOK = 4
    #: Pegged order (tracked at the same-side touch).
    PEG = 5
    #: Midpoint order.
    MID = 6

    @classmethod
    def from_u8(cls, code: int) -> Optional["OrderType"]:
        """Decode a wire value; ``None`` for unknown codes."""
        for member in cls:
            if member.value == code:
                return member
        return None


def _check_domain(name: str, value: object, lo: int, hi: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if not (lo <= value <= hi):
        raise ValueError(f"{name} out of range [{lo}, {hi}]: {value}")


@dataclass(frozen=True)
class OrderRequest:
    """Strategy order request (wire domains enforced on construction)."""

    #: Client order id (unique per session), u64.
    order_id: int
    #: Target instrument, u32.
    instrument_id: int
    #: BID=0 buys, ASK=1 sells (u8; the domain check is a risk rule).
    side: int
    #: Base units, i64 (> 0 is a risk rule).
    qty: int
    #: Limit price in ticks, i64; 0 for unpriced orders.
    price_ticks: int
    #: Order type code (u8; see :class:`OrderType`).
    order_type: int
    #: Target venue, u16; 0 = route via SOR.
    venue_id: int
    #: Submitting strategy.
    strategy_id: str
    #: 0 = fully passive, 1 = immediate.
    urgency: float
    #: Submission event time (ns), i64.
    timestamp: int

    def __post_init__(self) -> None:
        _check_domain("order_id", self.order_id, 0, _U64_MAX)
        _check_domain("instrument_id", self.instrument_id, 0, _U32_MAX)
        _check_domain("side", self.side, 0, _U8_MAX)
        _check_domain("qty", self.qty, _I64_MIN, _I64_MAX)
        _check_domain("price_ticks", self.price_ticks, _I64_MIN, _I64_MAX)
        _check_domain("order_type", self.order_type, 0, _U8_MAX)
        _check_domain("venue_id", self.venue_id, 0, _U16_MAX)
        _check_domain("timestamp", self.timestamp, _I64_MIN, _I64_MAX)
        if not isinstance(self.strategy_id, str):
            raise ValueError(f"strategy_id must be a str, got {self.strategy_id!r}")
        if isinstance(self.urgency, bool) or not isinstance(self.urgency, (int, float)):
            raise ValueError(f"urgency must be a float, got {self.urgency!r}")
        object.__setattr__(self, "urgency", float(self.urgency))

    def validation_error(self) -> Optional[str]:
        """Schema-level validation reason (``None`` when valid)."""
        return order_validation_error(self)


def order_validation_error(order: OrderRequest) -> Optional[str]:
    """Contract-level validation (``venue::order_validation_error``):
    ``None`` when valid, else the exact reason text."""
    if order.side > 1:
        return f"side must be 0 or 1: {order.side}"
    ot = OrderType.from_u8(order.order_type)
    if ot is None:
        return f"unknown order_type: {order.order_type}"
    if order.qty <= 0:
        return f"qty must be > 0: {order.qty}"
    if not (math.isfinite(order.urgency) and 0.0 <= order.urgency <= 1.0):
        return f"urgency must be in [0, 1]: {rust_display_f64(order.urgency)}"
    if ot == OrderType.MARKET:
        if order.price_ticks != 0:
            return f"MARKET order must carry price_ticks 0: {order.price_ticks}"
    elif ot == OrderType.LIMIT:
        if order.price_ticks <= 0:
            return f"LIMIT order needs price_ticks > 0: {order.price_ticks}"
    elif ot in (OrderType.IOC, OrderType.FOK):
        # IOC/FOK may be priced (limit-style) or unpriced (market-style)
        if order.price_ticks < 0:
            return f"price_ticks must be >= 0: {order.price_ticks}"
    else:
        # PEG/MID carry no price (the venue derives it)
        if order.price_ticks != 0:
            return f"PEG/MID orders carry price_ticks 0: {order.price_ticks}"
    return None


@dataclass(frozen=True)
class Fill:
    """A fill notification (from the venue layer / drop copy)."""

    #: Event time (ns), i64.
    ts: int
    #: Strategy the fill belongs to.
    strategy_id: str
    #: Filled instrument, u32.
    instrument_id: int
    #: Originating order id (0 = external adjustment, no open order), u64.
    order_id: int
    #: BID=0 buy, ASK=1 sell (u8; > 1 is a malformed fill).
    side: int
    #: Filled quantity, i64 (<= 0 is a malformed fill).
    qty: int
    #: Fill price in ticks, i64 (<= 0 is a malformed fill).
    price_ticks: int

    def __post_init__(self) -> None:
        _check_domain("ts", self.ts, _I64_MIN, _I64_MAX)
        _check_domain("instrument_id", self.instrument_id, 0, _U32_MAX)
        _check_domain("order_id", self.order_id, 0, _U64_MAX)
        _check_domain("side", self.side, 0, _U8_MAX)
        _check_domain("qty", self.qty, _I64_MIN, _I64_MAX)
        _check_domain("price_ticks", self.price_ticks, _I64_MIN, _I64_MAX)
        if not isinstance(self.strategy_id, str):
            raise ValueError(f"strategy_id must be a str, got {self.strategy_id!r}")

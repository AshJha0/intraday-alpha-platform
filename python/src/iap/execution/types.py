"""Execution-layer records and enums (mirror of ``cpp/include/iap/execution/execution.hpp``).

Every enum value is pinned to the C++ ``std::uint8_t`` code; every price is
``int64`` ticks, every quantity ``int64`` base units and every timestamp
``int64`` ns (PLATFORM_CONVENTIONS.md section 1). Money (fees, impact) is a
research double reported to 1e-9.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

I64_MAX = (1 << 63) - 1


class OrderType(IntEnum):
    """Child order type. The simulator has no PEG/MID types (documented optimism)."""

    MARKET = 0
    LIMIT = 1
    IOC = 2
    FOK = 3


class OrderState(IntEnum):
    """Child order lifecycle state."""

    PENDING = 0  #: submitted, in flight to the venue
    ACTIVE = 1  #: resting passively at the venue
    FILLED = 2
    CANCELLED = 3  #: includes IOC/FOK/MARKET unfilled remainders


class Liquidity(IntEnum):
    """Which side of the spread a fill took."""

    TAKER = 0
    MAKER = 1


class CancelReason(IntEnum):
    """Why a child order ended CANCELLED (rules 3, 7, 8)."""

    NONE = 0  #: not cancelled
    UNFILLED_REMAINDER = 1  #: MARKET/IOC remainder, FOK miss
    VENUE_NOT_TRADING = 2  #: rule 8: arrived while halted/auction/stale
    USER = 3  #: cancel() arrived (rule 7)
    EXPIRED = 4  #: time-in-force (expire_ts)
    END_OF_STREAM = 5  #: cancel_all()


@dataclass(frozen=True, slots=True)
class VenueSpec:
    """One venue's execution profile (``configs/venues/venues.json``).

    Equity venues charge ``taker_fee_per_share`` on aggressive fills and
    rebate ``maker_rebate_per_share`` on passive fills; FX venues charge
    ``commission_per_million`` of notional on every fill (rule 5). The
    latency leg is ``latency_mean_ns`` + one SplitMix64 draw uniform on
    ``[0, latency_jitter_ns]`` (rule 1).
    """

    venue_id: int
    name: str = ""
    is_fx: bool = False
    taker_fee_per_share: float = 0.0
    maker_rebate_per_share: float = 0.0
    commission_per_million: float = 0.0
    latency_mean_ns: int = 0
    latency_jitter_ns: int = 0

    def __post_init__(self) -> None:
        if not (0 <= self.venue_id <= 0xFFFF):
            raise ValueError(f"venue_id must be u16, got {self.venue_id}")
        if self.latency_mean_ns < 0 or self.latency_jitter_ns < 0:
            raise ValueError(
                f"venue {self.venue_id}: latency mean/jitter must be >= 0"
            )


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Instrument reference data the simulator needs.

    ``qty_unit`` is the real base units per qty unit: ``lot_size`` for FX
    (1 qty unit = 1,000 base ccy), 1 for EQUITY/ETF whose qty is already in
    shares (conventions section 1). ``adv`` is the average daily volume in
    base units. ``notional = qty * qty_unit * price_ticks * tick_size`` in
    ``quote_ccy``.
    """

    instrument_id: int
    tick_size: float
    qty_unit: float = 1.0
    adv: float = 1.0
    quote_ccy: str = "USD"

    def __post_init__(self) -> None:
        if not (0 <= self.instrument_id <= 0xFFFFFFFF):
            raise ValueError(f"instrument_id must be u32, got {self.instrument_id}")
        if not (self.tick_size > 0.0 and self.qty_unit > 0.0 and self.adv > 0.0):
            raise ValueError(
                f"tick_size, qty_unit and adv must be > 0 for instrument "
                f"{self.instrument_id}"
            )
        if not self.quote_ccy:
            raise ValueError(
                f"quote_ccy must be non-empty for instrument {self.instrument_id}"
            )


@dataclass(frozen=True, slots=True)
class LatencyConfig:
    """Internal (decision -> wire) latency legs; the venue leg comes from VenueSpec.

    Defaults are the ones pinned by the golden replay-fills scenario.
    """

    decision_ns: int = 50_000
    risk_ns: int = 50_000
    wire_ns: int = 100_000

    def __post_init__(self) -> None:
        if self.decision_ns < 0 or self.risk_ns < 0 or self.wire_ns < 0:
            raise ValueError("latency legs must be >= 0")

    @property
    def internal_ns(self) -> int:
        """decision + risk + wire (the part of rule 1 that does not depend on the venue)."""
        return self.decision_ns + self.risk_ns + self.wire_ns


@dataclass(slots=True)
class ExecCounters:
    """Named counters (conventions section 8: every drop is counted)."""

    venue_not_trading_cancels: int = 0  #: rule 8 aggressive arrivals
    expired_orders: int = 0  #: rule 7 time-in-force
    user_cancels: int = 0  #: rule 7 cancel arrivals
    reopen_touch_fills: int = 0  #: rule 8 uncross fills
    overlay_thinned_fills: int = 0  #: rule 3b: a walk saw consumed liquidity

    def to_dict(self) -> dict:
        """Counters as a plain dict (exact integers)."""
        return {
            "venue_not_trading_cancels": self.venue_not_trading_cancels,
            "expired_orders": self.expired_orders,
            "user_cancels": self.user_cancels,
            "reopen_touch_fills": self.reopen_touch_fills,
            "overlay_thinned_fills": self.overlay_thinned_fills,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    """One simulated fill.

    Aggressive fills are stamped with the child's ``arrival_ts``; passive
    fills with the triggering event's ``exchange_ts`` (rules 2/4). ``fee``
    is > 0 for a cost, < 0 for a rebate; ``impact_cost`` is the linear
    impact charge (taker fills only, rule 6).
    """

    fill_id: int
    order_id: int
    parent_id: int
    instrument_id: int
    venue_id: int
    side: int  #: side of OUR order (0 buy / 1 sell)
    price_ticks: int
    qty: int
    ts: int  #: exchange_ts of the fill
    liquidity: Liquidity
    fee: float
    impact_cost: float

    def to_dict(self) -> dict:
        """Row in the key order of ``tests/golden/expected_replay_fills.json``."""
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "parent_id": self.parent_id,
            "instrument_id": self.instrument_id,
            "venue_id": self.venue_id,
            "side": self.side,
            "price_ticks": self.price_ticks,
            "qty": self.qty,
            "ts": self.ts,
            "liquidity": self.liquidity.name,
            "fee": self.fee,
            "impact_cost": self.impact_cost,
        }


@dataclass(slots=True)
class ChildOrder:
    """A child order worked by the simulator.

    The caller fills the request fields; the simulator owns the runtime
    state (``arrival_ts`` onwards) and resets it on ``submit``.
    """

    # ---- request fields (caller) ----
    parent_id: int = 0
    instrument_id: int = 0
    venue_id: int = 0
    side: int = 0  #: 0 = buy, 1 = sell
    type: OrderType = OrderType.LIMIT
    limit_ticks: int = 0  #: ignored for MARKET
    qty: int = 0
    decision_ts: int = 0
    expire_ts: int = 0  #: 0 = good till cancelled (rule 7)
    # ---- simulator-owned runtime state ----
    order_id: int = 0  #: assigned by submit()
    arrival_ts: int = 0
    state: OrderState = OrderState.PENDING
    remaining: int = 0
    ahead_qty: int = 0  #: displayed qty ahead of us at our level
    resting: bool = False
    cross_exempt: bool = False  #: crossing-rule exemption (rule 4)
    cancel_reason: CancelReason = CancelReason.NONE
    cancel_arrival_ts: int = 0  #: 0 = no cancel in flight

    @property
    def is_terminal(self) -> bool:
        """True once the order is FILLED or CANCELLED."""
        return self.state in (OrderState.FILLED, OrderState.CANCELLED)

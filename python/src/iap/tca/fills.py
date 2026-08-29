"""TCA data model: market timeline, child fills, parent orders (spec §19).

Prices here are research doubles (already converted from ticks via the
instrument tick_size); quantities are int base units; timestamps int ns.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Fill:
    """One child execution of a parent order."""

    ts: int
    price: float
    qty: int
    mid_at_fill: float
    half_spread_at_fill: float
    opp_depth_at_fill: int  # displayed size on the contra side when filling


@dataclass
class ParentOrder:
    """A simulated parent order with its child fills."""

    order_id: int
    instrument_id: int
    side: int                # 0 = BID (buy), 1 = ASK (sell)
    qty_target: int
    decision_ts: int         # when the signal fired
    arrival_ts: int          # when the first child could act (post-delay)
    end_ts: int              # end of the execution horizon
    fills: List[Fill] = field(default_factory=list)

    @property
    def sign(self) -> int:
        """+1 for buys, -1 for sells."""
        return 1 if self.side == 0 else -1

    @property
    def qty_filled(self) -> int:
        return sum(f.qty for f in self.fills)

    @property
    def fill_vwap(self) -> float:
        q = self.qty_filled
        if q == 0:
            return float("nan")
        return sum(f.price * f.qty for f in self.fills) / q


class MarketTimeline:
    """Event-time market state series with prevailing-state lookup.

    Parallel arrays (ts non-decreasing).  ``prevailing(t)`` returns the index
    of the latest state with ts <= t (or None before the first state) —
    exactly the label-alignment convention of ``iap.labels``.
    """

    def __init__(self) -> None:
        self.ts: List[int] = []
        self.bid: List[float] = []
        self.ask: List[float] = []
        self.bid_sz: List[int] = []
        self.ask_sz: List[int] = []
        self.trades: List[Tuple[int, float, int]] = []  # (ts, price, qty)

    def append(self, ts: int, bid: float, ask: float,
               bid_sz: int, ask_sz: int) -> None:
        if self.ts and ts < self.ts[-1]:
            raise ValueError("timeline timestamps must be non-decreasing")
        if ask < bid:
            raise ValueError("crossed timeline state")
        self.ts.append(ts)
        self.bid.append(bid)
        self.ask.append(ask)
        self.bid_sz.append(bid_sz)
        self.ask_sz.append(ask_sz)

    def add_trade(self, ts: int, price: float, qty: int) -> None:
        self.trades.append((ts, price, qty))

    def __len__(self) -> int:
        return len(self.ts)

    def prevailing(self, t: int) -> Optional[int]:
        """Index of the latest state with ts <= t, or None."""
        i = bisect.bisect_right(self.ts, t) - 1
        return i if i >= 0 else None

    def mid(self, i: int) -> float:
        return 0.5 * (self.bid[i] + self.ask[i])

    def half_spread(self, i: int) -> float:
        return 0.5 * (self.ask[i] - self.bid[i])

    def mid_at(self, t: int) -> float:
        """Prevailing mid at time t (NaN before the first state)."""
        i = self.prevailing(t)
        return float("nan") if i is None else self.mid(i)

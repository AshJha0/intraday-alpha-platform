"""TCA data model: market timeline, child fills, parent orders (spec §19).

Prices here are research doubles (already converted from ticks via the
instrument tick_size); quantities are int base units; timestamps int ns.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


#: Fill liquidity flags (API_PORTFOLIO_TCA.md §2.4).
TAKER = "TAKER"
MAKER = "MAKER"


@dataclass(frozen=True)
class Fill:
    """One child execution of a parent order.

    ``mid_at_fill`` / ``half_spread_at_fill`` are the reference state of the
    fill (pinned §2.4): the state prevailing at ``ts`` for a TAKER fill, the
    state strictly BEFORE ``ts`` for a MAKER fill (a passive fill is caused by
    the event stamped ``ts``; the post-event state already reflects the
    trade-through that hit us). Use :func:`stamp_fill` to build one from a
    timeline.
    """

    ts: int
    price: float
    qty: int
    mid_at_fill: float
    half_spread_at_fill: float
    opp_depth_at_fill: int  # displayed size on the contra side when filling
    liquidity: str = TAKER


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
        #: HALT start timestamps (a markout window containing one is undefined)
        self.halts: List[int] = []
        #: crossed consolidated states skipped by the builder (pinned §2.1)
        self.crossed_states_skipped: int = 0

    def append(self, ts: int, bid: float, ask: float,
               bid_sz: int, ask_sz: int) -> None:
        """Append one BBO state. Locked (bid == ask, half-spread 0) is a
        legal state; crossed (ask < bid) is not (builders skip + count it)."""
        if self.ts and ts < self.ts[-1]:
            raise ValueError("timeline timestamps must be non-decreasing")
        if ask < bid:
            raise ValueError("crossed timeline state")
        self.ts.append(ts)
        self.bid.append(bid)
        self.ask.append(ask)
        self.bid_sz.append(bid_sz)
        self.ask_sz.append(ask_sz)

    def append_state_pinned(self, ts: int, bid: float, ask: float,
                            bid_sz: int, ask_sz: int) -> bool:
        """Builder rule (pinned §2.1): a CROSSED state (ask < bid) is skipped
        and counted in ``crossed_states_skipped`` (False); a LOCKED state is
        appended like any other (True)."""
        if ask < bid:
            self.crossed_states_skipped += 1
            return False
        self.append(ts, bid, ask, bid_sz, ask_sz)
        return True

    def add_trade(self, ts: int, price: float, qty: int) -> None:
        self.trades.append((ts, price, qty))

    def add_halt(self, ts: int) -> None:
        """Record a HALT status at ``ts`` (event time)."""
        self.halts.append(ts)

    @property
    def last_ts(self) -> Optional[int]:
        """Timestamp of the last state (None when empty)."""
        return self.ts[-1] if self.ts else None

    def halt_in(self, start_ts: int, end_ts: int) -> bool:
        """True when a HALT started inside ``(start_ts, end_ts]``."""
        return any(start_ts < h <= end_ts for h in self.halts)

    def mid_defined_at(self, t: int, after_ts: Optional[int] = None) -> bool:
        """Pinned §2.5 'defined' rule: a prevailing mid exists at ``t``
        (``t`` lies inside the timeline, ``last_ts >= t``) and, when
        ``after_ts`` is given, no HALT started inside ``(after_ts, t]``."""
        if self.prevailing(t) is None or self.ts[-1] < t:
            return False
        if after_ts is not None and self.halt_in(after_ts, t):
            return False
        return True

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


def stamp_fill(timeline: MarketTimeline, ts: int, price: float, qty: int,
               side: int, liquidity: str = TAKER) -> Fill:
    """Build a :class:`Fill` with the pinned reference state (§2.4): the
    state prevailing at ``ts`` for a TAKER fill, the state strictly before
    ``ts`` (``prevailing(ts - 1)``) for a MAKER fill. Raises when no state
    prevails (TCA never guesses reference prices)."""
    if liquidity not in (TAKER, MAKER):
        raise ValueError(f"liquidity must be TAKER or MAKER, got {liquidity!r}")
    if side not in (0, 1):
        raise ValueError("side must be 0 (buy) or 1 (sell)")
    ref_ts = ts if liquidity == TAKER else ts - 1
    i = timeline.prevailing(ref_ts)
    if i is None:
        raise ValueError(f"fill at {ts} precedes the first market state")
    opp = timeline.ask_sz[i] if side == 0 else timeline.bid_sz[i]
    return Fill(ts=ts, price=price, qty=qty, mid_at_fill=timeline.mid(i),
                half_spread_at_fill=timeline.half_spread(i),
                opp_depth_at_fill=opp, liquidity=liquidity)

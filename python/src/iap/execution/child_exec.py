"""Venue-aware child execution (v1.18 REL-04; review ids X-B, X-C, X-E).

The v1.17 IS / POV / AGGRESSIVE children are 1000-share MARKET orders walked
down 10 levels of a venue whose displayed touch can be 19-155 shares (IEX):
the order is sized for the parent, not for what the venue shows, the
remainder is cancelled and, for IS, never re-posted.  ``depth_exhausted``
counted it but nothing acted on it.

``ParentOrder.child_exec = ChildExecParams(...)`` (opt-in, default ``None`` =
the pinned behaviour) replaces that child with:

``style="capped_ioc"``
    a marketable IOC LIMIT capped at the ELIGIBLE displayed depth: the opposite
    side's top ``max_levels`` levels within ``max_slippage_ticks`` of the touch
    (0 = no band), net of what our own earlier fills already consumed
    (simulator rule 3b: displayed liquidity is never consumed twice), times
    ``depth_fraction``.  The limit price is the deepest eligible level, so the
    simulator can never walk past what the child was sized for.
``style="midpoint_peg"``
    a non-displayed midpoint-peg LIMIT (``ChildOrder.peg``; IEX's midpoint
    peg), limit = the opposite touch (never pay through it), time-in-force
    ``deadline_ns`` (0 = the parent window).  It rests off the display and fills
    only against hidden prints at the midpoint (simulator ``hidden_prints``).

Quantity the cap withholds, and the unfilled remainder of an IOC or an expired
peg, is **owed**, not dropped: the parent keeps explicit remaining-quantity
state (``carry``), re-posts it no sooner than ``repost_interval_ns`` apart and
at most ``max_reposts`` times, and anything still owed at the window end is
reported as RESIDUAL (``ParentReport.residual_qty``), never filled.  POV
parents carry nothing: the next TRADE's deficit (target minus committed)
is their re-post.

``max_participation`` (0 = off) caps the parent's issued quantity at that
fraction of the TRADE-print volume of the instrument on the EXECUTING venue
since the parent window opened (the volume our child can actually trade
against; our own simulated fills never enter the stream).

Failure counters, never silent: ``cap_no_depth`` (a post found no eligible
depth), ``cap_withheld_qty`` (qty the cap held back at least once),
``reposts``, ``residual_qty`` and ``depth_exhausted`` of the legacy MARKET
path all appear in :class:`~iap.execution.replay.ParentReport` and the
benchmark's ``failures`` block.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

STYLES: tuple[str, ...] = ("capped_ioc", "midpoint_peg")


@dataclass(frozen=True, slots=True)
class ChildExecParams:
    """Child-order policy of one parent (module docstring)."""

    style: str = "capped_ioc"
    #: displayed levels of the opposite side that may be used
    max_levels: int = 3
    #: fraction of the eligible net displayed depth the child may take
    depth_fraction: float = 1.0
    #: eligible levels lie within this many ticks of the touch (0 = no band)
    max_slippage_ticks: int = 0
    #: cap on issued qty as a fraction of the executing venue's TRADE volume (0 = off)
    max_participation: float = 0.0
    #: minimum spacing of re-posts of owed quantity
    repost_interval_ns: int = 1_000_000_000
    max_reposts: int = 20
    #: smallest child worth sending (shares)
    min_child_qty: int = 1
    #: midpoint_peg time-in-force (0 = until the parent window ends)
    deadline_ns: int = 0

    def __post_init__(self) -> None:
        if self.style not in STYLES:
            raise ValueError(f"style must be one of {STYLES}")
        if self.max_levels < 1:
            raise ValueError("max_levels must be >= 1")
        if not 0.0 < self.depth_fraction <= 1.0:
            raise ValueError("depth_fraction must be in (0, 1]")
        if self.max_slippage_ticks < 0 or self.deadline_ns < 0:
            raise ValueError("max_slippage_ticks and deadline_ns must be >= 0")
        if not 0.0 <= self.max_participation <= 1.0:
            raise ValueError("max_participation must be in [0, 1]")
        if self.repost_interval_ns < 0 or self.max_reposts < 0 or self.min_child_qty < 1:
            raise ValueError("repost_interval_ns / max_reposts >= 0, min_child_qty >= 1")


def eligible_depth(
    levels: Sequence[tuple[int, int]],
    side: int,
    params: ChildExecParams,
    consumed: Callable[[int], int],
) -> tuple[int, int]:
    """``(eligible qty, limit price in ticks)`` of one marketable child.

    ``levels`` are the OPPOSITE side's displayed ``(price_ticks, size)`` best
    first; ``side`` is OUR side (0 = buy walks the asks); ``consumed(price)``
    is what our own earlier fills already took at that level.  Returns
    ``(0, 0)`` when nothing is eligible.  The limit is the deepest price that
    contributed, so a child sized from this can never fill beyond it.
    """
    if not levels:
        return 0, 0
    touch = levels[0][0]
    total = 0
    limit = 0
    for k, (price, size) in enumerate(levels):
        if k >= params.max_levels:
            break
        if params.max_slippage_ticks and abs(price - touch) > params.max_slippage_ticks:
            break
        avail = max(size - consumed(price), 0)
        if avail > 0:
            total += avail
            limit = price
    del side  # the levels are already the side we trade against
    return int(total * params.depth_fraction), limit if total else 0


def participation_room(
    params: ChildExecParams, venue_volume: int, issued_qty: int, parent_qty: int
) -> int:
    """Shares the participation cap still allows (``parent_qty`` when off)."""
    if params.max_participation <= 0.0:
        return parent_qty
    return max(int(params.max_participation * venue_volume) - issued_qty, 0)

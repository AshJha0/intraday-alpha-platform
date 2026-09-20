"""Smart order routing (spec section 17: multi-venue routing research).

Python reference port of ``cpp/include/iap/sor/sor.hpp`` + ``sor.cpp``.
Deterministic venue selection over the per-venue books of one instrument
(PLATFORM_CONVENTIONS.md section 11.3):

- **Eligible venue**: a candidate whose book exists, is not stale, whose
  status is TRADING and whose configured ``latency_mean_ns`` is within
  ``SorOptions.max_venue_latency_ns``. A stale, halted or missing venue
  book is NEVER routed to, whatever the alternative.
- ``route_aggressive``: among eligible venues quoting the opposite side,
  the most favourable displayed best price (lowest ask for a buy / highest
  bid for a sell). Ties break to the lower ``taker_fee_per_share``, then to
  the lower ``commission_per_million`` (FX venues charge per notional,
  their per-share fee is 0), then to the lower venue_id.
- ``route_passive``: among eligible venues quoting OUR side, the highest
  maker rebate when ``prefer_rebate`` (ties: lower ``commission_per_million``,
  then lower venue_id); with ``prefer_rebate`` off, the lowest venue_id
  quoting our side.
- **No route** (``NO_ROUTE == 0``): no eligible venue quotes the needed
  side. The caller MUST NOT submit (``ExecutionReplay`` skips the child and
  counts ``sor_no_route``). An empty candidate list raises ValueError.

All iteration is in ascending venue_id order — same candidates + same
books => same route.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from iap.execution.config import SorOptions
from iap.execution.simulator import ExecutionSimulator
from iap.execution.types import VenueSpec
from iap.orderbook.book import ConsolidatedBook, OrderBook

#: Route result meaning "no eligible venue quotes the needed side".
NO_ROUTE = 0


def _sorted_candidates(candidates: Sequence[int]) -> list:
    if not candidates:
        raise ValueError("SOR: empty candidate venue list")
    return sorted(candidates)


class SmartOrderRouter:
    """Deterministic venue selection (rules in the module docstring)."""

    def __init__(
        self, venues: Mapping[int, VenueSpec], options: SorOptions = SorOptions()
    ) -> None:
        self._venues = dict(sorted(venues.items()))
        self._options = options

    @property
    def options(self) -> SorOptions:
        return self._options

    def _eligible(self, book: ConsolidatedBook, vid: int) -> Optional[OrderBook]:
        """The venue book when the venue is eligible, else None."""
        vb = book.books.get(vid)
        if vb is None or not ExecutionSimulator.venue_open(vb):
            return None
        spec = self._venues.get(vid)
        if spec is not None and spec.latency_mean_ns > self._options.max_venue_latency_ns:
            return None
        return vb

    def route_aggressive(
        self, book: ConsolidatedBook, side: int, candidates: Sequence[int]
    ) -> int:
        """Venue with the most favourable displayed opposite best; ``NO_ROUTE`` if none."""
        have = False
        best_vid = NO_ROUTE
        best_price = 0
        best_fee = 0.0
        best_comm = 0.0
        for vid in _sorted_candidates(candidates):
            vb = self._eligible(book, vid)
            if vb is None:
                continue
            quote = vb.best_ask() if side == 0 else vb.best_bid()
            if quote is None:
                continue
            spec = self._venues.get(vid)
            fee = 0.0 if spec is None else spec.taker_fee_per_share
            comm = 0.0 if spec is None else spec.commission_per_million
            price = quote[0]
            better = (
                not have
                or (price < best_price if side == 0 else price > best_price)
                or (
                    price == best_price
                    and (fee < best_fee or (fee == best_fee and comm < best_comm))
                )
            )
            if better:
                have = True
                best_vid = vid
                best_price = price
                best_fee = fee
                best_comm = comm
        return best_vid if have else NO_ROUTE

    def route_passive(
        self, book: ConsolidatedBook, side: int, candidates: Sequence[int]
    ) -> int:
        """Venue to rest on (rebate preference per options); ``NO_ROUTE`` if none."""
        have = False
        best_vid = NO_ROUTE
        best_rebate = 0.0
        best_comm = 0.0
        for vid in _sorted_candidates(candidates):
            vb = self._eligible(book, vid)
            if vb is None:
                continue
            quote = vb.best_bid() if side == 0 else vb.best_ask()
            if quote is None:
                continue
            if not self._options.prefer_rebate:
                return vid  # lowest eligible venue id quoting our side
            spec = self._venues.get(vid)
            rebate = 0.0 if spec is None else spec.maker_rebate_per_share
            comm = 0.0 if spec is None else spec.commission_per_million
            if not have or rebate > best_rebate or (rebate == best_rebate and comm < best_comm):
                have = True
                best_vid = vid
                best_rebate = rebate
                best_comm = comm
        return best_vid if have else NO_ROUTE

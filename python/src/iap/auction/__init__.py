"""Auction imbalance (NOII) research: plan item A1 (v1.10).

* :mod:`iap.auction.stream` - opt-in extraction of ITCH 5.0 ``I`` (NOII) and
  ``Q`` (cross) messages into a separate auction-imbalance stream.  It never
  touches the normalized IAP1 output or the dataset version.
* :mod:`iap.auction.features` - imbalance ratio, reference-price drift,
  far-near spread, near-vs-reference and time-to-cross per NOII snapshot.
* :mod:`iap.auction.targets` - cross price vs mid at t, and the drift of the
  mid into the cross; the closing cross is the primary target and the
  opening cross the second (``cross_type="O"``).
* :mod:`iap.auction.strategy` - a minutes-horizon strategy that takes at
  the touch before the cross and exits in the cross (``exit="cross"``) or
  with a taker trade just before it (``exit="taker"``), priced with
  :class:`iap.backtest.costs.CostModel`; a backtest and a purged,
  day-aligned walk-forward built on :mod:`iap.validation`.

Research code only: nothing here is on the trading path.
"""

from iap.auction.features import auction_features
from iap.auction.strategy import (
    AuctionBacktestResult,
    AuctionStrategyConfig,
    backtest,
    decision_rows,
    walk_forward,
)
from iap.auction.stream import AuctionStream, extract_auction_stream, read_auction_stream
from iap.auction.targets import auction_targets

__all__ = [
    "AuctionBacktestResult",
    "AuctionStrategyConfig",
    "AuctionStream",
    "auction_features",
    "auction_targets",
    "backtest",
    "decision_rows",
    "extract_auction_stream",
    "read_auction_stream",
    "walk_forward",
]

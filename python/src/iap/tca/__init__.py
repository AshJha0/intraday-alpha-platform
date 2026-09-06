"""Research TCA: benchmarks, IS decomposition, impact, adverse selection (§19)."""

from iap.tca.fills import MAKER, TAKER, Fill, MarketTimeline, ParentOrder, stamp_fill
from iap.tca.simulator import build_timeline, simulate_parent_orders
from iap.tca.tca import (
    adverse_selection,
    adverse_selection_with_counts,
    arrival_slippage_bps,
    impact_regression,
    interval_twap,
    interval_vwap,
    order_tca,
    perold_decomposition,
    spread_and_impact_cost,
    validate_order_window,
)

__all__ = [
    "MAKER",
    "TAKER",
    "stamp_fill",
    "adverse_selection_with_counts",
    "validate_order_window",
    "Fill",
    "MarketTimeline",
    "ParentOrder",
    "build_timeline",
    "simulate_parent_orders",
    "perold_decomposition",
    "arrival_slippage_bps",
    "interval_vwap",
    "interval_twap",
    "spread_and_impact_cost",
    "impact_regression",
    "adverse_selection",
    "order_tca",
]

"""Research TCA: benchmarks, IS decomposition, impact, adverse selection (§19)."""

from iap.tca.fills import Fill, MarketTimeline, ParentOrder
from iap.tca.simulator import build_timeline, simulate_parent_orders
from iap.tca.tca import (
    adverse_selection,
    arrival_slippage_bps,
    impact_regression,
    interval_twap,
    interval_vwap,
    order_tca,
    perold_decomposition,
    spread_and_impact_cost,
)

__all__ = [
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

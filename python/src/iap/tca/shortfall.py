"""Per-parent shortfall decomposition of a replayed parent order.

The cost accounting of ``research/execution/run_execution_study.py`` (its
module docstring defines every column), lifted here in v1.17 B1 so the
implementation-shortfall benchmark (``iap.research.execution_benchmark``)
and the study share one copy. Costs are in the quote currency; positive =
worse than the arrival mid:

- ``shortfall`` = Perold implementation shortfall (:func:`iap.tca.tca.order_tca`,
  decision = arrival = the window start) = ``trading`` of what filled +
  ``opportunity``, the UNFILLED quantity priced at the arrival-to-end mid move;
- ``spread`` = half-spread paid (+) or earned (-) at each fill;
  ``price_vs_mid_residual`` = the rest of the fill price vs the fill-time mid;
  ``timing`` = the timing leg of ``order_tca``;
- ``fees_net`` = taker fees - maker rebates; ``sim_impact`` = the simulator's
  impact charge (rule 6 child adder or rule 6b parent adder:
  ``ExecConfig.impact_label()`` says which — it is an ADDER on replayed
  fills, never a measured market reaction);
- ``completion`` = finishing the unfilled quantity at the window end: the
  half-spread then plus the given taker fee, per unfilled share;
- ``all_in = shortfall + fees_net + sim_impact + completion``.
"""

from __future__ import annotations

from collections.abc import Iterable

from iap.execution.types import Fill, Liquidity
from iap.tca.fills import MAKER, TAKER, MarketTimeline, stamp_fill
from iap.tca.fills import ParentOrder as TcaParent
from iap.tca.tca import order_tca

COST_KEYS = (
    "shortfall",
    "trading",
    "opportunity",
    "spread",
    "price_vs_mid_residual",
    "timing",
    "fees_net",
    "sim_impact",
    "completion",
    "all_in",
)


def parent_costs(
    parent, fills: Iterable[Fill], timeline: MarketTimeline, tick: float, taker_fee: float
) -> dict[str, float]:
    """Cost decomposition of one parent (module docstring), currency.

    ``parent`` is an ``iap.execution.ParentOrder`` (``parent_id``,
    ``instrument_id``, ``side``, ``qty``, ``start_ts``, ``end_ts`` are read);
    ``fills`` its replay fills in fill order; ``taker_fee`` the per-share
    taker fee the ``completion`` leg charges.
    """
    order = TcaParent(
        order_id=parent.parent_id,
        instrument_id=parent.instrument_id,
        side=parent.side,
        qty_target=parent.qty,
        decision_ts=parent.start_ts,
        arrival_ts=parent.start_ts,
        end_ts=parent.end_ts,
    )
    fees_net = sim_impact = 0.0
    maker_qty = 0
    for f in fills:
        liq = MAKER if f.liquidity == Liquidity.MAKER else TAKER
        order.fills.append(stamp_fill(timeline, f.ts, f.price_ticks * tick, f.qty, f.side, liq))
        fees_net += f.fee
        sim_impact += f.impact_cost
        if f.liquidity == Liquidity.MAKER:
            maker_qty += f.qty
    rec = order_tca(order, timeline)
    perold = rec["perold"]
    unfilled = parent.qty - order.qty_filled
    i_end = timeline.prevailing(parent.end_ts)
    completion = unfilled * (timeline.half_spread(i_end) + taker_fee)
    shortfall = float(perold["total_is"])
    return {
        "notional": parent.qty * float(rec["decision_mid"]),
        "qty": parent.qty,
        "filled_qty": order.qty_filled,
        "maker_qty": maker_qty,
        "shortfall": shortfall,
        "trading": float(perold["trading_cost"]),
        "opportunity": float(perold["opportunity_cost"]),
        "spread": float(rec["spread_cost"]),
        "price_vs_mid_residual": float(rec["impact_cost"]),
        "timing": float(rec["timing_cost"]),
        "fees_net": fees_net,
        "sim_impact": sim_impact,
        "completion": completion,
        "all_in": shortfall + fees_net + sim_impact + completion,
    }

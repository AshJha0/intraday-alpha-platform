"""P&L attribution of one decision — the pinned decomposition.

For a parent order with signal ``s`` and TCA result ``t`` (all in bps of
traded notional, contributions signed so that negative = cost):

    alpha_bps  = sign(side) * s.expected_return * 1e4
                 (sign = +1 for BID / buy, -1 for ASK / sell: buying on a
                 positive forecast or selling on a negative one is a
                 positive alpha contribution)
    spread_bps = -t.spread_cost_bps
    impact_bps = -t.impact_bps
    fees_bps   = -t.fees_bps
    timing_bps = -t.timing_cost_bps
    total_bps  = alpha_bps + spread_bps + impact_bps + fees_bps + timing_bps

``total_bps`` is the model's explained P&L, not the realized one.  The
**residual** ``realized_bps - total_bps`` (unexplained by the five terms:
delay and opportunity cost, forecast error, drift after the horizon) is
reported next to the attribution by :func:`attribution_report`, never
folded into a component — a decomposition that always sums to the realized
number would hide exactly the error it is supposed to expose.

The identity ``total == sum`` is enforced by the ``Attribution`` contract
to 1e-9; every term is a finite double.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from iap.contracts.types import AlphaSignal, Attribution, ParentOrder, Side, TCAResult

__all__ = ["AttributionReport", "attribute", "attribution_report", "residual_bps"]


@dataclass(frozen=True)
class AttributionReport:
    """The attribution, the realized P&L it is compared with, and the
    unexplained residual ``realized_bps - attribution.total_bps``."""

    attribution: Attribution
    realized_bps: float
    residual_bps: float


def _side_sign(side: Side) -> float:
    return 1.0 if side is Side.BID else -1.0


def _check_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ValueError(f"attribute: {name} must be a finite number, got {value!r}")
    return float(value)


def attribute(parent: ParentOrder, signal: AlphaSignal, tca: TCAResult,
              realized_bps: float) -> Attribution:
    """The pinned decomposition (module doc) for ``parent``.

    ``realized_bps`` is the measured P&L of the order in bps of traded
    notional; it is checked finite here so that an attribution is never
    produced for an order whose outcome was not measured, and the residual
    against it is exposed by :func:`residual_bps` /
    :func:`attribution_report` rather than absorbed into a term.  The
    signal and TCA result must belong to the order's instrument.
    """
    if signal.instrument_id != parent.instrument_id:
        raise ValueError(f"attribute: signal instrument {signal.instrument_id} != "
                         f"order instrument {parent.instrument_id}")
    if tca.parent_order_id != parent.parent_order_id:
        raise ValueError(f"attribute: TCA parent order {tca.parent_order_id} != "
                         f"order {parent.parent_order_id}")
    _check_finite("realized_bps", realized_bps)
    alpha = _side_sign(parent.side) * signal.expected_return * 1e4
    spread = -tca.spread_cost_bps
    impact = -tca.impact_bps
    fees = -tca.fees_bps
    timing = -tca.timing_cost_bps
    return Attribution(alpha_bps=alpha, spread_bps=spread, impact_bps=impact,
                       fees_bps=fees, timing_bps=timing,
                       total_bps=alpha + spread + impact + fees + timing)


def residual_bps(attribution: Attribution, realized_bps: float) -> float:
    """``realized_bps - attribution.total_bps``: the part of the realized
    P&L the five terms do not explain."""
    return _check_finite("realized_bps", realized_bps) - attribution.total_bps


def attribution_report(parent: ParentOrder, signal: AlphaSignal, tca: TCAResult,
                       realized_bps: float) -> AttributionReport:
    """:func:`attribute` plus the residual, reported side by side."""
    attribution = attribute(parent, signal, tca, realized_bps)
    return AttributionReport(attribution=attribution, realized_bps=float(realized_bps),
                             residual_bps=residual_bps(attribution, realized_bps))

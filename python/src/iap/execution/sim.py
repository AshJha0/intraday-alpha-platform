"""EN4: execution and cost simulation (v2.0 U0 stage 5; E0 section 9, C6 and C8).

Family-agnostic: a net order is ``(name, qty, mid_decision)`` whatever
``(family_id, horizon, cohort_id)`` produced it; the ledger
(:mod:`iap.portfolio.ledger`) does the netting and the allocation, this module
prices the one external fill.

One fill-price convention (C6)::

    t_arrival = t_decision + latency           (C8: 500 ms, fixed; stepped only as a sensitivity)
    P_fill    = M_arrival + side * (spread_arrival / 2 + impact)

``impact`` is a deterministic function of the order's size over the displayed
depth at the touch at ``t_arrival`` and over the trailing 5-minute venue
volume, with fixed coefficients (:class:`ImpactCoefficients`, labelled
**estimated**, never calibrated to an outcome).  The three cost columns differ
**only** by the impact multiplier and the displayed-depth haircut::

    base          1.0x   100 % of displayed depth
    conservative  1.5x    75 %
    stress        2.0x    50 %

The decision-to-arrival mid movement is never a cost: the ledger reports it as
**latency drift** from the same :class:`~iap.portfolio.ledger.Fill` record, so
the waterfall reconciles exactly to the cash identity.

Failure cases (section 9), each deterministic and recorded on the
:class:`FillOutcome` with its reason: quote disappears, stale or crossed before
arrival (refuse); insufficient displayed depth (partial, remainder refused:
never filled at a worse price by assumption); price through the entry
threshold (refuse, discretionary entries only); participation breach (scale
to the cap, drop below the minimum ticket); halt (refuse: the ledger's risk
exit happens at the first valid quote after resumption, else the stress
valuation); data gap (no quote: refuse; :func:`closeout_valuation` gives the
flagged stress valuation of what remains); pre-close liquidation is a
mandatory order like any other (the ledger raises it).

Daily-horizon families execute at the close or the open (:func:`simulate_daily`)
against the auction print, with the same participation limit and a square-root
impact model ``impact = c * sigma_daily * sqrt(q / ADV) * price`` whose
coefficient is **labelled estimated** (its provenance string travels with every
fill); the auction has no quoted spread, so ``half_spread = 0`` and the
convention still holds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iap.portfolio.ledger import Fill, Valuation

__all__ = [
    "COLUMNS",
    "LATENCY_NS",
    "AuctionPrint",
    "CostColumn",
    "DailyImpactModel",
    "FeeSchedule",
    "FillOutcome",
    "ImpactCoefficients",
    "Market",
    "Quote",
    "SimError",
    "closeout_valuation",
    "expected_cost_bps",
    "impact_per_share",
    "simulate",
    "simulate_columns",
    "simulate_daily",
]

LATENCY_NS = 500_000_000  # C8: t_arrival = t_decision + 500 ms
QUOTE_STALE_NS = 5_000_000_000  # section 3: a quote older than 5 s is stale


class SimError(ValueError):
    """Malformed simulator inputs (a refusal is a result, not an error)."""


@dataclass(frozen=True, slots=True)
class CostColumn:
    name: str
    impact_multiplier: float
    depth_haircut: float  # fraction of displayed depth assumed present on arrival

    def __post_init__(self) -> None:
        if self.impact_multiplier <= 0 or not 0 < self.depth_haircut <= 1:
            raise SimError("impact_multiplier > 0 and 0 < depth_haircut <= 1")


COLUMNS: dict[str, CostColumn] = {
    "base": CostColumn("base", 1.0, 1.0),
    "conservative": CostColumn("conservative", 1.5, 0.75),
    "stress": CostColumn("stress", 2.0, 0.5),
}


@dataclass(frozen=True, slots=True)
class ImpactCoefficients:
    """Fixed impact coefficients (labelled estimated; never fitted to an outcome).

    ``impact_per_share = mid * (c_depth * (q / depth_at_touch) + c_volume * sqrt(q / volume_5m))``
    in price units; ``c_depth`` and ``c_volume`` are fractions of the mid.
    """

    c_depth: float = 0.0002
    c_volume: float = 0.0010
    label: str = "estimated"
    provenance: str = "E0 section 9 planning values; to be replaced by measured development-subset costs (C7)"

    def __post_init__(self) -> None:
        if self.c_depth < 0 or self.c_volume < 0:
            raise SimError("impact coefficients are >= 0")


@dataclass(frozen=True, slots=True)
class DailyImpactModel:
    """Square-root impact for daily horizons: ``impact = c * sigma_daily * sqrt(q / adv) * price``."""

    c: float = 1.0
    label: str = "estimated"
    provenance: str = "coefficient to be calibrated from the intraday evidence (REL-03, B8, REL-05); planning value"

    def __post_init__(self) -> None:
        if self.c < 0:
            raise SimError("c >= 0")


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Dated fees: per share plus a fraction of notional (both >= 0)."""

    per_share: float = 0.0005
    notional_rate: float = 0.0
    dated: str = "2026-10-24"

    def fee(self, qty: int, price: float) -> float:
        return abs(qty) * self.per_share + abs(qty) * price * self.notional_rate


@dataclass(frozen=True, slots=True)
class Quote:
    """The quote at ``ts``: ``size`` fields are displayed depth at the touch in shares."""

    ts: int
    bid: float
    ask: float
    bid_size: int
    ask_size: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def valid(self, at_ts: int, stale_ns: int = QUOTE_STALE_NS) -> str:
        """'' when usable at ``at_ts``; else the failure cause."""
        if not (math.isfinite(self.bid) and math.isfinite(self.ask)) or self.bid <= 0 or self.ask <= 0:
            return "no quote"
        if self.ask < self.bid:
            return "crossed quote"
        if self.ask == self.bid:
            return "locked quote"
        if at_ts - self.ts > stale_ns:
            return "stale quote"
        if self.ts > at_ts:
            return "quote from the future"
        return ""


@dataclass(frozen=True, slots=True)
class Market:
    """Conditions of one name at arrival: the quote (None = quote disappeared / data gap), trailing volume, halt."""

    name: str
    quote: Quote | None
    volume_5m: int
    halted: bool = False


@dataclass(frozen=True, slots=True)
class FillOutcome:
    name: str
    column: str
    requested: int
    filled: int
    refused: int
    reason: str  # '' when fully filled
    fill: Fill | None
    arrival_ts: int
    participation: float  # |submitted after scaling| / volume_5m
    depth_available: int


@dataclass(frozen=True, slots=True)
class AuctionPrint:
    ts: int
    price: float
    volume: int
    sigma_daily: float
    adv: int


def impact_per_share(
    qty: int,
    mid: float,
    depth: int,
    volume_5m: int,
    column: CostColumn,
    coeffs: ImpactCoefficients,
) -> float:
    """Deterministic impact in price units for ``|qty|`` shares against ``depth`` (post-haircut) and 5-minute volume."""
    q = abs(qty)
    if q == 0:
        return 0.0
    d = max(depth, 1)
    v = max(volume_5m, 1)
    raw = mid * (coeffs.c_depth * (q / d) + coeffs.c_volume * math.sqrt(q / v))
    return column.impact_multiplier * raw


def expected_cost_bps(
    qty: int,
    quote: Quote,
    volume_5m: int,
    column: CostColumn = COLUMNS["conservative"],
    coeffs: ImpactCoefficients | None = None,
    fees: FeeSchedule | None = None,
) -> float:
    """One-way expected cost of ``qty`` in bps of its notional (half-spread + impact + fees): the hurdle's C_entry / C_exit."""
    coeffs = coeffs or ImpactCoefficients()
    fees = fees or FeeSchedule()
    mid = quote.mid
    depth = quote.ask_size if qty > 0 else quote.bid_size
    depth = int(math.floor(depth * column.depth_haircut))
    per_share = quote.spread / 2.0 + impact_per_share(qty, mid, depth, volume_5m, column, coeffs)
    cost = abs(qty) * per_share + fees.fee(qty, mid)
    return 1e4 * cost / (abs(qty) * mid) if qty else 0.0


def simulate(
    *,
    ts_decision: int,
    name: str,
    qty: int,
    mid_decision: float,
    market: Market,
    column: CostColumn | str = "conservative",
    coeffs: ImpactCoefficients | None = None,
    fees: FeeSchedule | None = None,
    participation_cap: float = 0.05,
    min_ticket: int = 1,
    limit_price: float | None = None,
    mandatory: bool = False,
    latency_ns: int = LATENCY_NS,
) -> FillOutcome:
    """Price the aggregate net order ``qty`` (signed shares) in ``name`` under one column.

    ``limit_price`` is the entry threshold of a discretionary order (E0
    section 9 "price through the entry threshold"): a fill price beyond it
    refuses the order.  Mandatory orders ignore the threshold and the minimum
    ticket but still respect depth and participation.
    """
    col = COLUMNS[column] if isinstance(column, str) else column
    coeffs = coeffs or ImpactCoefficients()
    fees = fees or FeeSchedule()
    if isinstance(qty, bool) or not isinstance(qty, int) or qty == 0:
        raise SimError("qty must be a non-zero int")
    if market.name != name:
        raise SimError("market conditions are for another name")
    if not (0 < participation_cap <= 1):
        raise SimError("participation_cap in (0, 1]")
    arrival = ts_decision + latency_ns
    side = 1 if qty > 0 else -1

    def refuse(reason: str, depth: int = 0, part: float = 0.0) -> FillOutcome:
        return FillOutcome(name, col.name, qty, 0, qty, reason, None, arrival, part, depth)

    if market.halted:
        return refuse("halt")
    q = market.quote
    if q is None:
        return refuse("quote disappeared before arrival")
    cause = q.valid(arrival)
    if cause:
        return refuse(cause)
    # participation: scale to the cap of trailing 5-minute volume, drop below the minimum ticket
    cap_shares = int(math.floor(participation_cap * market.volume_5m))
    submitted = qty
    reason = ""
    if abs(qty) > cap_shares:
        if cap_shares < min_ticket:
            return refuse(f"participation breach: cap {cap_shares} shares below the minimum ticket {min_ticket}")
        submitted = side * cap_shares
        reason = f"participation: scaled {abs(qty)} to {cap_shares} shares ({participation_cap:.0%} of 5-minute volume)"
    if not mandatory and abs(submitted) < min_ticket:
        return refuse(f"below the minimum ticket {min_ticket}")
    # depth: displayed at the touch on arrival, haircut by the column
    displayed = q.ask_size if side > 0 else q.bid_size
    depth = int(math.floor(displayed * col.depth_haircut))
    if depth <= 0:
        return refuse("insufficient depth: none displayed at the touch", depth)
    filled_abs = min(abs(submitted), depth)
    if filled_abs < abs(submitted):
        reason = (reason + "; " if reason else "") + (
            f"insufficient depth: {filled_abs} of {abs(submitted)} filled, remainder refused"
        )
    impact = impact_per_share(filled_abs, q.mid, depth, market.volume_5m, col, coeffs)
    half_spread = q.spread / 2.0
    price = q.mid + side * (half_spread + impact)
    if limit_price is not None and not mandatory:
        if (side > 0 and price > limit_price) or (side < 0 and price < limit_price):
            return refuse(
                f"price through the entry threshold: fill {price:.6f} vs limit {limit_price:.6f}",
                depth,
                filled_abs / max(market.volume_5m, 1),
            )
    fill = Fill(
        ts=ts_decision,
        name=name,
        qty=side * filled_abs,
        price=price,
        fee=fees.fee(filled_abs, price),
        half_spread=half_spread,
        impact=impact,
        mid_decision=mid_decision,
        mid_arrival=q.mid,
        column=col.name,
    )
    return FillOutcome(
        name,
        col.name,
        qty,
        side * filled_abs,
        qty - side * filled_abs,
        reason,
        fill,
        arrival,
        filled_abs / max(market.volume_5m, 1),
        depth,
    )


def simulate_columns(**kwargs: Any) -> dict[str, FillOutcome]:
    """The same order under the three columns (the cost-stress table): they differ only by multiplier and haircut."""
    kwargs.pop("column", None)
    return {c: simulate(column=c, **kwargs) for c in COLUMNS}


def simulate_daily(
    *,
    ts_decision: int,
    name: str,
    qty: int,
    mid_decision: float,
    auction: AuctionPrint,
    model: DailyImpactModel | None = None,
    fees: FeeSchedule | None = None,
    participation_cap: float = 0.05,
    min_ticket: int = 1,
    mandatory: bool = False,
    column: CostColumn | str = "conservative",
) -> FillOutcome:
    """Execution at the close or open auction for daily horizons: participation of the auction volume, square-root impact."""
    col = COLUMNS[column] if isinstance(column, str) else column
    model = model or DailyImpactModel()
    fees = fees or FeeSchedule()
    if isinstance(qty, bool) or not isinstance(qty, int) or qty == 0:
        raise SimError("qty must be a non-zero int")
    side = 1 if qty > 0 else -1
    if not math.isfinite(auction.price) or auction.price <= 0 or auction.volume <= 0:
        return FillOutcome(name, col.name, qty, 0, qty, "no auction print", None, auction.ts, 0.0, 0)
    cap_shares = int(math.floor(participation_cap * auction.volume))
    reason = ""
    submitted = qty
    if abs(qty) > cap_shares:
        if cap_shares < min_ticket:
            return FillOutcome(
                name, col.name, qty, 0, qty, "participation breach at the auction", None, auction.ts, 0.0, 0
            )
        submitted = side * cap_shares
        reason = f"participation: scaled {abs(qty)} to {cap_shares} shares of the auction volume"
    if not mandatory and abs(submitted) < min_ticket:
        return FillOutcome(name, col.name, qty, 0, qty, f"below the minimum ticket {min_ticket}", None, auction.ts, 0.0, 0)
    q = abs(submitted)
    impact = col.impact_multiplier * model.c * auction.sigma_daily * math.sqrt(q / max(auction.adv, 1)) * auction.price
    price = auction.price + side * impact
    fill = Fill(
        ts=ts_decision,
        name=name,
        qty=side * q,
        price=price,
        fee=fees.fee(q, price),
        half_spread=0.0,
        impact=impact,
        mid_decision=mid_decision,
        mid_arrival=auction.price,
        column=col.name,
    )
    return FillOutcome(
        name, col.name, qty, side * q, qty - side * q, reason, fill, auction.ts, q / auction.volume, auction.volume
    )


def closeout_valuation(
    last_valid_mid: float,
    qty: int,
    volume_5m: int,
    depth_at_touch: int,
    cause: str,
    coeffs: ImpactCoefficients | None = None,
    reference_spread: float = 0.0,
) -> Valuation:
    """The registered valuation of an unliquidated position: last valid mid minus the stress column, flagged with ``cause``."""
    coeffs = coeffs or ImpactCoefficients()
    col = COLUMNS["stress"]
    depth = int(math.floor(depth_at_touch * col.depth_haircut))
    haircut = reference_spread / 2.0 + impact_per_share(qty, last_valid_mid, depth, volume_5m, col, coeffs)
    side = 1 if qty > 0 else -1
    price = last_valid_mid - side * haircut  # a long is valued lower, a short higher
    return Valuation(price=max(price, 1e-9), cause=cause or "unpriced")


def three_kinds_of_session() -> Mapping[str, str]:
    """E0 section 10 (C11): the three session kinds the reports count."""
    return {
        "usable_no_trades": "R_d = 0 and counted",
        "coverage_failure": "dropped before analysis under R0 section 3 and listed",
        "unpriced_at_close": "R_d carries the stress valuation and a failure flag",
    }

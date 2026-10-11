"""Deterministic P&L account and path counters of the MVP engine (split out
of :mod:`iap.mvp.engine`, v1.13 E4; re-exported there).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Account:
    """Per-instrument deterministic P&L account (quote currency = USD), the
    Java ``BacktestEngine.Account`` semantics: ``equity = cash + position *
    mark``; ``gross_pnl`` accumulates ``position * dmark``; ``spread_cost``
    is the fill-vs-mark slippage; fees / rebates / impact are exact sums."""

    position: int = 0
    cash: float = 0.0
    fees: float = 0.0
    rebates: float = 0.0
    impact: float = 0.0
    spread_cost: float = 0.0
    gross_pnl: float = 0.0
    mark: float = 0.0
    mark_valid: bool = False
    fill_count: int = 0
    orders_submitted: int = 0
    session_volume: int = 0
    filled_qty: int = 0
    last_child_decision_ts: int | None = None
    equity_peak: float = 0.0
    max_drawdown: float = 0.0

    def equity(self) -> float:
        return self.cash + float(self.position) * (self.mark if self.mark_valid else 0.0)

    def mark_to(self, mid: float) -> None:
        """Mark-to-market at ``mid`` (the risk engine's reference mid)."""
        if self.mark_valid:
            self.gross_pnl += float(self.position) * (mid - self.mark)
        self.mark = mid
        self.mark_valid = True
        eq = self.equity()
        self.equity_peak = max(self.equity_peak, eq)
        self.max_drawdown = max(self.max_drawdown, self.equity_peak - eq)

    def book_fill(self, side: int, qty: int, price: float, fee: float, impact: float) -> None:
        sgn = 1.0 if side == 0 else -1.0
        self.cash -= sgn * float(qty) * price
        self.cash -= fee
        self.cash -= impact
        if fee >= 0.0:
            self.fees += fee
        else:
            self.rebates += -fee
        self.impact += impact
        if not self.mark_valid:
            self.mark = price
            self.mark_valid = True
        self.spread_cost += sgn * float(qty) * (price - self.mark)
        self.position += qty if side == 0 else -qty
        self.filled_qty += qty
        self.fill_count += 1

    @property
    def fees_net(self) -> float:
        return self.fees - self.rebates


@dataclass
class Counters:
    """Every drop / block / skip on the path is counted (conventions §8)."""

    events: int = 0
    decisions: int = 0
    decisions_without_covariance: int = 0
    decisions_flat: int = 0
    decisions_parent_live: int = 0
    decisions_window_beyond_session: int = 0  #: parent window would end after the session close
    parent_orders: int = 0
    child_orders_generated: int = 0
    child_orders_submitted: int = 0
    risk_allowed: int = 0
    risk_rejected: int = 0
    sor_no_route: int = 0
    participation_capped: int = 0
    participation_blocked: int = 0
    slice_interval_blocked: int = 0
    latency_budget_blocked: int = 0
    sequence_gaps: int = 0
    feed_recoveries: int = 0
    fills: int = 0
    parents_finalised: int = 0
    parents_without_tca: int = 0
    timeline_crossed_skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in sorted(self.__dict__.items())}

"""REL-11 portfolio evaluation under ENFORCED constraints (v1.18 REL-17; code path only).

REL-11 itself stays conditional on REL-08's continuation rule; nothing here
runs a study.  This is the walk-forward loop REL-11's books (baseline,
candidate, combined) go through so that every book is held to the same
:class:`~iap.risk.portfolio_limits.PortfolioLimits`:

per rebalance ``t`` (inputs aligned by the caller, identical windows and
cost accounting across books, programme section 9.7):

1. if the kill switch is latched, no new weights are formed: the book holds
   ``w_prev`` and the step is logged ``halted`` (flattening is an operator
   decision, not automatic);
2. otherwise :func:`~iap.portfolio.enforced.solve_enforced` forms the
   target; a :class:`~iap.risk.portfolio_limits.LimitRefused` result is
   logged with its breaches and the book holds ``w_prev``;
3. P&L over the period is ``w . r_t`` minus linear cost
   ``tc_linear . |w - w_prev|`` (return units; impact enters through
   ``solve_kw["impact"]`` in the objective and a caller-supplied
   ``cost_fn`` in the accounting, so the two agree as in ``breadth_book``);
4. equity is marked through :meth:`PortfolioGuard.mark`, which trips the
   daily-loss and drawdown stops (``day_starts`` marks a new trading day).

Returns the periodic net series plus the refusal and halt log; REL-11's
net Sharpe, deflated Sharpe and comparisons are computed from that series
by the REL-11 report, not here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from iap.portfolio.enforced import solve_enforced
from iap.risk.portfolio_limits import LimitRefused, PortfolioGuard

__all__ = ["EnforcedBookResult", "RebalanceInput", "run_enforced_book"]


@dataclass(frozen=True)
class RebalanceInput:
    """One rebalance: expected returns, covariance and the realised returns after it."""

    ts: int
    alpha: np.ndarray
    sigma: np.ndarray
    realised: np.ndarray
    tc_linear: np.ndarray
    day_start: bool = False


@dataclass
class EnforcedBookResult:
    ts: list[int] = field(default_factory=list)
    gross: list[float] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)
    net: list[float] = field(default_factory=list)
    weights: list[np.ndarray] = field(default_factory=list)
    status: list[str] = field(default_factory=list)  # traded | refused | halted
    refusals: list[dict[str, Any]] = field(default_factory=list)
    limits_version: str = ""

    def summary(self) -> dict[str, Any]:
        net = np.asarray(self.net, dtype=float)
        return {
            "limits_version": self.limits_version,
            "n": len(net),
            "net_total": float(net.sum()),
            "net_mean": float(net.mean()) if len(net) else None,
            "n_traded": self.status.count("traded"),
            "n_refused": self.status.count("refused"),
            "n_halted": self.status.count("halted"),
        }


def run_enforced_book(
    steps: Sequence[RebalanceInput],
    guard: PortfolioGuard,
    instrument_ids: Sequence[int],
    risk_aversion: float,
    *,
    w0: np.ndarray | None = None,
    betas: Mapping[int, float] | None = None,
    sectors: Mapping[int, str] | None = None,
    adv_notional: Mapping[int, float] | None = None,
    turnover_cap: float | None = None,
    cost_fn: Callable[[np.ndarray, np.ndarray], float] | None = None,
    **solve_kw: Any,
) -> EnforcedBookResult:
    """Walk-forward book under the enforced limits (module docs)."""
    n = len(instrument_ids)
    w_prev = np.zeros(n) if w0 is None else np.asarray(w0, dtype=np.float64)
    out = EnforcedBookResult(limits_version=guard.limits.limits_version)
    equity = guard.equity if guard.equity is not None else guard.capital
    for s in steps:
        if s.day_start:
            guard.start_day(equity)
        w = w_prev
        if guard.halted:
            status = "halted"
        else:
            try:
                res = solve_enforced(
                    s.alpha,
                    s.sigma,
                    w_prev,
                    risk_aversion,
                    s.tc_linear,
                    guard,
                    instrument_ids,
                    betas=betas,
                    sectors=sectors,
                    adv_notional=adv_notional,
                    turnover_cap=turnover_cap,
                    **solve_kw,
                )
                w = res.weights
                status = "traded"
            except LimitRefused as exc:
                status = "halted" if exc.halted else "refused"
                out.refusals.append(
                    {"ts": s.ts, "halted": exc.halted, "breaches": [str(b) for b in exc.breaches]}
                )
        trade = w - w_prev
        cost = float(np.asarray(s.tc_linear) @ np.abs(trade))
        if cost_fn is not None:
            cost += float(cost_fn(w_prev, w))
        gross = float(w @ np.asarray(s.realised, dtype=np.float64))
        net = gross - cost
        equity += net * guard.capital
        guard.mark(equity)
        out.ts.append(s.ts)
        out.gross.append(gross)
        out.cost.append(cost)
        out.net.append(net)
        out.weights.append(w.copy())
        out.status.append(status)
        w_prev = w
    return out

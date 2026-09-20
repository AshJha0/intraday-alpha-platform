"""Single-stock mean-variance portfolio construction for the MVP.

:class:`SingleStockPortfolio` implements
:class:`~iap.contracts.protocols.PortfolioConstructor` over the production
optimizer :func:`iap.portfolio.optimizer.solve` (deterministic PGD,
API_PORTFOLIO_TCA.md §1) with the RiskMetrics EWMA variance of
:func:`iap.portfolio.covariance.ewma_covariance` on completed 1-minute bar
log returns — the same sizing chain the Java paper vertical runs
(``PaperTrading.solveWeight``).

Weights are positions in units of ``max_position_qty``: ``w = qty /
max_position_qty`` in the box ``[-w_max, w_max]`` where ``w_max = min(1,
max_notional / (max_position_qty * mark))`` (the notional cap at the current
mark), ``|w - w_prev| <= turnover_cap`` per decision, ``sqrt(w' Sigma w) <=
vol_target_per_bar``.  The optimizer's expected return is the acting
signal's ``expected_return`` when its confidence reaches ``conf_min`` and
``0`` otherwise; the linear transaction-cost penalty is ``tc_bps * 1e-4``.
An infeasible solve holds the previous weight (status ``INFEASIBLE``,
never NaN); the integer target is ``round_half_away(w * max_position_qty)``.

The constructor is pure: same state + signal + constraints => the same
target, bit for bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Sequence, Tuple

import numpy as np

from iap.contracts.types import AlphaSignal, PortfolioLeg, PortfolioTarget, SolverStatus
from iap.contracts.versions import content_hash
from iap.mvp.config import PortfolioSpec
from iap.portfolio.covariance import ewma_covariance
from iap.portfolio.optimizer import Constraints, solve

__all__ = ["PortfolioState", "PortfolioConstraints", "SingleStockPortfolio",
           "round_half_away"]


def round_half_away(x: float) -> int:
    """Round half away from zero (port-friendly, no banker's rounding)."""
    return int(math.floor(x + 0.5)) if x >= 0.0 else -int(math.floor(-x + 0.5))


@dataclass(frozen=True)
class PortfolioState:
    """What the constructor knows about the book at decision time."""

    instrument_id: int
    timestamp_ns: int
    position_qty: int
    mark_price: float                 #: last consolidated mid (quote ccy)
    bar_returns: Tuple[float, ...]    #: completed 1-minute bar log returns


@dataclass(frozen=True)
class PortfolioConstraints:
    """The constraint set + solver parameters (hashed into ``portfolio_version``)."""

    spec: PortfolioSpec

    def to_dict(self) -> Dict[str, Any]:
        s = self.spec
        return {
            "risk_aversion": s.risk_aversion, "tc_bps": s.tc_bps,
            "max_position_qty": s.max_position_qty, "max_notional": s.max_notional,
            "turnover_cap": s.turnover_cap, "vol_target_per_bar": s.vol_target_per_bar,
            "ewma_lambda": s.ewma_lambda, "min_bars": s.min_bars, "conf_min": s.conf_min,
            "solver": {"iters": s.solver.iters, "proj_passes": s.solver.proj_passes,
                       "step_decay": s.solver.step_decay, "feas_tol": s.solver.feas_tol},
            "weight_unit": "position / max_position_qty",
            "covariance": "ewma_1m_bars",
        }


class SingleStockPortfolio:
    """See the module docstring."""

    def __init__(self, strategy_id: str, acting_alpha_id: str, feature_version: str,
                 model_version: str) -> None:
        self._strategy_id = strategy_id
        self._acting = acting_alpha_id
        self._feature_version = feature_version
        self._model_version = model_version
        self.solves = 0
        self.infeasible_solves = 0
        #: Infeasible solves whose residual breach was a RISK constraint
        #: (vol/box/gross/net/currency) rather than a purely trading one
        #: (participation/turnover). A trading-only breach means "we are
        #: moving toward compliance but had to slice"; a risk breach means
        #: the book is still outside its mandate and a human should know.
        self.infeasible_risk_solves = 0

    @property
    def acting_alpha_id(self) -> str:
        """``model_version`` of the signal the sizing acts on (the ensemble)."""
        return self._acting

    @staticmethod
    def ready(state: PortfolioState, constraints: PortfolioConstraints) -> bool:
        """True once enough completed bars exist for the EWMA variance."""
        return len(state.bar_returns) >= constraints.spec.min_bars

    @staticmethod
    def portfolio_version(constraints: PortfolioConstraints) -> str:
        return content_hash(constraints.to_dict())

    def construct(self, signals: Sequence[AlphaSignal], portfolio_state: PortfolioState,
                  constraints: PortfolioConstraints) -> PortfolioTarget:
        """One deterministic solve (see module doc); raises before warm-up."""
        state = portfolio_state
        spec = constraints.spec
        if not self.ready(state, constraints):
            raise ValueError(f"portfolio: {len(state.bar_returns)} bars < min_bars {spec.min_bars}")
        acting = [s for s in signals if s.model_version == self._acting]
        if len(acting) != 1:
            raise ValueError(f"portfolio: expected exactly one {self._acting!r} signal, "
                             f"got {len(acting)}")
        sig = acting[0]
        if sig.instrument_id != state.instrument_id:
            raise ValueError("portfolio: signal instrument != state instrument")
        if not (state.mark_price > 0.0 and math.isfinite(state.mark_price)):
            raise ValueError(f"portfolio: mark price must be > 0, got {state.mark_price!r}")

        w_prev = float(state.position_qty) / float(spec.max_position_qty)
        w_max = min(1.0, spec.max_notional / (float(spec.max_position_qty) * state.mark_price))
        alpha = sig.expected_return if sig.confidence >= spec.conf_min else 0.0
        returns = np.asarray(state.bar_returns, dtype=np.float64).reshape(-1, 1)
        sigma = ewma_covariance(returns, lam=spec.ewma_lambda, init_window=spec.min_bars)
        cons = Constraints(
            w_min=np.array([-w_max]), w_max=np.array([w_max]),
            participation=np.array([spec.turnover_cap]),
            vol_target=spec.vol_target_per_bar,
        )
        result = solve(
            np.array([alpha]), sigma, np.array([w_prev]), spec.risk_aversion,
            np.array([spec.tc_bps * 1e-4]), cons,
            step_decay=spec.solver.step_decay, iters=spec.solver.iters,
            proj_passes=spec.solver.proj_passes, feas_tol=spec.solver.feas_tol,
        )
        self.solves += 1
        w = float(result.weights[0])
        if not result.feasible:
            self.infeasible_solves += 1
            # Do NOT fall back to w_prev. The usual cause of infeasibility is
            # that w_prev itself breaches a RISK constraint (vol/box/gross)
            # while a trading constraint stops us reaching the feasible set in
            # one step — precisely when holding the book is the worst action
            # available. In a vol spike that fallback pinned the book at 10.6x
            # the vol target indefinitely, though the solver had already
            # computed a strictly less-violating step. solve() now returns the
            # least-violating candidate, so the target it hands back is the
            # best reachable move toward compliance; we take it and count the
            # residual breach by kind so an operator can alarm on RISK.
            if result.risk_violation > 0.0:
                self.infeasible_risk_solves += 1
        target_qty = round_half_away(w * float(spec.max_position_qty))
        return PortfolioTarget(
            strategy_id=self._strategy_id, timestamp_ns=state.timestamp_ns,
            portfolio_version=self.portfolio_version(constraints),
            feature_version=self._feature_version, model_version=self._model_version,
            solver_status=SolverStatus(result.status), objective_value=float(result.objective),
            turnover=abs(w - w_prev),
            targets=(PortfolioLeg(
                instrument_id=state.instrument_id, target_qty=target_qty, target_weight=w,
                expected_return_bps=sig.expected_return * 1e4, prev_qty=state.position_qty),),
        )

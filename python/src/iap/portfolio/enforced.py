"""Optimizer under the enforced portfolio limits (v1.18 REL-17).

:func:`constraints_from_limits` turns a :class:`~iap.risk.portfolio_limits.
PortfolioLimits` set into the optimizer's :class:`~iap.portfolio.optimizer.
Constraints` (every limit that is linear or a box: name, gross, net,
liquidity-adjusted participation, beta and sector-net rows through the
general exposure halfspaces); :func:`solve_enforced` solves with the exact
Dykstra projection and then hands the result to the guard, which REFUSES
(:class:`~iap.risk.portfolio_limits.LimitRefused`) any weight set that still
breaches a limit, including the non-linear ``sector_gross_max`` and an
INFEASIBLE solve.  A latched kill switch refuses before solving.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from iap.portfolio.diagnostics import constraint_audit
from iap.portfolio.exposures import build_exposure_matrix
from iap.portfolio.optimizer import Constraints, PGDResult, solve
from iap.risk.portfolio_limits import LimitRefused, PortfolioGuard, PortfolioLimits

__all__ = ["EnforcedResult", "constraints_from_limits", "solve_enforced"]


def constraints_from_limits(
    limits: PortfolioLimits,
    instrument_ids: Sequence[int],
    capital: float,
    *,
    betas: Mapping[int, float] | None = None,
    sectors: Mapping[int, str] | None = None,
    adv_notional: Mapping[int, float] | None = None,
    turnover_cap: float | None = None,
) -> Constraints:
    """The optimizer constraint set implied by ``limits`` (module docs)."""
    ids = [int(i) for i in instrument_ids]
    n = len(ids)
    box = limits.name_max if limits.name_max is not None else limits.gross_max
    bound = float(box) if box is not None else 1e6
    participation = None
    if limits.participation_max is not None:
        adv = np.array([float((adv_notional or {}).get(i, 0.0)) for i in ids])
        adv = np.where(np.isfinite(adv) & (adv > 0), adv, 0.0)
        participation = limits.participation_max * adv / float(capital)
    rows: list[np.ndarray] = []
    bounds: list[float] = []
    if limits.beta_max is not None or limits.sector_net_max is not None:
        em = build_exposure_matrix(
            ids,
            betas=betas,
            sectors=sectors,
            beta_row=limits.beta_max is not None,
            sector_rows=limits.sector_net_max is not None,
            min_members=1,
        )
        for r, lab in enumerate(em.labels):
            rows.append(em.matrix[r])
            b = limits.beta_max if lab == "beta" else limits.sector_net_max
            bounds.append(float(b) if b is not None else 0.0)
    return Constraints(
        w_min=np.full(n, -bound),
        w_max=np.full(n, bound),
        gross_cap=limits.gross_max,
        net_cap=limits.net_max,
        participation=participation,
        turnover_cap=turnover_cap,
        currency_matrix=np.vstack(rows) if rows else None,
        currency_bounds=np.array(bounds) if rows else None,
    )


@dataclass(frozen=True)
class EnforcedResult:
    result: PGDResult
    constraints: Constraints
    audit: dict[str, Any]
    limits_version: str

    @property
    def weights(self) -> np.ndarray:
        return self.result.weights


def solve_enforced(
    alpha: np.ndarray,
    Sigma: np.ndarray,
    w_prev: np.ndarray,
    risk_aversion: float,
    tc_linear: np.ndarray,
    guard: PortfolioGuard,
    instrument_ids: Sequence[int],
    *,
    betas: Mapping[int, float] | None = None,
    sectors: Mapping[int, str] | None = None,
    adv_notional: Mapping[int, float] | None = None,
    turnover_cap: float | None = None,
    **solve_kw: Any,
) -> EnforcedResult:
    """Solve under the limits; raise :class:`LimitRefused` on any breach."""
    if guard.halted:
        raise LimitRefused((), guard.limits.limits_version, halted=True)
    cons = constraints_from_limits(
        guard.limits,
        instrument_ids,
        guard.capital,
        betas=betas,
        sectors=sectors,
        adv_notional=adv_notional,
        turnover_cap=turnover_cap,
    )
    solve_kw.setdefault("projection", "dykstra")
    res = solve(alpha, Sigma, w_prev, risk_aversion, tc_linear, cons, **solve_kw)
    guard.check_weights(
        res.weights,
        instrument_ids,
        w_prev=w_prev,
        betas=betas,
        sectors=sectors,
        adv_notional=adv_notional,
    )
    audit = constraint_audit(res.weights, cons, np.asarray(w_prev, dtype=np.float64), Sigma)
    return EnforcedResult(res, cons, audit, guard.limits.limits_version)

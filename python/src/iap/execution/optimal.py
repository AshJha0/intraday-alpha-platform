"""Almgren-Chriss optimal execution (plan item X1, v1.10, Python only, opt-in).

Closed-form implementation-shortfall trajectory of Almgren & Chriss (2000),
"Optimal execution of portfolio transactions", for working ``X`` shares in
``N`` equal intervals of length ``tau = T / N``:

- linear permanent impact ``g(v) = gamma * v`` and temporary impact
  ``h(v) = eps * sign(v) + eta * v`` (``v`` = trading rate);
- ``eta_tilde = eta - gamma * tau / 2`` and ``kappa`` solving
  ``2 (cosh(kappa tau) - 1) / tau^2 = lam * sigma^2 / eta_tilde``;
- holdings ``x_j = X sinh(kappa (T - t_j)) / sinh(kappa T)``, ``t_j = j tau``,
  trades ``n_j = x_{j-1} - x_j`` (``j = 1..N``);
- ``E = gamma X^2 / 2 + eps sum|n_j| + (eta_tilde / tau) sum n_j^2`` and
  ``V = sigma^2 tau sum_{j=1}^{N} x_j^2``.

``lam = 0`` gives the risk-neutral linear (TWAP) trajectory; larger ``lam``
front-loads. Units are the caller's: with ``sigma`` in price per sqrt(day),
``eta`` in price per (share/day) and ``T`` in days, ``E`` is in currency and
``V`` in currency squared.

This is not the pinned ``AlgoType.IS`` schedule (``exp(-ra i / (N-1))``,
part of the cross-language golden contract); it is used only when a
``ParentOrder`` sets ``is_model=ISModel.ALMGREN_CHRISS``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from iap.execution.calibration import ExecCalibration
    from iap.execution.config import ExecConfig


@dataclass(frozen=True, slots=True)
class ACParams:
    """Almgren-Chriss model inputs (see the module docstring for units)."""

    sigma: float  #: volatility, price per sqrt(time unit)
    eta: float  #: temporary impact, price per (share / time unit)
    gamma: float = 0.0  #: permanent impact, price per share
    eps: float = 0.0  #: fixed cost per share (half spread + fees)
    horizon: float = 1.0  #: T, in time units
    risk_aversion: float = 1e-6  #: lambda

    def __post_init__(self) -> None:
        if self.sigma < 0 or self.eta <= 0 or self.gamma < 0 or self.horizon <= 0:
            raise ValueError("need sigma >= 0, eta > 0, gamma >= 0, horizon > 0")
        if self.risk_aversion < 0:
            raise ValueError("risk_aversion must be >= 0")


def ac_params_from_calibration(
    *,
    sigma: float,
    adv: float,
    price: float,
    horizon: float = 1.0,
    risk_aversion: float = 1e-6,
    calibration: ExecCalibration | None = None,
    config: ExecConfig | None = None,
    permanent_fraction: float = 0.0,
    eps: float = 0.0,
) -> ACParams:
    """Map the simulator's linear impact coefficient to ``eta``.

    The cost model charges ``coeff`` bps per percent of ADV; read as a rate
    (percent of ADV per time unit), the temporary cost per share is
    ``coeff * 1e-4 * price * 100 * v / adv``, so ``eta = coeff * 1e-2 * price
    / adv``. ``coeff`` comes from ``calibration`` when it carries an estimated
    slope, else from ``config.impact_coeff_bps_per_pct_adv``, else the
    ``ExecConfig`` default. ``gamma = permanent_fraction * eta``.
    """
    from iap.execution.config import ExecConfig

    if adv <= 0 or price <= 0:
        raise ValueError("adv and price must be > 0")
    coeff = calibration.impact_coeff_bps_per_pct_adv if calibration is not None else None
    if coeff is None:
        coeff = (config or ExecConfig()).impact_coeff_bps_per_pct_adv
    eta = float(coeff) * 1e-2 * price / adv
    return ACParams(
        sigma=sigma,
        eta=eta,
        gamma=permanent_fraction * eta,
        eps=eps,
        horizon=horizon,
        risk_aversion=risk_aversion,
    )


def _eta_tilde(p: ACParams, tau: float) -> float:
    et = p.eta - p.gamma * tau / 2.0
    if et <= 0:
        raise ValueError("eta - gamma * tau / 2 must be > 0 (use more slices)")
    return et


def ac_kappa(p: ACParams, n: int) -> float:
    """The discrete urgency ``kappa`` for ``n`` intervals."""
    if n <= 0:
        raise ValueError("n must be > 0")
    tau = p.horizon / n
    k2 = p.risk_aversion * p.sigma**2 / _eta_tilde(p, tau)
    if k2 == 0.0:
        return 0.0
    return math.acosh(1.0 + k2 * tau * tau / 2.0) / tau


def ac_holdings(qty: float, p: ACParams, n: int) -> list[float]:
    """Holdings ``x_0 = qty, ..., x_n = 0`` of the optimal trajectory."""
    kappa = ac_kappa(p, n)
    t_end = p.horizon
    tau = t_end / n
    out = []
    for j in range(n + 1):
        t = j * tau
        if kappa * t_end < 1e-12:
            out.append(qty * (t_end - t) / t_end)
        else:
            out.append(qty * math.sinh(kappa * (t_end - t)) / math.sinh(kappa * t_end))
    out[-1] = 0.0
    return out


def ac_trajectory(qty: float, p: ACParams, n: int) -> list[float]:
    """Trade list ``n_1..n_N`` (sums to ``qty``)."""
    x = ac_holdings(qty, p, n)
    return [x[j - 1] - x[j] for j in range(1, n + 1)]


def ac_cost(
    qty: float, p: ACParams, n: int, trades: Sequence[float] | None = None
) -> tuple[float, float]:
    """``(E, V)``: expected shortfall and its variance for ``trades``
    (default: the optimal trajectory over ``n`` intervals)."""
    tr = ac_trajectory(qty, p, n) if trades is None else list(trades)
    tau = p.horizon / len(tr)
    et = _eta_tilde(p, tau)
    x = [qty]
    for v in tr:
        x.append(x[-1] - v)
    e = (
        0.5 * p.gamma * qty * qty
        + p.eps * sum(abs(v) for v in tr)
        + et / tau * sum(v * v for v in tr)
    )
    var = p.sigma**2 * tau * sum(h * h for h in x[1:])
    return e, var


def efficient_frontier(
    qty: float, p: ACParams, n: int, risk_aversions: Sequence[float]
) -> list[tuple[float, float, float]]:
    """``(lambda, E, V)`` sorted by ``lambda``: ``E`` rises and ``V`` falls
    as ``lambda`` grows."""
    return [
        (lam, *ac_cost(qty, replace(p, risk_aversion=lam), n)) for lam in sorted(risk_aversions)
    ]

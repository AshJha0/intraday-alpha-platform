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
    adv: float | None = None,
    price: float,
    horizon: float | None = None,
    risk_aversion: float = 1e-6,
    calibration: ExecCalibration | None = None,
    config: ExecConfig | None = None,
    permanent_fraction: float = 0.0,
    eps: float = 0.0,
    adv_per_day: float | None = None,
    horizon_s: float | None = None,
    session_s: float = 23_400.0,
    impact_source: str = "slope",
) -> ACParams:
    """Map the simulator's linear impact coefficient to ``eta``.

    The cost model charges ``coeff`` bps per percent of ADV; read as a rate
    (percent of ADV per time unit), the temporary cost per share is
    ``coeff * 1e-4 * price * 100 * v / adv``, so ``eta = coeff * 1e-2 * price
    / adv``. ``coeff`` comes from ``calibration`` when it carries an estimated
    slope, else from ``config.impact_coeff_bps_per_pct_adv``, else the
    ``ExecConfig`` default. ``gamma = permanent_fraction * eta``.

    **Units (v1.13, X-P2-15).** ``eta`` scales as ``1 / adv``, so ``adv`` must
    be in shares per the SAME time unit as ``horizon`` and ``sigma`` (an
    ``adv`` in shares/day with a horizon in seconds is off by 23,400x).
    Two forms are accepted, never mixed:

    * legacy: ``adv`` and ``horizon`` in one caller-chosen time unit
      (``horizon`` defaults to 1);
    * explicit: ``adv_per_day`` (shares per trading day) and ``horizon_s``
      (seconds); the time unit is then one trading day of ``session_s``
      seconds (23,400 = 6.5 h), ``horizon = horizon_s / session_s`` and
      ``sigma`` must be per sqrt(trading day).

    Anything else (both ``adv`` and ``adv_per_day``, ``horizon_s`` with
    ``adv`` or with ``horizon``, ``adv_per_day`` without ``horizon_s``, or no
    ADV) raises ``ValueError``.

    ``impact_source`` (v1.16 P0-5): ``"slope"`` (default) reads the v1.9
    full-move slope as above; ``"residual"`` reads the LINEAR fit of the
    residual move after the depth walk (``impact.residual``, estimated with
    ``estimate_calibration(impact_fit=True)``, item P0-2) at its reference
    horizon, and raises ``ValueError`` when the calibration has none.
    """
    from iap.execution.config import ExecConfig

    if adv_per_day is not None or horizon_s is not None:
        if adv is not None or horizon is not None:
            raise ValueError(
                "ambiguous units: pass either (adv, horizon) in one time unit or "
                "(adv_per_day, horizon_s), not a mix"
            )
        if adv_per_day is None or horizon_s is None:
            raise ValueError("adv_per_day and horizon_s must be given together")
        if horizon_s <= 0 or session_s <= 0:
            raise ValueError("horizon_s and session_s must be > 0")
        adv, horizon = float(adv_per_day), float(horizon_s) / float(session_s)
    if adv is None:
        raise ValueError("adv (or adv_per_day with horizon_s) is required")
    if horizon is None:
        horizon = 1.0
    if adv <= 0 or price <= 0:
        raise ValueError("adv and price must be > 0")
    if impact_source not in ("slope", "residual"):
        raise ValueError(f"impact_source must be 'slope' or 'residual', got {impact_source!r}")
    if impact_source == "residual":
        res = (calibration.doc.get("impact") or {}).get("residual") if calibration else None
        lin = ((res or {}).get("by_horizon", {}).get((res or {}).get("horizon")) or {}).get(
            "linear"
        )
        if not lin:
            raise ValueError("impact_source='residual' needs a calibration with impact.residual")
        coeff = lin["coeff_bps_per_pct_adv"]
    else:
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


def ac_holdings(qty: float, p: ACParams, n: int, drift: float = 0.0) -> list[float]:
    """Holdings ``x_0 = qty, ..., x_n = 0`` of the optimal trajectory.

    ``drift`` (v1.17 B3, default 0 = the v1.10 trajectory, byte-identical)
    is the expected price drift per time unit in the direction that
    FAVOURS WAITING (for the AC sell program, an expected rise): the
    Euler-Lagrange solution of ``2 eta_tilde x'' = 2 lam sigma^2 x - drift``
    with ``x(0) = X``, ``x(T) = 0`` is ``x(t) = (X - xb) sinh(k (T - t)) /
    sinh(k T) - xb sinh(k t) / sinh(k T) + xb`` with ``xb = drift / (2 lam
    sigma^2)``; for ``k = 0`` (risk-neutral) the limit is ``X (T - t) / T +
    drift t (T - t) / (4 eta_tilde)``. A positive drift holds MORE in the
    middle of the window (trades later), a negative one front-loads;
    holdings are not clamped here (``slice_weights`` floors the trades).
    """
    kappa = ac_kappa(p, n)
    t_end = p.horizon
    tau = t_end / n
    out = []
    risk = p.risk_aversion * p.sigma**2
    for j in range(n + 1):
        t = j * tau
        if kappa * t_end < 1e-12:
            x = qty * (t_end - t) / t_end
            if drift != 0.0:
                x += drift * t * (t_end - t) / (4.0 * _eta_tilde(p, tau))
        else:
            s_t = math.sinh(kappa * t_end)
            x = qty * math.sinh(kappa * (t_end - t)) / s_t
            if drift != 0.0 and risk > 0.0:
                xb = drift / (2.0 * risk)
                x += xb * (1.0 - (math.sinh(kappa * (t_end - t)) + math.sinh(kappa * t)) / s_t)
        out.append(x)
    out[-1] = 0.0
    return out


def ac_trajectory(qty: float, p: ACParams, n: int, drift: float = 0.0) -> list[float]:
    """Trade list ``n_1..n_N`` (sums to ``qty``); ``drift`` as in :func:`ac_holdings`."""
    x = ac_holdings(qty, p, n, drift)
    return [x[j - 1] - x[j] for j in range(1, n + 1)]


def ac_drift_from_signal(
    side: int, expected_return: float, price: float, remaining_horizon: float
) -> float:
    """The AC ``drift`` (price per time unit, favours-waiting sign) of a
    signal that expects ``expected_return`` (dimensionless, the alpha's
    sign: positive = rise) to be realised over the parent's remaining
    window of ``remaining_horizon`` time units (v1.17 B3).

    A buy is hurt by a rise, a sell helped: ``drift = alignment * price /
    remaining_horizon`` with ``alignment = -er`` (buy) / ``+er`` (sell)
    (``iap.execution.urgency.alignment``).  The alpha's expected move is
    taken as the total over the remaining window — never extrapolated per
    unit time from its (much shorter) horizon.
    """
    from iap.execution.urgency import alignment

    if remaining_horizon <= 0 or price <= 0:
        raise ValueError("remaining_horizon and price must be > 0")
    return alignment(side, expected_return) * price / remaining_horizon


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

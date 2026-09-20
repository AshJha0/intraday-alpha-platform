"""Deterministic projected-gradient portfolio optimizer (spec §15).

Maximizes the research objective

    f(w) = alpha'w  -  lambda * w' Sigma w  -  sum_i tc_i * |w_i - w_prev_i|

subject to (any subset active):

- position box            w_min <= w <= w_max
- participation           |w - w_prev| <= participation (per asset)
- net exposure            |sum(w)| <= net_cap
- currency exposure (FX)  |E w| <= currency_bounds  (rows of E per currency)
- gross exposure          ||w||_1 <= gross_cap
- turnover                ||w - w_prev||_1 <= turnover_cap
- volatility target       w' Sigma w <= vol_target^2

Algorithm (PINNED — the Java production service must replicate exactly;
see API_PORTFOLIO_TCA.md):

1. ``w = project(w_prev)``.
2. For ``k = 0 .. iters-1``:
   a. step  ``eta_k = eta0 / (1 + step_decay * k)``;
   b. gradient ascent on the smooth part: ``v = w + eta_k*(alpha - 2*lam*Sigma w)``;
   c. proximal step for the L1 t-cost around ``w_prev``: soft-threshold
      ``d = v - w_prev`` by ``eta_k * tc`` elementwise; ``v = w_prev + d``;
   d. ``w = project(v)``.
3. Keep the best FEASIBLE iterate by objective (ties keep the earliest);
   return it with its objective and ``feasible=True``.
4. INFEASIBLE (pinned): when no iterate — the initial projection included
   — satisfies ``feas_tol``, the result is ``feasible=False`` carrying the
   LEAST-VIOLATING candidate. Candidates are ``w_prev`` (holding the book,
   ``best_iteration = -1``), the initial projection (0) and every iterate
   (``k + 1``), ranked lexicographically by ``(risk violation, total
   violation, candidate order)`` — so ties keep the earliest and holding
   wins only a genuine tie. Constraints split into RISK (box, net,
   currency, gross, vol — they bound the BOOK) and TRADING (participation,
   turnover — they bound the TRADE), and ranking on the risk violation
   first is what forces an over-risked book to move: a breached risk
   constraint is live exposure that holding perpetuates, while a breached
   trading constraint only means the step is larger than one bar's cap and
   the execution layer slices it. ``violations`` names the residual
   breaches (pinned order) and ``risk_violation`` is the number to alarm
   on. Weights are never NaN/inf and the objective is never ``-inf``;
   callers MUST check ``feasible`` before acting.
5. Every input must be finite (alpha, Sigma, w_prev, tc, risk_aversion,
   bounds); a NaN/inf anywhere raises ValueError — it never propagates.

``project`` is ``proj_passes`` fixed passes of cyclic projections in the
pinned order box -> participation -> net -> currency -> gross -> turnover ->
vol.  Gross/turnover use the exact sort-based L1-ball projection; net and
currency use exact halfspace projections; the vol cap uses radial scaling
(a pinned feasible retraction, not the exact ellipsoidal projection —
documented and mirrored by production).  Everything is deterministic: fixed
iteration counts, no RNG, no unordered iteration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

_TOL = 1e-8

#: Constraints that bound the BOOK's exposure. A residual breach here is
#: live over-exposure, so holding ``w_prev`` perpetuates it — this is why
#: the INFEASIBLE result ranks on the risk violation before the total.
RISK_CONSTRAINTS = ("BOX", "NET", "CURRENCY", "GROSS", "VOL")
#: Constraints that bound the TRADE. A residual breach here only means the
#: step exceeds one bar's cap; the execution layer slices it over bars.
TRADING_CONSTRAINTS = ("PARTICIPATION", "TURNOVER")


@dataclass
class Constraints:
    """Constraint set for the optimizer; any member may be None (inactive)."""

    w_min: np.ndarray
    w_max: np.ndarray
    gross_cap: Optional[float] = None
    net_cap: Optional[float] = None
    participation: Optional[np.ndarray] = None  # per-asset |trade| cap
    turnover_cap: Optional[float] = None        # total L1 trade cap
    vol_target: Optional[float] = None          # sqrt(w'Sigma w) cap
    currency_matrix: Optional[np.ndarray] = None   # (C, N)
    currency_bounds: Optional[np.ndarray] = None   # (C,)

    def validate(self, n: int) -> None:
        self.w_min = np.asarray(self.w_min, dtype=np.float64)
        self.w_max = np.asarray(self.w_max, dtype=np.float64)
        if self.w_min.shape != (n,) or self.w_max.shape != (n,):
            raise ValueError("w_min/w_max must have shape (n,)")
        if not (np.all(np.isfinite(self.w_min)) and np.all(np.isfinite(self.w_max))):
            raise ValueError("w_min/w_max must be finite")
        if np.any(self.w_min > self.w_max):
            raise ValueError("w_min > w_max for some asset")
        # Every scalar cap must be finite BEFORE the sign tests below: a NaN
        # compares false against every one of them, so it would flow through
        # validation untouched and surface as a NaN max_violation — a frozen
        # book instead of a loud failure at config load.
        for name in ("gross_cap", "net_cap", "turnover_cap", "vol_target"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.gross_cap is not None and self.gross_cap <= 0:
            raise ValueError("gross_cap must be > 0")
        if self.net_cap is not None and self.net_cap < 0:
            raise ValueError("net_cap must be >= 0")
        if self.participation is not None:
            self.participation = np.asarray(self.participation,
                                            dtype=np.float64)
            if not np.all(np.isfinite(self.participation)):
                raise ValueError("participation must be finite")
            if self.participation.shape != (n,) or \
                    np.any(self.participation < 0):
                raise ValueError("participation must be (n,) and >= 0")
        if self.turnover_cap is not None and self.turnover_cap < 0:
            raise ValueError("turnover_cap must be >= 0")
        if self.vol_target is not None and self.vol_target <= 0:
            raise ValueError("vol_target must be > 0")
        if (self.currency_matrix is None) != (self.currency_bounds is None):
            raise ValueError("currency_matrix and currency_bounds go together")
        if self.currency_matrix is not None:
            self.currency_matrix = np.asarray(self.currency_matrix,
                                              dtype=np.float64)
            self.currency_bounds = np.asarray(self.currency_bounds,
                                              dtype=np.float64)
            if self.currency_matrix.shape[1] != n:
                raise ValueError("currency_matrix must have n columns")
            if self.currency_bounds.shape != (self.currency_matrix.shape[0],):
                raise ValueError("currency_bounds shape mismatch")
            if not (np.all(np.isfinite(self.currency_matrix))
                    and np.all(np.isfinite(self.currency_bounds))):
                raise ValueError("currency_matrix/currency_bounds must be finite")
            if np.any(self.currency_bounds < 0):
                raise ValueError("currency_bounds must be >= 0")


@dataclass
class PGDResult:
    """Solver output: best feasible iterate + audit trail (see module
    docstring, step 4, for the pinned INFEASIBLE result)."""

    weights: np.ndarray
    objective: float
    iterations: int
    #: 1-based iterate index; 0 = the initial projection of ``w_prev``,
    #: -1 = ``w_prev`` itself was held (INFEASIBLE only).
    best_iteration: int
    max_violation: float
    trajectory: List[float] = field(default_factory=list)
    feasible: bool = True
    #: Names of the constraints ``weights`` still violates, pinned order
    #: (empty when feasible) — what a caller names in its alarm.
    violations: Tuple[str, ...] = ()
    #: Largest RISK-constraint violation of ``weights`` (0 when none): live
    #: book exposure, as opposed to a trade over one bar's trading cap.
    risk_violation: float = 0.0

    @property
    def status(self) -> str:
        """``"OPTIMAL"`` or ``"INFEASIBLE"`` (audit wording, pinned)."""
        return "OPTIMAL" if self.feasible else "INFEASIBLE"

    @property
    def violation_kind(self) -> str:
        """``"NONE"`` / ``"RISK"`` / ``"TRADING"`` / ``"RISK_AND_TRADING"``
        — the alarm class of an INFEASIBLE result. RISK means the book is
        over-exposed right now; TRADING means only that the step exceeds a
        participation/turnover cap and the execution layer must slice it."""
        risk = any(name in RISK_CONSTRAINTS for name in self.violations)
        trading = any(name in TRADING_CONSTRAINTS for name in self.violations)
        if risk and trading:
            return "RISK_AND_TRADING"
        if risk:
            return "RISK"
        if trading:
            return "TRADING"
        return "NONE"


def objective(w: np.ndarray, alpha: np.ndarray, Sigma: np.ndarray,
              w_prev: np.ndarray, risk_aversion: float,
              tc_linear: np.ndarray) -> float:
    """The research objective f(w) (see module docstring)."""
    w = np.asarray(w, dtype=np.float64)
    return float(
        alpha @ w
        - risk_aversion * (w @ Sigma @ w)
        - tc_linear @ np.abs(w - w_prev)
    )


def project_l1_ball(v: np.ndarray, radius: float) -> np.ndarray:
    """Exact Euclidean projection onto {x : ||x||_1 <= radius} (sort-based)."""
    if radius < 0:
        raise ValueError("radius must be >= 0")
    a = np.abs(v)
    if a.sum() <= radius:
        return v.copy()
    if radius == 0.0:
        return np.zeros_like(v)
    u = np.sort(a)[::-1]
    css = np.cumsum(u)
    ks = np.arange(1, len(u) + 1)
    cond = u - (css - radius) / ks > 0
    rho = int(np.max(np.flatnonzero(cond))) + 1
    theta = (css[rho - 1] - radius) / rho
    return np.sign(v) * np.maximum(a - theta, 0.0)


def project(v: np.ndarray, cons: Constraints, w_prev: np.ndarray,
            Sigma: Optional[np.ndarray], passes: int = 8) -> np.ndarray:
    """Cyclic projection onto the constraint set (pinned order, fixed passes)."""
    w = np.asarray(v, dtype=np.float64).copy()
    n = len(w)
    for _ in range(passes):
        # 1. box
        np.clip(w, cons.w_min, cons.w_max, out=w)
        # 2. participation (per-asset trade box)
        if cons.participation is not None:
            lo = w_prev - cons.participation
            hi = w_prev + cons.participation
            np.clip(w, lo, hi, out=w)
        # 3. net exposure halfspaces
        if cons.net_cap is not None:
            s = w.sum()
            if abs(s) > cons.net_cap:
                w -= (s - np.sign(s) * cons.net_cap) / n
        # 4. currency exposure halfspaces (row order pinned)
        if cons.currency_matrix is not None:
            for c in range(cons.currency_matrix.shape[0]):
                e = cons.currency_matrix[c]
                denom = float(e @ e)
                if denom == 0.0:
                    continue
                val = float(e @ w)
                bound = float(cons.currency_bounds[c])
                if abs(val) > bound:
                    w -= ((val - np.sign(val) * bound) / denom) * e
        # 5. gross exposure L1 ball
        if cons.gross_cap is not None:
            w = project_l1_ball(w, cons.gross_cap)
        # 6. turnover L1 ball around w_prev
        if cons.turnover_cap is not None:
            w = w_prev + project_l1_ball(w - w_prev, cons.turnover_cap)
        # 7. vol target (radial retraction, pinned)
        if cons.vol_target is not None:
            if Sigma is None:
                raise ValueError("vol_target requires Sigma")
            q = float(w @ Sigma @ w)
            cap = cons.vol_target * cons.vol_target
            if q > cap:
                w *= cons.vol_target / np.sqrt(q)
    return w


def violation_breakdown(w: np.ndarray, cons: Constraints, w_prev: np.ndarray,
                        Sigma: Optional[np.ndarray]) -> Dict[str, float]:
    """Per-constraint violation of w, keyed by the names in
    :data:`RISK_CONSTRAINTS` / :data:`TRADING_CONSTRAINTS`, in the pinned
    projection order. An inactive constraint is absent; an active one that
    holds maps to a value <= 0. ``max_violation`` is the maximum of these
    (floored at 0), so the two can never disagree."""
    out: Dict[str, float] = {}
    out["BOX"] = max(float(np.max(cons.w_min - w, initial=0.0)),
                     float(np.max(w - cons.w_max, initial=0.0)))
    if cons.participation is not None:
        out["PARTICIPATION"] = float(
            np.max(np.abs(w - w_prev) - cons.participation, initial=0.0))
    if cons.net_cap is not None:
        out["NET"] = abs(float(w.sum())) - cons.net_cap
    if cons.currency_matrix is not None:
        exc = np.abs(cons.currency_matrix @ w) - cons.currency_bounds
        out["CURRENCY"] = float(np.max(exc, initial=0.0))
    if cons.gross_cap is not None:
        out["GROSS"] = float(np.abs(w).sum()) - cons.gross_cap
    if cons.turnover_cap is not None:
        out["TURNOVER"] = float(np.abs(w - w_prev).sum()) - cons.turnover_cap
    if cons.vol_target is not None and Sigma is not None:
        out["VOL"] = float(np.sqrt(max(w @ Sigma @ w, 0.0))) - cons.vol_target
    return out


def max_violation(w: np.ndarray, cons: Constraints, w_prev: np.ndarray,
                  Sigma: Optional[np.ndarray]) -> float:
    """Largest constraint violation of w (0 when feasible)."""
    v = 0.0
    for value in violation_breakdown(w, cons, w_prev, Sigma).values():
        v = max(v, value)
    return max(v, 0.0)


def _risk_violation(breakdown: Dict[str, float]) -> float:
    """Largest violation among the RISK constraints only (0 when none).
    This is the figure a caller alarms on: it is live book exposure, not a
    trade that merely exceeds one bar's participation/turnover cap."""
    v = 0.0
    for name in RISK_CONSTRAINTS:
        value = breakdown.get(name)
        if value is not None:
            v = max(v, value)
    return max(v, 0.0)


def _violated(breakdown: Dict[str, float], tol: float) -> Tuple[str, ...]:
    """Names of the constraints violated by more than ``tol``, in the
    pinned projection order (deterministic, never unordered)."""
    order = ("BOX", "PARTICIPATION", "NET", "CURRENCY", "GROSS", "TURNOVER",
             "VOL")
    return tuple(name for name in order
                 if breakdown.get(name) is not None and breakdown[name] > tol)


def solve(
    alpha: np.ndarray,
    Sigma: np.ndarray,
    w_prev: np.ndarray,
    risk_aversion: float,
    tc_linear: np.ndarray,
    constraints: Constraints,
    eta0: Optional[float] = None,
    step_decay: float = 0.01,
    iters: int = 500,
    proj_passes: int = 8,
    feas_tol: float = 1e-7,
) -> PGDResult:
    """Deterministic PGD solve of the research portfolio problem.

    All parameters are pinned by the caller; identical inputs produce
    bit-identical outputs (no RNG, fixed iteration structure).

    ``eta0=None`` selects the pinned auto step ``1 / L`` with
    ``L = max(2 * risk_aversion * ||Sigma||_inf, 1e-6)`` (infinity-norm bound
    on the Hessian's largest eigenvalue) — itself deterministic.
    """
    alpha = np.asarray(alpha, dtype=np.float64)
    Sigma = np.asarray(Sigma, dtype=np.float64)
    w_prev = np.asarray(w_prev, dtype=np.float64)
    tc_linear = np.asarray(tc_linear, dtype=np.float64)
    n = len(alpha)
    if Sigma.shape != (n, n):
        raise ValueError("Sigma must be (n, n)")
    if w_prev.shape != (n,) or tc_linear.shape != (n,):
        raise ValueError("w_prev/tc_linear must be (n,)")
    if not np.isfinite(risk_aversion) or risk_aversion < 0:
        raise ValueError("risk_aversion must be finite and >= 0")
    if np.any(tc_linear < 0):
        raise ValueError("tc_linear must be >= 0")
    # pinned: NaN/inf never propagate into weights
    for name, arr in (("alpha", alpha), ("Sigma", Sigma), ("w_prev", w_prev),
                      ("tc_linear", tc_linear)):
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"{name} must be finite")
    if eta0 is None:
        L = max(2.0 * risk_aversion * float(np.abs(Sigma).sum(axis=1).max()),
                1e-6)
        eta0 = 1.0 / L
    if iters < 1 or proj_passes < 1 or eta0 <= 0 or step_decay < 0:
        raise ValueError("bad solver parameters")
    if np.any(np.abs(Sigma - Sigma.T) > 1e-12):
        raise ValueError("Sigma must be symmetric")
    constraints.validate(n)

    def f(w: np.ndarray) -> float:
        return objective(w, alpha, Sigma, w_prev, risk_aversion, tc_linear)

    def rank(cand: np.ndarray) -> Tuple[float, float]:
        b = violation_breakdown(cand, constraints, w_prev, Sigma)
        total = 0.0
        for value in b.values():
            total = max(total, value)
        return _risk_violation(b), max(total, 0.0)

    # Least-violating fallback for the INFEASIBLE result. Candidates are
    # considered in order — holding w_prev first, then the initial
    # projection, then each iterate — and ranked by (risk violation, total
    # violation); a strict `<` keeps the EARLIEST on a tie, so holding wins
    # only when nothing computed later is any better.
    #
    # DEFECT this replaces: the solver used to discard every iterate and
    # return w_prev unconditionally. But w_prev cannot violate
    # participation or turnover (its own trade is identically zero), so an
    # infeasible solve means w_prev is breaching a RISK constraint — the
    # one situation in which holding is the WORST available action. A vol
    # spike then parked the book at many times the vol target indefinitely,
    # and the returned answer could be strictly worse than an iterate the
    # solver had already computed and thrown away.
    least_w = w_prev.copy()
    least_rank = rank(least_w)
    least_k = -1

    w = project(w_prev, constraints, w_prev, Sigma, proj_passes)
    best_w = w.copy()
    feasible = max_violation(w, constraints, w_prev, Sigma) <= feas_tol
    best_f = f(w) if feasible else -np.inf
    best_k = 0
    if rank(w) < least_rank:
        least_w, least_rank, least_k = w.copy(), rank(w), 0
    trajectory: List[float] = []
    for k in range(iters):
        eta = eta0 / (1.0 + step_decay * k)
        grad = alpha - 2.0 * risk_aversion * (Sigma @ w)
        v = w + eta * grad
        d = v - w_prev
        d = np.sign(d) * np.maximum(np.abs(d) - eta * tc_linear, 0.0)
        v = w_prev + d
        w = project(v, constraints, w_prev, Sigma, proj_passes)
        fw = f(w)
        trajectory.append(fw)
        if max_violation(w, constraints, w_prev, Sigma) <= feas_tol \
                and fw > best_f:
            best_f = fw
            best_w = w.copy()
            best_k = k + 1
            feasible = True
        cand_rank = rank(w)
        if cand_rank < least_rank:
            least_w, least_rank, least_k = w.copy(), cand_rank, k + 1
    if not feasible:
        # INFEASIBLE (pinned step 4): the least-violating candidate, never
        # -inf / NaN, with the residual breach named so a caller can alarm.
        breakdown = violation_breakdown(least_w, constraints, w_prev, Sigma)
        return PGDResult(
            weights=least_w,
            objective=f(least_w),
            iterations=iters,
            best_iteration=least_k,
            max_violation=least_rank[1],
            trajectory=trajectory,
            feasible=False,
            violations=_violated(breakdown, feas_tol),
            risk_violation=least_rank[0],
        )
    return PGDResult(
        weights=best_w,
        objective=float(best_f),
        iterations=iters,
        best_iteration=best_k,
        max_violation=max_violation(best_w, constraints, w_prev, Sigma),
        trajectory=trajectory,
        feasible=True,
    )

"""U10 and the Track C configurations C0-C2: the multi-family combined book (v2.0 U0 stage 9; plan section 6).

Lives in :mod:`iap.alpha` next to the H1 client (:mod:`iap.alpha.h1`) because it
drives the engine from above (EN1-EN5), as H1 does; U11's netting attribution
is :mod:`iap.portfolio.netting` and U12's report is :mod:`iap.reports.u12`.
Built on the stage-5 engine, not beside it: forecasts enter through EN1
(:func:`iap.alpha.forecast.consume`: validated, available, registered), cohorts
live in the EN2 ledger (:class:`iap.portfolio.ledger.Ledger`, which nets and
allocates across families), books are repaired and checked by EN3
(:func:`iap.portfolio.project.project`, :func:`~iap.portfolio.project.feasibility`,
:func:`~iap.portfolio.project.check_book`) and every net order is priced once
by EN4 (:func:`iap.execution.sim.simulate`).  Weights are fractions of the
registered capital C of the whole book.

The three configurations (plan section 6)::

    C0 independent sleeves     one ledger per family, capital C * budget_f; each family's deterministic
                               book; no coordination, no netting between sleeves, each sleeve pays its
                               own external charge
    C1 shared risk and netting one ledger for every family: one aggregate executable position, orders
                               netted before routing, deterministic allocation across families and
                               cohorts, one external charge per execution; each candidate cohort is
                               checked jointly against the book-level constraints on the combined book
    C2 cost-aware combined     per horizon bucket, max_w [w' mu - (lambda/2) w' Sigma w - kappa c'|w|]
                               under the same constraints, with lambda, kappa, Sigma, the combination
                               weights and the solver fixed by a written development protocol
                               (:class:`C2Protocol`; nothing here supplies a default)

Horizon handling (V2-C8), enforced, not advised:

- forecasts are combined **only within a horizon** (:func:`combine_within_horizon`
  refuses a mixed-horizon set and a family whose registered horizon differs);
- families at different horizons meet only at the **exposure level**: the
  combined executable position is the sum of live cohort positions, and the
  ledger nets orders across them;
- the C2 objective is solved per horizon bucket with every live cohort (other
  buckets and the bucket's own earlier cohorts) entered as **fixed holdings**:
  a live cohort's obligation is a constraint on the new decision, never a term
  traded against, and no cohort is ever shortened by another's forecast;
- after every horizon-specific solve (C2) or family candidate (C1) the
  combined book is re-checked with :func:`~iap.portfolio.project.check_book`;
  a breach refuses that candidate (fail-closed, recorded with the violations);
- overlapping exposure between horizons is reported as a diagnostic
  (shared-name gross).

Choices U0 leaves open are recorded as proposed decisions TC-PD-n in the
module constants and in every report (:data:`PROPOSED_DECISIONS`); none is a
threshold: thresholds come from the caller's declared specs.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from iap.alpha.forecast import ForecastError, ForecastRecord, consume, validate_horizon
from iap.execution.sim import (
    FeeSchedule,
    ImpactCoefficients,
    Market,
    closeout_valuation,
    simulate,
)
from iap.portfolio.ledger import CohortState, FamilyConfig, Ledger, Valuation
from iap.portfolio.project import (
    ConstraintSpec,
    SolverIdentity,
    check_book,
    feasibility,
    project,
)
from iap.reports.e5 import SessionRow

__all__ = [
    "CONFIGURATIONS",
    "C2_FAMILY",
    "PROPOSED_DECISIONS",
    "BookPanel",
    "C2Protocol",
    "CombinedBookError",
    "ConfigurationRun",
    "ExecutionSpec",
    "FamilySpec",
    "combine_within_horizon",
    "family_book",
    "run_configuration",
    "solve_c2_bucket",
]

CONFIGURATIONS: tuple[str, ...] = ("C0", "C1", "C2")
#: The ledger family of a C2 bucket cohort: one cohort per horizon bucket and decision.
C2_FAMILY = "c2_combined"

PROPOSED_DECISIONS: dict[str, str] = {
    "TC-PD-1": (
        "family deterministic book (C0/C1): w_raw = G * (mu - mean mu) / sum |mu - mean mu| over the "
        "family's consumed forecasts, then the EN3 repair and feasibility rules with the family's "
        "declared ConstraintSpec; cohort gross G = budget / holding_steps (H1's 1 / n_live rule)"
    ),
    "TC-PD-2": (
        "joint-check order: horizon buckets by (holding_steps, horizon), families by family_id; the "
        "first candidate that breaches the book-level constraints is refused, later ones are checked "
        "against the book without it"
    ),
    "TC-PD-3": (
        "external charge split: pro rata to allocated quantity on the fill's side, crossed shares on "
        "that side included; float remainder to the last cohort key"
    ),
    "TC-PD-4": "financing on carried inventory is a book-level line, not allocated to families",
    "TC-PD-5": (
        "C2 combined forecast: a name enters a bucket only when every family of the bucket has a "
        "forecast for it at the decision (no imputation); mu_combined = sum_f a_f mu_f with the "
        "protocol's weights a_f"
    ),
    "TC-PD-6": (
        "C2 cost term: kappa * sum_i c_i |w_i| with c_i the round-trip expected cost (2 x the one-way "
        "panel estimate, entry plus scheduled exit) in fractions of notional"
    ),
    "TC-PD-7": (
        "C2 book gross inside the solve is bounded conservatively by sum |fixed| + sum |w| <= cap; "
        "the exact combined gross is then re-checked by check_book"
    ),
    "TC-PD-8": (
        "a C2 solve whose rounded optimum holds no position opens no cohort and is logged as "
        "'flat optimum', not as a refusal"
    ),
}


class CombinedBookError(ValueError):
    """Malformed Track C inputs (a refusal of a candidate is a recorded result, not an error)."""


# --------------------------------------------------------------------------- declarations
@dataclass(frozen=True, slots=True)
class FamilySpec:
    """One family's registration in the combined book: horizon, holding, capital budget and its own constraints."""

    family_id: str
    horizon: str
    holding_steps: int
    budget: float  # fraction of the book's capital C
    spec: ConstraintSpec
    flat_at_close: bool = True

    def __post_init__(self) -> None:
        validate_horizon(self.horizon)
        if isinstance(self.holding_steps, bool) or not isinstance(self.holding_steps, int):
            raise CombinedBookError("holding_steps must be an int")
        if self.holding_steps < 1:
            raise CombinedBookError("holding_steps must be >= 1")
        if not math.isfinite(self.budget) or not 0 < self.budget <= 1:
            raise CombinedBookError("budget is a fraction of C in (0, 1]")
        if not isinstance(self.spec, ConstraintSpec):
            raise CombinedBookError("spec must be a ConstraintSpec")

    @property
    def cohort_gross(self) -> float:
        return self.budget / self.holding_steps


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """EN4 parameters shared by every configuration (one execution model; only the book differs)."""

    column: str
    coeffs: ImpactCoefficients
    fees: FeeSchedule
    participation_cap: float
    min_ticket: int
    latency_ns: int


@dataclass(frozen=True, slots=True)
class C2Protocol:
    """The written development protocol that fixes C2 (plan section 6).  Every field is required."""

    risk_aversion: float
    turnover_penalty: float
    combination_weights: Mapping[str, float]  # family_id -> a_f, within its horizon bucket
    covariance: Mapping[str, Any]  # {"names": tuple, "cov": N x N, per-decision-step returns}
    estimator: str  # e.g. "ewma lam=0.94 + constant-correlation shrinkage 0.1"
    window: tuple[str, str]  # the frozen estimation window
    bucket_specs: Mapping[str, ConstraintSpec]  # horizon -> the bucket cohort's own constraints
    solver: SolverIdentity

    def __post_init__(self) -> None:
        for n in ("risk_aversion", "turnover_penalty"):
            v = getattr(self, n)
            if (
                isinstance(v, bool)
                or not isinstance(v, int | float)
                or not math.isfinite(v)
                or v < 0
            ):
                raise CombinedBookError(f"C2 protocol {n} must be a finite number >= 0")
        if not self.combination_weights:
            raise CombinedBookError("C2 protocol needs combination weights")
        for f, a in self.combination_weights.items():
            if isinstance(a, bool) or not isinstance(a, int | float) or not math.isfinite(a):
                raise CombinedBookError(f"combination weight for {f!r} must be finite")
        if not self.estimator or not self.window or len(self.window) != 2:
            raise CombinedBookError("C2 protocol names its covariance estimator and frozen window")
        cov = np.asarray(self.covariance.get("cov"), dtype=float)
        names = tuple(self.covariance.get("names", ()))
        if cov.shape != (len(names), len(names)) or not np.all(np.isfinite(cov)):
            raise CombinedBookError("C2 covariance must be a finite N x N matrix over its names")
        if not np.allclose(cov, cov.T, atol=1e-15):
            raise CombinedBookError("C2 covariance must be symmetric")
        if float(np.linalg.eigvalsh(cov)[0]) < -1e-15:
            raise CombinedBookError("C2 covariance must be positive semi-definite")

    def identity(self) -> dict[str, Any]:
        return {
            "risk_aversion": self.risk_aversion,
            "turnover_penalty": self.turnover_penalty,
            "combination_weights": dict(sorted(self.combination_weights.items())),
            "estimator": self.estimator,
            "window": list(self.window),
            "covariance_names": list(self.covariance["names"]),
            "bucket_specs": {
                h: dataclasses.asdict(s) for h, s in sorted(self.bucket_specs.items())
            },
            "solver": self.solver.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class BookPanel:
    """The synthetic (development-only) world the configurations run on; every callable is deterministic.

    ``forecasts(s, k)`` returns the ForecastRecords of every family for the
    decision at session ``s``, step ``k``; ``market(s, k, name, arrival_ts)``
    the EN4 market at arrival; ``cost_bps(s, k, name)`` the one-way expected
    cost in bps of notional; ``liquidity_cap(s, k, name)`` the participation
    cap in weight units (fractions of C).
    """

    capital: float
    names: tuple[str, ...]
    betas: Mapping[str, float]
    sectors: Mapping[str, str]
    n_sessions: int
    steps_per_session: int
    t0_ns: int
    session_ns: int
    step_ns: int
    mids: Callable[[int, int], Mapping[str, float]]
    forecasts: Callable[[int, int], Sequence[ForecastRecord]]
    market: Callable[[int, int, str, int], Market]
    cost_bps: Callable[[int, int, str], float]
    liquidity_cap: Callable[[int, int, str], float]
    registry: Any
    book_spec: ConstraintSpec
    book_tol: float
    execution: ExecutionSpec

    def ts(self, s: int, k: int) -> int:
        return self.t0_ns + s * self.session_ns + k * self.step_ns


@dataclass(slots=True)
class ConfigurationRun:
    """What one configuration produced: its ledgers, the session rows of the whole book, refusals and diagnostics."""

    configuration: str
    ledgers: dict[str, Ledger]
    session_rows: list[SessionRow]
    refusals: list[dict[str, Any]]
    flat_optima: list[dict[str, Any]]
    shared_name_gross: list[float]
    joint_checks: int
    protocol: dict[str, Any] = field(default_factory=dict)
    proposed_decisions: dict[str, str] = field(default_factory=lambda: dict(PROPOSED_DECISIONS))


# --------------------------------------------------------------------------- forecasts
def _group(
    records: Sequence[ForecastRecord], families: Mapping[str, FamilySpec]
) -> dict[str, dict[str, ForecastRecord]]:
    out: dict[str, dict[str, ForecastRecord]] = {}
    for r in records:
        fam = families.get(r.family_id)
        if fam is None:
            raise ForecastError(f"forecast from unregistered family {r.family_id!r}")
        if r.horizon != fam.horizon:
            raise ForecastError(
                f"{r.family_id} is registered at {fam.horizon}, forecast declares {r.horizon} (V2-C8)"
            )
        out.setdefault(r.family_id, {})[r.name] = r
    return out


def combine_within_horizon(
    by_family: Mapping[str, Mapping[str, ForecastRecord]],
    weights: Mapping[str, float],
    horizon: str,
) -> dict[str, float]:
    """V2-C8 / TC-PD-5: ``sum_f a_f mu_f`` per name over families **at one horizon**; names missing a family are dropped."""
    if not by_family:
        return {}
    for f, recs in by_family.items():
        if f not in weights:
            raise CombinedBookError(f"no combination weight for family {f!r}")
        for r in recs.values():
            if r.horizon != horizon:
                raise CombinedBookError(
                    f"forecast {f}/{r.name} at {r.horizon} offered to the {horizon} bucket: forecasts "
                    "are never combined across horizons (V2-C8)"
                )
    common = set.intersection(*(set(recs) for recs in by_family.values()))
    return {
        n: sum(weights[f] * by_family[f][n].mu_hat_bps for f in sorted(by_family))
        for n in sorted(common)
    }


# --------------------------------------------------------------------------- books
def _to_intents(
    weights: Mapping[str, float], mids: Mapping[str, float], capital: float, min_ticket: int
) -> dict[str, int]:
    intents: dict[str, int] = {}
    for n in sorted(weights):
        q = int(math.floor(abs(weights[n]) * capital / mids[n] + 0.5))
        if q < min_ticket or q == 0:
            continue
        intents[n] = q if weights[n] > 0 else -q
    return intents


def family_book(
    mus: Mapping[str, float],
    *,
    gross: float,
    spec: ConstraintSpec,
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    liquidity_caps: Mapping[str, float],
    mids: Mapping[str, float],
    capital: float,
    min_ticket: int,
    solver: SolverIdentity | None = None,
) -> tuple[dict[str, int], str]:
    """TC-PD-1: a family's deterministic cohort as share intents, or ``({}, reason)``."""
    if len(mus) < 2:
        return {}, f"fewer than 2 forecasts ({len(mus)})"
    mean = sum(mus.values()) / len(mus)
    dev = {n: mus[n] - mean for n in sorted(mus) if mus[n] - mean != 0.0}
    total = sum(abs(v) for v in dev.values())
    if total == 0:
        return {}, "every forecast equals the cross-sectional mean"
    raw = {n: gross * v / total for n, v in dev.items()}
    s = dataclasses.replace(spec, gross_cap=gross)
    res = project(
        raw,
        betas,
        sectors,
        spec=s,
        liquidity_caps={n: liquidity_caps[n] for n in raw},
        solver=solver,
    )
    if res.refused:
        return {}, f"repair refused (active: {', '.join(res.active)})"
    intents = _to_intents(res.weights, mids, capital, min_ticket)
    final = {n: q * mids[n] / capital for n, q in intents.items()}
    feas = feasibility(final, gross_target=gross, spec=s)
    if not feas.feasible:
        return {}, feas.reason
    return intents, ""


def _live_book(led: Ledger, mids: Mapping[str, float], capital: float) -> dict[str, float]:
    """Weights of every live position plus pending entries at their intents (the book the next decision faces)."""
    w: dict[str, float] = {}
    for c in led.cohorts.values():
        if c.state is CohortState.PENDING:
            src = c.intents
        elif c.state in (CohortState.LIVE, CohortState.CLOSING):
            src = c.allocated
        else:
            continue
        for n, q in src.items():
            if q:
                w[n] = w.get(n, 0.0) + q * mids[n] / capital
    return w


def _by_horizon(
    led: Ledger, mids: Mapping[str, float], capital: float
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for c in led.cohorts.values():
        if c.state not in (CohortState.LIVE, CohortState.CLOSING):
            continue
        for n, q in c.allocated.items():
            if q:
                b = out.setdefault(c.horizon, {})
                b[n] = b.get(n, 0.0) + q * mids[n] / capital
    return out


def _shared_name_gross(
    ledgers: Mapping[str, Ledger], mids: Mapping[str, float], capital: float
) -> float:
    """Diagnostic: gross weight in names held at two or more horizons."""
    per_h: dict[str, dict[str, float]] = {}
    for led in ledgers.values():
        for h, b in _by_horizon(led, mids, capital).items():
            dst = per_h.setdefault(h, {})
            for n, v in b.items():
                dst[n] = dst.get(n, 0.0) + v
    count: dict[str, int] = {}
    for b in per_h.values():
        for n in b:
            count[n] = count.get(n, 0) + 1
    return sum(abs(b[n]) for b in per_h.values() for n in b if count[n] >= 2)


# --------------------------------------------------------------------------- C2 solve
def solve_c2_bucket(
    mu_bps: Mapping[str, float],
    cost_bps: Mapping[str, float],
    fixed: Mapping[str, float],
    *,
    protocol: C2Protocol,
    gross: float,
    book_spec: ConstraintSpec,
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    liquidity_caps: Mapping[str, float],
    name_cap: float | None = None,
) -> tuple[dict[str, float], str, dict[str, Any]]:
    """One horizon bucket's C2 decision with every live exposure fixed: ``(weights, refusal, info)``.

    ``max_w mu'w - (lambda/2) w'Sigma w - kappa c'|w|`` (mu and c in fractions
    of notional), subject to: the new cohort is dollar-neutral and within
    ``gross``; the combined book ``fixed + w`` within the book-level beta,
    sector and name limits; the book gross bounded by TC-PD-7; per-name
    ``|w_i| <= min(participation cap, name_cap)`` (``name_cap``: the bucket
    cohort's own single-name limit, as in the EN3 repair).  ``|w|`` is linear through ``w = p - n``, ``p, n >= 0``.
    """
    names = tuple(sorted(mu_bps))
    if not names:
        return {}, "", {"names": 0}
    cov_names = list(protocol.covariance["names"])
    missing = [n for n in names if n not in cov_names]
    if missing:
        raise CombinedBookError(f"C2 covariance has no row for {missing}")
    idx = [cov_names.index(n) for n in names]
    sigma = np.asarray(protocol.covariance["cov"], dtype=float)[np.ix_(idx, idx)]
    N = len(names)
    mu = np.array([mu_bps[n] / 1e4 for n in names])
    c = np.array([2.0 * cost_bps[n] / 1e4 for n in names])  # TC-PD-6: round trip
    lam, kappa = float(protocol.risk_aversion), float(protocol.turnover_penalty)
    beta = np.array([betas[n] for n in names])
    f_in = np.array([fixed.get(n, 0.0) for n in names])
    beta_fixed = sum(betas[n] * v for n, v in fixed.items())
    sec_names = sorted({sectors[n] for n in names} | {sectors[n] for n in fixed})
    sec_fixed = {s: sum(v for n, v in fixed.items() if sectors[n] == s) for s in sec_names}
    gross_fixed = sum(abs(v) for v in fixed.values())

    def w_of(x: np.ndarray) -> np.ndarray:
        return x[:N] - x[N:]

    def obj(x: np.ndarray) -> float:
        w = w_of(x)
        return float(-mu @ w + 0.5 * lam * w @ sigma @ w + kappa * c @ (x[:N] + x[N:]))

    def grad(x: np.ndarray) -> np.ndarray:
        g = -mu + lam * (sigma @ w_of(x))
        return np.concatenate([g + kappa * c, -g + kappa * c])

    A_w = np.hstack([np.eye(N), -np.eye(N)])  # w = A_w x
    one_w = np.ones(N) @ A_w
    rows: list[tuple[str, np.ndarray, float]] = []  # a.x <= b
    rows.append(("beta_upper", beta @ A_w, book_spec.beta_tol - beta_fixed))
    rows.append(("beta_lower", -beta @ A_w, book_spec.beta_tol + beta_fixed))
    for s in sec_names:
        a = np.array([1.0 if sectors[n] == s else 0.0 for n in names]) @ A_w
        rows.append((f"sector_upper:{s}", a, book_spec.sector_cap - sec_fixed[s]))
        rows.append((f"sector_lower:{s}", -a, book_spec.sector_cap + sec_fixed[s]))
    for k, n in enumerate(names):
        rows.append((f"name_upper:{n}", A_w[k], book_spec.name_cap - f_in[k]))
        rows.append((f"name_lower:{n}", -A_w[k], book_spec.name_cap + f_in[k]))
    rows.append(("cohort_gross", np.ones(2 * N), gross))
    rows.append(("book_gross", np.ones(2 * N), book_spec.gross_cap - gross_fixed))
    A = np.array([a for _, a, _ in rows])
    b = np.array([v for _, _, v in rows])
    bounds = []
    for _side in (0, 1):
        for n in names:
            hi = liquidity_caps[n] if name_cap is None else min(liquidity_caps[n], name_cap)
            bounds.append((0.0, float(max(0.0, hi))))
    from scipy.optimize import minimize

    sol = protocol.solver
    res = minimize(
        obj,
        np.zeros(2 * N),
        jac=grad,
        method=sol.method,
        bounds=bounds,
        constraints=[
            {"type": "eq", "fun": lambda x: np.array([one_w @ x]), "jac": lambda x: one_w[None, :]},
            {"type": "ineq", "fun": lambda x: b - A @ x, "jac": lambda x: -A},
        ],
        options={"ftol": sol.ftol, "maxiter": sol.maxiter},
    )
    x = np.clip(np.asarray(res.x, dtype=float), [lo for lo, _ in bounds], [hi for _, hi in bounds])
    viol = max(float(np.max(A @ x - b)), abs(float(one_w @ x)), 0.0)
    active = [lbl for (lbl, a, v) in rows if v - float(a @ x) <= sol.constraint_tol]
    info = {
        "names": N,
        "objective": float(obj(x)),
        "iterations": int(getattr(res, "nit", 0)),
        "max_violation": viol,
        "active": active,
    }
    if not res.success or viol > sol.constraint_tol or not np.all(np.isfinite(x)):
        return (
            {},
            (
                f"C2 solve refused: {'not converged' if not res.success else 'out of tolerance'} "
                f"(max violation {viol:.3e}; active: {', '.join(active)})"
            ),
            info,
        )
    w = w_of(x)
    return {n: float(v) for n, v in zip(names, w, strict=True) if v != 0.0}, "", info


# --------------------------------------------------------------------------- the runner
def _validate(families: Sequence[FamilySpec], configuration: str, protocol: C2Protocol | None):
    if configuration not in CONFIGURATIONS:
        raise CombinedBookError(f"configuration must be one of {CONFIGURATIONS}")
    fams = {f.family_id: f for f in families}
    if len(fams) != len(families) or not fams:
        raise CombinedBookError("families must be non-empty with distinct family_id")
    if C2_FAMILY in fams:
        raise CombinedBookError(f"{C2_FAMILY!r} is reserved for C2 bucket cohorts")
    if sum(f.budget for f in families) > 1.0 + 1e-12:
        raise CombinedBookError("family budgets sum to more than the book's capital")
    steps: dict[str, int] = {}
    for f in families:
        if steps.setdefault(f.horizon, f.holding_steps) != f.holding_steps:
            raise CombinedBookError(f"families at {f.horizon} declare different holding steps")
    if configuration == "C2":
        if protocol is None:
            raise CombinedBookError("C2 needs its development protocol (C2Protocol)")
        missing = sorted(set(fams) - set(protocol.combination_weights))
        if missing:
            raise CombinedBookError(f"C2 protocol has no combination weight for {missing}")
        no_spec = sorted({f.horizon for f in families} - set(protocol.bucket_specs))
        if no_spec:
            raise CombinedBookError(f"C2 protocol has no bucket spec for horizons {no_spec}")
    return fams


def run_configuration(
    panel: BookPanel,
    families: Sequence[FamilySpec],
    configuration: str,
    *,
    protocol: C2Protocol | None = None,
) -> ConfigurationRun:
    """Run one of C0-C2 over every session of ``panel`` (module docstring)."""
    fams = _validate(families, configuration, protocol)
    C = panel.capital
    ex = panel.execution
    buckets = sorted({(f.holding_steps, f.horizon) for f in families})
    if configuration == "C0":
        ledgers = {fid: Ledger() for fid in sorted(fams)}
        for fid, led in ledgers.items():
            led.register_family(FamilyConfig(fid, flat_at_close=fams[fid].flat_at_close))
            led.deposit(C * fams[fid].budget)
    else:
        led = Ledger()
        for fid in sorted(fams):
            led.register_family(FamilyConfig(fid, flat_at_close=fams[fid].flat_at_close))
        if configuration == "C2":
            led.register_family(
                FamilyConfig(C2_FAMILY, flat_at_close=all(f.flat_at_close for f in families))
            )
        led.deposit(C)
        ledgers = {"book": led}
    next_id: dict[tuple[str, str], int] = {}
    refusals: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    shared: list[float] = []
    joint_checks = 0
    session_rows: list[SessionRow] = []

    def new_key(fid: str, h: str) -> tuple[str, str, int]:
        i = next_id.get((fid, h), 1)
        next_id[(fid, h)] = i + 1
        return (fid, h, i)

    def refuse(led_: Ledger, key, ts, expiry, reason: str) -> None:
        led_.refuse_cohort(key, decision_ts=ts, expiry_ts=expiry, reason=reason)
        refusals.append({"ts": ts, "cohort": list(key), "reason": reason})

    K = panel.steps_per_session
    for s in range(panel.n_sessions):
        sid = f"s{s}"
        for led in ledgers.values():
            led.open_session(sid)
        traded = False
        for k in range(K):
            ts = panel.ts(s, k)
            mids = {n: float(v) for n, v in panel.mids(s, k).items()}
            pre_close = k == K - 1
            if not pre_close:
                recs = consume(panel.forecasts(s, k), t_decision=ts, registry=panel.registry)
                recs = tuple(r for r in recs if r.name in mids)
                grouped = _group(recs, fams)
                caps = {n: panel.liquidity_cap(s, k, n) for n in mids}
                for steps, h in buckets:
                    expiry = ts + steps * panel.step_ns
                    in_bucket = sorted(f for f in grouped if fams[f].horizon == h)
                    if configuration in ("C0", "C1"):
                        for fid in in_bucket:
                            fam = fams[fid]
                            led = ledgers[fid] if configuration == "C0" else ledgers["book"]
                            key = new_key(fid, h)
                            mus = {n: r.mu_hat_bps for n, r in grouped[fid].items()}
                            intents, reason = family_book(
                                mus,
                                gross=fam.cohort_gross,
                                spec=fam.spec,
                                betas=panel.betas,
                                sectors=panel.sectors,
                                liquidity_caps=caps,
                                mids=mids,
                                capital=C,
                                min_ticket=ex.min_ticket,
                            )
                            if reason:
                                refuse(led, key, ts, expiry, reason)
                                continue
                            if configuration == "C1":
                                joint = _live_book(led, mids, C)
                                for n, q in intents.items():
                                    joint[n] = joint.get(n, 0.0) + q * mids[n] / C
                                joint_checks += 1
                                chk = check_book(
                                    joint,
                                    panel.betas,
                                    panel.sectors,
                                    spec=panel.book_spec,
                                    tol=panel.book_tol,
                                )
                                if not chk.ok:
                                    refuse(
                                        led,
                                        key,
                                        ts,
                                        expiry,
                                        "combined book outside a hard constraint: "
                                        + "; ".join(chk.violations),
                                    )
                                    continue
                            led.open_cohort(
                                key,
                                formation_ts=ts,
                                decision_ts=ts,
                                expiry_ts=expiry,
                                intents=intents,
                                forecasts={n: mus[n] for n in intents},
                            )
                    elif in_bucket:
                        assert protocol is not None
                        led = ledgers["book"]
                        key = new_key(C2_FAMILY, h)
                        mu = combine_within_horizon(
                            {f: grouped[f] for f in in_bucket}, protocol.combination_weights, h
                        )
                        gross = sum(fams[f].budget for f in in_bucket) / steps
                        bspec = dataclasses.replace(protocol.bucket_specs[h], gross_cap=gross)
                        fixed = _live_book(led, mids, C)
                        w, reason, info = solve_c2_bucket(
                            mu,
                            {n: panel.cost_bps(s, k, n) for n in mu},
                            fixed,
                            protocol=protocol,
                            gross=gross,
                            book_spec=panel.book_spec,
                            betas=panel.betas,
                            sectors=panel.sectors,
                            liquidity_caps=caps,
                            name_cap=bspec.name_cap,
                        )
                        if reason:
                            refuse(led, key, ts, expiry, reason)
                            continue
                        intents = _to_intents(w, mids, C, ex.min_ticket)
                        if not intents:
                            flat.append({"ts": ts, "horizon": h, "names": info["names"]})
                            next_id[(C2_FAMILY, h)] -= 1
                            continue
                        final = {n: q * mids[n] / C for n, q in intents.items()}
                        feas = feasibility(final, gross_target=gross, spec=bspec)
                        if not feas.feasible:
                            refuse(led, key, ts, expiry, feas.reason)
                            continue
                        joint = dict(fixed)
                        for n, v in final.items():
                            joint[n] = joint.get(n, 0.0) + v
                        joint_checks += 1
                        chk = check_book(
                            joint,
                            panel.betas,
                            panel.sectors,
                            spec=panel.book_spec,
                            tol=panel.book_tol,
                        )
                        if not chk.ok:
                            refuse(
                                led,
                                key,
                                ts,
                                expiry,
                                "combined book outside a hard constraint after the solve: "
                                + "; ".join(chk.violations),
                            )
                            continue
                        led.open_cohort(
                            key,
                            formation_ts=ts,
                            decision_ts=ts,
                            expiry_ts=expiry,
                            intents=intents,
                            forecasts={n: mu[n] for n in intents},
                        )
            outcomes: dict[str, str] = {}
            for led in ledgers.values():
                decision = led.decide(ts, mids, pre_close=pre_close)
                for n in sorted(decision.orders):
                    o = decision.orders[n]
                    if o.submitted == 0:
                        continue
                    out = simulate(
                        ts_decision=ts,
                        name=n,
                        qty=o.submitted,
                        mid_decision=o.mid_decision,
                        market=panel.market(s, k, n, ts + ex.latency_ns),
                        column=ex.column,
                        coeffs=ex.coeffs,
                        fees=ex.fees,
                        participation_cap=ex.participation_cap,
                        min_ticket=ex.min_ticket,
                        mandatory=o.mandatory != 0,
                        latency_ns=ex.latency_ns,
                    )
                    outcomes[n] = out.reason
                    if out.fill is not None:
                        led.fill(out.fill)
                        traded = True
                led.settle()
            shared.append(_shared_name_gross(ledgers, mids, C))
            if pre_close:
                net = gross_pnl = 0.0
                causes: set[str] = set()
                for led in ledgers.values():
                    vals: dict[str, Valuation] = {}
                    for n, q in sorted(led.positions.items()):
                        if q == 0:
                            continue
                        m = panel.market(s, k, n, ts + ex.latency_ns)
                        reason = outcomes.get(n, "")
                        cause = (
                            "halt"
                            if "halt" in reason
                            else ("no quote" if "quote" in reason else "depth exhausted")
                        )
                        depth = 0
                        spread = 0.0
                        if m.quote is not None:
                            depth = m.quote.bid_size if q > 0 else m.quote.ask_size
                            spread = m.quote.spread
                        vals[n] = closeout_valuation(
                            mids[n],
                            q,
                            m.volume_5m,
                            depth,
                            cause,
                            ex.coeffs,
                            reference_spread=spread,
                        )
                    res = led.close_session(vals)
                    net += res.net_pnl
                    gross_pnl += res.waterfall["midpoint_reference_pnl"]
                    causes |= {f["cause"] for f in res.closeout_failures}
                moved = (
                    traded or net != 0.0 or gross_pnl != 0.0
                )  # carried inventory moves without a trade
                kind = (
                    "unpriced_at_close" if causes else ("traded" if moved else "usable_no_trades")
                )
                rows_kw = {"flags": tuple(sorted(causes))} if causes else {}
                session_rows.append(SessionRow(sid, kind, net, gross_pnl, **rows_kw))
    run = ConfigurationRun(
        configuration,
        ledgers,
        session_rows,
        refusals,
        flat,
        shared,
        joint_checks,
        protocol.identity() if (configuration == "C2" and protocol is not None) else {},
    )
    return run

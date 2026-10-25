"""EN3: projection and risk (v2.0 U0 stage 5; E0 section 6 items 4, 6, 7 and section 11).

Family-agnostic (plan V2-C1): every call is about one cohort of one
``(family_id, horizon, cohort_id)`` and the combined executable book; H1 is the
first client and its numbers are the defaults.

Repair (E0 section 6 item 4, C4).  Weights are fractions of the registered
capital C.  Given the raw inverse-volatility book ``w_raw`` the repair is the
one constrained least-squares projection::

    w* = argmin ||w - w_raw||^2   subject to
         sum w = 0                                   dollar-neutral
         |beta . w| <= beta_tol                      market beta (the signal's betas)
         |sum_{i in s} w_i| <= sector_cap            per sector
         |w_i| <= name_cap                           single name
         sign(w_i) = sign(w_raw_i) or w_i = 0        a long never becomes a short
         sum |w_i| <= gross_cap                      cohort gross cap
         |w_i| <= liquidity_cap_i                    item 5 (participation)

Sign preservation makes ``|w_i| = s_i w_i`` linear, so every constraint is
linear and the objective strictly convex: the solution is unique and no
tie-break exists.  Solver: SciPy ``minimize`` with method, tolerance and
iteration cap fixed in :class:`SolverIdentity` (recorded with every result);
a solution that does not converge or violates a constraint by more than the
tolerance (1e-9 in weight units) is a **refusal with the active constraint
set**; there is no sequential repair pass.  No forecast, covariance or
risk-aversion parameter enters.

No-trade band (item 6, C3): :func:`choose_band` picks one threshold from the
grid {2, 5, 10} bps of C by the outcome-blind rule "smallest threshold whose
turnover reduction relative to no band exceeds 10 %", tie to the smaller, else
0; :func:`apply_band` withholds a *discretionary* aggregate net order below the
band unless withholding would leave the executable book outside a hard
constraint (the band never overrides a constraint or a mandatory order).

Feasibility skip rules (item 7): :func:`feasibility` refuses a cohort with
fewer than ``min_names_per_leg`` names in either leg, deployed gross below
``min_deployed_fraction`` of the target, or one name above
``max_name_share_of_gross`` of the cohort's gross; nothing is re-scaled up.

Joint re-check: :func:`check_book` evaluates the combined executable book
(every live cohort plus the candidate) against the book-level constraints
after each horizon's solve; a breach refuses the candidate.

Diagnostics (section 11): :func:`diagnostic_covariance` (the 20-session sample
covariance of residual returns, diagnostic only, never used for weights) and
:func:`variance_contribution_table` (share of portfolio variance by name with
top-10 and Herfindahl, by sector, by the first five principal components, and
the share explained by the market and sector factors after neutralisation).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "BAND_GRID_BPS",
    "BandChoice",
    "BookCheck",
    "ConstraintSpec",
    "FeasibilityResult",
    "ProjectionError",
    "ProjectionResult",
    "SolverIdentity",
    "apply_band",
    "check_book",
    "choose_band",
    "construction_stages",
    "diagnostic_covariance",
    "feasibility",
    "project",
    "variance_contribution_table",
]

BAND_GRID_BPS: tuple[int, ...] = (2, 5, 10)


class ProjectionError(ValueError):
    """Malformed inputs to the projection (never a refusal: refusals are results)."""


@dataclass(frozen=True, slots=True)
class SolverIdentity:
    """The fixed solver (E0 section 6 item 4): recorded with the implementation identity."""

    method: str = "SLSQP"
    constraint_tol: float = 1e-9
    ftol: float = 1e-15
    maxiter: int = 1000

    def as_dict(self) -> dict[str, Any]:
        return {
            "library": "scipy.optimize.minimize",
            "method": self.method,
            "constraint_tol": self.constraint_tol,
            "ftol": self.ftol,
            "maxiter": self.maxiter,
        }


@dataclass(frozen=True, slots=True)
class ConstraintSpec:
    """Hard constraints in weight units (fractions of C).  Defaults are E0's proposed H1 values."""

    beta_tol: float = 0.05
    sector_cap: float = 0.02
    name_cap: float = 0.02
    gross_cap: float = 1.0
    min_names_per_leg: int = 10
    min_deployed_fraction: float = 0.5
    max_name_share_of_gross: float = 0.15

    def __post_init__(self) -> None:
        for n in (
            "beta_tol",
            "sector_cap",
            "name_cap",
            "gross_cap",
            "min_deployed_fraction",
            "max_name_share_of_gross",
        ):
            v = getattr(self, n)
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v < 0:
                raise ProjectionError(f"{n} must be a finite number >= 0")
        if self.min_names_per_leg < 0:
            raise ProjectionError("min_names_per_leg must be >= 0")


@dataclass(slots=True)
class ProjectionResult:
    """``weights`` is empty when refused; ``active`` lists the binding constraints (|slack| <= tol)."""

    names: tuple[str, ...]
    weights: dict[str, float]
    refused: bool
    reason: str
    active: tuple[str, ...]
    max_violation: float
    iterations: int
    solver: dict[str, Any]
    distance: float

    @property
    def gross(self) -> float:
        return sum(abs(w) for w in self.weights.values())


@dataclass(frozen=True, slots=True)
class FeasibilityResult:
    feasible: bool
    reason: str
    longs: int
    shorts: int
    deployed_gross: float
    largest_name_share: float


@dataclass(frozen=True, slots=True)
class BookCheck:
    ok: bool
    violations: tuple[str, ...]
    net: float
    beta: float
    gross: float
    sector_net: dict[str, float]
    largest_name: float


@dataclass(frozen=True, slots=True)
class BandChoice:
    band_bps: int
    turnover_no_band: float
    reductions: dict[int, float]
    rule: str = "smallest threshold whose turnover reduction relative to no band exceeds 10 %; tie to the smaller; else 0"


# --------------------------------------------------------------------------- helpers
def _finite_vector(values: Mapping[str, float], what: str, names: Sequence[str]) -> np.ndarray:
    out = np.empty(len(names))
    for k, n in enumerate(names):
        if n not in values:
            raise ProjectionError(f"{what} is missing {n!r}")
        v = values[n]
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise ProjectionError(f"{what}[{n!r}] must be a finite number")
        out[k] = float(v)
    return out


def _constraints(
    names: Sequence[str],
    signs: np.ndarray,
    betas: np.ndarray,
    sectors: Sequence[str],
    spec: ConstraintSpec,
    caps: np.ndarray,
) -> tuple[list[tuple[str, str, np.ndarray, float]], list[tuple[float, float]]]:
    """Linear constraints as ``(label, kind, a, b)`` meaning ``a.w == b`` or ``a.w <= b``, and the bounds."""
    n = len(names)
    rows: list[tuple[str, str, np.ndarray, float]] = []
    rows.append(("dollar_neutral", "eq", np.ones(n), 0.0))
    rows.append(("beta_upper", "le", betas.copy(), spec.beta_tol))
    rows.append(("beta_lower", "le", -betas, spec.beta_tol))
    for s in sorted(set(sectors)):
        a = np.array([1.0 if sec == s else 0.0 for sec in sectors])
        rows.append((f"sector_upper:{s}", "le", a, spec.sector_cap))
        rows.append((f"sector_lower:{s}", "le", -a, spec.sector_cap))
    rows.append(("gross_cap", "le", signs.copy(), spec.gross_cap))
    bounds = []
    for k in range(n):
        hi = float(min(spec.name_cap, caps[k]))
        bounds.append((0.0, hi) if signs[k] > 0 else (-hi, 0.0))
    return rows, bounds


def _evaluate(
    w: np.ndarray,
    names: Sequence[str],
    rows: list[tuple[str, str, np.ndarray, float]],
    bounds: list[tuple[float, float]],
    tol: float,
    cap_labels: Sequence[str] = (),
) -> tuple[float, list[str]]:
    worst = 0.0
    active: list[str] = []
    for label, kind, a, b in rows:
        v = float(a @ w)
        slack = abs(v - b) if kind == "eq" else b - v
        if kind == "eq":
            worst = max(worst, slack)
            if slack <= tol:
                active.append(label)
        else:
            worst = max(worst, -slack)
            if slack <= tol:
                active.append(label)
    for k, (lo, hi) in enumerate(bounds):
        worst = max(worst, lo - w[k], w[k] - hi)
        label = cap_labels[k] if cap_labels else "name_cap"
        if hi == lo:
            active.append(f"liquidity_zero:{names[k]}")
        elif (hi > 0 and hi - w[k] <= tol) or (lo < 0 and w[k] - lo <= tol):
            active.append(f"{label}:{names[k]}")
    return worst, active


# --------------------------------------------------------------------------- the repair
def project(
    w_raw: Mapping[str, float],
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    *,
    spec: ConstraintSpec | None = None,
    liquidity_caps: Mapping[str, float] | None = None,
    solver: SolverIdentity | None = None,
) -> ProjectionResult:
    """E0 section 6 item 4: the unique constrained least-squares repair of ``w_raw``, or a refusal."""
    spec = spec or ConstraintSpec()
    solver = solver or SolverIdentity()
    names = tuple(sorted(w_raw))
    if not names:
        raise ProjectionError("w_raw is empty")
    raw = _finite_vector(w_raw, "w_raw", names)
    beta = _finite_vector(betas, "betas", names)
    for n in names:
        if n not in sectors or not isinstance(sectors[n], str) or not sectors[n]:
            raise ProjectionError(f"sectors is missing {n!r}")
    secs = [sectors[n] for n in names]
    caps = np.full(len(names), math.inf)
    if liquidity_caps is not None:
        for k, n in enumerate(names):
            if n in liquidity_caps:
                c = liquidity_caps[n]
                if isinstance(c, bool) or not isinstance(c, int | float) or math.isnan(c) or c < 0:
                    raise ProjectionError(f"liquidity_caps[{n!r}] must be >= 0")
                caps[k] = float(c)
    if np.any(raw == 0.0):
        zero = [n for n, v in zip(names, raw, strict=True) if v == 0.0]
        raise ProjectionError(f"w_raw has zero weights {zero}: a name without a leg has no sign")
    signs = np.sign(raw)
    rows, bounds = _constraints(names, signs, beta, secs, spec, caps)
    ident = solver.as_dict()

    from scipy.optimize import minimize  # the only solver dependency, imported where it is used

    a_eq = np.array([a for _l, k, a, _b in rows if k == "eq"])
    b_eq = np.array([b for _l, k, _a, b in rows if k == "eq"])
    a_le = np.array([a for _l, k, a, _b in rows if k == "le"])
    b_le = np.array([b for _l, k, _a, b in rows if k == "le"])
    cons = [
        {"type": "eq", "fun": (lambda w: a_eq @ w - b_eq), "jac": (lambda w: a_eq)},
        {"type": "ineq", "fun": (lambda w: b_le - a_le @ w), "jac": (lambda w: -a_le)},
    ]
    x0 = np.clip(raw, [lo for lo, _ in bounds], [hi for _, hi in bounds])
    res = minimize(
        lambda w: float((w - raw) @ (w - raw)),
        x0,
        jac=lambda w: 2.0 * (w - raw),
        method=solver.method,
        bounds=bounds,
        constraints=cons,
        options={"ftol": solver.ftol, "maxiter": solver.maxiter},
    )
    w = np.asarray(res.x, dtype=float)
    # clip the sign side exactly (a long never becomes a short): the solver's rounding noise, within tol
    w = np.clip(w, [lo for lo, _ in bounds], [hi for _, hi in bounds])
    cap_labels = ["liquidity_cap" if caps[k] < spec.name_cap else "name_cap" for k in range(len(names))]
    worst, active = _evaluate(w, names, rows, bounds, solver.constraint_tol, cap_labels)
    iterations = int(getattr(res, "nit", 0))
    distance = float(np.sqrt((w - raw) @ (w - raw)))
    if not res.success or worst > solver.constraint_tol or not np.all(np.isfinite(w)):
        reason = (
            f"repair refused: {'not converged' if not res.success else 'out of tolerance'} "
            f"(max violation {worst:.3e} > {solver.constraint_tol}; status {res.status}: {res.message})"
        )
        return ProjectionResult(
            names, {}, True, reason, tuple(active), worst, iterations, ident, distance
        )
    return ProjectionResult(
        names,
        {n: float(v) for n, v in zip(names, w, strict=True)},
        False,
        "",
        tuple(active),
        worst,
        iterations,
        ident,
        distance,
    )


def construction_stages(
    w_raw: Mapping[str, float],
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    *,
    spec: ConstraintSpec | None = None,
    liquidity_caps: Mapping[str, float] | None = None,
    solver: SolverIdentity | None = None,
) -> dict[str, ProjectionResult]:
    """Section 11's cost-of-neutrality stages: the repair with constraints added one group at a time.

    Stages: ``raw`` (no constraint), ``dollar_neutral``, ``beta_neutral``,
    ``sector_constrained``, ``name_and_gross_capped``, ``participation_limited``
    (= the registered repair).  Diagnostic only; nothing selects a stage.
    """
    spec = spec or ConstraintSpec()
    big = 1e9
    stages = {
        "raw": ConstraintSpec(big, big, big, big, 0, 0.0, 1.0),
        "dollar_neutral": ConstraintSpec(big, big, big, big, 0, 0.0, 1.0),
        "beta_neutral": ConstraintSpec(spec.beta_tol, big, big, big, 0, 0.0, 1.0),
        "sector_constrained": ConstraintSpec(spec.beta_tol, spec.sector_cap, big, big, 0, 0.0, 1.0),
        "name_and_gross_capped": ConstraintSpec(
            spec.beta_tol, spec.sector_cap, spec.name_cap, spec.gross_cap, 0, 0.0, 1.0
        ),
        "participation_limited": spec,
    }
    out: dict[str, ProjectionResult] = {}
    for stage, s in stages.items():
        if stage == "raw":
            names = tuple(sorted(w_raw))
            out[stage] = ProjectionResult(
                names, {n: float(w_raw[n]) for n in names}, False, "", (), 0.0, 0, {}, 0.0
            )
            continue
        out[stage] = project(
            w_raw,
            betas,
            sectors,
            spec=s,
            liquidity_caps=liquidity_caps if stage == "participation_limited" else None,
            solver=solver,
        )
    return out


# --------------------------------------------------------------------------- feasibility
def feasibility(
    weights: Mapping[str, float],
    *,
    gross_target: float,
    spec: ConstraintSpec | None = None,
) -> FeasibilityResult:
    """E0 section 6 item 7 after the repair and the liquidity scaling; the first failing rule is the reason."""
    spec = spec or ConstraintSpec()
    if not math.isfinite(gross_target) or gross_target <= 0:
        raise ProjectionError("gross_target must be a positive finite fraction of C")
    longs = sum(1 for w in weights.values() if w > 0)
    shorts = sum(1 for w in weights.values() if w < 0)
    gross = sum(abs(w) for w in weights.values())
    largest = max((abs(w) for w in weights.values()), default=0.0)
    share = largest / gross if gross > 0 else 0.0
    reason = ""
    if longs < spec.min_names_per_leg or shorts < spec.min_names_per_leg:
        reason = (
            f"fewer than {spec.min_names_per_leg} names per leg (long {longs}, short {shorts})"
        )
    elif gross < spec.min_deployed_fraction * gross_target:
        reason = (
            f"deployed gross {gross:.6f} below {spec.min_deployed_fraction:.0%} of the target {gross_target:.6f}"
        )
    elif share > spec.max_name_share_of_gross:
        reason = f"single name at {share:.4f} of cohort gross exceeds {spec.max_name_share_of_gross}"
    return FeasibilityResult(reason == "", reason, longs, shorts, gross, share)


def check_book(
    weights: Mapping[str, float],
    betas: Mapping[str, float],
    sectors: Mapping[str, str],
    *,
    spec: ConstraintSpec | None = None,
    book_gross_cap: float | None = None,
    tol: float = 1e-9,
) -> BookCheck:
    """The combined executable book against the hard constraints (the joint re-check after each horizon solve)."""
    spec = spec or ConstraintSpec()
    cap = spec.gross_cap if book_gross_cap is None else book_gross_cap
    net = beta = gross = 0.0
    sector_net: dict[str, float] = {}
    largest = 0.0
    for n, w in weights.items():
        if not math.isfinite(w):
            raise ProjectionError(f"weight for {n!r} is not finite")
        if n not in betas or n not in sectors:
            raise ProjectionError(f"betas/sectors are missing {n!r}")
        net += w
        beta += betas[n] * w
        gross += abs(w)
        sector_net[sectors[n]] = sector_net.get(sectors[n], 0.0) + w
        largest = max(largest, abs(w))
    v: list[str] = []
    if abs(net) > tol:
        v.append(f"dollar_neutral: net {net:.3e}")
    if abs(beta) > spec.beta_tol + tol:
        v.append(f"beta: {beta:.6f} > {spec.beta_tol}")
    for s, x in sorted(sector_net.items()):
        if abs(x) > spec.sector_cap + tol:
            v.append(f"sector:{s}: {x:.6f} > {spec.sector_cap}")
    if largest > spec.name_cap + tol:
        v.append(f"name_cap: {largest:.6f} > {spec.name_cap}")
    if gross > cap + tol:
        v.append(f"gross_cap: {gross:.6f} > {cap}")
    return BookCheck(not v, tuple(v), net, beta, gross, sector_net, largest)


# --------------------------------------------------------------------------- the no-trade band
def choose_band(
    turnover_by_band: Mapping[int, float], *, grid: Sequence[int] = BAND_GRID_BPS
) -> BandChoice:
    """Outcome-blind choice on the development subset: ``turnover_by_band[b]`` is total turnover with band b (0 = none)."""
    if 0 not in turnover_by_band:
        raise ProjectionError("turnover_by_band needs the no-band entry 0")
    base = float(turnover_by_band[0])
    if not math.isfinite(base) or base < 0:
        raise ProjectionError("turnover without a band must be a finite number >= 0")
    reductions: dict[int, float] = {}
    chosen = 0
    for b in sorted(grid):
        if b not in turnover_by_band:
            raise ProjectionError(f"turnover_by_band is missing the grid value {b}")
        t = float(turnover_by_band[b])
        if t > base + 1e-12:
            raise ProjectionError(f"a band cannot raise turnover (band {b}: {t} > {base})")
        reductions[b] = (base - t) / base if base > 0 else 0.0
    for b in sorted(grid):  # ascending: the smallest qualifying threshold; a tie is the smaller by construction
        if reductions[b] > 0.10:
            chosen = b
            break
    return BandChoice(chosen, base, reductions)


def apply_band(
    net_orders_bps: Mapping[str, float],
    mandatory: Mapping[str, bool],
    band_bps: float,
    *,
    book_after_withholding: Mapping[str, Mapping[str, float]] | None = None,
    betas: Mapping[str, float] | None = None,
    sectors: Mapping[str, str] | None = None,
    spec: ConstraintSpec | None = None,
    book_gross_cap: float | None = None,
) -> tuple[frozenset[str], dict[str, str]]:
    """Names whose discretionary aggregate net order (|bps of C| < band) is withheld, and why each was sent anyway.

    ``book_after_withholding[name]`` is the executable book (weights) that
    would stand if that name's order were withheld; when it breaks a hard
    constraint the order is sent regardless (feasibility clause).  Mandatory
    orders are never withheld.
    """
    if band_bps < 0 or not math.isfinite(band_bps):
        raise ProjectionError("band_bps must be a finite number >= 0")
    withheld: set[str] = set()
    sent_anyway: dict[str, str] = {}
    for n in sorted(net_orders_bps):
        q = net_orders_bps[n]
        if not math.isfinite(q):
            raise ProjectionError(f"net order for {n!r} is not finite")
        if mandatory.get(n, False) or band_bps == 0 or abs(q) >= band_bps or q == 0:
            continue
        if book_after_withholding is not None and n in book_after_withholding:
            chk = check_book(
                book_after_withholding[n],
                betas or {},
                sectors or {},
                spec=spec,
                book_gross_cap=book_gross_cap,
            )
            if not chk.ok:
                sent_anyway[n] = "; ".join(chk.violations)
                continue
        withheld.add(n)
    return frozenset(withheld), sent_anyway


# --------------------------------------------------------------------------- diagnostics
def diagnostic_covariance(residuals: np.ndarray, names: Sequence[str]) -> dict[str, Any]:
    """The frozen diagnostic covariance: sample covariance (ddof 1) of a ``T x N`` residual-return panel."""
    r = np.asarray(residuals, dtype=float)
    if r.ndim != 2 or r.shape[1] != len(names) or r.shape[0] < 2:
        raise ProjectionError("residuals must be T x N with T >= 2 and N == len(names)")
    if not np.all(np.isfinite(r)):
        raise ProjectionError("residuals contain non-finite values")
    cov = np.cov(r, rowvar=False, ddof=1)
    cov = np.atleast_2d(cov)
    return {"names": tuple(names), "cov": cov, "observations": int(r.shape[0]), "role": "diagnostic only"}


def variance_contribution_table(
    weights: Mapping[str, float],
    covariance: Mapping[str, Any],
    sectors: Mapping[str, str],
    *,
    factor_loadings: np.ndarray | None = None,
    factor_covariance: np.ndarray | None = None,
    top: int = 10,
    components: int = 5,
) -> dict[str, Any]:
    """Section 11: variance shares by name (top ``top``, Herfindahl), by sector, by the first PCs, and by factors."""
    names = list(covariance["names"])
    cov = np.asarray(covariance["cov"], dtype=float)
    w = np.array([float(weights.get(n, 0.0)) for n in names])
    total = float(w @ cov @ w)
    if not math.isfinite(total) or total < 0:
        raise ProjectionError("portfolio variance is not finite")
    contrib = w * (cov @ w)  # Euler contributions, sum to total
    shares = contrib / total if total > 0 else np.zeros_like(contrib)
    by_name = sorted(zip(names, shares.tolist(), strict=True), key=lambda t: (-t[1], t[0]))
    by_sector: dict[str, float] = {}
    for n, s in zip(names, shares.tolist(), strict=True):
        by_sector[sectors[n]] = by_sector.get(sectors[n], 0.0) + s
    evals, evecs = np.linalg.eigh(cov)
    order = np.argsort(evals)[::-1]
    evals, evecs = evals[order], evecs[:, order]
    k = min(components, len(names))
    pc_shares = []
    for j in range(k):
        proj = float(evecs[:, j] @ w)
        pc_shares.append(float(evals[j]) * proj * proj / total if total > 0 else 0.0)
    out: dict[str, Any] = {
        "portfolio_variance": total,
        "by_name_top": by_name[:top],
        "top_share": float(sum(s for _, s in by_name[:top])),
        "herfindahl": float(np.sum(shares**2)),
        "by_sector": dict(sorted(by_sector.items())),
        "principal_components": pc_shares,
        "pc_share_total": float(sum(pc_shares)),
        "observations": covariance.get("observations"),
        "label": "diagnostic covariance; never used to set weights",
    }
    if factor_loadings is not None and factor_covariance is not None:
        b = np.asarray(factor_loadings, dtype=float)
        f = np.asarray(factor_covariance, dtype=float)
        if b.shape[0] != len(names) or f.shape != (b.shape[1], b.shape[1]):
            raise ProjectionError("factor_loadings must be N x K and factor_covariance K x K")
        e = b.T @ w
        out["factor_variance"] = float(e @ f @ e)
        out["factor_share_of_total"] = (
            float(e @ f @ e) / (total + float(e @ f @ e))
            if total + float(e @ f @ e) > 0
            else 0.0
        )
        out["factor_exposures"] = e.tolist()
    return out

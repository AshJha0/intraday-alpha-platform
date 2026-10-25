"""The canonical confirmatory gate (v1.19 / v2.0 stage 1, R1 and R2; defects D-01, D-02, D-03).

Specification: ``python/tests/spec/CONFIRMATORY_TEST_SPEC.md`` (R0, frozen 2026-10-24). This module
is the ONLY place in the repository that constructs a PROMOTE verdict; every other path (the
walk-forward gates of :mod:`iap.validation.validate`, the ledger's Bonferroni thresholds, the R7
and REL screens) is report-only or capped at ITERATE / ``ineligible``, and a test enumerates the
constructors.

**The procedure (R0 section 2).**  One-sided family-wise ``alpha_op = 0.020`` (claim FWER <= 0.025).
P-IND: one-sample Student t on the G session values, G - 1 degrees of freedom, one-sided (greater).
P-BLK (candidate only): the same on the B means of consecutive 5-session blocks, B - 1 df, B >= 4,
trailing incomplete block dropped.  Holm step-down over the registered family K.  A hypothesis is
PROMOTE when Holm rejects it; the family outcome is PROMOTE when at least one is.  CR1 and HAC
statistics are diagnostics, never the decision statistic.

**Fail closed (R0 sections 2-5).**  ``decide`` refuses, in this order and before any statistic:
the family and procedure declaration; the registry and selection history (a spent window, looks on
the confirmation dataset by the same family, a family that differs from the prereg's); the power
table and the registered G; the domain declaration (R3-G: D-BLK evidence and grid, D-BLK withdrawn
until R3 shows all twelve candidate cells pass, tier 2, an untested G under tier 1 (b), the real-data
evidence screen of section 4 and the reviewer-approved domain justification memo with an explicit
``eligible`` conclusion); then the sessions (duplicates, order, T0, non-finite values, the m_min
drop rule and the window cap); then degenerate statistics.  A ``refused: <reason>`` outcome is
terminal.  Nothing a caller controls can produce eligibility without the evidence fields.

**MDE (R2, R0 section 2).**  :func:`mde` is the one minimum-detectable-effect implementation: exact
noncentral-t inversion at the given power, df as the procedure, at the given one-sided alpha
(callers pass ``alpha_op / K`` and ``alpha_op``; a two-sided convention passes ``alpha / 2`` and
says so).  Every other MDE emitter in the repository scales this function.

Deterministic: pure numpy / scipy, no randomness, no I/O except :func:`dblk_validated`, which reads
the committed ``research/validation/R0_RESULTS.json`` and fails closed when it is absent.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "ALPHA_OP",
    "BLOCK_LENGTH",
    "DOMAINS",
    "MIN_B_BLK",
    "MIN_G_IND",
    "PROCEDURES",
    "PROMOTE",
    "REFUSED",
    "REJECT",
    "TIER1B_G_GRID",
    "WINDOW_CAP",
    "DBLK_RHO_GRID",
    "MDERefused",
    "PromoteToken",
    "Verdict",
    "block_means",
    "cr1_tstat",
    "dblk_validated",
    "decide",
    "decide_many",
    "hac_tstat",
    "holm",
    "inference_many",
    "inference_only",
    "mde",
    "mde_at_t",
    "power_at_t",
    "student_t_one_sample",
]

ALPHA_OP = 0.020
PROCEDURES = ("P-IND", "P-BLK")
DOMAINS = ("D-IND", "D-BLK")
MIN_G_IND = 10
MIN_B_BLK = 4
BLOCK_LENGTH = 5
TIER1B_G_GRID = (10, 15, 20, 30, 45, 60, 90)
DBLK_RHO_GRID = (0.0, 0.1, 0.2, 0.3)
WINDOW_CAP = 1.25
#: R0 section 4 bands (proposed; the hypothesis's prereg fixes the numbers before T0)
KURTOSIS_HIGH_SIDE = 1.0
LAG1_REFUSAL = 0.3
NONLINEAR_LEVEL = 0.05
PROMOTE = "PROMOTE"
REJECT = "REJECT"
REFUSED = "refused"
PENDING = "confirmation pending"
EXPLORATORY = "exploratory"
RESULTS_REL = "research/validation/R0_RESULTS.json"
#: synthetic generator classes the claimed domain covers (R0 sections 2 and 5)
TIER1A_GENERATORS = ("normal_homoskedastic", "normal_equicorrelated")
TIER1B_GENERATORS = ("normal_heteroskedastic_logspaced_1_5_cyclic",)
DBLK_GENERATOR = "ar1_gaussian_stationary"

_ROOT = Path(__file__).resolve().parents[4]
_TOKEN_KEY = object()


# ------------------------------------------------------------------- token --
class PromoteToken:
    """The PROMOTE token.  Constructible only by :func:`decide` in this module:
    the constructor needs the module-private key, so no other code can mint one.
    ``sha256`` is the inference object's hash the token certifies."""

    __slots__ = ("sha256",)

    def __init__(self, key: object, sha256: str) -> None:
        if key is not _TOKEN_KEY:
            raise PermissionError(
                "PROMOTE is constructed only by iap.validation.confirmatory.decide (R1)"
            )
        self.sha256 = str(sha256)

    def __repr__(self) -> str:
        return f"PromoteToken({self.sha256[:12]})"


@dataclass(frozen=True)
class Verdict:
    """The gate's result.  ``outcome`` is ``PROMOTE``, ``REJECT``, ``confirmation pending``
    or ``refused: <reason>``; ``promoted`` lists the hypotheses Holm rejected; ``token`` is
    the :class:`PromoteToken` when the outcome is PROMOTE and ``None`` otherwise."""

    outcome: str
    reason: str
    procedure: str
    domain: str
    tier: str
    G: int
    K: int
    alpha_op: float
    hypotheses: tuple[str, ...]
    promoted: tuple[str, ...] = ()
    statistics: tuple[dict[str, Any], ...] = ()
    sessions_used: tuple[str, ...] = ()
    dropped_sessions: tuple[dict[str, Any], ...] = ()
    selection_history: dict[str, Any] = field(default_factory=dict)
    exploratory: bool = False
    inference_sha256: str = ""
    token: PromoteToken | None = field(default=None, compare=False, repr=False)

    @property
    def refused(self) -> bool:
        return self.outcome.startswith(REFUSED)

    @property
    def is_promote(self) -> bool:
        return self.outcome == PROMOTE and self.token is not None

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "token"}
        d["hypotheses"] = list(self.hypotheses)
        d["promoted"] = list(self.promoted)
        d["statistics"] = [dict(s) for s in self.statistics]
        d["sessions_used"] = list(self.sessions_used)
        d["dropped_sessions"] = [dict(s) for s in self.dropped_sessions]
        d["gate"] = "confirmatory"
        return d


def _refuse(reason: str, design: Mapping[str, Any], **extra: Any) -> Verdict:
    hyps = tuple(str(h) for h in (design.get("family") or ()))
    return Verdict(
        outcome=f"{REFUSED}: {reason}",
        reason=reason,
        procedure=str(design.get("procedure", "")),
        domain=str(design.get("domain", "")),
        tier=str(design.get("tier", "")),
        G=int(design.get("G") or 0),
        K=len(hyps),
        alpha_op=float(design.get("alpha_op", ALPHA_OP)),
        hypotheses=hyps,
        **extra,
    )


# -------------------------------------------------------------- statistics --
def _scaled_deviations(x: np.ndarray) -> tuple[float, np.ndarray, float] | None:
    """mean, deviations / scale, scale; ``None`` when every value is identical
    (the t is scale-free, so 1e-300 and 1e300 magnitudes are exact, never NaN)."""
    m = float(np.mean(x))
    d = x - m
    s = float(np.max(np.abs(d)))
    if not math.isfinite(s) or s == 0.0:
        return None
    return m, d / s, s


def student_t_one_sample(x: Sequence[float] | np.ndarray) -> dict[str, float]:
    """One-sample Student t of the mean of ``x`` against 0, one-sided (greater),
    ``n - 1`` df.  ``se`` is the CR1 standard error with one observation per
    cluster, ``sqrt(n / (n - 1) * sum (x - m)^2 / n^2) = sd / sqrt(n)``.  NaN
    statistics when ``n < 2`` or the values are identical."""
    from scipy import stats

    x = np.asarray(x, dtype=float)
    n = int(x.size)
    nan = {
        "n": n,
        "df": n - 1,
        "mean": float("nan"),
        "se": float("nan"),
        "t": float("nan"),
        "p": float("nan"),
    }
    if n < 2 or not np.all(np.isfinite(x)):
        return nan
    sd = _scaled_deviations(x)
    if sd is None:
        nan["mean"] = float(np.mean(x))
        return nan
    m, dn, s = sd
    se_n = math.sqrt(float(np.sum(dn * dn)) / (n - 1)) / math.sqrt(n)
    t = (m / s) / se_n
    return {
        "n": n,
        "df": n - 1,
        "mean": m,
        "se": se_n * s,
        "t": float(t),
        "p": float(stats.t.sf(t, n - 1)),
    }


def block_means(x: Sequence[float] | np.ndarray, block_length: int = BLOCK_LENGTH) -> np.ndarray:
    """Means of consecutive blocks of ``block_length`` sessions in order; a trailing
    incomplete block is dropped and its sessions do not count (R0 section 5.1)."""
    x = np.asarray(x, dtype=float)
    b = int(x.size) // int(block_length)
    if b == 0:
        return np.empty(0)
    return x[: b * block_length].reshape(b, block_length).mean(axis=1)


def holm(p: Sequence[float] | np.ndarray, alpha: float) -> dict[str, Any]:
    """Holm (1979) step-down at one-sided ``alpha`` over the family ``p`` (NaN = 1,
    no rejection).  Returns the rejection mask, the adjusted p-values and the
    number of rejections.  Step-down: sorted ascending, reject p_(i) while
    ``p_(i) <= alpha / (K - i + 1)``; stop at the first failure."""
    p = np.asarray(p, dtype=float)
    k = int(p.size)
    pf = np.where(np.isfinite(p), p, 1.0)
    order = np.argsort(pf, kind="stable")
    rejected = np.zeros(k, dtype=bool)
    adjusted = np.empty(k)
    running = 0.0
    stop = False
    for i, idx in enumerate(order):
        running = max(running, min(1.0, (k - i) * float(pf[idx])))
        adjusted[idx] = running
        if not stop and pf[idx] <= alpha / (k - i):
            rejected[idx] = True
        else:
            stop = True
    return {"rejected": rejected, "adjusted": adjusted, "n_rejected": int(rejected.sum())}


def cr1_tstat(x: Sequence[float] | np.ndarray) -> float:
    """CR1 t of the mean with one observation per cluster (diagnostic; equals the
    Student t of :func:`student_t_one_sample`)."""
    return student_t_one_sample(x)["t"]


def hac_tstat(x: Sequence[float] | np.ndarray, lags: int = 2) -> float:
    """Newey-West (Bartlett kernel, ``lags``) t of the mean of ``x`` (diagnostic
    only; the ``statsmodels`` ``OLS(x, 1).fit(cov_type="HAC", maxlags=lags)`` t
    without small-sample correction)."""
    x = np.asarray(x, dtype=float)
    n = int(x.size)
    if n < 2:
        return float("nan")
    m = float(x.mean())
    d = x - m
    s0 = float(np.dot(d, d))
    for h in range(1, int(lags) + 1):
        if h >= n:
            break
        w = 1.0 - h / (lags + 1.0)
        s0 += 2.0 * w * float(np.dot(d[h:], d[:-h]))
    if s0 <= 0.0:
        return float("nan")
    return m / math.sqrt(s0 / (n * n))


# -------------------------------------------------------------------- MDE --
class MDERefused(ValueError):
    """The MDE is undefined for these inputs (alpha >= 0.5, power >= 1, df < 1, ...)."""


def mde(
    G: int,
    alpha: float,
    power: float = 0.8,
    *,
    procedure: str = "P-IND",
    block_length: int = BLOCK_LENGTH,
    tol: float = 1e-12,
) -> float:
    """Minimum detectable effect in units of the session-value standard deviation:
    the smallest mean ``delta`` (in sd units) for which the one-sided Student t at
    ``alpha`` with the procedure's df rejects with probability ``power``, by exact
    inversion of the noncentral t distribution (``nc = delta * sqrt(n)``).

    ``n`` is G under P-IND and B = G // block_length under P-BLK (the sd is then the
    sd of a block mean).  Refuses ``alpha`` outside (0, 0.5), ``power`` outside
    (alpha, 1), ``n < 2``.  The result is finite for every admissible input
    (``df = 2`` at ``alpha = 1e-4`` included)."""
    from scipy import stats

    if procedure not in PROCEDURES:
        raise MDERefused(f"unknown procedure {procedure!r}")
    n = int(G) if procedure == "P-IND" else int(G) // int(block_length)
    if not (0.0 < alpha < 0.5):
        raise MDERefused(f"alpha must be in (0, 0.5), got {alpha}")
    if not (alpha < power < 1.0):
        raise MDERefused(f"power must be in (alpha, 1), got {power}")
    if n < 2:
        raise MDERefused(f"the procedure has {n} observation(s); needs at least 2")
    return mde_at_t(n, float(stats.t.isf(alpha, n - 1)), power, tol=tol)


def mde_at_t(n: int, tcrit: float, power: float = 0.8, *, tol: float = 1e-12) -> float:
    """The same inversion at a critical value instead of an alpha: the smallest
    ``delta`` (sd units) for which a one-sample t on ``n`` values with ``n - 1`` df
    exceeds ``tcrit`` with probability ``power`` (``nc = delta * sqrt(n)``).  Used by
    gates registered as a |t| threshold (R7).  With :func:`power_at_t` this is the ONE
    noncentral-t evaluation in the repository."""
    from scipy import stats

    n = int(n)
    if n < 2:
        raise MDERefused(f"{n} observation(s); needs at least 2")
    if not math.isfinite(tcrit) or tcrit <= 0.0:
        raise MDERefused(f"tcrit must be finite and positive, got {tcrit}")
    if not (0.0 < power < 1.0):
        raise MDERefused(f"power must be in (0, 1), got {power}")
    df = n - 1

    def pw(nc: float) -> float:
        return float(stats.nct.sf(tcrit, df, nc))

    if pw(0.0) >= power:
        raise MDERefused("power is reached at a zero effect: tcrit is below the null quantile")
    lo, hi = 0.0, 1.0
    while pw(hi) < power:
        hi *= 2.0
        if hi > 1e6:
            raise MDERefused("noncentral t inversion did not converge")
    for _ in range(400):
        mid = 0.5 * (lo + hi)
        if pw(mid) >= power:
            hi = mid
        else:
            lo = mid
        if hi - lo <= tol * max(1.0, hi):
            break
    return 0.5 * (lo + hi) / math.sqrt(n)


def power_at_t(n: int, tcrit: float, delta: float) -> float:
    """Power of the one-sample t on ``n`` values (``n - 1`` df) against the critical
    value ``tcrit`` at a true mean of ``delta`` sd units: the noncentral-t upper tail
    at ``nc = delta * sqrt(n)``.  The forward map :func:`mde_at_t` inverts; every
    power figure outside this module (R7, the adversarial suite's theoretical power)
    calls it, so the noncentral t is evaluated in one place."""
    from scipy import stats

    n = int(n)
    if n < 2 or not math.isfinite(tcrit):
        return float("nan")
    return float(stats.nct.sf(tcrit, n - 1, float(delta) * math.sqrt(n)))


# ------------------------------------------------------------- D-BLK state --
def dblk_validated(root: Path | str | None = None) -> bool:
    """True only when the committed ``research/validation/R0_RESULTS.json`` shows
    every candidate-domain (D-BLK) cell of R3-A passing.  Absent file, missing
    cells or any failure: False (D-BLK withdrawn, applied mechanically)."""
    p = Path(root or _ROOT) / RESULTS_REL
    if not p.is_file():
        return False
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cells = [c for c in doc.get("cells", []) if c.get("domain_class") == "candidate"]
    return len(cells) == 12 and all(c.get("result") == "pass" for c in cells)


# ---------------------------------------------------------- eligibility --
def _family(design: Mapping[str, Any]) -> tuple[str, ...] | str:
    fam = design.get("family")
    if not isinstance(fam, Sequence) or isinstance(fam, str) or len(fam) == 0:
        return "empty family"
    hyps = tuple(str(h) for h in fam)
    if len(set(hyps)) != len(hyps):
        return "duplicated hypothesis in the family"
    return hyps


def _registry_checks(design: Mapping[str, Any]) -> str | None:
    reg = design.get("registry") or {}
    if reg.get("window_state") == "spent":
        return "confirmation window spent"
    if int(reg.get("confirmation_dataset_looks") or 0) > 0:
        return "looks on confirmation data by the same family"
    prereg_family = design.get("prereg_family")
    if prereg_family is not None and list(prereg_family) != list(design.get("family") or ()):
        return "family differs from the registered prereg"
    return None


def _power_and_g(design: Mapping[str, Any]) -> str | None:
    table = design.get("power_table")
    if not isinstance(table, Mapping) or "G_min" not in table:
        return "power table missing"
    g = design.get("G")
    if not isinstance(g, int) or isinstance(g, bool) or g < 1:
        return "G not registered"
    proc = design.get("procedure")
    if proc not in PROCEDURES:
        return "unknown procedure"
    if proc == "P-IND" and (g < MIN_G_IND or g < int(table["G_min"])):
        return "G below registered minimum"
    if proc == "P-BLK":
        if g // int(design.get("block_length", BLOCK_LENGTH)) < MIN_B_BLK:
            return "B below 4"
        if g < int(table["G_min"]):
            return "G below registered minimum"
    return None


def _in_grid(value: float, grid: Sequence[float]) -> bool:
    return any(abs(float(value) - x) <= 1e-12 for x in grid)


def _real_data_screen(data: Mapping[str, Any]) -> str | None:
    """R0 section 4 on real data: the evidence screen and the domain justification memo."""
    ev = data.get("evidence")
    if not isinstance(ev, Mapping):
        return "domain evidence missing"
    kurt = ev.get("kurtosis")
    if not isinstance(kurt, Sequence) or not kurt:
        return "domain evidence missing (kurtosis)"
    for row in kurt:
        lo = row.get("lo") if isinstance(row, Mapping) else None
        if lo is None or not math.isfinite(float(lo)):
            return "domain evidence missing (kurtosis interval)"
        if float(lo) > KURTOSIS_HIGH_SIDE:
            return "distribution outside validated domain"
    design_ind = ev.get("design_independence")
    need = (
        "non_overlapping_sessions",
        "no_label_crossing_session_boundary",
        "within_session_conditioning",
    )
    if not isinstance(design_ind, Mapping) or any(
        design_ind.get(n) not in (True, False) for n in need
    ):
        return "domain evidence missing (design independence)"
    if not all(bool(design_ind[n]) for n in need):
        return "dependence assumptions unsupported"
    serial = ev.get("serial_dependence")
    if not isinstance(serial, Sequence) or not serial:
        return "domain evidence missing (serial dependence)"
    for row in serial:
        if not isinstance(row, Mapping) or "lag1" not in row or "intervals" not in row:
            return "domain evidence missing (serial dependence)"
        if not math.isfinite(float(row["lag1"])) or float(row["lag1"]) > LAG1_REFUSAL:
            return "dependence assumptions unsupported"
        for lo, hi in row["intervals"]:
            if lo > 0 or hi < 0:
                return "dependence assumptions unsupported"
    nonlin = ev.get("nonlinear_dependence")
    if not isinstance(nonlin, Sequence) or not nonlin:
        return "domain evidence missing (nonlinear dependence)"
    for row in nonlin:
        if not isinstance(row, Mapping) or "p_by_lag" not in row:
            return "domain evidence missing (nonlinear dependence)"
        if any(math.isfinite(float(p)) and float(p) < NONLINEAR_LEVEL for p in row["p_by_lag"]):
            return "dependence assumptions unsupported"
    memo = data.get("memo")
    if not isinstance(memo, Mapping) or not memo:
        return "domain justification memo missing"
    for key in ("path", "sha256", "approval_path", "approval_sha256"):
        if not memo.get(key):
            return "domain justification memo approval not bound"
    if "conclusion" not in memo:
        return "domain justification memo has no conclusion"
    if memo.get("conclusion") != "eligible":
        return "domain justification memo concludes not eligible"
    return None


def _domain_checks(design: Mapping[str, Any], root: Path | str | None) -> str | None:
    domain = design.get("domain")
    proc = design.get("procedure")
    g = int(design["G"])
    data = design.get("data") or {}
    origin = data.get("origin")
    if origin not in ("synthetic", "real"):
        return "data origin not declared"
    if domain == "D-BLK":
        if proc != "P-BLK":
            return "dependence assumptions unsupported"
        dep = design.get("dependence")
        if (
            not isinstance(dep, Mapping)
            or "rho" not in dep
            or not isinstance(dep.get("evidence"), Mapping)
        ):
            return "dependence evidence missing"
        evid = dep["evidence"]
        if not all(k in evid for k in ("generator", "block_length")):
            return "dependence evidence missing"
        rho = float(dep["rho"])
        if rho < 0 or not _in_grid(rho, DBLK_RHO_GRID) or int(evid["block_length"]) != BLOCK_LENGTH:
            return "dependence assumptions unsupported"
        if origin == "synthetic" and data.get("generator") != DBLK_GENERATOR:
            return "dependence assumptions unsupported"
        if not dblk_validated(root):
            return "dependence assumptions unsupported (D-BLK withdrawn for v1.19)"
        if origin == "real":
            return _real_data_screen(data)
        return None
    if domain != "D-IND":
        return "distribution outside validated domain"
    if proc != "P-IND":
        return "dependence assumptions unsupported"
    tier = str(design.get("tier", ""))
    if tier not in ("1a", "1b"):
        return "distribution outside validated domain"
    if tier == "1b" and g not in TIER1B_G_GRID:
        return "G outside validated grid"
    if origin == "synthetic":
        gen = data.get("generator")
        if gen == DBLK_GENERATOR:
            if float(data.get("rho", 0.0)) > 0.0:
                return "dependence assumptions unsupported"
            return None  # rho = 0 AR(1) is independent normal sessions
        if tier == "1a" and gen in TIER1A_GENERATORS:
            return None
        if tier == "1b" and gen in TIER1B_GENERATORS:
            return None
        return "distribution outside validated domain"
    return _real_data_screen(data)


def _session_checks(
    design: Mapping[str, Any], sessions: Sequence[Mapping[str, Any]], hyps: tuple[str, ...]
) -> tuple[str | None, list[str], np.ndarray, list[dict[str, Any]], str | None]:
    """Order, duplicates, T0, finiteness, the m_min drop and the window cap.
    Returns (refusal, sessions used, G x K values, dropped, pending)."""
    k = len(hyps)
    if not sessions:
        return "no sessions", [], np.empty((0, k)), [], None
    ids = [str(s.get("session")) for s in sessions]
    if len(set(ids)) != len(ids):
        return "duplicated session", [], np.empty((0, k)), [], None
    if ids != sorted(ids):
        return "sessions out of order", [], np.empty((0, k)), [], None
    t0 = design.get("T0")
    rows: list[np.ndarray] = []
    used: list[str] = []
    dropped: list[dict[str, Any]] = []
    m_min = design.get("m_min") or {}
    for s in sessions:
        sid = str(s.get("session"))
        if t0 is not None and (sid < str(t0) or s.get("straddles_T0")):
            return "session straddles or precedes T0", [], np.empty((0, k)), [], None
        vals = s.get("values")
        if isinstance(vals, Mapping):
            try:
                v = np.array([float(vals[h]) for h in hyps])
            except KeyError:
                return (
                    "session lacks a value for a registered hypothesis",
                    [],
                    np.empty((0, k)),
                    [],
                    None,
                )
        else:
            v = np.asarray(vals, dtype=float).ravel() if vals is not None else np.empty(0)
            if v.size != k:
                return (
                    "session lacks a value for a registered hypothesis",
                    [],
                    np.empty((0, k)),
                    [],
                    None,
                )
        if not np.all(np.isfinite(v)):
            return "non-finite value", [], np.empty((0, k)), [], None
        if m_min:
            failing = []
            if "usable_units" in m_min and int(s.get("usable_units", -1)) < int(
                m_min["usable_units"]
            ):
                failing.append("usable_units")
            if "coverage_time_min" in m_min and float(s.get("coverage_time_min", -1)) < float(
                m_min["coverage_time_min"]
            ):
                failing.append("coverage_time")
            if "coverage_grid" in m_min and float(s.get("coverage_grid", -1)) < float(
                m_min["coverage_grid"]
            ):
                failing.append("coverage_grid")
            if failing:
                dropped.append({"session": sid, "failing": failing})
                continue
        used.append(sid)
        rows.append(v)
    g = int(design["G"])
    if len(used) < g:
        cap = int(math.floor(WINDOW_CAP * g))
        if len(sessions) >= cap:
            return None, used, np.empty((0, k)), dropped, PENDING
        return "fewer sessions than registered", [], np.empty((0, k)), dropped, None
    return None, used[:g], np.array(rows[:g]), dropped, None


def _statistics(
    design: Mapping[str, Any], values: np.ndarray
) -> tuple[str | None, list[dict[str, Any]], np.ndarray]:
    proc = design["procedure"]
    stats_rows: list[dict[str, Any]] = []
    ps = np.empty(values.shape[1])
    for j in range(values.shape[1]):
        x = values[:, j]
        if proc == "P-BLK":
            x = block_means(x, int(design.get("block_length", BLOCK_LENGTH)))
        st = student_t_one_sample(x)
        if not math.isfinite(st["t"]):
            return "zero variance", [], ps
        stats_rows.append(st)
        ps[j] = st["p"]
    return None, stats_rows, ps


def _inference_sha256(
    design: Mapping[str, Any],
    hyps: tuple[str, ...],
    stats_rows: Sequence[Mapping[str, Any]],
    promoted: Sequence[str],
    outcome: str,
) -> str:
    obj = {
        "procedure": design.get("procedure"),
        "domain": design.get("domain"),
        "tier": design.get("tier", ""),
        "G": design.get("G"),
        "alpha_op": float(design.get("alpha_op", ALPHA_OP)),
        "hypotheses": list(hyps),
        "statistics": [dict(s) for s in stats_rows],
        "promoted": list(promoted),
        "outcome": outcome,
    }
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _eligibility(
    design: Mapping[str, Any], root: Path | str | None
) -> tuple[str | None, tuple[str, ...]]:
    fam = _family(design)
    if isinstance(fam, str):
        return fam, ()
    for check in (_registry_checks, _power_and_g):
        r = check(design)
        if r:
            return r, fam
    r = _domain_checks(design, root)
    return r, fam


def decide(
    design: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    *,
    root: Path | str | None = None,
) -> Verdict:
    """The gate.  ``design`` is the registered design (the prereg's fields: ``family``,
    ``procedure``, ``domain``, ``tier``, ``G``, ``alpha_op``, ``power_table``, ``T0``,
    ``m_min``, ``registry``, ``data`` with its ``origin`` and, for real data, the
    section 4 ``evidence`` and ``memo``; ``dependence`` for D-BLK).  ``sessions`` are
    the session records in registered date order, each with ``session`` and ``values``
    (per hypothesis) and, when ``m_min`` is registered, the usable-unit fields.
    Refusals are ordered as in the module docstring; see R0 for every rule."""
    reason, hyps = _eligibility(design, root)
    if reason:
        return _refuse(reason, design)
    selection = dict(design.get("registry") or {})
    reason, used, values, dropped, pending = _session_checks(design, sessions, hyps)
    if reason:
        return _refuse(reason, design, dropped_sessions=tuple(dropped), selection_history=selection)
    base = {
        "procedure": str(design["procedure"]),
        "domain": str(design["domain"]),
        "tier": str(design.get("tier", "")),
        "G": int(design["G"]),
        "K": len(hyps),
        "alpha_op": float(design.get("alpha_op", ALPHA_OP)),
        "hypotheses": hyps,
        "sessions_used": tuple(used),
        "dropped_sessions": tuple(dropped),
        "selection_history": selection,
    }
    if pending:
        return Verdict(
            outcome=PENDING,
            reason="window cap reached without the registered count; nothing examined",
            **base,
        )
    reason, stats_rows, ps = _statistics(design, values)
    if reason:
        return _refuse(
            reason,
            design,
            sessions_used=tuple(used),
            dropped_sessions=tuple(dropped),
            selection_history=selection,
        )
    h = holm(ps, base["alpha_op"])
    for j, row in enumerate(stats_rows):
        row["hypothesis"] = hyps[j]
        row["p_holm"] = float(h["adjusted"][j])
        row["rejected"] = bool(h["rejected"][j])
    promoted = tuple(hyps[j] for j in range(len(hyps)) if h["rejected"][j])
    outcome = PROMOTE if promoted else REJECT
    sha = _inference_sha256(design, hyps, stats_rows, promoted, outcome)
    return Verdict(
        outcome=outcome,
        reason=f"Holm at alpha_op {base['alpha_op']:g} over K = {len(hyps)}: {len(promoted)} rejection(s)",
        statistics=tuple(stats_rows),
        promoted=promoted,
        inference_sha256=sha,
        token=PromoteToken(_TOKEN_KEY, sha) if outcome == PROMOTE else None,
        **base,
    )


def inference_only(design: Mapping[str, Any], values: np.ndarray) -> dict[str, Any]:
    """The statistic and Holm set WITHOUT the eligibility gate, for exploratory
    reporting of out-of-domain cells (R0 section 5: 'reported only').  Never a
    verdict: the result is labelled exploratory and carries no PROMOTE."""
    reason, stats_rows, ps = _statistics(design, np.asarray(values, dtype=float))
    if reason:
        return {"label": EXPLORATORY, "degenerate": reason, "n_rejected": 0, "statistics": []}
    h = holm(ps, float(design.get("alpha_op", ALPHA_OP)))
    return {
        "label": EXPLORATORY,
        "n_rejected": h["n_rejected"],
        "rejected": [bool(v) for v in h["rejected"]],
        "statistics": stats_rows,
        "note": "exploratory; no gate outcome depends on this",
    }


def decide_many(
    design: Mapping[str, Any],
    panels: np.ndarray,
    *,
    root: Path | str | None = None,
    sessions_meta: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The gate applied to ``n`` complete synthetic datasets at once (``panels`` is
    ``(n, G, K)``; every dataset shares the design and the session metadata).  The
    design-level refusals are evaluated once and apply to every dataset; the
    statistics are vectorised.  A test checks dataset-by-dataset equality with
    :func:`decide` (identical rejection sets, p-values to 1e-12).

    Returns ``outcome`` (``"refused: ..."`` for all, or per-dataset ``PROMOTE`` /
    ``REJECT`` codes), the per-dataset rejection counts and the PROMOTE mask."""
    panels = np.asarray(panels, dtype=float)
    n, g, k = panels.shape
    reason, hyps = _eligibility(design, root)
    if reason:
        return {
            "outcome": f"{REFUSED}: {reason}",
            "n": n,
            "promote": np.zeros(n, dtype=bool),
            "n_rejected": np.zeros(n, dtype=int),
        }
    if len(hyps) != k:
        return {
            "outcome": f"{REFUSED}: session lacks a value for a registered hypothesis",
            "n": n,
            "promote": np.zeros(n, dtype=bool),
            "n_rejected": np.zeros(n, dtype=int),
        }
    meta = list(sessions_meta or [{"session": f"S{i:04d}"} for i in range(g)])
    probe = [{**meta[i], "values": panels[0, i]} for i in range(g)]
    reason, used, _values, dropped, pending = _session_checks(design, probe, hyps)
    if reason:
        return {
            "outcome": f"{REFUSED}: {reason}",
            "n": n,
            "promote": np.zeros(n, dtype=bool),
            "n_rejected": np.zeros(n, dtype=int),
        }
    if pending:
        return {
            "outcome": PENDING,
            "n": n,
            "promote": np.zeros(n, dtype=bool),
            "n_rejected": np.zeros(n, dtype=int),
        }
    keep = [i for i, m in enumerate(meta) if str(m.get("session")) in set(used)]
    out = _many_statistics(design, panels[:, keep, :])
    out.update(sessions_used=tuple(used), dropped_sessions=tuple(dropped))
    return out


def inference_many(design: Mapping[str, Any], panels: np.ndarray) -> dict[str, Any]:
    """The vectorised statistic and Holm set of :func:`decide_many` WITHOUT the
    eligibility gate (the many-dataset form of :func:`inference_only`): every row of
    ``panels`` ``(n, G, K)`` is one complete dataset, all sessions used.  For the
    adversarial suite's out-of-domain and candidate cells, whose rejection counts R0
    asks to report although the gate refuses the declaration.  Labelled exploratory;
    it carries no PROMOTE and no outcome."""
    out = _many_statistics(design, np.asarray(panels, dtype=float))
    out["outcome"] = EXPLORATORY
    out["note"] = "exploratory; no gate outcome depends on this"
    out.pop("codes", None)
    out.pop("code_names", None)
    out.pop("promote", None)
    return out


def _many_statistics(design: Mapping[str, Any], x: np.ndarray) -> dict[str, Any]:
    """Per-dataset Student t (P-IND) or block-mean t (P-BLK), Holm step-down and the
    rejection counts over ``x`` of shape ``(n, G, K)``; the same arithmetic as
    :func:`student_t_one_sample` and :func:`holm` dataset by dataset (tested)."""
    from scipy import stats

    n, _g, k = x.shape
    if design["procedure"] == "P-BLK":
        bl = int(design.get("block_length", BLOCK_LENGTH))
        b = x.shape[1] // bl
        x = x[:, : b * bl, :].reshape(n, b, bl, k).mean(axis=2)
    m = x.shape[1]
    mean = x.mean(axis=1)
    d = x - mean[:, None, :]
    scale = np.abs(d).max(axis=1)
    degenerate = ~np.isfinite(scale) | (scale == 0.0)
    safe_scale = np.where(degenerate, 1.0, scale)
    dn = d / safe_scale[:, None, :]
    se_n = np.sqrt((dn * dn).sum(axis=1) / (m - 1)) / math.sqrt(m)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (mean / safe_scale) / se_n
    p = np.where(degenerate, np.nan, stats.t.sf(np.nan_to_num(t), m - 1))
    alpha = float(design.get("alpha_op", ALPHA_OP))
    # Holm step-down, vectorised: sorted p against alpha / (K - i); the rejections
    # are the leading run of the sorted comparisons.
    pf = np.where(np.isfinite(p), p, 1.0)
    ps = np.sort(pf, axis=1)
    thr = alpha / (k - np.arange(k))
    ok = ps <= thr[None, :]
    run = np.cumprod(ok, axis=1).sum(axis=1)
    any_degenerate = degenerate.any(axis=1)
    n_rej = np.where(any_degenerate, 0, run)
    outcome_codes = np.where(
        any_degenerate, 2, np.where(n_rej > 0, 1, 0)
    )  # 2 refused (zero variance)
    return {
        "outcome": "per-dataset",
        "n": n,
        "codes": outcome_codes,
        "code_names": {0: REJECT, 1: PROMOTE, 2: f"{REFUSED}: zero variance"},
        "promote": n_rej > 0,
        "n_rejected": n_rej,
        "p": p,
        "t": t,
    }

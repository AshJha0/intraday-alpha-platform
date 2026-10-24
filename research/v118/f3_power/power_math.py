"""F3 power diagnostic: pure arithmetic (UNSIGNED DRAFT; reads nothing, runs nothing for the study).

Frozen rules of ``research/v118/F3_POWER_DIAGNOSTIC_SPEC.md``: Wilson interval, the MDE80 rule (section 6.1), the
bootstrap percentile rule (6.2), the heterogeneous-to-homogeneous ratios (6.3) and the programming-integrity band (7).
Every constant comes from ``declaration.json``; nothing is hard-coded here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from scipy import stats

LE_LABEL = "<= {lo}"
GT_LABEL = "> {hi}"
LOW = -1.0  # ordered encoding of "MDE80 <= first grid value"
HIGH = math.inf  # ordered encoding of "MDE80 > last grid value"


def wilson(k: int, n: int, z: float) -> tuple[float, float]:
    """Two-sided Wilson score interval for k successes in n trials."""
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return min(max(0.0, centre - half), p), max(
        min(1.0, centre + half), p
    )  # clamp: rounding at p = 0 or 1


def mde_point(grid: Sequence[float], p: Sequence[float], target: float) -> float:
    """MDE80 by the frozen first-crossing rule.  Returns ``LOW`` if the first grid point already reaches ``target``,
    ``HIGH`` if no grid point does, else the linear interpolation between the last point below and the first at or
    above ``target``.  Never extrapolates."""
    if p[0] >= target:
        return LOW
    for j in range(1, len(grid)):
        if p[j] >= target:
            return grid[j - 1] + (target - p[j - 1]) / (p[j] - p[j - 1]) * (grid[j] - grid[j - 1])
    return HIGH


def mde_nonmonotone(
    grid: Sequence[float], p: Sequence[float], target: float
) -> list[tuple[float, float]]:
    """(d, p) of every grid point after the first crossing whose power is below ``target`` (empty if monotone)."""
    first = next((j for j, v in enumerate(p) if v >= target), None)
    if first is None:
        return []
    return [(grid[j], p[j]) for j in range(first + 1, len(grid)) if p[j] < target]


def mde_label(v: float, grid: Sequence[float], digits: int = 1) -> str:
    if v == LOW:
        return LE_LABEL.format(lo=grid[0])
    if v == HIGH:
        return GT_LABEL.format(hi=grid[-1])
    return f"{v:.{digits}f}"


def ordered_percentiles(vals: Sequence[float], lo_q: float, hi_q: float) -> tuple[float, float]:
    """Frozen index rule on the ordered replicates: lower = s[floor(lo_q (B-1))], upper = s[ceil(hi_q (B-1))]."""
    s = sorted(vals)
    b = len(s)
    return s[int(math.floor(lo_q * (b - 1)))], s[int(math.ceil(hi_q * (b - 1)))]


def numeric_share(vals: Sequence[float]) -> float:
    return sum(1 for v in vals if v not in (LOW, HIGH)) / len(vals)


def power_ratio(k_b: int, k_a: int, n: int, min_count: int, z: float) -> dict[str, Any]:
    """Heterogeneous-to-homogeneous power ratio and delta-method interval on log R; defined only if both counts
    reach ``min_count``."""
    if k_b < min_count or k_a < min_count:
        return {"defined": False, "reason": f"count < {min_count}", "k_b": k_b, "k_a": k_a}
    pb, pa = k_b / n, k_a / n
    se = math.sqrt((1 - pb) / (n * pb) + (1 - pa) / (n * pa))
    r = pb / pa
    return {
        "defined": True,
        "ratio": r,
        "lo": r * math.exp(-z * se),
        "hi": r * math.exp(z * se),
        "k_b": k_b,
        "k_a": k_a,
    }


def mde_ratio(
    mde_b: float, mde_a: float, flags_b: Sequence[Any], flags_a: Sequence[Any]
) -> dict[str, Any]:
    """MDE80 ratio MDE_b / MDE_a0: numeric only when both are numeric and neither curve is non-monotone; a
    '> hi' result in b over a numeric a is reported as a bound; everything else is undefined."""
    if flags_b or flags_a:
        return {"kind": "undefined", "reason": "non-monotone curve"}
    if mde_a in (LOW, HIGH):
        return {"kind": "undefined", "reason": "homogeneous MDE80 is a boundary label"}
    if mde_b == LOW:
        return {"kind": "undefined", "reason": "heterogeneous MDE80 is a boundary label"}
    if mde_b == HIGH:
        return {"kind": "bound", "reason": "heterogeneous MDE80 is above the grid"}
    return {"kind": "numeric", "ratio": mde_b / mde_a}


# ------------------------------------------------------------ integrity band --
def pi_ref(alpha_op: float, g: int, k: int, d: float) -> float:
    """P(noncentral t(g-1, ncp d) >= t_{1 - alpha_op/k}(g-1)): the probability the planted hypothesis clears the first
    Holm step; a lower bound for its Holm power under a0."""
    crit = stats.t.ppf(1.0 - alpha_op / k, g - 1)
    return float(stats.nct.sf(crit, g - 1, d))


def excess(alpha_op: float, k: int) -> float:
    """Probability that at least one of the k - 1 exact null p-values clears the first Holm step (a0)."""
    return 1.0 - (1.0 - alpha_op / k) ** (k - 1)


def band(
    alpha_op: float, g: int, k: int, d: float, tau: float, excess_k: float | None = None
) -> tuple[float, float]:
    """Integrity band; ``excess_k`` is the value fixed in the declaration (``integrity.excess.value_by_K``), the analytic
    :func:`excess` when omitted."""
    ref = pi_ref(alpha_op, g, k, d)
    ex = excess(alpha_op, k) if excess_k is None else excess_k
    return ref - tau, min(1.0, ref + ex) + tau


def integrity_violations(
    alpha_op: float,
    phat: Mapping[tuple[int, int, float], float],
    tau: float,
    excess_by_k: Mapping[int, float] | None = None,
) -> list[tuple[int, int, float]]:
    """(G, K, d) combinations whose a0 power estimate lies outside the analytic band."""
    bad = []
    for (g, k, d), v in sorted(phat.items()):
        lo, hi = band(alpha_op, g, k, d, tau, None if excess_by_k is None else excess_by_k[k])
        if not (lo <= v <= hi):
            bad.append((g, k, d))
    return bad


def holm_planted_rejected(p: np.ndarray, alpha: float, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Holm step-down at ``alpha`` over the first ``k`` columns of ``p`` (n, >=k).  Returns (planted rejected,
    any null rejected) as boolean arrays; the planted hypothesis is column 0.  A NaN p counts as 1."""
    pf = np.where(np.isfinite(p[:, :k]), p[:, :k], 1.0)
    ps = np.sort(pf, axis=1)
    ok = ps <= (alpha / (k - np.arange(k)))[None, :]
    nrej = np.cumprod(ok, axis=1).sum(axis=1)
    idx = np.clip(nrej - 1, 0, k - 1)
    cutoff = np.take_along_axis(ps, idx[:, None], axis=1)[:, 0]
    planted = (nrej > 0) & (pf[:, 0] <= cutoff)
    any_null = nrej > planted.astype(nrej.dtype)
    return planted, any_null

"""The admission test (ML protocol sections 7-8): paired Delta U, block bootstrap, Holm within tier, the carried candidate.

- :func:`block_length_from_dependence` fixes the block length from the
  incumbent's development series **before any comparison** (the smallest L in
  1..max such that every autocorrelation beyond L is inside ``2 / sqrt(G)``).
- :func:`block_bootstrap_mean` is the stationary block bootstrap of the mean of
  a session series (fixed block length, SplitMix64 seed).
- :func:`admission_test` returns the one-sided interval, the bootstrap p-value
  (share of resample means ``<= delta``) and the admission flag
  ``CI_lower > delta``.
- :func:`holm_within_tier` adjusts the candidates' p-values over the tier's K
  (:func:`iap.validation.confirmatory.holm`).
- :func:`choose_candidate` is the lexicographic tie-break (class order, then the
  class-specific complexity, then grid order) among the admitted candidates.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from iap.core.rng import SplitMix64
from iap.ml.protocol import CLASS_ORDER, tier_of
from iap.validation.confirmatory import holm

__all__ = [
    "AdmissionResult",
    "admission_test",
    "block_bootstrap_mean",
    "block_length_from_dependence",
    "choose_candidate",
    "holm_within_tier",
    "paired_delta",
]


def paired_delta(
    u_candidate: Sequence[float],
    u_incumbent: Sequence[float],
    session_ids: Sequence[str],
    incumbent_ids: Sequence[str],
) -> np.ndarray:
    """``Delta U_d`` on identical sessions; refuses series that do not align session by session."""
    if list(session_ids) != list(incumbent_ids):
        raise ValueError("candidate and incumbent were not run on identical sessions")
    a = np.asarray(u_candidate, dtype=float)
    b = np.asarray(u_incumbent, dtype=float)
    if a.shape != b.shape or a.ndim != 1:
        raise ValueError("paired series must be one-dimensional and equal in length")
    return a - b


def _acf(x: np.ndarray, lag: int) -> float:
    n = x.size
    if lag >= n or x.std() == 0:
        return 0.0
    xc = x - x.mean()
    num = float((xc[:-lag] * xc[lag:]).sum())
    den = float((xc * xc).sum())
    return num / den if den > 0 else 0.0


def block_length_from_dependence(series: Sequence[float], *, max_block: int = 10) -> dict[str, Any]:
    """The smallest ``L`` such that every lag autocorrelation beyond ``L`` (up to ``max_block``) is inside ``2 / sqrt(G)``."""
    x = np.asarray(series, dtype=float)
    g = int(x.size)
    if g < 2 * max_block + 2:
        raise ValueError(f"too few sessions ({g}) to fix a block length up to {max_block}")
    band = 2.0 / math.sqrt(g)
    acf = {lag: _acf(x, lag) for lag in range(1, max_block + 1)}
    chosen = max_block
    for L in range(1, max_block + 1):
        if all(abs(acf[lag]) <= band for lag in range(L + 1, max_block + 1)):
            chosen = L
            break
    return {
        "block_length": int(chosen),
        "band": band,
        "acf": {str(k): float(v) for k, v in acf.items()},
        "sessions": g,
    }


def block_bootstrap_mean(
    series: Sequence[float], block_length: int, *, resamples: int, seed: int
) -> np.ndarray:
    """Means of ``resamples`` stationary-block resamples of ``series`` (blocks of fixed length, wrapped)."""
    x = np.asarray(series, dtype=float)
    n = int(x.size)
    if block_length < 1 or n < block_length:
        raise ValueError("block_length must be in 1..n")
    rng = SplitMix64(seed)
    nb = math.ceil(n / block_length)
    out = np.empty(resamples)
    for b in range(resamples):
        idx = np.empty(nb * block_length, dtype=int)
        for k in range(nb):
            start = rng.below(n)
            idx[k * block_length : (k + 1) * block_length] = (start + np.arange(block_length)) % n
        out[b] = x[idx[:n]].mean()
    return out


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    mean_bps: float
    ci_lower_bps: float
    delta_bps: float
    p_value: float
    admitted: bool
    block_length: int
    resamples: int
    sessions: int
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def admission_test(
    delta_u: Sequence[float],
    *,
    delta_bps: float,
    block_length: int,
    resamples: int = 2000,
    seed: int = 20261025,
    level: float = 0.95,
) -> AdmissionResult:
    """``CI_lower(Delta U) > delta`` from the stationary block bootstrap; p = share of resample means ``<= delta``."""
    x = np.asarray(delta_u, dtype=float)
    if x.size < 2 * block_length:
        raise ValueError("too few paired sessions for the block bootstrap")
    boots = block_bootstrap_mean(x, block_length, resamples=resamples, seed=seed)
    lower = float(np.quantile(boots, 1.0 - level))
    p = float((boots <= delta_bps).mean())
    return AdmissionResult(
        float(x.mean()),
        lower,
        float(delta_bps),
        p,
        bool(lower > delta_bps),
        int(block_length),
        int(resamples),
        int(x.size),
        int(seed),
    )


def holm_within_tier(
    p_values: Mapping[str, float], *, alpha: float = 0.05, extra_looks: int = 0
) -> dict[str, Any]:
    """Holm over the tier's candidates (plus ``extra_looks`` early-stopping evaluations counted as unit p-values)."""
    names = sorted(p_values)
    p = [float(p_values[n]) for n in names] + [1.0] * int(extra_looks)
    res = holm(np.asarray(p), alpha)
    return {
        "K": len(p),
        "alpha": alpha,
        "adjusted": {n: float(res["adjusted"][i]) for i, n in enumerate(names)},
        "rejected": {n: bool(res["rejected"][i]) for i, n in enumerate(names)},
    }


def choose_candidate(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Lexicographic: lowest class in M0 < M1 < M2 < M3 < M4, then the class-specific complexity, then grid order.

    Each candidate is ``{"name", "model_class", "complexity", "grid_index", "admitted"}``;
    only admitted candidates are eligible.  Returns ``None`` when none is admitted.
    """
    eligible = [c for c in candidates if c.get("admitted")]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda c: (
            CLASS_ORDER.index(tier_of(str(c["model_class"]))),
            float(c["complexity"]),
            int(c.get("grid_index", 0)),
            str(c["name"]),
        ),
    )

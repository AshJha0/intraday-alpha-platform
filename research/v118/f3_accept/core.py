"""F3 acceptance study core: synthetic null generators, procedure P1, family evaluation, exact binomial bounds.

Self-contained (numpy + scipy + the platform SplitMix64 stream) so that the bound file set is small.  Every
parameter comes from ``declaration.json`` (bound by the prereg).  UNSIGNED DRAFT: nothing here has been run for
acceptance.  Procedure P1 = one-sided Student t(G - 1) on the CR1 statistic (CR1 = CR2 = CR3 for one value per
session and an intercept-only model), selected prospectively by the outside reviewer on 2026-10-24.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "src"))
from iap.research import known_null as kn  # noqa: E402

NH = 16


def regime_index(decl: Mapping[str, Any]) -> dict[str, int]:
    return dict(decl["seeds"]["regime_index"])


def seed_for(decl: Mapping[str, Any], regime: str, g: int, dry: bool = False) -> int:
    base = decl["seeds"]["dry_run_base"] if dry else decl["seeds"]["base"]
    return int(base) + 10 * regime_index(decl)[regime] + int(g)


def scales(decl: Mapping[str, Any], g: int) -> np.ndarray:
    return np.exp(np.linspace(0.0, math.log(float(decl["generators"]["scale_ratio"])), g))


def _normals(rng: kn.SplitMix64, *shape: int) -> np.ndarray:
    return kn.splitmix_normals(rng, int(np.prod(shape))).reshape(shape)


def draw_chunk(
    decl: Mapping[str, Any], regime: str, g: int, n: int, rng: kn.SplitMix64
) -> np.ndarray:
    """``n`` independent datasets, shape (n, g, 16), true mean 0 everywhere; advances ``rng``."""
    z = _normals(rng, n, g, NH)
    if regime == "a0":
        return z
    if regime == "a1":
        rho = float(decl["generators"]["rho_a1"])
        f = _normals(rng, n, g, 1)
        return math.sqrt(rho) * f + math.sqrt(1 - rho) * z
    sc = scales(decl, g)[None, :, None]
    if regime == "b0":
        return z * sc
    if regime == "b1":
        c = (_normals(rng, n, g, NH, 3) ** 2).sum(axis=3)
        t3 = z / np.sqrt(c / 3) / math.sqrt(3.0)
        ln = (np.exp(z) - math.exp(0.5)) / math.sqrt((math.e - 1) * math.e)
        kind = (np.arange(g) % 3)[None, :, None]
        return np.where(kind == 0, z, np.where(kind == 1, t3, ln)) * sc
    raise ValueError(regime)


def p1_pvalues(x: np.ndarray) -> np.ndarray:
    """x: (n, g, 16) -> one-sided p of mean > 0 from the CR1 t statistic, t(g-1) reference.  NaN if degenerate."""
    g = x.shape[1]
    m = x.mean(axis=1)
    ss = ((x - m[:, None, :]) ** 2).sum(axis=1)
    se = np.sqrt(g / (g - 1) * ss / (g * g))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, m / se, np.nan)
    return np.where(np.isfinite(t), stats.t.sf(np.nan_to_num(t), g - 1), np.nan)


def family_rejections(p: np.ndarray, alpha_op: float, families: list[int]) -> dict[int, int]:
    """Count of datasets with at least one Holm rejection at one-sided ``alpha_op`` per family (the first K
    columns).  Under the complete null Holm rejects something iff min p <= alpha_op / K (unit-tested against
    ``known_null.holm``).  A NaN p counts as p = 1 (no rejection) and is reported separately by the caller."""
    pf = np.where(np.isfinite(p), p, 1.0)
    return {k: int((pf[:, :k].min(axis=1) <= alpha_op / k).sum()) for k in families}


def run_cell(
    decl: Mapping[str, Any], regime: str, g: int, n: int, *, dry: bool = False
) -> dict[str, Any]:
    rng = kn.SplitMix64(seed_for(decl, regime, g, dry))
    chunk = int(decl["chunk"])
    fams = list(decl["families"])
    counts = dict.fromkeys(fams, 0)
    nan = 0
    done = 0
    while done < n:
        m = min(chunk, n - done)
        p = p1_pvalues(draw_chunk(decl, regime, g, m, rng))
        nan += int((~np.isfinite(p)).sum())
        for k, c in family_rejections(p, float(decl["alpha_op"]), fams).items():
            counts[k] += c
        done += m
    return {"regime": regime, "G": g, "n": n, "seed": seed_for(decl, regime, g, dry), "nan_p": nan,
            "rejections": {f"K{k}": counts[k] for k in fams}}  # fmt: skip


def cp_upper(k: int, n: int, conf: float = 0.95) -> float:
    """One-sided Clopper-Pearson upper bound for a binomial proportion: Beta^-1(conf; k + 1, n - k)."""
    return 1.0 if k >= n else float(stats.beta.ppf(conf, k + 1, n - k))


def k_max(n: int, target: float, conf: float = 0.95) -> int:
    """Largest count k with cp_upper(k, n) <= target (the pass threshold)."""
    lo, hi = 0, int(n * target) + 1
    while lo < hi:  # cp_upper is increasing in k
        mid = (lo + hi + 1) // 2
        if cp_upper(mid, n, conf) <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo

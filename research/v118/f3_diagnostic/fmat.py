"""Helpers shared by the F3 matrix diagnostics (imports the code under test unmodified)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "python" / "src"))
from iap.research import known_null as kn  # noqa: E402
from iap.validation.multiple_testing import holm  # noqa: E402

ALPHA = 0.025


def derangement_array(g: int) -> np.ndarray:
    return np.asarray(kn.derangements(g), dtype=np.int64)  # (n, g), lexicographic


def inference_over(M: np.ndarray, perms: np.ndarray):
    """M (g, g) one hypothesis; per derangement: p, mean, se of the S2 statistic (sessions with
    no value dropped, as in the recorded run).  Uses kn.cluster_inference unmodified."""
    g = M.shape[0]
    rows = np.arange(g)
    n = perms.shape[0]
    p = np.empty(n)
    m = np.empty(n)
    se = np.empty(n)
    for d in range(n):
        x = M[rows, perms[d]]
        x = x[np.isfinite(x)]
        r = kn.cluster_inference(x, ALPHA)
        p[d] = r["p"]
        m[d] = np.nan if r["mean"] is None else r["mean"]
        se[d] = np.nan if r["se"] is None else r["se"]
    return p, m, se


def family_rejections(P: np.ndarray, Mn: np.ndarray, alpha: float = ALPHA) -> np.ndarray:
    """P, Mn (n, K): per derangement, Holm over K then mean > 0 and p_holm <= alpha."""
    out = np.zeros(P.shape[0], dtype=bool)
    for d in range(P.shape[0]):
        adj = holm([float(x) for x in P[d]])
        out[d] = any(
            a is not None and a <= alpha and mu > 0 for a, mu in zip(adj, Mn[d], strict=True)
        )
    return out


def additive_fit(M: np.ndarray, iters: int = 200):
    """Row + column + grand mean fit on the off-diagonal cells (ALS); returns (mu, a, b, resid)."""
    g = M.shape[0]
    off = ~np.eye(g, dtype=bool) & np.isfinite(M)
    mu = float(M[off].mean())
    a = np.zeros(g)
    b = np.zeros(g)
    for _ in range(iters):
        for i in range(g):
            sel = off[i]
            a[i] = np.mean(M[i, sel] - mu - b[sel])
        for j in range(g):
            sel = off[:, j]
            b[j] = np.mean(M[sel, j] - mu - a[sel])
    fit = mu + a[:, None] + b[None, :]
    return mu, a, b, np.where(off, M - fit, np.nan)

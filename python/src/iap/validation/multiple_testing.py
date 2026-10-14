"""Multiple-testing adjustments for a family of hypotheses (v1.16 item 15).

REPORT-ONLY: no gate, threshold or verdict reads these. The promotion gate
keeps the ledger-wide Bonferroni ``|t|`` threshold (``iap.validation.ledger``);
these columns show how the family of alphas reported side by side looks
under the standard family-wise and false-discovery procedures.

- :func:`holm` — Holm (1979) step-down adjusted p-values (FWER).
- :func:`benjamini_hochberg` — Benjamini-Hochberg (1995) step-up adjusted
  p-values ("q-values", FDR under independence / PRDS).
- :func:`romano_wolf` — Romano-Wolf (2005) step-down max-t adjusted p-values
  (FWER, using the joint dependence of the test statistics) from observed
  t statistics and a matrix of bootstrap t statistics CENTRED at the null.
- :func:`fold_bootstrap_t` — such a matrix from per-fold statistics shared
  across hypotheses (resampling folds jointly, seeded, deterministic).

Every function returns values in the input order; ``None`` / non-finite
inputs give ``None`` and are excluded from the family size.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

__all__ = [
    "benjamini_hochberg",
    "fold_bootstrap_t",
    "holm",
    "p_two_sided",
    "romano_wolf",
]


def p_two_sided(t: float | None) -> float | None:
    """Two-sided normal p-value of a t statistic (``None`` when not finite)."""
    if t is None or not math.isfinite(t):
        return None
    return math.erfc(abs(t) / math.sqrt(2.0))


def _finite(values: Sequence[float | None]) -> list[int]:
    return [i for i, v in enumerate(values) if v is not None and math.isfinite(v)]


def holm(pvalues: Sequence[float | None]) -> list[float | None]:
    """Holm step-down adjusted p-values."""
    idx = _finite(pvalues)
    m = len(idx)
    out: list[float | None] = [None] * len(pvalues)
    running = 0.0
    for rank, i in enumerate(sorted(idx, key=lambda k: (pvalues[k], k))):
        running = max(running, min(1.0, (m - rank) * float(pvalues[i])))
        out[i] = running
    return out


def benjamini_hochberg(pvalues: Sequence[float | None]) -> list[float | None]:
    """Benjamini-Hochberg step-up adjusted p-values (q-values)."""
    idx = _finite(pvalues)
    m = len(idx)
    out: list[float | None] = [None] * len(pvalues)
    order = sorted(idx, key=lambda k: (pvalues[k], k))
    running = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        running = min(running, float(pvalues[i]) * m / (rank + 1))
        out[i] = min(1.0, running)
    return out


def romano_wolf(t_obs: Sequence[float | None], t_boot: np.ndarray) -> list[float | None]:
    """Romano-Wolf step-down adjusted p-values for two-sided tests.

    ``t_boot`` is ``(B, m)``: bootstrap t statistics of the m hypotheses,
    centred so the null holds (e.g. :func:`fold_bootstrap_t`). Hypotheses are
    taken in decreasing ``|t_obs|``; the k-th adjusted p-value is the share
    of draws whose max ``|t*|`` over the hypotheses not yet rejected is at
    least ``|t_obs|``, made monotone.
    """
    t_boot = np.asarray(t_boot, dtype=float)
    idx = _finite(t_obs)
    out: list[float | None] = [None] * len(t_obs)
    if not idx:
        return out
    if t_boot.ndim != 2 or t_boot.shape[1] != len(t_obs):
        raise ValueError("t_boot must be (B, len(t_obs))")
    order = sorted(idx, key=lambda k: (-abs(float(t_obs[k])), k))
    absb = np.abs(np.nan_to_num(t_boot, nan=0.0))
    running = 0.0
    for pos, i in enumerate(order):
        rest = order[pos:]
        mx = absb[:, rest].max(axis=1)
        p = float(np.mean(mx >= abs(float(t_obs[i]))))
        running = max(running, p)
        out[i] = running
    return out


def fold_bootstrap_t(
    fold_stats: np.ndarray, n_boot: int = 2000, seed: int = 20261014
) -> tuple[list[float | None], np.ndarray]:
    """Observed and null-centred bootstrap t statistics from per-fold values.

    ``fold_stats`` is ``(F, m)``: one statistic (e.g. the fold IC) per fold
    and hypothesis, folds aligned across hypotheses (NaN = missing). Folds
    are resampled JOINTLY with replacement, so the dependence between the
    hypotheses is kept; each draw's t is ``(mean* - mean) / (sd* / sqrt(F))``.
    Returns ``(t_obs, t_boot)`` for :func:`romano_wolf`. With few folds the
    null distribution is coarse; label it as fold-based where reported.
    """
    x = np.asarray(fold_stats, dtype=float)
    f, m = x.shape
    rng = np.random.default_rng(seed)
    t_obs: list[float | None] = []
    for j in range(m):
        col = x[:, j][np.isfinite(x[:, j])]
        if col.size >= 2 and col.std(ddof=1) > 0:
            t_obs.append(float(col.mean() / (col.std(ddof=1) / math.sqrt(col.size))))
        else:
            t_obs.append(None)
    mean = np.nanmean(x, axis=0)
    draws = rng.integers(0, f, size=(n_boot, f))
    t_boot = np.full((n_boot, m), np.nan)
    for b in range(n_boot):
        s = x[draws[b]]
        with np.errstate(invalid="ignore", divide="ignore"):
            n = np.sum(np.isfinite(s), axis=0)
            sd = np.nanstd(s, axis=0, ddof=1)
            t_boot[b] = (np.nanmean(s, axis=0) - mean) / (sd / np.sqrt(n))
    return t_obs, t_boot


def report_family(reports: dict[str, dict]) -> list[dict]:
    """Per-alpha rows for a batch report: the gate t's two-sided p, its
    Bonferroni, Holm and BH adjustments over every alpha with a gate t, and
    a Romano-Wolf p from a joint fold-IC bootstrap within each asset class
    (folds aligned by fold number; ``None`` with fewer than 3 shared folds)."""
    ids = sorted(reports)
    p = [p_two_sided(reports[a].get("gate_tstat")) for a in ids]
    m = sum(v is not None for v in p)
    hol, bh = holm(p), benjamini_hochberg(p)
    rw: dict[str, float | None] = dict.fromkeys(ids)
    for cls in sorted({str(reports[a].get("asset_class")) for a in ids}):
        members = [a for a in ids if str(reports[a].get("asset_class")) == cls]
        folds = sorted({int(f["fold"]) for a in members for f in reports[a].get("folds") or []})
        if len(folds) < 3:
            continue
        x = np.full((len(folds), len(members)), np.nan)
        for j, a in enumerate(members):
            for f in reports[a].get("folds") or []:
                if not f.get("degenerate") and f.get("ic") is not None:
                    x[folds.index(int(f["fold"])), j] = float(f["ic"])
        t_obs, t_boot = fold_bootstrap_t(x)
        for a, v in zip(members, romano_wolf(t_obs, t_boot), strict=True):
            rw[a] = v
    return [
        {
            "alpha_id": a,
            "gate_t": reports[a].get("gate_tstat"),
            "p": p[i],
            "bonferroni": None if p[i] is None else min(1.0, p[i] * m),
            "holm": hol[i],
            "bh": bh[i],
            "romano_wolf_fold": rw[a],
        }
        for i, a in enumerate(ids)
    ]

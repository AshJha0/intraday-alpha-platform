"""Combination weights: four estimators over a stack of member signals.

Input everywhere: ``Z`` — an ``n x K`` array of member signals (one column
per member alpha, NaN where the member has no opinion on the row), ``y`` —
the label of each row (NaN where it has none), and for the ridge penalty
search the inner-fold id and the timestamp of each row.  The caller
(:class:`iap.combine.model.CombinedAlpha`) builds the stack from the
members' OUT-OF-SAMPLE predictions inside one training window, so nothing
here ever sees a test row.

**Standardisation** (:func:`standardise`).  Each column is centred and
scaled by its own mean and standard deviation over its finite rows of the
stack; a missing signal becomes 0.0 — the member's mean, "no opinion".  A
member with fewer than :data:`MIN_OBS` finite rows or no variance is
INACTIVE: its column is zero and its weight is 0.  The same mean and scale
are stored and applied to the test rows, so no statistic of a test row
enters its own standardisation.

**The methods** (:data:`METHODS`; one documented default,
:data:`DEFAULT_METHOD`):

``equal_weight``
    ``w_k = 1 / K_active``.  The baseline every other method has to beat,
    and the default: with a short sample the error in ANY estimated weight
    vector is of the order of the gain a correct one would bring, and the
    equal-weight blend estimates nothing.  It uses the members' signals in
    their hypothesised orientation and never looks at a label.
``ic_weighted``
    ``w_k`` proportional to ``max(IC_k, 0) / (1 - IC_k**2)`` — each member's
    in-stack IC over the residual variance of its own univariate forecast,
    the inverse-variance weighting of independent forecasts.  A member whose
    stack IC contradicts its hypothesis gets weight 0 rather than a short:
    betting against a stated rationale is a new hypothesis, not a weight.
``ridge``
    ``w = (G + lambda * I)^-1 g`` with ``G = Z'Z / n`` and ``g = Z'y / n``
    on the standardised stack (``y`` centred and scaled to unit variance).
    ``lambda`` is chosen from :data:`RIDGE_PENALTIES` by forward-chained
    cross-validation over the inner folds of the stack: for each inner fold
    ``j > 1`` the weights are fitted on the folds before it — minus the rows
    whose label window or embargo reaches into fold ``j`` — and scored on
    fold ``j`` by squared error; the penalty with the smallest pooled error
    wins, the LARGER one on a tie.  When no split is possible the largest
    penalty is used (:attr:`WeightFit.detail` says so).
``shrinkage_mv``
    ``w = S*^-1 a``: the mean-variance blend of the signals, with ``a =
    cov(z, y)`` and ``S*`` the Ledoit-Wolf shrinkage of the signal
    covariance towards a scaled identity (:func:`ledoit_wolf`; numpy only).

``decay_aware`` (v1.17, C3)
    ``ic_weighted`` with each member's weight multiplied by its **survival
    ratio** at the combination horizon: ``r_k = clip(IC_k(h_c) / IC_k(h_k),
    0, 1)`` where ``IC_k(h_k)`` is the member's in-stack IC at its OWN label
    horizon and ``IC_k(h_c)`` at the combination's (``r_k = 1`` when the two
    horizons coincide, 0 when the own-horizon IC is not positive).  A member
    whose signal has decayed by the time the combination's label resolves
    is down-weighted by how much of it is left.  The own-horizon labels
    come from the caller (``own_labels``); without them the method equals
    ``ic_weighted`` and ``WeightFit.detail["own_labels"]`` says so.

**Stacking revision v1.17 (review R-8).**  Two fixes to the label-reading
methods, recorded as ``WeightFit.detail["stacking_revision"]``:

1. the ridge penalty search re-standardises the stack INSIDE every
   forward-chained split — the mean and scale of each member over the
   split's training rows, applied to its test rows — instead of the
   whole-stack moments, which let the test fold's rows into the moments
   that scored it;
2. a "no opinion" row (NaN signal) no longer enters ``Z'Z / n`` and ``Z'y /
   n`` as a 0.0: both are **pairwise-complete** moments — ``G_jk`` is the
   mean of ``z_j z_k`` over the rows where BOTH members have an opinion,
   ``g_j`` the mean of ``z_j y`` over the rows where member ``j`` has one —
   so a member that is silent on half the stack is not read as "zero
   signal" on those rows (which shrank its weight and biased the
   covariance toward zero).  The Ledoit-Wolf shrinkage of ``shrinkage_mv``
   is applied to the same pairwise-complete covariance.

A result committed before v1.17 under ``ridge`` / ``shrinkage_mv`` was
computed without these fixes; the report pipeline writes the stacking
revision into every new experiment identity (:mod:`iap.combine.report`),
so a rerun of an old identity is not silently different.

**Correlation between members.**  ``ridge`` and ``shrinkage_mv`` account for
it explicitly: both invert a (shrunk) estimate of the signal covariance, so
two members that say the same thing share one weight between them.
``equal_weight`` and ``ic_weighted`` do NOT — by definition — and nothing is
orthogonalised: a block of near-duplicate members is over-weighted by them.
That is what the effective number of independent bets
(:func:`effective_bets`) measures, and what the lifecycle's
``cross_alpha_correlation`` gate keeps out of the allocated set.

Every weight vector is normalised to ``sum(|w|) = 1`` (the combination is
re-standardised afterwards, so only the direction matters) and every
function is deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from iap.core.shrinkage import ledoit_wolf  # noqa: F401  (re-exported)
from iap.validation.metrics import ic

__all__ = [
    "DEFAULT_METHOD",
    "METHODS",
    "MIN_OBS",
    "RIDGE_PENALTIES",
    "STACKING_REVISION",
    "Standardised",
    "WeightFit",
    "apply_standardisation",
    "blend",
    "correlation_matrix",
    "effective_bets",
    "fit_weights",
    "ledoit_wolf",
    "pairwise_moments",
    "standardise",
]

#: Combination methods, by name; the first is the default.
METHODS = ("equal_weight", "ic_weighted", "ridge", "shrinkage_mv", "decay_aware")
DEFAULT_METHOD = "equal_weight"
#: The stacking revision of the label-reading methods (module docs).
STACKING_REVISION = "v1.17"
#: Ridge penalties searched inside the training window (dimensionless: the
#: stack is standardised, so ``G`` has a unit diagonal).
RIDGE_PENALTIES = (0.01, 0.1, 1.0, 10.0, 100.0)
#: Fewer finite rows than this is no evidence (``iap.validation.metrics.ic``).
MIN_OBS = 32
_EPS = 1e-12


@dataclass(frozen=True)
class Standardised:
    """A standardised stack (module docs): the ``n x K`` array with NaN
    replaced by 0.0, the per-member mean and scale, and the active mask."""

    values: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    active: np.ndarray


@dataclass(frozen=True)
class WeightFit:
    """Weights of one method on one stack: ``weights`` has one entry per
    member (0.0 for an inactive one), normalised to ``sum(|w|) = 1`` or all
    zero when the method found nothing to weight."""

    method: str
    weights: np.ndarray
    detail: dict[str, Any] = field(default_factory=dict)


def standardise(z: np.ndarray) -> Standardised:
    """Centre and scale each column over its finite rows (module docs)."""
    z = np.asarray(z, dtype=float)
    if z.ndim != 2:
        raise ValueError("standardise: expected an n x K array")
    n, k = z.shape
    values = np.zeros((n, k))
    mean = np.zeros(k)
    scale = np.zeros(k)
    active = np.zeros(k, dtype=bool)
    for j in range(k):
        col = z[:, j]
        ok = np.isfinite(col)
        if int(ok.sum()) < MIN_OBS:
            continue
        m = float(np.mean(col[ok]))
        s = float(np.std(col[ok]))
        if not s > _EPS:
            continue
        mean[j], scale[j], active[j] = m, s, True
        values[ok, j] = (col[ok] - m) / s
    return Standardised(values=values, mean=mean, scale=scale, active=active)


def apply_standardisation(z: np.ndarray, mean: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """``z`` under a STORED mean and scale (NaN and inactive columns -> 0.0)."""
    z = np.asarray(z, dtype=float)
    out = np.zeros(z.shape)
    for j in range(z.shape[1]):
        if not scale[j] > 0.0:
            continue
        ok = np.isfinite(z[:, j])
        out[ok, j] = (z[ok, j] - mean[j]) / scale[j]
    return out


def blend(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """``sum_k weights[k] * values[:, k]``, accumulated column by column.

    Deliberately NOT ``values @ weights``: a BLAS matrix-vector product may
    round one row's dot product differently depending on how many rows the
    array has, and a score must be bit-identical whether the frame holds the
    rows after it or not (the truncation and recompute leakage probes).
    Element-wise accumulation in a fixed member order is.
    """
    out = np.zeros(values.shape[0])
    for j in range(values.shape[1]):
        if weights[j] != 0.0:
            out = out + weights[j] * values[:, j]
    return out


def correlation_matrix(z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pairwise-complete Pearson correlation of the columns of ``z``.

    Returns ``(corr, n_common)``: entry ``(i, j)`` is computed over the rows
    where BOTH columns are finite and is NaN when there are fewer than
    :data:`MIN_OBS` of them or either column is constant on them; the
    diagonal is 1.0 for a column with enough finite rows and variance.
    """
    z = np.asarray(z, dtype=float)
    k = z.shape[1]
    corr = np.full((k, k), np.nan)
    n_common = np.zeros((k, k), dtype=np.int64)
    finite = np.isfinite(z)
    for i in range(k):
        for j in range(i, k):
            ok = finite[:, i] & finite[:, j]
            n = int(ok.sum())
            n_common[i, j] = n_common[j, i] = n
            if n < MIN_OBS:
                continue
            a = z[ok, i] - np.mean(z[ok, i])
            b = z[ok, j] - np.mean(z[ok, j])
            den = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
            if not den > 0.0:
                continue
            rho = 1.0 if i == j else float(np.clip(np.sum(a * b) / den, -1.0, 1.0))
            corr[i, j] = corr[j, i] = rho
    return corr, n_common


def effective_bets(corr: np.ndarray) -> dict[str, Any]:
    """The effective number of independent bets in a correlation matrix:
    the participation ratio of its eigenvalues,
    ``N_eff = (sum lambda)^2 / sum lambda^2``.

    ``K`` uncorrelated signals give ``N_eff = K``; ``K`` copies of one
    signal give 1.  Members without a defined correlation with every other
    member (NaN rows) are dropped first, and an undefined off-diagonal entry
    between two kept members is read as 0.0.  Returns ``{"n_members",
    "n_effective", "eigenvalues", "mean_abs_offdiag", "mean_offdiag"}``.
    """
    c = np.asarray(corr, dtype=float)
    keep = np.isfinite(np.diag(c))
    c = np.where(np.isfinite(c), c, 0.0)[np.ix_(keep, keep)]
    k = int(c.shape[0])
    if k == 0:
        return {
            "n_members": 0,
            "n_effective": 0.0,
            "eigenvalues": [],
            "mean_abs_offdiag": None,
            "mean_offdiag": None,
        }
    eig = np.sort(np.linalg.eigvalsh((c + c.T) / 2.0))[::-1]
    eig = np.where(eig > 0.0, eig, 0.0)
    total = float(eig.sum())
    n_eff = total * total / float(np.sum(eig * eig)) if total > 0.0 else 0.0
    off = c[~np.eye(k, dtype=bool)]
    return {
        "n_members": k,
        "n_effective": float(n_eff),
        "eigenvalues": [float(v) for v in eig],
        "mean_abs_offdiag": float(np.mean(np.abs(off))) if off.size else None,
        "mean_offdiag": float(np.mean(off)) if off.size else None,
    }


def _normalise(w: np.ndarray) -> np.ndarray:
    total = float(np.sum(np.abs(w)))
    if not np.isfinite(total) or not total > 0.0:
        return np.zeros_like(w)
    return w / total


def _unit_label(y: np.ndarray) -> tuple[np.ndarray, bool]:
    """``y`` centred and scaled to unit variance; ``False`` when it is constant."""
    m = float(np.mean(y))
    s = float(np.std(y))
    if not s > 0.0:
        return np.zeros_like(y), False
    return (y - m) / s, True


def pairwise_moments(
    zs: np.ndarray, yc: np.ndarray, present: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """``(G, g)``: ``G[j, k]`` = mean of ``zs[:, j] * zs[:, k]`` over the rows
    where ``present[:, j] & present[:, k]``, ``g[j]`` = mean of ``zs[:, j]
    * yc`` over the rows where ``present[:, j]`` (module docs, R-8 fix 2);
    0.0 for a pair with no common row.  ``zs`` is the standardised stack
    (absent rows 0.0), ``present`` its finite mask."""
    p = present.astype(float)
    n_pair = p.T @ p
    g_mat = (zs.T @ zs) / np.where(n_pair > 0.0, n_pair, 1.0)
    n_one = p.sum(axis=0)
    g_vec = (zs.T @ yc) / np.where(n_one > 0.0, n_one, 1.0)
    return g_mat, g_vec


def _ridge_solve(
    zs: np.ndarray, yc: np.ndarray, penalty: float, present: np.ndarray | None = None
) -> np.ndarray:
    k = zs.shape[1]
    if present is None:
        present = np.ones(zs.shape, dtype=bool)
    g_mat, g_vec = pairwise_moments(zs, yc, present)
    return np.linalg.solve(g_mat + penalty * np.eye(k), g_vec)


def _restandardise(z_raw: np.ndarray, train: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(values, present)`` of ``z_raw`` standardised with the moments of
    its ``train`` rows only (R-8 fix 1); a column inactive on the training
    rows is 0.0 everywhere."""
    std = standardise(z_raw[train])
    values = apply_standardisation(z_raw, std.mean, std.scale)
    present = np.isfinite(z_raw) & std.active[None, :]
    return values, present


def _ridge_penalty(
    z_raw: np.ndarray, y: np.ndarray, folds: np.ndarray, ts: np.ndarray, purge_ns: int
) -> tuple[float, dict[str, Any]]:
    """The penalty chosen by forward-chained CV over the inner folds
    (module docs) and what the search saw.  ``z_raw`` is the RAW stack
    (NaN = no opinion): every split standardises it with its own training
    rows' moments (R-8 fix 1) and solves on pairwise-complete moments
    (fix 2)."""
    sse = dict.fromkeys(RIDGE_PENALTIES, 0.0)
    splits = 0
    order = sorted(int(f) for f in np.unique(folds))
    for j in order[1:]:
        test = folds == j
        start = int(ts[test].min())
        train = (folds < j) & (ts + purge_ns < start)
        if int(train.sum()) < MIN_OBS or int(test.sum()) < MIN_OBS:
            continue
        m = float(np.mean(y[train]))
        s = float(np.std(y[train]))
        if not s > 0.0:
            continue
        zs, present = _restandardise(z_raw, train)
        if not present[train].any():
            continue
        splits += 1
        y_train, y_test = (y[train] - m) / s, (y[test] - m) / s
        for penalty in RIDGE_PENALTIES:
            w = _ridge_solve(zs[train], y_train, penalty, present[train])
            resid = y_test - zs[test] @ w
            sse[penalty] += float(np.sum(resid * resid))
    if splits == 0:
        return RIDGE_PENALTIES[-1], {"cv_splits": 0, "cv_sse": None}
    # the smallest pooled error; the LARGER penalty on a tie
    best = min(RIDGE_PENALTIES, key=lambda p: (sse[p], -p))
    return best, {"cv_splits": splits, "cv_sse": {f"{p:g}": sse[p] for p in RIDGE_PENALTIES}}


def fit_weights(
    method: str,
    z: np.ndarray,
    y: np.ndarray,
    folds: np.ndarray | None = None,
    ts: np.ndarray | None = None,
    purge_ns: int = 0,
    own_labels: Mapping[int, np.ndarray] | None = None,
) -> tuple[WeightFit, Standardised]:
    """Weights of ``method`` on the stack ``(z, y)`` (module docs).

    ``folds`` / ``ts`` / ``purge_ns`` (inner-fold id and timestamp per row,
    label horizon + embargo) are read by ``ridge`` only; ``own_labels``
    (``{column: label at that member's OWN horizon}``, NaN where it has
    none) by ``decay_aware`` only.  Rows without a finite label are dropped
    for every label-reading method.  Returns the fit and the
    standardisation it used.
    """
    if method not in METHODS:
        raise ValueError(f"unknown combination method {method!r}; known: {METHODS}")
    z = np.asarray(z, dtype=float)
    y = np.asarray(y, dtype=float)
    if z.ndim != 2 or y.shape != (z.shape[0],):
        raise ValueError("fit_weights: z must be n x K and y of length n")
    k = z.shape[1]
    std = standardise(z)
    active = std.active
    weights = np.zeros(k)
    detail: dict[str, Any] = {"n_active": int(active.sum())}
    if not active.any():
        return WeightFit(method, weights, detail), std

    if method == "equal_weight":
        weights[active] = 1.0
        return WeightFit(method, _normalise(weights), detail), std

    member_ic = np.array([ic(z[:, j], y) if active[j] else np.nan for j in range(k)], dtype=float)
    detail["member_ic"] = [float(v) if np.isfinite(v) else None for v in member_ic]
    if method in ("ic_weighted", "decay_aware"):
        good = active & np.isfinite(member_ic) & (member_ic > 0.0)
        weights[good] = member_ic[good] / (1.0 - member_ic[good] ** 2 + _EPS)
        if method == "decay_aware":
            survival = np.ones(k)
            own_ic: list[float | None] = [None] * k
            for j in range(k):
                lab = (own_labels or {}).get(j)
                if lab is None or not active[j]:
                    continue
                v = ic(z[:, j], np.asarray(lab, dtype=float))
                own_ic[j] = float(v) if np.isfinite(v) else None
                if not np.isfinite(v) or not v > 0.0:
                    survival[j] = 0.0
                else:
                    survival[j] = float(np.clip(member_ic[j] / v, 0.0, 1.0))
            weights = weights * survival
            detail["own_labels"] = bool(own_labels)
            detail["member_own_ic"] = own_ic
            detail["survival"] = [float(v) for v in survival]
        return WeightFit(method, _normalise(weights), detail), std

    rows = np.isfinite(y)
    zs = std.values[rows][:, active]
    present = np.isfinite(z[rows][:, active])
    yr = y[rows]
    detail["n_rows"] = int(rows.sum())
    detail["stacking_revision"] = STACKING_REVISION
    if int(rows.sum()) < MIN_OBS:
        return WeightFit(method, weights, detail), std
    yc, ok = _unit_label(yr)
    if not ok:
        return WeightFit(method, weights, detail), std

    if method == "ridge":
        if folds is None or ts is None:
            penalty, search = RIDGE_PENALTIES[-1], {"cv_splits": 0, "cv_sse": None}
        else:
            penalty, search = _ridge_penalty(
                z[rows][:, active],
                yr,
                np.asarray(folds)[rows],
                np.asarray(ts, dtype=np.int64)[rows],
                int(purge_ns),
            )
        detail.update(search)
        detail["ridge_penalty"] = float(penalty)
        weights[active] = _ridge_solve(zs, yc, penalty, present)
        return WeightFit(method, _normalise(weights), detail), std

    # shrinkage_mv: the Ledoit-Wolf shrinkage intensity is estimated on the
    # complete rows; the covariance it shrinks and the signal-label
    # covariance are the pairwise-complete moments (R-8 fix 2)
    g_mat, alpha = pairwise_moments(zs, yc, present)
    _, delta, mu = ledoit_wolf(zs)
    sigma = (1.0 - delta) * g_mat + delta * mu * np.eye(g_mat.shape[0])
    detail["shrinkage"] = float(delta)
    detail["shrinkage_target_variance"] = float(mu)
    weights[active] = np.linalg.solve(sigma, alpha)
    return WeightFit(method, _normalise(weights), detail), std

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

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from iap.validation.metrics import ic

__all__ = [
    "DEFAULT_METHOD",
    "METHODS",
    "MIN_OBS",
    "RIDGE_PENALTIES",
    "Standardised",
    "WeightFit",
    "apply_standardisation",
    "blend",
    "correlation_matrix",
    "effective_bets",
    "fit_weights",
    "ledoit_wolf",
    "standardise",
]

#: Combination methods, by name; the first is the default.
METHODS = ("equal_weight", "ic_weighted", "ridge", "shrinkage_mv")
DEFAULT_METHOD = "equal_weight"
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


def ledoit_wolf(x: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Ledoit-Wolf (2004) shrinkage of a sample covariance towards ``mu * I``.

    ``x`` is ``n x p``; it is centred here.  Returns ``(S*, delta, mu)``
    with ``S* = delta * mu * I + (1 - delta) * S``, ``S = X'X / n``,
    ``mu = trace(S) / p``, ``delta = min(b2, d2) / d2`` where
    ``d2 = ||S - mu I||_F^2`` and ``b2 = (1 / n^2) * sum_i ||x_i x_i' -
    S||_F^2`` (their Lemma 3.2-3.4 estimators).  ``delta`` is 1.0 when the
    sample covariance already is a scaled identity.  The estimator assumes
    independent rows; consecutive feature rows are not, so ``delta`` is on
    the low side here — the direction that trusts the sample more.
    """
    x = np.asarray(x, dtype=float)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 1:
        raise ValueError("ledoit_wolf: expected an n x p array with n >= 2, p >= 1")
    n, p = x.shape
    xc = x - x.mean(axis=0)
    s = xc.T @ xc / n
    mu = float(np.trace(s)) / p
    d2 = float(np.sum((s - mu * np.eye(p)) ** 2))
    if not d2 > 0.0:
        return mu * np.eye(p), 1.0, mu
    # sum_i ||x_i x_i' - S||_F^2 = sum_i |x_i|^4 - n ||S||_F^2
    row_sq = np.sum(xc * xc, axis=1)
    b2 = (float(np.sum(row_sq * row_sq)) - n * float(np.sum(s * s))) / (n * n)
    delta = min(max(b2, 0.0), d2) / d2
    return delta * mu * np.eye(p) + (1.0 - delta) * s, float(delta), mu


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


def _ridge_solve(zs: np.ndarray, yc: np.ndarray, penalty: float) -> np.ndarray:
    n, k = zs.shape
    g = zs.T @ zs / n + penalty * np.eye(k)
    return np.linalg.solve(g, zs.T @ yc / n)


def _ridge_penalty(
    zs: np.ndarray, y: np.ndarray, folds: np.ndarray, ts: np.ndarray, purge_ns: int
) -> tuple[float, dict[str, Any]]:
    """The penalty chosen by forward-chained CV over the inner folds
    (module docs) and what the search saw."""
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
        splits += 1
        y_train, y_test = (y[train] - m) / s, (y[test] - m) / s
        for penalty in RIDGE_PENALTIES:
            w = _ridge_solve(zs[train], y_train, penalty)
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
) -> tuple[WeightFit, Standardised]:
    """Weights of ``method`` on the stack ``(z, y)`` (module docs).

    ``folds`` / ``ts`` / ``purge_ns`` (inner-fold id and timestamp per row,
    label horizon + embargo) are read by ``ridge`` only.  Rows without a
    finite label are dropped for every label-reading method.  Returns the
    fit and the standardisation it used.
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
    if method == "ic_weighted":
        good = active & np.isfinite(member_ic) & (member_ic > 0.0)
        weights[good] = member_ic[good] / (1.0 - member_ic[good] ** 2 + _EPS)
        return WeightFit(method, _normalise(weights), detail), std

    rows = np.isfinite(y)
    zs = std.values[rows][:, active]
    yr = y[rows]
    detail["n_rows"] = int(rows.sum())
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
                zs,
                yr,
                np.asarray(folds)[rows],
                np.asarray(ts, dtype=np.int64)[rows],
                int(purge_ns),
            )
        detail.update(search)
        detail["ridge_penalty"] = float(penalty)
        weights[active] = _ridge_solve(zs, yc, penalty)
        return WeightFit(method, _normalise(weights), detail), std

    # shrinkage_mv
    sigma, delta, mu = ledoit_wolf(zs)
    detail["shrinkage"] = float(delta)
    detail["shrinkage_target_variance"] = float(mu)
    alpha = (zs - zs.mean(axis=0)).T @ yc / zs.shape[0]
    weights[active] = np.linalg.solve(sigma, alpha)
    return WeightFit(method, _normalise(weights), detail), std

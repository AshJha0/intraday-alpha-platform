"""Shared Ledoit-Wolf (2004) shrinkage estimator (numpy only).

Moved here from :mod:`iap.combine.weights` (which re-exports it) so the
portfolio covariance can reuse it without importing the alpha stack.
"""

from __future__ import annotations

import numpy as np


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

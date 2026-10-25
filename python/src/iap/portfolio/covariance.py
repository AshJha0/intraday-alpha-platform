"""EWMA covariance on 1-minute bars built from feature frames (spec §15).

Bars are last-observation mid prices (``mid_price_v1``) sampled per pinned
1-minute bucket, per instrument, from the feature parquet store; returns are
log returns between consecutive common bars.  The EWMA estimator is the
RiskMetrics recursion with pinned decay ``lam = 0.94``:

    S_t = lam * S_{t-1} + (1 - lam) * r_t r_t'

initialized with the sample covariance of the first ``init_window`` bars.
A small ridge (``ridge * trace/n``) keeps Sigma positive definite for the
optimizer.  Deterministic: sorted instruments, fixed bucket boundaries.

Note on centring (pinned, documented rather than changed): the seed is the
DEMEANED sample covariance (``np.cov``) of the first ``init_window`` bars,
while each recursive update adds the RAW second moment ``r_t r_t'`` (the
RiskMetrics zero-mean convention for short-horizon returns).  On 1-minute
returns the mean is negligible next to the dispersion, so the two agree to
within ``mean^2``; making them consistent would move the committed goldens.

Opt-in shrinkage: ``shrinkage="ledoit_wolf"`` shrinks the EWMA matrix toward
a constant-correlation target (Ledoit & Wolf 2004) with intensity
``shrink_intensity`` (fixed, deterministic; default 0.1) before the ridge.
``shrinkage="ledoit_wolf_estimated"`` (opt-in) instead uses the estimated
Ledoit-Wolf (2004) intensity from :func:`iap.core.shrinkage.ledoit_wolf`
(the same estimator the combiner uses; scaled-identity target ``mu * I``).
The intensity ``delta`` is ESTIMATED ON THE EQUALLY-WEIGHTED, DEMEANED SAMPLE
COVARIANCE of all ``T`` return rows (the Lemma 3.2-3.4 formulas assume
i.i.d. rows and have no EWMA analogue); it is then APPLIED to the EWMA
matrix, ``(1 - delta) S_ewma + delta * mu_ewma * I`` with
``mu_ewma = trace(S_ewma) / N``.  The EWMA's effective sample is smaller
than ``T`` (about ``(1 + lam) / (1 - lam)`` = 32 bars at 0.94), so this
``delta`` is on the low side for the EWMA matrix.
:func:`condition_number` reports the 2-norm condition number.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from iap import io as iap_io
from iap.core.shrinkage import ledoit_wolf

_REPO = Path(__file__).resolve().parents[4]
_BAR_NS = 60_000_000_000


def bars_from_features(
    features_dir: Path | None = None,
    instruments: Sequence[int] | None = None,
    bar_ns: int = _BAR_NS,
) -> tuple[list[int], np.ndarray, np.ndarray]:
    """Build a (bar_ts, instruments) aligned log-return matrix.

    Returns ``(instrument_ids, bar_ts, returns)`` where ``returns`` is
    (T, N): log returns between consecutive bars on which EVERY instrument
    has a mid (bars missing any instrument are dropped — pinned).
    """
    if bar_ns <= 0:
        raise ValueError("bar_ns must be > 0")
    fdir = Path(features_dir) if features_dir is not None else _REPO / "data" / "features"
    files = sorted(fdir.glob("features_*.parquet"), key=lambda p: int(p.stem.split("_")[1]))
    if instruments is not None:
        want = set(int(i) for i in instruments)
        files = [p for p in files if int(p.stem.split("_")[1]) in want]
    if not files:
        raise ValueError(f"no feature parquet files under {fdir}")

    series = {}
    for path in files:
        iid = int(path.stem.split("_")[1])
        df = iap_io.read_parquet(path, columns=["exchange_ts", "mid_price_v1"], purpose="covariance")
        df = df[np.isfinite(df["mid_price_v1"].to_numpy())]
        bucket = (df["exchange_ts"].to_numpy(dtype=np.int64) // bar_ns) * bar_ns
        # last observation per bucket (rows are time-ordered)
        s = pd.Series(df["mid_price_v1"].to_numpy(), index=bucket)
        series[iid] = s.groupby(level=0).last()

    ids = sorted(series)
    common = None
    for iid in ids:
        idx = set(series[iid].index.tolist())
        common = idx if common is None else (common & idx)
    if not common:
        raise ValueError("no common bars across instruments")
    bar_ts = np.array(sorted(common), dtype=np.int64)
    mids = np.column_stack([series[iid].loc[bar_ts].to_numpy(dtype=np.float64) for iid in ids])
    if np.any(mids <= 0):
        raise ValueError("non-positive mid encountered in bar construction")
    rets = np.diff(np.log(mids), axis=0)
    return ids, bar_ts[1:], rets


def ewma_covariance(
    returns: np.ndarray,
    lam: float = 0.94,
    init_window: int = 20,
    ridge: float = 1e-6,
    shrinkage: str | None = None,
    shrink_intensity: float = 0.1,
) -> np.ndarray:
    """RiskMetrics EWMA covariance of a (T, N) return matrix (per-bar units).

    ``shrinkage=None`` (default) is the pinned estimator.  ``"ledoit_wolf"``
    applies :func:`shrink_constant_correlation` before the ridge;
    ``"ledoit_wolf_estimated"`` applies the estimated LW intensity toward
    ``mu * I`` (see the module docstring; ``shrink_intensity`` is ignored).
    """
    returns = np.asarray(returns, dtype=np.float64)
    if returns.ndim != 2:
        raise ValueError("returns must be 2-D (T, N)")
    T, n = returns.shape
    if not 0.0 < lam < 1.0:
        raise ValueError("lam must be in (0, 1)")
    if T < 2:
        raise ValueError("need at least 2 return rows")
    w0 = min(max(init_window, 2), T)
    r0 = returns[:w0]
    S = np.cov(r0, rowvar=False, ddof=0)
    S = np.atleast_2d(S)
    for t in range(w0, T):
        r = returns[t]
        S = lam * S + (1.0 - lam) * np.outer(r, r)
    S = 0.5 * (S + S.T)
    if shrinkage is not None:
        if shrinkage == "ledoit_wolf":
            S = shrink_constant_correlation(S, shrink_intensity)
        elif shrinkage == "ledoit_wolf_estimated":
            S = shrink_identity(S, estimated_lw_intensity(returns))
        else:
            raise ValueError("shrinkage must be None, 'ledoit_wolf' or 'ledoit_wolf_estimated'")
    S += np.eye(n) * (ridge * (np.trace(S) / n if np.trace(S) > 0 else 1.0))
    return S


def shrink_constant_correlation(S: np.ndarray, intensity: float) -> np.ndarray:
    """``(1 - d) S + d F`` with F the constant-correlation target.

    F keeps the diagonal of S and sets every off-diagonal to
    ``rbar * s_i * s_j`` with ``rbar`` the mean pairwise correlation.
    """
    if not 0.0 <= intensity <= 1.0:
        raise ValueError("shrink_intensity must be in [0, 1]")
    S = np.asarray(S, dtype=np.float64)
    n = S.shape[0]
    sd = np.sqrt(np.clip(np.diag(S), 0.0, None))
    if n < 2 or np.any(sd == 0):
        return S.copy()
    corr = S / np.outer(sd, sd)
    rbar = (corr.sum() - n) / (n * (n - 1))
    F = rbar * np.outer(sd, sd)
    np.fill_diagonal(F, np.diag(S))
    return (1.0 - intensity) * S + intensity * F


def condition_number(S: np.ndarray) -> float:
    """2-norm condition number ``lambda_max / lambda_min`` (inf if singular)."""
    ev = np.linalg.eigvalsh(np.asarray(S, dtype=np.float64))
    if ev[0] <= 0:
        return float("inf")
    return float(ev[-1] / ev[0])


def estimated_lw_intensity(returns: np.ndarray) -> float:
    """Ledoit-Wolf (2004) intensity estimated on the sample covariance of ``returns``."""
    return ledoit_wolf(np.asarray(returns, dtype=np.float64))[1]


def shrink_identity(S: np.ndarray, intensity: float) -> np.ndarray:
    """``(1 - d) S + d * mu * I`` with ``mu = trace(S) / n``."""
    if not 0.0 <= intensity <= 1.0:
        raise ValueError("intensity must be in [0, 1]")
    S = np.asarray(S, dtype=np.float64)
    n = S.shape[0]
    mu = float(np.trace(S)) / n
    return (1.0 - intensity) * S + intensity * mu * np.eye(n)

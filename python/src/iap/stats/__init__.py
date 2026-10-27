"""Pure statistics shared by the backtest and validation packages (no ``iap`` imports).

Moved, unchanged, from :mod:`iap.validation.metrics` (``ic`` and the pinned label horizons) and
:mod:`iap.validation.deflated` (``pnl_sharpe``) in v2.1 Track 3.  ``iap.validation`` builds on
``iap.backtest``; the adaptive and quoting backtests only needed these helpers, and importing them
from validation made the two packages import each other.  Both old names still re-export them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

EPS = 1e-12
NS_S = 1_000_000_000

#: pinned label horizons in nanoseconds (mirrors iap.labels.HORIZONS_NS)
HORIZONS_NS: dict[str, int] = {
    "10ms": 10_000_000,
    "50ms": 50_000_000,
    "100ms": 100_000_000,
    "500ms": 500_000_000,
    "1s": 1 * NS_S,
    "5s": 5 * NS_S,
    "10s": 10 * NS_S,
    "30s": 30 * NS_S,
    "1m": 60 * NS_S,
    "5m": 300 * NS_S,
    "15m": 900 * NS_S,
}
HORIZON_ORDER: tuple[str, ...] = tuple(HORIZONS_NS)


def _pairwise(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.shape != y.shape:
        raise ValueError("score/label length mismatch")
    ok = np.isfinite(x) & np.isfinite(y)
    return x[ok], y[ok]


def ic(scores: np.ndarray, labels: np.ndarray, min_obs: int = 32) -> float:
    """Pearson information coefficient (NaN with < min_obs pairs or a
    degenerate side)."""
    x, y = _pairwise(scores, labels)
    if x.size < min_obs or x.std() <= EPS or y.std() <= EPS:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


#: Session days per year for the annualised Sharpe beside the per-day one.
DAYS_PER_YEAR = 252


def pnl_sharpe(daily: Sequence[float], annualize: int | None = DAYS_PER_YEAR) -> float | None:
    """P&L Sharpe of a per-day P&L series (mean / sd, ddof=1), annualised by
    ``sqrt(annualize)`` (:data:`DAYS_PER_YEAR` by default; ``None`` = per
    day).  The single shared helper: a Sharpe of P&L, not of returns on
    capital.  None for < 2 days or zero spread."""
    x = np.asarray(daily, dtype=float)
    if x.size < 2:
        return None
    sd = x.std(ddof=1)
    if sd == 0:
        return None
    s = float(x.mean() / sd)
    return s * math.sqrt(annualize) if annualize else s

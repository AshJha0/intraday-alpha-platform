"""Calibrated uncertainty for sizing (v1.15 M-P1-2).

Conformal prediction intervals on alpha forecasts, a confidence-scaled sizing
rule and a calibration report.  Opt-in: :class:`ConformalSizingConfig` has
``enabled=False`` by default, and nothing in the engines or in
``configs/prereg/v2.json`` turns it on.

Two calibrators:

* :class:`SplitConformal` -- classic split conformal on absolute residuals.
  Finite-sample marginal coverage ``>= 1 - alpha`` *if* calibration and test
  scores are exchangeable.
* :class:`AdaptiveConformal` -- adaptive conformal inference (Gibbs & Candes,
  2021) over a rolling score window.  Intraday residuals are not exchangeable
  (volatility clustering, open/close regimes, shifts between sessions), so the
  miscoverage level is updated online,
  ``alpha_{t+1} = alpha_t + gamma * (alpha - err_t)``, which gives long-run
  coverage close to nominal under distribution shift.

Why Python only: the MVP engine's sizing (:mod:`iap.mvp.sizing`) is
parity-checked against the other engines byte for byte; a conformal multiplier
there would move the goldens in every language.  This module is a research-side
post-processor applied to target positions *before* the existing risk caps, so
the parity path is unchanged.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class ConformalSizingConfig:
    """Opt-in switch and parameters.  ``enabled`` is OFF by default."""

    enabled: bool = False
    alpha: float = 0.1  # nominal miscoverage; coverage target 1 - alpha
    method: str = "adaptive"  # "split" | "adaptive"
    gamma: float = 0.005  # ACI step size
    window: int = 500  # rolling score window for the adaptive variant
    ref_width: float | None = None  # width giving full confidence; None = calibration median


def _conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    n = len(scores)
    if n == 0:
        return math.inf
    k = math.ceil((n + 1) * (1.0 - alpha))
    if k > n:
        return math.inf
    if k < 1:
        return 0.0
    return float(np.partition(scores, k - 1)[k - 1])


class SplitConformal:
    """Split conformal intervals ``pred +/- q`` from absolute calibration residuals."""

    def __init__(self, alpha: float = 0.1) -> None:
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be in (0, 1)")
        self.alpha = alpha
        self.q: float | None = None

    def fit(self, y_cal: Sequence[float], pred_cal: Sequence[float]) -> SplitConformal:
        scores = np.abs(np.asarray(y_cal, float) - np.asarray(pred_cal, float))
        self.q = _conformal_quantile(scores, self.alpha)
        return self

    def interval(self, pred: float | np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.q is None:
            raise RuntimeError("fit() first")
        p = np.asarray(pred, float)
        return p - self.q, p + self.q


@dataclass
class AdaptiveConformal:
    """Online adaptive conformal inference over a rolling residual window.

    Call :meth:`interval` for the next forecast, then :meth:`update` with the
    realised value once it is known (no look-ahead).
    """

    alpha: float = 0.1
    gamma: float = 0.005
    window: int = 500
    alpha_t: float = field(init=False)
    _scores: deque = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha must be in (0, 1)")
        if self.gamma < 0 or self.window < 1:
            raise ValueError("gamma >= 0 and window >= 1 required")
        self.alpha_t = self.alpha
        self._scores = deque(maxlen=self.window)

    def warm_start(self, y_cal: Iterable[float], pred_cal: Iterable[float]) -> AdaptiveConformal:
        for y, p in zip(y_cal, pred_cal, strict=True):
            self._scores.append(abs(float(y) - float(p)))
        return self

    def half_width(self) -> float:
        if self.alpha_t >= 1.0:
            return 0.0
        return _conformal_quantile(np.fromiter(self._scores, float), max(self.alpha_t, 1e-9))

    def interval(self, pred: float) -> tuple[float, float]:
        q = self.half_width()
        return pred - q, pred + q

    def update(self, y: float, pred: float) -> bool:
        """Record the realised value; returns True if it was covered."""
        lo, hi = self.interval(pred)
        covered = lo <= y <= hi
        self.alpha_t += self.gamma * (self.alpha - (0.0 if covered else 1.0))
        self._scores.append(abs(float(y) - float(pred)))
        return covered


def confidence_from_width(width: float, ref_width: float) -> float:
    """Confidence in [0, 1]: 1 at or below ``ref_width``, ``ref/width`` above it."""
    if ref_width <= 0:
        raise ValueError("ref_width must be > 0")
    if not math.isfinite(width):
        return 0.0
    if width <= ref_width:
        return 1.0
    return ref_width / width


def conformal_size(
    target_qty: float,
    confidence: float,
    max_position: float,
    *,
    lot: int = 1,
) -> int:
    """Scale a base target position by confidence, then apply the existing cap.

    Absolute size is monotone non-decreasing in ``confidence``, never above
    ``|target_qty|`` and never above ``max_position``; the sign is preserved.
    Rounds toward zero to whole lots so rounding can never push past a cap.
    """
    if max_position < 0 or lot < 1:
        raise ValueError("max_position >= 0 and lot >= 1 required")
    c = min(max(float(confidence), 0.0), 1.0)
    mag = min(abs(target_qty) * c, max_position)
    lots = math.floor(mag / lot + 1e-12)
    return int(math.copysign(lots * lot, target_qty)) if lots else 0


def calibration_report(
    y: Sequence[float],
    lo: Sequence[float],
    hi: Sequence[float],
    sessions: Sequence[object],
    alpha: float,
) -> dict:
    """Empirical coverage vs nominal, overall and by session."""
    y_, lo_, hi_ = (np.asarray(v, float) for v in (y, lo, hi))
    sess = np.asarray(list(sessions), dtype=object)
    if not len(y_) == len(lo_) == len(hi_) == len(sess):
        raise ValueError("length mismatch")
    cov = (lo_ <= y_) & (y_ <= hi_)
    width = hi_ - lo_

    def _row(mask: np.ndarray) -> dict:
        n = int(mask.sum())
        c = float(cov[mask].mean()) if n else float("nan")
        w = width[mask]
        w = w[np.isfinite(w)]
        return {
            "n": n,
            "coverage": c,
            "nominal": 1.0 - alpha,
            "gap": c - (1.0 - alpha),
            "mean_width": float(w.mean()) if len(w) else float("inf"),
        }

    by_session = {str(s): _row(sess == s) for s in dict.fromkeys(sess.tolist())}
    return {"overall": _row(np.ones(len(y_), bool)), "by_session": by_session}

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

**v1.17 (WS-D item D3).**  Two wirings, both opt-in and both off in every
registered configuration:

* :func:`conformal_sizing_hook` -- a :data:`iap.backtest.engine.SizingHook`
  for the research backtester: the split-conformal interval over the
  primary signal's residuals is mapped to a size multiplier,
  ``confidence_from_width(width, ref_width)``.
* :class:`iap.mvp.sizing.SizingMixin` -- the MVP engine scales the
  portfolio target by the same rule when ``portfolio.conformal`` is present
  in ``mvp.json`` (absent = the pinned golden run, byte for byte).

:func:`coverage_by_regime` is the coverage gate: empirical coverage per
regime label against nominal, with the day-clustered t of the coverage
shortfall in the v1.17 gate-statistic form.  It is REPORTED, not enforced,
by default (``enforce=False``: ``passed`` is ``None``).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np

_LOG = logging.getLogger(__name__)


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
    """Split conformal intervals from calibration residual scores.

    ``score="absolute"`` (default): ``|y - pred|``, constant width ``pred +/- q``.
    ``score="normalized"`` (opt-in): ``|y - pred| / sigma(x)`` with a caller
    supplied per-row scale estimate ``sigma`` (e.g. rolling realised vol), so
    the interval ``pred +/- q * sigma(x)`` widens and narrows with the input.
    Coverage is still ``>= 1 - alpha`` under exchangeability of the scores.
    """

    def __init__(self, alpha: float = 0.1, score: str = "absolute", sigma_floor: float = 1e-12):
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha must be in (0, 1)")
        if score not in ("absolute", "normalized"):
            raise ValueError("score must be 'absolute' or 'normalized'")
        if not sigma_floor > 0:
            raise ValueError("sigma_floor must be > 0")
        self.alpha = alpha
        self.score = score
        self.sigma_floor = sigma_floor
        self.q: float | None = None

    def _sigma(self, sigma: object, like: np.ndarray) -> np.ndarray:
        if self.score == "absolute":
            return np.ones_like(like)
        if sigma is None:
            raise ValueError("score='normalized' requires sigma")
        s = np.broadcast_to(np.asarray(sigma, float), like.shape)
        if not np.all(np.isfinite(s)):
            raise ValueError("sigma must be finite")
        return np.maximum(s, self.sigma_floor)

    def fit(
        self,
        y_cal: Sequence[float],
        pred_cal: Sequence[float],
        sigma_cal: Sequence[float] | None = None,
    ) -> SplitConformal:
        resid = np.abs(np.asarray(y_cal, float) - np.asarray(pred_cal, float))
        sig = self._sigma(sigma_cal, resid)
        scores = resid / sig
        self.q = _conformal_quantile(scores, self.alpha)
        self.n_cal = int(resid.size)
        #: median interval width over the calibration rows (the default
        #: ``ref_width`` of :func:`conformal_sizing_hook`); inf when ``q`` is
        self.cal_median_width = float(np.median(2.0 * self.q * sig)) if resid.size else math.inf
        return self

    def interval(
        self, pred: float | np.ndarray, sigma: float | np.ndarray | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        if self.q is None:
            raise RuntimeError("fit() first")
        p = np.asarray(pred, float)
        half = self.q * self._sigma(sigma, p)
        return p - half, p + half


@dataclass
class AdaptiveConformal:
    """Online adaptive conformal inference over a rolling residual window.

    Call :meth:`interval` for the next forecast, then :meth:`update` with the
    realised value once it is known (no look-ahead).

    ``alpha_t`` is clamped to ``[alpha / 10, 1 - 1e-9]``.  Unclamped, a long
    miss streak drives ``alpha_t`` to or below zero (infinite width, zero
    confidence, zero size) and a long cover streak to ``>= 1`` (zero width).
    Every update that hits a clamp increments ``saturations`` and logs a
    warning, so saturation is visible rather than a silent size collapse.
    """

    alpha: float = 0.1
    gamma: float = 0.005
    window: int = 500
    alpha_t: float = field(init=False)
    saturations: int = field(init=False, default=0)
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

    @property
    def alpha_bounds(self) -> tuple[float, float]:
        return self.alpha / 10.0, 1.0 - 1e-9

    def update(self, y: float, pred: float) -> bool:
        """Record the realised value; returns True if it was covered."""
        lo, hi = self.interval(pred)
        covered = lo <= y <= hi
        raw = self.alpha_t + self.gamma * (self.alpha - (0.0 if covered else 1.0))
        lo_a, hi_a = self.alpha_bounds
        self.alpha_t = min(max(raw, lo_a), hi_a)
        if self.alpha_t != raw:
            self.saturations += 1
            _LOG.warning(
                "ACI alpha_t saturated at %.3g (raw %.3g); saturations=%d",
                self.alpha_t,
                raw,
                self.saturations,
            )
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


def conformal_sizing_hook(
    calibrator: SplitConformal,
    *,
    ref_width: float | None = None,
    sigma_column: str | None = None,
    missing: float = 0.0,
):
    """A :data:`iap.backtest.engine.SizingHook` from a fitted
    :class:`SplitConformal` (v1.17 D3, opt-in).

    The hook reads the row's ``expected_return`` as the point forecast,
    takes the interval ``calibrator.interval(pred[, sigma])`` -- ``sigma``
    from the scores-frame column ``sigma_column`` when the calibrator uses
    the normalised score -- and returns
    :func:`confidence_from_width` ``(hi - lo, ref_width)``: full size at or
    below ``ref_width`` (default: the calibrator's median calibration
    width), shrinking as ``ref_width / width`` above it, 0 for an infinite
    width.  A missing or non-finite ``sigma`` gives ``missing`` (default 0,
    no trade).  The residuals the calibrator was fitted on must come from
    rows disjoint from the rows backtested (the caller's split; see
    :func:`coverage_by_regime` for the check).
    """
    if calibrator.q is None:
        raise RuntimeError("fit() the calibrator first")
    if calibrator.score == "normalized" and sigma_column is None:
        raise ValueError("a normalized-score calibrator needs sigma_column")
    if not 0.0 <= missing <= 1.0:
        raise ValueError("missing must be in [0, 1]")
    ref = calibrator.cal_median_width if ref_width is None else float(ref_width)
    if not (math.isfinite(ref) and ref > 0.0):
        raise ValueError(f"ref_width must be finite and > 0 (got {ref}); pass ref_width")

    def hook(row) -> float:
        sigma = None
        if sigma_column is not None:
            sigma = row.scores.get(sigma_column)
            if sigma is None or not np.isfinite(sigma):
                return missing
        lo, hi = calibrator.interval(row.expected_return, sigma)
        return confidence_from_width(float(hi - lo), ref)

    hook.__name__ = f"conformal_sizing_hook[alpha={calibrator.alpha},ref_width={ref:.3g}]"
    return hook


#: Default day-cluster t at which :func:`coverage_by_regime` flags a regime
#: as under-covered (one-sided; the gate form of ``iap.validation``).
COVERAGE_GATE_T = 2.0


def _coverage_cell(
    covered: np.ndarray, ts: np.ndarray, nominal: float, day_ns: int, threshold: float
) -> dict:
    """One regime's coverage row in the v1.17 gate-statistic form.

    The statistic is the day-clustered t of the mean coverage shortfall
    ``e_i = 1{covered_i} - nominal``: ``t = mean(e) / se`` with
    ``se^2 = G/(G-1) * sum_d s_d^2 / n^2`` over the ``G`` days, ``s_d`` the
    within-day sum of ``e_i - mean(e)`` (the same cluster rule as
    :func:`iap.validation.metrics.day_cluster_tstat`).  NaN below two days.
    """
    from iap.validation.metrics import day_cluster_inference

    n = int(covered.size)
    nan = float("nan")
    if n == 0:
        return {
            "n_pairs": 0,
            "n_days": 0,
            "coverage": nan,
            "nominal": nominal,
            "gap": nan,
            "tstat_day_cluster": nan,
            "under_coverage": False,
        }
    cov = float(covered.mean())
    e = covered.astype(float) - nominal
    _, inv = np.unique(ts // int(day_ns), return_inverse=True)
    g = int(inv.max()) + 1
    t = nan
    if g >= 2:
        s_d = np.bincount(inv, weights=e - e.mean(), minlength=g)
        meat = float(np.sum(s_d * s_d)) * g / (g - 1.0)
        if meat > 0.0:
            t = float(e.mean() * n / math.sqrt(meat))
    row = {
        "n_pairs": n,
        "n_days": g,
        "coverage": cov,
        "nominal": nominal,
        "gap": cov - nominal,
        "tstat_day_cluster": t,
        "under_coverage": bool(np.isfinite(t) and t < -threshold),
    }
    row.update({f"inference_{k}": v for k, v in day_cluster_inference(t, g).items()})
    return row


def coverage_by_regime(
    ts: Sequence[int],
    y: Sequence[float],
    lo: Sequence[float],
    hi: Sequence[float],
    regime: Sequence[object],
    alpha: float,
    *,
    threshold: float = COVERAGE_GATE_T,
    enforce: bool = False,
    day_ns: int = 86_400 * 1_000_000_000,
    ledger=None,
    alpha_id: str = "CONFORMAL",
    config: dict | None = None,
) -> dict:
    """Coverage-by-regime gate (v1.17 D3): empirical coverage of the
    intervals ``[lo, hi]`` per ``regime`` label against the nominal
    ``1 - alpha``, each with the day-clustered t of the shortfall in the
    v1.17 gate-statistic form (:func:`_coverage_cell`).

    A regime is ``under_coverage`` when its t is below ``-threshold``.  The
    gate is REPORTED by default: ``enforced`` is ``False`` and ``passed`` is
    ``None``; with ``enforce=True`` ``passed`` is ``True`` iff no regime is
    under-covered.  Nothing in the platform calls it with ``enforce=True``.
    ``ledger`` (an :class:`iap.validation.ledger.ExperimentLedger`) records
    the gate read as ONE look of kind ``"conformal_coverage_gate"`` whose
    identity is ``alpha_id``, ``alpha``, ``threshold``, ``enforce`` and the
    caller's ``config``; the caller saves the ledger.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if threshold < 0.0:
        raise ValueError("threshold must be >= 0")
    ts_ = np.asarray(ts, dtype=np.int64)
    y_, lo_, hi_ = (np.asarray(v, float) for v in (y, lo, hi))
    reg = np.asarray(list(regime), dtype=object)
    if not len(ts_) == len(y_) == len(lo_) == len(hi_) == len(reg):
        raise ValueError("length mismatch")
    nominal = 1.0 - alpha
    covered = (lo_ <= y_) & (y_ <= hi_)
    by_regime = {
        str(r): _coverage_cell(covered[reg == r], ts_[reg == r], nominal, day_ns, threshold)
        for r in dict.fromkeys(reg.tolist())
    }
    under = sorted(k for k, v in by_regime.items() if v["under_coverage"])
    out = {
        "nominal": nominal,
        "threshold_t": float(threshold),
        "statistic": "day-cluster t of mean(1{covered} - nominal)",
        "overall": _coverage_cell(covered, ts_, nominal, day_ns, threshold),
        "by_regime": by_regime,
        "under_covered_regimes": under,
        "enforced": bool(enforce),
        "passed": (not under) if enforce else None,
    }
    if ledger is not None:
        out["ledger_n"] = ledger.record(
            alpha_id,
            "conformal_coverage_gate",
            config={
                "alpha": float(alpha),
                "threshold_t": float(threshold),
                "enforce": bool(enforce),
                **(config or {}),
            },
            result={
                "overall": out["overall"],
                "under_covered_regimes": under,
                "passed": out["passed"],
                "regimes": {k: v["coverage"] for k, v in by_regime.items()},
            },
        )
    return out


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

"""AI4 baseline comparison and net-of-cost acceptance (v1.15, M-P0-1).

Code only: synthetic tests, no real-data run. The DeepLOB model must beat
simple baselines on the *same* features and the *same* chronological split
before it counts for anything, and it must pay its costs:

- Features: the flattened, train-normalised windows from
  :func:`iap.models.deeplob.build_lob_dataset` -- every model sees exactly
  the same ``X_train`` / ``X_test`` arrays.
- Baselines: ``linear`` (multinomial logistic regression, sklearn) and
  ``gbm`` (``HistGradientBoostingClassifier``, sklearn). Both are core
  dependencies, so the harness runs in CI without the ``deep`` extra.
- Net-of-cost metric (pinned): the predicted class maps to a direction
  (UP +1, DOWN -1, FLAT 0); a trade is taken only when the winning class
  probability is at least ``min_confidence``. A taker enters at the decision
  row ``t`` and exits at ``t + horizon`` at the point mid; it pays half the
  quoted spread at each end plus ``fee_bp`` per round trip::

      net_bp = dir * (mid[t+h] / mid[t] - 1) * 1e4
               - 0.5 * (spread[t] / mid[t] + spread[t+h] / mid[t+h]) * 1e4
               - fee_bp

  Overlapping holding periods are thinned to one trade per ``horizon`` rows
  (the first eligible trade, then the next at least ``horizon`` rows later)
  so the trades used for inference are non-overlapping.
- Acceptance (pinned): mean net bp per trade > 0, a one-sided lower 95%
  bound (normal approximation on the non-overlapping trades) > 0, at least
  ``min_trades`` trades, and the candidate's mean net bp strictly above every
  baseline's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression

from iap.models.deeplob import DOWN, N_CLASSES, UP, mid_price

Z_95_ONE_SIDED = 1.6448536269514722


class FlatSklearnClassifier:
    """Wrap an sklearn classifier so it takes ``(N, W, F)`` windows and always
    returns probabilities over all three classes."""

    def __init__(self, estimator: Any) -> None:
        self.estimator = estimator

    @staticmethod
    def _flat(x: np.ndarray) -> np.ndarray:
        return x.reshape(x.shape[0], -1)

    def fit(self, x: np.ndarray, y: np.ndarray) -> FlatSklearnClassifier:
        self.estimator.fit(self._flat(x), y)
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        p = self.estimator.predict_proba(self._flat(x))
        out = np.zeros((x.shape[0], N_CLASSES))
        out[:, np.asarray(self.estimator.classes_, dtype=np.int64)] = p
        return out

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.predict_proba(x).argmax(axis=1)


def linear_baseline(seed: int = 0) -> FlatSklearnClassifier:
    return FlatSklearnClassifier(LogisticRegression(C=1.0, max_iter=2000, random_state=seed))


def gbm_baseline(seed: int = 0) -> FlatSklearnClassifier:
    return FlatSklearnClassifier(
        HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, random_state=seed)
    )


#: Baselines every AI4 candidate is compared against (pinned names).
BASELINES: dict[str, Callable[[int], Any]] = {"linear": linear_baseline, "gbm": gbm_baseline}


@dataclass(frozen=True)
class NetCostResult:
    n_signals: int
    n_trades: int
    mean_net_bp: float
    std_net_bp: float
    lower95_bp: float
    gross_bp: float
    cost_bp: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def spread(snapshots: np.ndarray) -> np.ndarray:
    """Level-1 quoted spread from a ``(T, 4L)`` snapshot matrix."""
    return snapshots[:, 0] - snapshots[:, 2]


def directions(proba: np.ndarray, min_confidence: float = 0.0) -> np.ndarray:
    """-1/0/+1 per row from class probabilities (FLAT or low confidence = 0)."""
    cls = proba.argmax(axis=1)
    d = np.where(cls == UP, 1, np.where(cls == DOWN, -1, 0))
    return np.where(proba.max(axis=1) >= min_confidence, d, 0).astype(np.int64)


def _non_overlapping(idx: np.ndarray, horizon: int) -> np.ndarray:
    keep: list[int] = []
    nxt = -np.inf
    for k, t in enumerate(idx):
        if t >= nxt:
            keep.append(k)
            nxt = t + horizon
    return np.asarray(keep, dtype=np.int64)


def net_of_cost(
    snapshots: np.ndarray,
    end_idx: np.ndarray,
    direction: np.ndarray,
    horizon: int,
    *,
    fee_bp: float = 0.0,
) -> NetCostResult:
    """Taker P&L in bp per non-overlapping trade (see module docstring)."""
    end_idx = np.asarray(end_idx, dtype=np.int64)
    direction = np.asarray(direction, dtype=np.int64)
    if end_idx.shape != direction.shape:
        raise ValueError("end_idx and direction must have identical shapes")
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    if end_idx.size and end_idx.max() + horizon >= snapshots.shape[0]:
        raise ValueError("an exit row t + horizon runs past the end of the snapshots")
    mid = mid_price(snapshots)
    sprd = spread(snapshots)
    sig = np.flatnonzero(direction != 0)
    trades = sig[_non_overlapping(end_idx[sig], horizon)]
    if trades.size == 0:
        return NetCostResult(int(sig.size), 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    t = end_idx[trades]
    gross = direction[trades] * (mid[t + horizon] / mid[t] - 1.0) * 1e4
    cost = 0.5 * (sprd[t] / mid[t] + sprd[t + horizon] / mid[t + horizon]) * 1e4 + fee_bp
    net = gross - cost
    n = net.size
    sd = float(net.std(ddof=1)) if n > 1 else 0.0
    mean = float(net.mean())
    lower = mean - Z_95_ONE_SIDED * sd / np.sqrt(n) if n > 1 else -np.inf
    return NetCostResult(
        n_signals=int(sig.size),
        n_trades=int(n),
        mean_net_bp=mean,
        std_net_bp=sd,
        lower95_bp=float(lower),
        gross_bp=float(gross.mean()),
        cost_bp=float(cost.mean()),
    )


def compare_models(
    snapshots: np.ndarray,
    dataset: Mapping[str, np.ndarray],
    models: Mapping[str, Any],
    *,
    horizon: int,
    fee_bp: float = 0.0,
    min_confidence: float = 0.0,
) -> dict[str, dict[str, Any]]:
    """Fit every model on ``X_train`` and score it on ``X_test`` (same arrays
    for all). Returns per-model accuracy, macro-F1 and the net-of-cost result."""
    out: dict[str, dict[str, Any]] = {}
    y_test = dataset["y_test"]
    for name, model in models.items():
        model.fit(dataset["X_train"], dataset["y_train"])
        proba = model.predict_proba(dataset["X_test"])
        pred = proba.argmax(axis=1)
        d = directions(proba, min_confidence)
        res = net_of_cost(snapshots, dataset["end_test"], d, horizon, fee_bp=fee_bp)
        out[name] = {
            "accuracy": float((pred == y_test).mean()) if y_test.size else 0.0,
            "macro_f1": macro_f1(y_test, pred),
            "net": res.to_dict(),
        }
    return out


def macro_f1(y: np.ndarray, pred: np.ndarray) -> float:
    f1s = []
    for c in range(N_CLASSES):
        tp = float(np.sum((pred == c) & (y == c)))
        fp = float(np.sum((pred == c) & (y != c)))
        fn = float(np.sum((pred != c) & (y == c)))
        denom = 2 * tp + fp + fn
        f1s.append(2 * tp / denom if denom > 0 else 0.0)
    return float(np.mean(f1s))


def accept(
    results: Mapping[str, Mapping[str, Any]],
    candidate: str,
    *,
    baselines: tuple[str, ...] = tuple(BASELINES),
    min_trades: int = 30,
) -> dict[str, Any]:
    """Pinned net-of-cost acceptance for ``candidate`` against ``baselines``."""
    c = results[candidate]["net"]
    best_base = max(results[b]["net"]["mean_net_bp"] for b in baselines)
    checks = {
        "mean_net_positive": c["mean_net_bp"] > 0.0,
        "lower95_positive": c["lower95_bp"] > 0.0,
        "enough_trades": c["n_trades"] >= min_trades,
        "beats_baselines": c["mean_net_bp"] > best_base,
    }
    return {
        "candidate": candidate,
        "accepted": all(checks.values()),
        "checks": checks,
        "best_baseline_mean_net_bp": best_base,
    }

"""Deep limit-order-book models over L2 snapshots (v1.15, AI4).

Code only: no training run, no real data. Two model families share one
dataset path:

- :class:`NumpyLOBSoftmax`: a multinomial logistic regression on the
  flattened snapshot window, pure numpy, deterministic under a seed. It is
  the CI-safe baseline and always available.
- :class:`iap.models.deeplob_torch.DeepLOBClassifier`: the DeepLOB
  architecture (Zhang, Zohren and Roberts, 2019): conv blocks over
  (price, size) pairs and levels, an inception module, then an LSTM; plus an
  MLP baseline. Both need the optional ``deep`` extra (torch); probe with
  :func:`torch_available`.

Dataset conventions (pinned):

- A snapshot row is ``[ask_px_1, ask_sz_1, bid_px_1, bid_sz_1, ...,
  ask_px_L, ask_sz_L, bid_px_L, bid_sz_L]`` (the DeepLOB layout), shape
  ``(T, 4L)``.
- Normalisation statistics come from the training rows only
  (:func:`fit_normalizer`).
- A window ending at row ``t`` covers rows ``t-W+1 .. t`` (inclusive).
- The label at ``t`` compares the mean mid over ``t+1 .. t+h`` with the mid
  at ``t`` (the DeepLOB smoothed label). Rows whose horizon runs past the end
  are dropped, so the label is strictly forward-looking and the features
  strictly not.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

import numpy as np

#: Class ids for the three-way move label.
DOWN, FLAT, UP = 0, 1, 2
N_CLASSES = 3


def torch_available() -> bool:
    """True when the optional ``deep`` extra (torch) is importable."""
    return importlib.util.find_spec("torch") is not None


def snapshot_matrix(
    bid_px: np.ndarray, bid_sz: np.ndarray, ask_px: np.ndarray, ask_sz: np.ndarray
) -> np.ndarray:
    """Stack ``(T, L)`` level arrays into the ``(T, 4L)`` DeepLOB layout."""
    arrs = [np.asarray(a, dtype=np.float64) for a in (ask_px, ask_sz, bid_px, bid_sz)]
    shape = arrs[0].shape
    if len(shape) != 2 or any(a.shape != shape for a in arrs):
        raise ValueError("bid/ask price/size arrays must share one (T, L) shape")
    t, levels = shape
    out = np.empty((t, 4 * levels), dtype=np.float64)
    for k, a in enumerate(arrs):
        out[:, k::4] = a
    return out


def mid_price(snapshots: np.ndarray) -> np.ndarray:
    """Level-1 mid from a ``(T, 4L)`` snapshot matrix."""
    return 0.5 * (snapshots[:, 0] + snapshots[:, 2])


@dataclass(frozen=True)
class Normalizer:
    mean: np.ndarray
    std: np.ndarray

    def transform(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.std


def fit_normalizer(train_rows: np.ndarray) -> Normalizer:
    """Per-column z-score fitted on training rows only."""
    mean = train_rows.mean(axis=0)
    std = train_rows.std(axis=0)
    return Normalizer(mean=mean, std=np.where(std > 0, std, 1.0))


def smoothed_labels(mid: np.ndarray, horizon: int, threshold: float) -> np.ndarray:
    """Three-way label per row; ``-1`` where the horizon runs past the end.

    ``m_plus = mean(mid[t+1 : t+1+horizon])``; ``r = m_plus / mid[t] - 1``;
    UP if ``r > threshold``, DOWN if ``r < -threshold``, else FLAT.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    mid = np.asarray(mid, dtype=np.float64)
    n = mid.shape[0]
    out = np.full(n, -1, dtype=np.int64)
    if n <= horizon:
        return out
    csum = np.concatenate([[0.0], np.cumsum(mid)])
    t = np.arange(n - horizon)
    m_plus = (csum[t + 1 + horizon] - csum[t + 1]) / horizon
    r = m_plus / mid[t] - 1.0
    out[t] = np.where(r > threshold, UP, np.where(r < -threshold, DOWN, FLAT))
    return out


def make_windows(
    features: np.ndarray, labels: np.ndarray, window: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(X, y, end_idx)``: windows ``(N, window, F)`` ending at a labelled row.

    Only rows ``t >= window-1`` with a valid label are kept; ``end_idx`` is
    each window's last row (the decision time).
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    ends = np.arange(window - 1, features.shape[0])
    ends = ends[labels[ends] >= 0]
    if ends.size == 0:
        return np.empty((0, window, features.shape[1])), np.empty(0, dtype=np.int64), ends
    view = np.lib.stride_tricks.sliding_window_view(features, window, axis=0)
    x = np.ascontiguousarray(view[ends - (window - 1)].transpose(0, 2, 1))
    return x, labels[ends], ends


def build_lob_dataset(
    snapshots: np.ndarray,
    *,
    window: int,
    horizon: int,
    threshold: float,
    train_frac: float = 0.7,
) -> dict[str, np.ndarray]:
    """Chronological train/test windows with train-only normalisation.

    Rows before ``cut`` are train. A train window is kept only if its label
    horizon ends before ``cut``; a test window only if its first row is at or
    after ``cut``. So no train label sees a test row, and no test feature
    sees a train-period statistic other than the normaliser.
    """
    mid = mid_price(snapshots)
    labels = smoothed_labels(mid, horizon, threshold)
    cut = int(snapshots.shape[0] * train_frac)
    norm = fit_normalizer(snapshots[:cut])
    x, y, ends = make_windows(norm.transform(snapshots), labels, window)
    train = ends + horizon < cut
    test = ends - (window - 1) >= cut
    return {
        "X_train": x[train],
        "y_train": y[train],
        "X_test": x[test],
        "y_test": y[test],
        "end_train": ends[train],
        "end_test": ends[test],
    }


class NumpyLOBSoftmax:
    """Multinomial logistic regression on flattened windows (numpy only).

    Full-batch gradient descent with L2; deterministic for a given seed.
    """

    def __init__(
        self, *, lr: float = 0.5, epochs: int = 300, l2: float = 1e-4, seed: int = 0
    ) -> None:
        self.lr = lr
        self.epochs = epochs
        self.l2 = l2
        self.seed = seed
        self.W: np.ndarray | None = None
        self.b: np.ndarray | None = None

    @staticmethod
    def _flat(x: np.ndarray) -> np.ndarray:
        return x.reshape(x.shape[0], -1)

    @staticmethod
    def _softmax(z: np.ndarray) -> np.ndarray:
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def fit(self, x: np.ndarray, y: np.ndarray) -> NumpyLOBSoftmax:
        xf = self._flat(x)
        n, d = xf.shape
        rng = np.random.default_rng(self.seed)
        w = rng.normal(0.0, 0.01, size=(d, N_CLASSES))
        b = np.zeros(N_CLASSES)
        onehot = np.eye(N_CLASSES)[y]
        for _ in range(self.epochs):
            g = (self._softmax(xf @ w + b) - onehot) / n
            w -= self.lr * (xf.T @ g + self.l2 * w)
            b -= self.lr * g.sum(axis=0)
        self.W, self.b = w, b
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if self.W is None or self.b is None:
            raise RuntimeError("fit first")
        return self._softmax(self._flat(x) @ self.W + self.b)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.predict_proba(x).argmax(axis=1)


def synthetic_lob(n: int, levels: int = 10, *, seed: int = 0, tick: float = 0.01) -> np.ndarray:
    """A synthetic ``(n, 4*levels)`` L2 book for tests: random-walk mid with a
    one-tick spread, and level sizes driven by a persistent imbalance that
    leads the next mid move."""
    rng = np.random.default_rng(seed)
    imb = np.zeros(n)
    for t in range(1, n):
        imb[t] = 0.9 * imb[t - 1] + rng.normal(0, 0.3)
    steps = np.sign(np.concatenate([[0.0], imb[:-1]])) * (rng.random(n) < 0.6)
    mid = 100.0 + tick * np.cumsum(steps)
    lv = np.arange(levels)
    ask_px = mid[:, None] + tick * (0.5 + lv)
    bid_px = mid[:, None] - tick * (0.5 + lv)
    base = rng.uniform(180, 220, size=(n, levels))
    bid_sz = base * np.exp(imb)[:, None]
    ask_sz = base * np.exp(-imb)[:, None]
    return snapshot_matrix(bid_px, bid_sz, ask_px, ask_sz)

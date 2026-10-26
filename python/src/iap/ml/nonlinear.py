"""M2 and M3 models (ML protocol sections 4-6): a penalised cubic-spline GAM and bounded gradient-boosted trees.

Both are written in numpy against the protocol's text rather than wrapped from a
library, because the protocol fixes details no library exposes together
(scikit-learn's ``HistGradientBoostingRegressor`` has no row subsample and its
early stopping cannot be pointed at the validation partition every 10 trees;
``GradientBoostingRegressor`` has no leaf L2).  Both implement the research
loop's :class:`~iap.research.loop.Forecaster` protocol exactly as the M1 models
do, so the engine runs them unchanged.

- :class:`GAMModel` (``M2.gam``): per feature, standardise on the development
  window, clip to the development range, cubic truncated-power basis
  ``z, z^2, z^3, (z - k_j)^3_+`` with 5 knots at the development quantiles
  1/6 .. 5/6; every basis column is scaled to unit development sd; the knot
  columns carry the penalty ``lambda`` (the polynomial part is unpenalised).
  The penalty is on the same scale as M1 ridge's ``alpha`` (unnormalised normal
  equations).  Complexity = effective degrees of freedom
  ``trace((B'B + lambda P)^-1 B'B)`` (intercept excluded, as for ridge).
- :class:`GBTModel` (``M3.gbt``): squared loss on the label in bps; features
  binned at up to 64 development quantiles; each tree grown best-first to at
  most ``max_leaves`` leaves with at least ``min_samples_leaf`` rows per leaf;
  leaf value ``learning_rate * sum(residual) / (n + l2_leaf)``; rows subsampled
  at 0.8 per tree by a counter-based SplitMix64 stream (bit-identical to
  :class:`iap.core.rng.SplitMix64`, vectorised).  Early stopping (section 6):
  validation MSE every 10 trees, stop once the best loss has not improved by
  the relative 1e-4 for 50 trees, keep the trees up to the best evaluation.
  The number of early-stopping evaluations is recorded (``evaluations``): it
  enters the tier's Holm K.  Complexity = ``max_leaves x kept trees``.

No wall clock, no unseeded randomness: the same inputs give the same
``fit_hash``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from iap.contracts.versions import content_hash
from iap.ml.models import design

__all__ = ["GAMModel", "GBTModel", "splitmix_uniforms"]

_GAMMA = np.uint64(0x9E3779B97F4A7C15)
_MIX1 = np.uint64(0xBF58476D1CE4E5B9)
_MIX2 = np.uint64(0x94D049BB133111EB)
N_KNOTS = 5
N_BINS = 64


def splitmix_uniforms(seed: int, start: int, n: int) -> np.ndarray:
    """Draws ``start .. start + n - 1`` of ``SplitMix64(seed).uniform()``, computed without the loop."""
    with np.errstate(over="ignore"):
        k = np.arange(start + 1, start + n + 1, dtype=np.uint64)
        z = np.uint64(seed & ((1 << 64) - 1)) + k * _GAMMA
        z = (z ^ (z >> np.uint64(30))) * _MIX1
        z = (z ^ (z >> np.uint64(27))) * _MIX2
        z = z ^ (z >> np.uint64(31))
    return (z >> np.uint64(11)).astype(np.float64) * 2.0**-53


def _clean(x: np.ndarray, y: np.ndarray, min_rows: int) -> tuple[np.ndarray, np.ndarray]:
    ok = np.all(np.isfinite(x), axis=1) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.shape[0] < min_rows:
        raise ValueError(f"too few rows to fit ({x.shape[0]})")
    return x, y


def _round(v: float) -> float:
    return float(np.round(v, 12))


# ---------------------------------------------------------------------------- M2


@dataclass(slots=True)
class GAMModel:
    """``M2.gam``: additive penalised cubic regression splines (module docstring)."""

    features: tuple[str, ...]
    lam: float
    n_knots: int = N_KNOTS
    mu: np.ndarray = field(default_factory=lambda: np.zeros(0))
    sd: np.ndarray = field(default_factory=lambda: np.ones(0))
    lo: np.ndarray = field(default_factory=lambda: np.zeros(0))
    hi: np.ndarray = field(default_factory=lambda: np.zeros(0))
    knots: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))
    col_mu: np.ndarray = field(default_factory=lambda: np.zeros(0))
    col_sd: np.ndarray = field(default_factory=lambda: np.ones(0))
    coef: np.ndarray = field(default_factory=lambda: np.zeros(0))
    intercept: float = 0.0
    edf: float = 0.0
    resid_sd_bps: float = 0.0
    n: int = 0
    fitted: bool = False
    registry_id: str = "0" * 64
    model_class: str = "M2.gam"

    def _basis(self, x: np.ndarray) -> np.ndarray:
        z = np.clip((x - self.mu) / self.sd, self.lo, self.hi)
        cols = []
        for j in range(z.shape[1]):
            zj = z[:, j]
            cols += [zj, zj**2, zj**3]
            cols += [np.maximum(zj - k, 0.0) ** 3 for k in self.knots[j]]
        return np.column_stack(cols)

    def fit(self, x: np.ndarray, y: np.ndarray) -> GAMModel:
        per = 3 + self.n_knots
        x, y = _clean(x, y, max(30, 5 * per * x.shape[1]))
        self.n = int(x.shape[0])
        self.mu = x.mean(axis=0)
        sd = x.std(axis=0, ddof=1)
        self.sd = np.where(sd > 0, sd, 1.0)
        z = (x - self.mu) / self.sd
        self.lo, self.hi = z.min(axis=0), z.max(axis=0)
        qs = np.arange(1, self.n_knots + 1) / (self.n_knots + 1)
        self.knots = np.quantile(z, qs, axis=0).T  # (features, knots)
        b = self._basis(x)
        self.col_mu = b.mean(axis=0)
        csd = b.std(axis=0, ddof=1)
        self.col_sd = np.where(csd > 0, csd, 1.0)
        bs = (b - self.col_mu) / self.col_sd
        self.intercept = float(y.mean())
        yc = y - self.intercept
        pen = np.tile(np.r_[np.zeros(3), np.ones(self.n_knots)], x.shape[1])
        gram = bs.T @ bs
        a = gram + self.lam * np.diag(pen) + 1e-9 * np.eye(gram.shape[0])
        self.coef = np.linalg.solve(a, bs.T @ yc)
        self.edf = float(np.trace(np.linalg.solve(a, gram)))
        resid = yc - bs @ self.coef
        self.resid_sd_bps = float(np.sqrt((resid @ resid) / max(self.n - 1, 1)))
        self.fitted = True
        return self

    def predict(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        x = design(rows, self.features)
        if not self.fitted:
            return np.full(x.shape[0], np.nan), np.full(x.shape[0], np.nan)
        ok = np.all(np.isfinite(x), axis=1)
        mu = np.full(x.shape[0], np.nan)
        if ok.any():
            bs = (self._basis(x[ok]) - self.col_mu) / self.col_sd
            mu[ok] = self.intercept + bs @ self.coef
        return mu, np.where(ok, self.resid_sd_bps, np.nan)

    @property
    def complexity(self) -> float:
        return float(self.edf)

    def parameters(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "lambda": self.lam,
            "n_knots": self.n_knots,
            "feature_mean": [_round(v) for v in self.mu],
            "feature_sd": [_round(v) for v in self.sd],
            "clip_lo": [_round(v) for v in self.lo],
            "clip_hi": [_round(v) for v in self.hi],
            "knots": [[_round(v) for v in row] for row in self.knots],
            "basis_mean": [_round(v) for v in self.col_mu],
            "basis_sd": [_round(v) for v in self.col_sd],
            "coef": [_round(v) for v in self.coef],
            "intercept_bps": _round(self.intercept),
            "resid_sd_bps": _round(self.resid_sd_bps),
            "n": self.n,
            "complexity": _round(self.complexity),
        }

    def fit_hash(self) -> str:
        return content_hash({"class": self.model_class, **self.parameters()})


# ---------------------------------------------------------------------------- M3


def _bin_edges(x: np.ndarray, n_bins: int) -> list[np.ndarray]:
    edges = []
    for j in range(x.shape[1]):
        e = np.unique(np.quantile(x[:, j], np.arange(1, n_bins) / n_bins))
        edges.append(e)
    return edges


def _binned(x: np.ndarray, edges: list[np.ndarray]) -> np.ndarray:
    out = np.empty(x.shape, dtype=np.int16)
    for j, e in enumerate(edges):
        out[:, j] = np.searchsorted(e, x[:, j], side="right")
    return out


@dataclass(slots=True)
class _Tree:
    # flat arrays: feature (-1 = leaf), bin threshold (go left if bin <= threshold), children, value
    feature: list[int]
    threshold: list[int]
    left: list[int]
    right: list[int]
    value: list[float]

    def leaves(self) -> int:
        return sum(1 for f in self.feature if f < 0)

    def apply(self, xb: np.ndarray) -> np.ndarray:
        out = np.empty(xb.shape[0])
        stack = [(0, np.arange(xb.shape[0]))]
        while stack:
            node, idx = stack.pop()
            f = self.feature[node]
            if f < 0:
                out[idx] = self.value[node]
                continue
            go_left = xb[idx, f] <= self.threshold[node]
            stack.append((self.left[node], idx[go_left]))
            stack.append((self.right[node], idx[~go_left]))
        return out


def _best_split(
    xb: np.ndarray, r: np.ndarray, idx: np.ndarray, n_bins: int, min_leaf: int, l2: float
) -> tuple[float, int, int]:
    """(gain, feature, bin threshold) of the best split of the rows ``idx``; gain 0 when none is allowed."""
    rs, n = r[idx], idx.size
    total_g = rs.sum()
    parent = total_g * total_g / (n + l2)
    best = (0.0, -1, -1)
    for j in range(xb.shape[1]):
        b = xb[idx, j]
        g = np.cumsum(np.bincount(b, weights=rs, minlength=n_bins + 1))
        c = np.cumsum(np.bincount(b, minlength=n_bins + 1))
        ok = (c >= min_leaf) & ((n - c) >= min_leaf)
        if not ok.any():
            continue
        gl, cl = g[ok], c[ok]
        gain = gl * gl / (cl + l2) + (total_g - gl) ** 2 / (n - cl + l2) - parent
        k = int(np.argmax(gain))
        if gain[k] > best[0] + 1e-12:
            best = (float(gain[k]), j, int(np.flatnonzero(ok)[k]))
    return best


def _grow(
    xb: np.ndarray,
    r: np.ndarray,
    idx: np.ndarray,
    *,
    max_leaves: int,
    min_leaf: int,
    l2: float,
    lr: float,
    n_bins: int,
) -> _Tree:
    t = _Tree([-1], [-1], [-1], [-1], [0.0])
    rows = {0: idx}
    frontier: dict[int, tuple[float, int, int]] = {0: _best_split(xb, r, idx, n_bins, min_leaf, l2)}
    leaves = 1
    while leaves < max_leaves:
        # best-first; ties go to the lowest node id (deterministic)
        cand = [(g, -node) for node, (g, _f, _b) in frontier.items() if g > 0]
        if not cand:
            break
        _g, neg = max(cand)
        node = -neg
        _gain, f, thr = frontier.pop(node)
        sub = rows.pop(node)
        go_left = xb[sub, f] <= thr
        t.feature[node], t.threshold[node] = f, thr
        for side, part in (("left", sub[go_left]), ("right", sub[~go_left])):
            child = len(t.feature)
            t.feature.append(-1)
            t.threshold.append(-1)
            t.left.append(-1)
            t.right.append(-1)
            t.value.append(0.0)
            getattr(t, side)[node] = child
            rows[child] = part
            frontier[child] = _best_split(xb, r, part, n_bins, min_leaf, l2)
        leaves += 1
    for node, part in rows.items():
        t.value[node] = float(lr * r[part].sum() / (part.size + l2))
    return t


@dataclass(slots=True)
class GBTModel:
    """``M3.gbt``: bounded gradient-boosted trees with validation early stopping (module docstring)."""

    features: tuple[str, ...]
    max_leaves: int
    learning_rate: float
    l2_leaf: float
    max_trees: int = 400
    min_samples_leaf: int = 200
    subsample: float = 0.8
    seed: int = 20261025
    evaluate_every: int = 10
    patience: int = 50
    min_rel_improvement: float = 1e-4
    n_bins: int = N_BINS
    edges: list[np.ndarray] = field(default_factory=list)
    trees: list[_Tree] = field(default_factory=list)
    base: float = 0.0
    evaluations: int = 0
    loss_curve: list[tuple[int, float]] = field(default_factory=list)
    resid_sd_bps: float = 0.0
    n: int = 0
    fitted: bool = False
    registry_id: str = "0" * 64
    model_class: str = "M3.gbt"

    def fit(self, x: np.ndarray, y: np.ndarray, x_val: np.ndarray, y_val: np.ndarray) -> GBTModel:
        """Fit on the purged training rows; ``x_val, y_val`` are the validation rows read for early stopping only."""
        x, y = _clean(x, y, max(2 * self.min_samples_leaf, 30))
        x_val, y_val = _clean(x_val, y_val, 1)
        self.n = int(x.shape[0])
        self.edges = _bin_edges(x, self.n_bins)
        xb, vb = _binned(x, self.edges), _binned(x_val, self.edges)
        self.base = float(y.mean())
        pred = np.full(self.n, self.base)
        vpred = np.full(y_val.size, self.base)
        trees: list[_Tree] = []
        best_loss, best_trees = float(np.mean((y_val - vpred) ** 2)), 0
        self.loss_curve = [(0, best_loss)]
        self.evaluations = 0
        draw = 0
        for k in range(1, self.max_trees + 1):
            u = splitmix_uniforms(self.seed, draw, self.n)
            draw += self.n
            idx = np.flatnonzero(u < self.subsample)
            t = _grow(
                xb,
                y - pred,
                idx,
                max_leaves=self.max_leaves,
                min_leaf=self.min_samples_leaf,
                l2=self.l2_leaf,
                lr=self.learning_rate,
                n_bins=self.n_bins,
            )
            trees.append(t)
            pred += t.apply(xb)
            vpred += t.apply(vb)
            if k % self.evaluate_every == 0:
                self.evaluations += 1
                loss = float(np.mean((y_val - vpred) ** 2))
                self.loss_curve.append((k, loss))
                if loss < best_loss * (1.0 - self.min_rel_improvement):
                    best_loss, best_trees = loss, k
                elif k - best_trees >= self.patience:
                    break
        self.trees = trees[:best_trees]
        resid = y - self.base - sum((t.apply(xb) for t in self.trees), np.zeros(self.n))
        self.resid_sd_bps = float(np.sqrt((resid @ resid) / max(self.n - 1, 1)))
        self.fitted = True
        return self

    def predict(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        x = design(rows, self.features)
        if not self.fitted:
            return np.full(x.shape[0], np.nan), np.full(x.shape[0], np.nan)
        ok = np.all(np.isfinite(x), axis=1)
        mu = np.full(x.shape[0], np.nan)
        if ok.any():
            xb = _binned(x[ok], self.edges)
            mu[ok] = self.base + sum((t.apply(xb) for t in self.trees), np.zeros(xb.shape[0]))
        return mu, np.where(ok, self.resid_sd_bps, np.nan)

    @property
    def n_trees(self) -> int:
        return len(self.trees)

    @property
    def complexity(self) -> float:
        return float(self.max_leaves * self.n_trees)

    def parameters(self) -> dict[str, Any]:
        trees = [
            [t.feature, t.threshold, t.left, t.right, [_round(v) for v in t.value]]
            for t in self.trees
        ]
        return {
            "features": list(self.features),
            "max_leaves": self.max_leaves,
            "learning_rate": self.learning_rate,
            "l2_leaf": self.l2_leaf,
            "min_samples_leaf": self.min_samples_leaf,
            "subsample": self.subsample,
            "seed": self.seed,
            "max_trees": self.max_trees,
            "n_trees": self.n_trees,
            "realised_leaves": sum(t.leaves() for t in self.trees),
            "early_stopping_evaluations": self.evaluations,
            "loss_curve": [[k, _round(v)] for k, v in self.loss_curve],
            "bin_edges": [[_round(v) for v in e] for e in self.edges],
            "base_bps": _round(self.base),
            "trees_sha256": content_hash(trees),
            "resid_sd_bps": _round(self.resid_sd_bps),
            "n": self.n,
            "complexity": self.complexity,
        }

    def fit_hash(self) -> str:
        trees = [[t.feature, t.threshold, t.left, t.right, t.value] for t in self.trees]
        return content_hash({"class": self.model_class, **self.parameters(), "trees": trees})

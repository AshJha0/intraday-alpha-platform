"""Fill-probability hazard model for passive orders (v1.12 X4).

The FIFO simulator answers "did this order fill?" by replay; a passive
policy has to answer it BEFORE posting: ``P(fill within t | x)`` for a LIMIT
joining the touch, with ``x`` the state at decision time. This module fits
that probability as a grouped-time (discrete) hazard model:

- time since the post is cut into buckets ``[e_b, e_{b+1})`` (``edges_ns``);
- the hazard of bucket ``b`` (P(fill in b | not filled before b, x)) is
  ``h_b(x) = 1 - exp(-exp(a_b + beta . z(x)))`` (``link="cloglog"``, the
  default: the exact grouped-time form of a continuous proportional-hazards
  / Cox model with a piecewise-constant baseline ``exp(a_b) / width_b``) or
  ``sigmoid(a_b + beta . z(x))`` (``link="logit"``);
- ``z(x)`` are the standardised features :data:`FEATURES`: ``log1p`` of the
  displayed queue ahead, ``log1p`` of the queue-depletion rate (per s, e.g.
  :meth:`ExecCalibration.queue_depletion_hazard`), the spread in ticks, the
  queue imbalance (our side's L1 / both sides' L1), and the time of day as
  ``sin`` / ``cos`` of the fraction of the UTC day;
- ``P(fill within t) = 1 - prod_b (1 - h_b) ^ overlap_b(t) / width_b`` (a
  partial bucket contributes geometrically; beyond the last edge the last
  bucket's hazard continues).

Fitting expands every sample into person-period rows (one per bucket it was
at risk in; censored at ``ttl_ns`` when it did not fill) and runs
Newton / IRLS with a small ridge on ``beta`` — numpy only, no RNG, so the
same data gives the same JSON bit for bit. Training samples come from the
v1.9 M3 maker labels (:func:`samples_from_maker_labels`): every label order
is a LIMIT at the touch with a known decision time, fill time (or none) and
queue ahead. TAKER-filled label rows are dropped (they did not rest).

Persistence: :meth:`FillHazardModel.to_dict` is a versioned JSON document
(``schema = "iap.fill_hazard/1"``, sorted keys); the fitted object is also
picklable, so it can be registered in the v1.11 :mod:`iap.mlops` registry
(:meth:`FillHazardModel.register`, kind ``classifier``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SCHEMA = "iap.fill_hazard/1"
FEATURES = ("queue_ahead", "depletion_per_s", "spread_ticks", "imbalance", "tod")
#: model columns derived from :data:`FEATURES` (tod -> sin, cos)
COLUMNS = ("log_queue_ahead", "log_depletion", "spread_ticks", "imbalance", "tod_sin", "tod_cos")
DEFAULT_EDGES_NS = (
    0,
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
    2_000_000_000,
    5_000_000_000,
)
LINKS = ("cloglog", "logit")
DAY_NS = 86_400_000_000_000


def time_of_day(ts_ns: int | np.ndarray) -> np.ndarray:
    """Fraction of the UTC day of an epoch-ns timestamp."""
    return (np.asarray(ts_ns, dtype=np.int64) % DAY_NS).astype(float) / DAY_NS


def hazard_features(
    *,
    queue_ahead: float,
    spread_ticks: float,
    imbalance: float = 0.0,
    depletion_per_s: float | None = None,
    ts_ns: int = 0,
) -> dict[str, float]:
    """One feature row (the same construction training and decisions use)."""
    return {
        "queue_ahead": float(queue_ahead),
        "depletion_per_s": float(depletion_per_s or 0.0),
        "spread_ticks": float(spread_ticks),
        "imbalance": float(imbalance),
        "tod": float(time_of_day(ts_ns)),
    }


def _design(frame: pd.DataFrame) -> np.ndarray:
    for f in FEATURES:
        if f not in frame.columns:
            raise ValueError(f"hazard features need column {f!r}")
    q = np.maximum(frame["queue_ahead"].to_numpy(dtype=float), 0.0)
    d = np.maximum(frame["depletion_per_s"].to_numpy(dtype=float), 0.0)
    tod = frame["tod"].to_numpy(dtype=float)
    return np.column_stack(
        [
            np.log1p(q),
            np.log1p(d),
            frame["spread_ticks"].to_numpy(dtype=float),
            frame["imbalance"].to_numpy(dtype=float),
            np.sin(2 * math.pi * tod),
            np.cos(2 * math.pi * tod),
        ]
    )


def samples_from_maker_labels(
    labels: pd.DataFrame,
    *,
    ttl_ns: int,
    sides: Sequence[str] = ("bid", "ask"),
    depletion_per_s: Mapping[str, Sequence[float]] | None = None,
    imbalance: Mapping[str, Sequence[float]] | None = None,
) -> pd.DataFrame:
    """Survival samples from :func:`iap.labels.maker_labels.maker_labels`.

    One row per (decision, side) with a post: ``duration_ns`` (fill time -
    decision time, or ``ttl_ns`` when censored), ``filled`` and the
    :data:`FEATURES`. ``depletion_per_s`` / ``imbalance`` are optional
    per-side arrays row-aligned with ``labels`` (0 when absent — then the
    model learns no dependence on them). Taker-filled rows are dropped.
    """
    if ttl_ns <= 0:
        raise ValueError("ttl_ns must be positive")
    ts = labels["exchange_ts"].to_numpy(dtype=np.int64)
    spread = (labels["ask_post_ticks"] - labels["bid_post_ticks"]).to_numpy(dtype=float)
    parts = []
    for s in sides:
        posted = labels[f"{s}_post_ticks"].to_numpy() > 0
        keep = posted & ~labels[f"{s}_taker"].to_numpy(dtype=bool)
        filled = labels[f"{s}_filled"].to_numpy(dtype=bool)
        dur = np.where(filled, labels[f"{s}_fill_ts"].to_numpy(dtype=np.int64) - ts, ttl_ns)
        dep = np.zeros(len(ts)) if not depletion_per_s else np.asarray(depletion_per_s[s], float)
        imb = np.zeros(len(ts)) if not imbalance else np.asarray(imbalance[s], float)
        parts.append(
            pd.DataFrame(
                {
                    "exchange_ts": ts,
                    "side": s,
                    "duration_ns": np.clip(dur, 0, None),
                    "filled": filled,
                    "queue_ahead": labels[f"{s}_queue_ahead"].to_numpy(dtype=float),
                    "depletion_per_s": dep,
                    "spread_ticks": np.where(np.isfinite(spread), np.maximum(spread, 0), 0),
                    "imbalance": imb,
                    "tod": time_of_day(ts),
                }
            )[keep]
        )
    return pd.concat(parts, ignore_index=True)


def _inv_link(eta: np.ndarray, link: str) -> tuple[np.ndarray, np.ndarray]:
    """(mu, dmu/deta), clipped away from 0 and 1."""
    eta = np.clip(eta, -30.0, 30.0)
    if link == "cloglog":
        ee = np.exp(eta)
        mu = -np.expm1(-ee)
        d = ee * np.exp(-ee)
    else:
        mu = 1.0 / (1.0 + np.exp(-eta))
        d = mu * (1.0 - mu)
    eps = 1e-12
    return np.clip(mu, eps, 1 - eps), np.maximum(d, eps)


class FillHazardModel:
    """Discrete-time fill hazard (module docstring). Build with :meth:`fit`."""

    def __init__(
        self,
        edges_ns: Sequence[int] = DEFAULT_EDGES_NS,
        link: str = "cloglog",
        ridge: float = 1e-3,
    ) -> None:
        e = [int(x) for x in edges_ns]
        if len(e) < 2 or e[0] != 0 or any(b <= a for a, b in zip(e, e[1:], strict=False)):
            raise ValueError("edges_ns must start at 0 and increase strictly (>= 2 edges)")
        if link not in LINKS:
            raise ValueError(f"link must be one of {LINKS}")
        if ridge < 0:
            raise ValueError("ridge must be >= 0")
        self.edges_ns = tuple(e)
        self.link = link
        self.ridge = float(ridge)
        self.alpha: np.ndarray | None = None  # per bucket
        self.beta: np.ndarray | None = None  # per column, on standardised z
        self.mean = np.zeros(len(COLUMNS))
        self.std = np.ones(len(COLUMNS))
        self.n_samples = 0
        self.n_filled = 0
        self.model_id: str | None = None

    # ------------------------------------------------------------- fitting
    @property
    def n_buckets(self) -> int:
        return len(self.edges_ns) - 1

    def fit(self, samples: pd.DataFrame, max_iter: int = 50, tol: float = 1e-10) -> FillHazardModel:
        """Fit on survival samples (``duration_ns``, ``filled``, :data:`FEATURES`)."""
        dur = samples["duration_ns"].to_numpy(dtype=np.int64)
        filled = samples["filled"].to_numpy(dtype=bool)
        X = _design(samples)
        if len(dur) == 0 or not filled.any():
            raise ValueError("fill hazard needs at least one filled sample")
        self.mean = X.mean(axis=0)
        sd = X.std(axis=0)
        self.std = np.where(sd > 1e-12, sd, 1.0)
        Z = (X - self.mean) / self.std
        edges = np.asarray(self.edges_ns, dtype=np.int64)
        nb = self.n_buckets
        # bucket of the event / censoring time; samples past the last edge are
        # censored there (a fill after the horizon is not an event in it)
        ev_b = np.searchsorted(edges, dur, side="right") - 1
        is_ev = filled & (dur < edges[-1])
        # at risk in buckets 0..last (inclusive) where last = ev_b for an
        # event, the bucket before the censoring point otherwise
        last = np.where(is_ev, ev_b, np.minimum(ev_b, nb) - 1)
        # a censoring in the middle of bucket b counts as at risk in b when
        # at least half the bucket was observed (actuarial convention)
        mid = (
            edges[np.clip(ev_b, 0, nb - 1)]
            + (edges[np.clip(ev_b + 1, 1, nb)] - edges[np.clip(ev_b, 0, nb - 1)]) // 2
        )
        half = (~is_ev) & (ev_b < nb) & (dur >= mid)
        last = np.where(half, ev_b, last)
        reps = np.maximum(last + 1, 0)
        rows = np.repeat(np.arange(len(dur)), reps)
        bucket = np.concatenate([np.arange(k) for k in reps]) if reps.sum() else np.zeros(0, int)
        y = (is_ev[rows] & (bucket == ev_b[rows])).astype(float)
        if y.sum() == 0:
            raise ValueError("no fill falls inside the hazard buckets")
        D = np.zeros((rows.size, nb + Z.shape[1]))
        D[np.arange(rows.size), bucket] = 1.0
        D[:, nb:] = Z[rows]
        # start: per-bucket empirical hazard
        theta = np.zeros(D.shape[1])
        for b in range(nb):
            m = bucket == b
            p = (y[m].sum() + 0.5) / (m.sum() + 1.0) if m.any() else 0.5
            p = min(max(p, 1e-6), 1 - 1e-6)
            theta[b] = (
                math.log(-math.log1p(-p)) if self.link == "cloglog" else math.log(p / (1 - p))
            )
        pen = np.zeros(D.shape[1])
        pen[nb:] = self.ridge
        pen[:nb] = 1e-8  # keeps empty buckets identifiable
        for _ in range(max_iter):
            mu, d = _inv_link(D @ theta, self.link)
            w = d * d / (mu * (1 - mu))
            grad = D.T @ ((y - mu) * d / (mu * (1 - mu))) - pen * theta
            H = (D * w[:, None]).T @ D + np.diag(pen)
            step = np.linalg.solve(H, grad)
            theta = theta + step
            if float(np.max(np.abs(step))) < tol:
                break
        self.alpha = theta[:nb].copy()
        self.beta = theta[nb:].copy()
        self.n_samples = int(len(dur))
        self.n_filled = int(is_ev.sum())
        return self

    # ---------------------------------------------------------- prediction
    def _check(self) -> None:
        if self.alpha is None or self.beta is None:
            raise RuntimeError("FillHazardModel used before fit() / from_dict()")

    def bucket_hazards(self, features: pd.DataFrame | Mapping[str, float]) -> np.ndarray:
        """``h_b(x)``, shape (rows, buckets)."""
        self._check()
        frame = pd.DataFrame([features]) if isinstance(features, Mapping) else features
        z = (_design(frame) - self.mean) / self.std
        eta = self.alpha[None, :] + (z @ self.beta)[:, None]
        mu, _ = _inv_link(eta, self.link)
        return mu

    def p_fill_within(
        self, features: pd.DataFrame | Mapping[str, float], t_ns: int
    ) -> np.ndarray | float:
        """``P(fill within t_ns | x)``; a float for a single mapping."""
        h = self.bucket_hazards(features)
        edges = np.asarray(self.edges_ns, dtype=float)
        width = np.diff(edges)
        overlap = np.clip(float(t_ns) - edges[:-1], 0.0, width)
        extra = max(float(t_ns) - edges[-1], 0.0)
        overlap = overlap.copy()
        overlap[-1] += extra  # the last bucket's hazard continues
        log_s = (np.log1p(-h) * (overlap / width)[None, :]).sum(axis=1)
        p = -np.expm1(log_s)
        return float(p[0]) if isinstance(features, Mapping) else p

    # --------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        self._check()
        return {
            "schema": SCHEMA,
            "link": self.link,
            "edges_ns": list(self.edges_ns),
            "ridge": self.ridge,
            "features": list(FEATURES),
            "columns": list(COLUMNS),
            "alpha": [float(x) for x in self.alpha],
            "beta": [float(x) for x in self.beta],
            "mean": [float(x) for x in self.mean],
            "std": [float(x) for x in self.std],
            "n_samples": self.n_samples,
            "n_filled": self.n_filled,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> FillHazardModel:
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"not a {SCHEMA} document: {doc.get('schema')!r}")
        if list(doc.get("columns", [])) != list(COLUMNS):
            raise ValueError("fill hazard document has different feature columns")
        m = cls(doc["edges_ns"], doc["link"], doc.get("ridge", 1e-3))
        m.alpha = np.asarray(doc["alpha"], dtype=float)
        m.beta = np.asarray(doc["beta"], dtype=float)
        m.mean = np.asarray(doc["mean"], dtype=float)
        m.std = np.asarray(doc["std"], dtype=float)
        m.n_samples = int(doc.get("n_samples", 0))
        m.n_filled = int(doc.get("n_filled", 0))
        if m.alpha.size != m.n_buckets or m.beta.size != len(COLUMNS):
            raise ValueError("fill hazard document has inconsistent shapes")
        return m

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> FillHazardModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------- mlops (v1.11)
    def register(self, registry, **meta):
        """Register in an :class:`iap.mlops.ModelRegistry` (kind ``classifier``;
        ``params`` carry the JSON document so the record is self-describing)."""
        from iap.mlops.registry import ModelRegistry

        reg = registry if isinstance(registry, ModelRegistry) else ModelRegistry(registry)
        params = {"fill_hazard": self.to_dict(), **meta.pop("params", {})}
        meta.setdefault("name", "fill_hazard")
        return reg.register(self, kind="classifier", params=params, **meta)

    @classmethod
    def from_registry(cls, registry, model_id: str) -> FillHazardModel:
        from iap.mlops.registry import ModelRegistry

        reg = registry if isinstance(registry, ModelRegistry) else ModelRegistry(registry)
        model, rec = reg.load(model_id)
        if not isinstance(model, cls):
            model = cls.from_dict(rec.params["fill_hazard"])
        model.model_id = rec.model_id
        return model

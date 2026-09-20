"""Streaming alpha scoring for the MVP: ``Alpha`` adapters + the ensemble.

:class:`LinearZAlpha` wraps one fitted :class:`~iap.alpha.base.LinearAlpha`
(loaded from ``configs/strategies/alpha_params.json`` with the registry
hash check) into the :class:`~iap.contracts.protocols.Alpha` protocol: one
:class:`~iap.features.engine.FeatureVector` in, one
:class:`~iap.contracts.types.AlphaSignal` out.  The raw signal is the
alpha class's own :meth:`~iap.alpha.base.LinearAlpha.raw_signal` (reused,
not re-derived); the scaling is the pinned ``linear_z_v1`` rule of
API_ALPHA.md §2 applied to that one row — ``z = clip((raw - mu) /
(sigma + EPS), ±z_clip)``, ``er = beta * z``, ``conf = min(1, |z| /
conf_scale)``, an invalid input or a dead alpha scores ``(0, 0)``.  The
batch scorer :meth:`~iap.alpha.base.LinearAlpha.score` is a research
interface keyed by the pinned universe ids; ``python/tests/test_mvp.py``
asserts the adapter reproduces it row for row.

:class:`AlphaEnsemble` combines the member signals exactly as
:func:`iap.backtest.engine.ensemble_scores` does (equal-weight z average
rescaled by the mean ``|beta|``, mean confidence) by calling that function
on one-row frames, so the MVP's ensemble and the research ensemble are
the same code path.
"""

from __future__ import annotations

import math
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd

from iap.alpha import load_params_file
from iap.alpha.base import EPS, LinearAlpha
from iap.backtest.engine import ensemble_scores
from iap.contracts.types import AlphaSignal, Direction
from iap.contracts.versions import content_hash
from iap.features.engine import FeatureVector

__all__ = ["LinearZAlpha", "AlphaEnsemble", "load_alphas", "direction_of"]


def direction_of(expected_return: float) -> Direction:
    """Sign of an expected return as a :class:`Direction`."""
    if expected_return > 0.0:
        return Direction.UP
    if expected_return < 0.0:
        return Direction.DOWN
    return Direction.FLAT


class LinearZAlpha:
    """One fitted ``linear_z_v1`` alpha as a streaming scorer (see module doc)."""

    def __init__(self, model: LinearAlpha, feature_names: Sequence[str],
                 horizon_ns: int) -> None:
        if not model.is_fitted():
            raise ValueError(f"{model.alpha_id}: parameters not loaded")
        self._model = model
        self._horizon_ns = int(horizon_ns)
        index = {name: i for i, name in enumerate(feature_names)}
        missing = [f for f in model.features if f not in index]
        if missing:
            raise ValueError(f"{model.alpha_id}: features {missing} are not in the registry")
        self._columns: Tuple[Tuple[str, int], ...] = tuple(
            (f, index[f]) for f in model.features)
        self._version = content_hash(model.params())

    @property
    def alpha_id(self) -> str:
        return self._model.alpha_id

    @property
    def version(self) -> str:
        """``content_hash`` of the fitted parameter block."""
        return self._version

    @property
    def model(self) -> LinearAlpha:
        return self._model

    @property
    def beta(self) -> float:
        return self._model.beta

    def raw(self, features: FeatureVector) -> float:
        """The oriented raw signal of one vector (NaN when any input is invalid)."""
        row: Dict[str, List[float]] = {"exchange_ts": [features.timestamp]}
        for name, i in self._columns:
            row[name] = [features.values[i] if features.validity[i] else math.nan]
        return float(self._model.raw_signal(pd.DataFrame(row)).iloc[0])

    def generate(self, features: FeatureVector) -> AlphaSignal:
        """Score one feature vector (pinned ``linear_z_v1`` semantics)."""
        m = self._model
        raw = self.raw(features)
        if m.is_dead or not math.isfinite(raw):
            er, conf = 0.0, 0.0
        else:
            z = (raw - m.mu) / (m.sigma + EPS)
            z = min(max(z, -m.z_clip), m.z_clip)
            er = m.beta * z
            conf = min(1.0, abs(z) / m.conf_scale)
        return AlphaSignal(
            timestamp=features.timestamp, instrument_id=features.instrument_id,
            expected_return=er, confidence=conf, horizon_ns=self._horizon_ns,
            direction=direction_of(er), model_version=m.alpha_id,
        )


class AlphaEnsemble:
    """Equal-weight ensemble of the member alphas (``ensemble_scores``)."""

    def __init__(self, members: Sequence[LinearZAlpha], horizon_ns: int) -> None:
        if not members:
            raise ValueError("ensemble needs at least one member")
        ids = [m.alpha_id for m in members]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate ensemble members: {ids}")
        self._members: Tuple[LinearZAlpha, ...] = tuple(sorted(members, key=lambda m: m.alpha_id))
        self._betas = {m.alpha_id: m.beta for m in self._members}
        for m in self._members:
            if m.beta == 0.0:
                raise ValueError(f"{m.alpha_id}: a dead alpha cannot enter the ensemble")
        self._horizon_ns = int(horizon_ns)
        self._alpha_id = "-".join(m.alpha_id for m in self._members)
        self._version = content_hash({
            "ensemble": "equal_weight_z_v1",
            "members": {m.alpha_id: m.version for m in self._members},
            "horizon_ns": self._horizon_ns,
        })

    @property
    def members(self) -> Tuple[LinearZAlpha, ...]:
        return self._members

    @property
    def alpha_id(self) -> str:
        """Member ids joined with ``-`` (a generic contract id)."""
        return self._alpha_id

    @property
    def version(self) -> str:
        """``content_hash`` of the ensemble definition + member versions (sha256)."""
        return self._version

    def combine(self, signals: Mapping[str, AlphaSignal]) -> AlphaSignal:
        """The ensemble signal of one decision from its member signals."""
        first = next(iter(signals.values()))
        iid = first.instrument_id
        ts = first.timestamp
        frames = {}
        for m in self._members:
            sig = signals[m.alpha_id]
            if sig.instrument_id != iid or sig.timestamp != ts:
                raise ValueError("ensemble: member signals must share instrument and time")
            frames[m.alpha_id] = {iid: pd.DataFrame({
                "exchange_ts": np.array([ts], dtype=np.int64),
                "expected_return": np.array([sig.expected_return], dtype=float),
                "confidence": np.array([sig.confidence], dtype=float),
            })}
        out = ensemble_scores(frames, self._betas)[iid]
        er = float(out["expected_return"].iloc[0])
        conf = float(out["confidence"].iloc[0])
        return AlphaSignal(
            timestamp=ts, instrument_id=iid, expected_return=er, confidence=conf,
            horizon_ns=self._horizon_ns, direction=direction_of(er),
            model_version=self._alpha_id,
        )


def load_alphas(params_path, alpha_ids: Sequence[str], feature_names: Sequence[str],
                feature_version: str, horizon_ns: int) -> Tuple[LinearZAlpha, ...]:
    """Load the configured alphas from ``alpha_params.json`` (registry-hash checked)."""
    models = load_params_file(params_path, expected_feature_version=feature_version)
    out = []
    for aid in alpha_ids:
        model = models.get(aid)
        if model is None:
            raise ValueError(f"{params_path}: no fitted parameters for alpha {aid!r}")
        if not isinstance(model, LinearAlpha):
            raise ValueError(f"{aid}: not a linear_z_v1 alpha")
        if model.is_dead:
            raise ValueError(f"{aid}: dead alpha (sigma <= 0 or beta == 0) cannot trade")
        out.append(LinearZAlpha(model, feature_names, horizon_ns))
    return tuple(out)

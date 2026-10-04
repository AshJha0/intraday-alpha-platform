"""``CombinedAlpha`` — an alpha whose inputs are alphas.

The combination satisfies the :class:`iap.alpha.base.AlphaModel` interface
(``fit`` / ``score`` / ``params``), so it goes through
``iap.validation.validate.validate_alpha`` — walk-forward, leakage tests,
cost model, backtest, gates — exactly as a single alpha does.

**What "fitted strictly out of sample" means here (pinned).**
``validate_alpha`` hands ``fit`` the training window of an outer
walk-forward fold (purged at the combination's horizon and embargoed) and
nothing else.  Inside that window ``fit``:

1. splits the window again with the SAME splitter the validation uses
   (:class:`iap.validation.splits.WalkForwardSplitter`: expanding, purged,
   embargoed, row-mass folds; :data:`INNER_FOLDS` of them);
2. for every inner fold fits each member on the inner training rows and
   scores the inner test rows — the member's OUT-OF-SAMPLE prediction for
   rows it was not fitted on.  These rows, stacked over the inner folds,
   are the **stack**;
3. estimates the standardisation and the combination weights on the stack
   (:mod:`iap.combine.weights`; the ridge penalty by forward-chained CV over
   the inner folds), then the scale of the combined score (mean, standard
   deviation, and the OLS slope ``beta`` of the label on the clipped
   z-score — as :class:`iap.alpha.base.LinearAlpha` does for one signal);
4. refits every member on the whole training window for scoring.

``score`` then applies the stored member parameters, the stored
standardisation, the stored weights and the stored scale to the rows it is
given.  No weight, mean, scale or penalty is a function of a test row:
deleting or corrupting every row from the outer test start onwards leaves
the fitted parameters bit-identical (``python/tests/test_combine.py``).

**Member purge.**  The outer and inner splits purge at the COMBINATION's
horizon.  A member with a longer label horizon would be fitted on rows whose
own label reaches past the end of the training rows, so each member is
fitted on the training rows minus the last ``member horizon - combination
horizon`` of them.

**Member signal.**  The standardised signal ``z = expected_return / beta``
of the member's score on rows with confidence > 0 — the quantity the
validation pools as its gate input — and NaN elsewhere (no opinion).  A
member whose fit is dead (``beta == 0``) has no signal at all.

**Scoring** (mirrors ``LinearAlpha``)::

    c    = sum_k w_k * (z_k - mean_k) / scale_k      (missing z_k -> 0;
                                                      accumulated in member order)
    zc   = clip((c - mu) / (sigma + EPS), -4, +4)
    er   = beta * zc
    conf = min(1, |zc| / 2)

A row on which no member with a non-zero weight has an opinion scores
``(0.0, 0.0)``; a combination with no weights, no variance or ``beta == 0``
is dead and scores ``(0.0, 0.0)`` everywhere.

**Hypothesis.**  ``hypothesis_confirmed`` is ``beta > 0`` AND the weights
are net long the members' stated hypotheses (``sum(w) > 0``): the blend
forecasts in the direction its members were built to forecast.  Under
``equal_weight`` this is ``beta > 0``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from iap.alpha import build
from iap.alpha.base import EPS, AlphaModel
from iap.combine.weights import (
    DEFAULT_METHOD,
    METHODS,
    MIN_OBS,
    apply_standardisation,
    blend,
    fit_weights,
)
from iap.validation.metrics import HORIZONS_NS
from iap.validation.splits import WalkForwardSplitter

__all__ = [
    "INNER_FOLDS",
    "CombinedAlpha",
    "combination_horizon",
    "member_purged_train",
    "member_signal",
]

#: Inner walk-forward folds of the stack (module docs).
INNER_FOLDS = 3
Z_CLIP = 4.0
CONF_SCALE = 2.0


def combination_horizon(member_horizons: Sequence[str]) -> str:
    """The pinned horizon of a combination: the (lower) median of its
    members' label horizons — a rule fixed before any result was seen."""
    if not member_horizons:
        raise ValueError("combination_horizon: no members")
    ordered = sorted(member_horizons, key=lambda h: HORIZONS_NS[h])
    return ordered[(len(ordered) - 1) // 2]


def member_purged_train(
    frames: Mapping[int, pd.DataFrame], member_horizon: str, horizon: str
) -> dict[int, pd.DataFrame]:
    """``frames`` minus the rows whose ``member_horizon`` label reaches past
    the end of a window that was purged at ``horizon`` (module docs)."""
    extra = HORIZONS_NS[member_horizon] - HORIZONS_NS[horizon]
    nonempty = [df for df in frames.values() if len(df)]
    if extra <= 0 or not nonempty:
        return dict(frames)
    end = max(int(df["exchange_ts"].iloc[-1]) for df in nonempty)
    out: dict[int, pd.DataFrame] = {}
    for iid, df in frames.items():
        ts = df["exchange_ts"].to_numpy(dtype=np.int64)
        out[iid] = df[ts <= end - extra].reset_index(drop=True)
    return out


def member_signal(
    model: AlphaModel, frames: Mapping[int, pd.DataFrame], universe: Sequence[int]
) -> dict[int, np.ndarray]:
    """Per-instrument standardised signal ``z`` of a fitted member, NaN
    where it has no opinion (module docs, "Member signal")."""
    beta = float(model.params().get("beta", 0.0) or 0.0)
    scores = model.score(frames) if beta != 0.0 else {}
    out: dict[int, np.ndarray] = {}
    for iid in universe:
        z = np.full(len(frames[iid]), np.nan)
        sc = scores.get(iid)
        if sc is not None:
            er = sc["expected_return"].to_numpy(dtype=float)
            ok = sc["confidence"].to_numpy(dtype=float) > 0.0
            z[ok] = er[ok] / beta
        out[iid] = z
    return out


def _label(df: pd.DataFrame, horizon: str) -> np.ndarray:
    """The label a fit may read: ``label_mid_<h>`` on valid rows, else NaN."""
    y = df[f"label_mid_{horizon}"].to_numpy(dtype=float).copy()
    y[~df[f"label_valid_{horizon}"].to_numpy(dtype=bool)] = np.nan
    return y


class CombinedAlpha(AlphaModel):
    """A weighted blend of member alphas' standardised signals.

    Economic rationale: each member states a weak, noisy hypothesis about
    the next mid move.  Signals whose errors are not perfectly correlated
    average those errors down faster than they average the common forecast
    down, so a blend of K members with pairwise correlation rho has the
    information coefficient of one member times ``sqrt(K / (1 + (K - 1) *
    rho))`` — the effective breadth.  The blend earns that only to the
    extent the members carry signal in the first place and are not copies
    of one another.
    """

    cross_sectional = True

    def __init__(
        self,
        member_ids: Sequence[str],
        method: str = DEFAULT_METHOD,
        *,
        asset_class: str | None = None,
        horizon: str | None = None,
        alpha_id: str | None = None,
        inner_folds: int = INNER_FOLDS,
        embargo_ns: int = 60_000_000_000,
        member_factory: Callable[[str], AlphaModel] = build,
    ) -> None:
        if method not in METHODS:
            raise ValueError(f"unknown combination method {method!r}; known: {METHODS}")
        ids = list(member_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("CombinedAlpha: member ids must be non-empty and unique")
        if inner_folds < 2:
            raise ValueError("CombinedAlpha: inner_folds must be >= 2")
        self.member_ids: tuple[str, ...] = tuple(sorted(ids))
        self.method = method
        self._factory = member_factory
        probes = [member_factory(aid) for aid in self.member_ids]
        classes = sorted({p.asset_class for p in probes})
        if asset_class is None:
            if len(classes) != 1:
                raise ValueError(f"CombinedAlpha: members span asset classes {classes}")
            asset_class = classes[0]
        elif classes != [asset_class]:
            raise ValueError(f"CombinedAlpha: members are {classes}, not {asset_class!r}")
        self.asset_class = asset_class
        self.member_horizons: dict[str, str] = {p.alpha_id: p.horizon for p in probes}
        self.horizon = (
            horizon if horizon is not None else combination_horizon([p.horizon for p in probes])
        )
        if self.horizon not in HORIZONS_NS:
            raise ValueError(f"CombinedAlpha: unknown horizon {self.horizon!r}")
        self.alpha_id = alpha_id if alpha_id is not None else f"COMB_{asset_class}"
        self.name = f"{method} combination of {len(self.member_ids)} {asset_class} alphas"
        self.features = tuple(sorted({f for p in probes for f in p.features}))
        self.inner_folds = int(inner_folds)
        self.embargo_ns = int(embargo_ns)
        k = len(self.member_ids)
        self.weights = np.zeros(k)
        self.member_mean = np.zeros(k)
        self.member_scale = np.zeros(k)
        self.mu = 0.0
        self.sigma = 0.0
        self.beta = 0.0
        self.n_train = 0
        self.n_stack_rows = 0
        self.detail: dict = {}
        self.train_window: dict[str, int] | None = None
        self._members: dict[str, AlphaModel] = {}
        self._fitted = False

    # -- helpers ----------------------------------------------------------

    @property
    def is_dead(self) -> bool:
        """True when the fit found nothing to trade on (module docs)."""
        return not (self.sigma > 0.0) or self.beta == 0.0 or not np.any(self.weights != 0.0)

    def _fit_members(self, train: Mapping[int, pd.DataFrame]) -> dict[str, AlphaModel]:
        members: dict[str, AlphaModel] = {}
        for aid in self.member_ids:
            model = self._factory(aid)
            model.fit(member_purged_train(train, self.member_horizons[aid], self.horizon))
            members[aid] = model
        return members

    def _signals(
        self,
        members: Mapping[str, AlphaModel],
        frames: Mapping[int, pd.DataFrame],
        universe: Sequence[int],
    ) -> dict[int, np.ndarray]:
        """``{instrument: rows x K}`` member signals (NaN = no opinion)."""
        per_member = [member_signal(members[aid], frames, universe) for aid in self.member_ids]
        return {iid: np.column_stack([m[iid] for m in per_member]) for iid in universe}

    def stack(
        self, train: Mapping[int, pd.DataFrame]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """The members' out-of-sample predictions inside ``train`` (module
        docs, steps 1-2): ``(z, y, inner fold id, exchange_ts)``, one row per
        inner-test row.  Empty when the window is too short to split."""
        universe = self.universe(list(train))
        frames = {i: train[i] for i in universe}
        k = len(self.member_ids)
        empty = (np.empty((0, k)), np.empty(0), np.empty(0, np.int64), np.empty(0, np.int64))
        if not any(len(df) for df in frames.values()):
            return empty
        splitter = WalkForwardSplitter(n_folds=self.inner_folds, embargo_ns=self.embargo_ns)
        try:
            folds = list(splitter.split_frames(frames, HORIZONS_NS[self.horizon]))
        except ValueError:
            return empty  # too few rows for the inner split: no stack
        zs, ys, fs, ts = [], [], [], []
        for fold, inner_train, inner_test in folds:
            members = self._fit_members(inner_train)
            signals = self._signals(members, inner_test, universe)
            for iid in universe:
                df = inner_test[iid]
                zs.append(signals[iid])
                ys.append(_label(df, self.horizon))
                fs.append(np.full(len(df), fold.index, dtype=np.int64))
                ts.append(df["exchange_ts"].to_numpy(dtype=np.int64))
        return np.vstack(zs), np.concatenate(ys), np.concatenate(fs), np.concatenate(ts)

    # -- AlphaModel -------------------------------------------------------

    def fit(self, train: Mapping[int, pd.DataFrame]) -> None:
        universe = self.universe(list(train))
        frames = {i: train[i] for i in universe}
        k = len(self.member_ids)
        self.weights, self.member_mean, self.member_scale = np.zeros(k), np.zeros(k), np.zeros(k)
        self.mu, self.sigma, self.beta = 0.0, 0.0, 0.0
        spans = [
            (int(df["exchange_ts"].iloc[0]), int(df["exchange_ts"].iloc[-1]))
            for df in frames.values()
            if len(df)
        ]
        self.train_window = (
            {"start_ts": min(a for a, _ in spans), "end_ts": max(b for _, b in spans)}
            if spans
            else None
        )
        z, y, folds, ts = self.stack(frames)
        self.n_stack_rows = int(z.shape[0])
        self.n_train = int(np.sum(np.isfinite(y)))
        self.detail = {"n_stack_rows": self.n_stack_rows}
        self._members = self._fit_members(frames)
        self._fitted = True
        if z.shape[0] < MIN_OBS:
            return
        fit, std = fit_weights(
            self.method, z, y, folds, ts, HORIZONS_NS[self.horizon] + self.embargo_ns
        )
        self.detail.update(fit.detail)
        self.weights = fit.weights
        self.member_mean, self.member_scale = std.mean, std.scale
        used = self.weights != 0.0
        if not used.any():
            return
        c = blend(std.values, self.weights)
        opinion = np.any(np.isfinite(z[:, used]), axis=1)
        rows = opinion & np.isfinite(y)
        if int(opinion.sum()) < MIN_OBS or int(rows.sum()) < MIN_OBS:
            return
        self.mu = float(np.mean(c[opinion]))
        self.sigma = float(np.std(c[opinion]))
        if not self.sigma > 0.0:
            self.sigma = 0.0
            return
        zc = np.clip((c[rows] - self.mu) / (self.sigma + EPS), -Z_CLIP, Z_CLIP)
        var = float(np.mean(zc * zc) - np.mean(zc) ** 2)
        if var > EPS:
            cov = float(np.mean(zc * y[rows]) - np.mean(zc) * np.mean(y[rows]))
            self.beta = cov / var

    def score(self, data: Mapping[int, pd.DataFrame]) -> dict[int, pd.DataFrame]:
        if not self._fitted:
            raise RuntimeError(f"{self.alpha_id}: score() before fit()/load_params()")
        universe = self.universe(list(data))
        frames = {i: data[i] for i in universe}
        dead = self.is_dead
        signals = {} if dead else self._signals(self._members, frames, universe)
        used = self.weights != 0.0
        out: dict[int, pd.DataFrame] = {}
        for iid in universe:
            n = len(frames[iid])
            er = np.zeros(n)
            conf = np.zeros(n)
            if not dead and n:
                z = signals[iid]
                c = blend(
                    apply_standardisation(z, self.member_mean, self.member_scale), self.weights
                )
                ok = np.any(np.isfinite(z[:, used]), axis=1)
                zc = np.clip((c - self.mu) / (self.sigma + EPS), -Z_CLIP, Z_CLIP)
                er[ok] = self.beta * zc[ok]
                conf[ok] = np.minimum(1.0, np.abs(zc[ok]) / CONF_SCALE)
            out[iid] = pd.DataFrame(
                {
                    "exchange_ts": frames[iid]["exchange_ts"].to_numpy(),
                    "expected_return": er,
                    "confidence": conf,
                }
            )
        return out

    def params(self) -> dict:
        weights = {aid: float(w) for aid, w in zip(self.member_ids, self.weights, strict=True)}
        return {
            "alpha_id": self.alpha_id,
            "model": "combination_v1",
            "method": self.method,
            "horizon": self.horizon,
            "members": list(self.member_ids),
            "weights": weights,
            "member_mean": [float(v) for v in self.member_mean],
            "member_scale": [float(v) for v in self.member_scale],
            "mu": self.mu,
            "sigma": self.sigma,
            "beta": self.beta,
            "beta_fit": self.beta,
            "hypothesis_confirmed": bool(self.beta > 0.0 and float(np.sum(self.weights)) > 0.0),
            "z_clip": Z_CLIP,
            "conf_scale": CONF_SCALE,
            "inner_folds": self.inner_folds,
            "n_train": self.n_train,
            "n_stack_rows": self.n_stack_rows,
            "train_window": self.train_window,
            "detail": self.detail,
            "dead": bool(self.is_dead),
            "fitted": self._fitted,
            "member_params": {aid: m.params() for aid, m in sorted(self._members.items())},
        }

    def load_params(self, blob: dict) -> None:
        """Restore a fit from :meth:`params` output (members included)."""
        if blob.get("model") != "combination_v1":
            raise ValueError(f"{self.alpha_id}: unsupported model {blob.get('model')!r}")
        if tuple(blob.get("members", ())) != self.member_ids:
            raise ValueError(f"{self.alpha_id}: params members differ from this combination's")
        if blob.get("method") != self.method or blob.get("horizon") != self.horizon:
            raise ValueError(f"{self.alpha_id}: params method / horizon differ")
        weights = np.array([float(blob["weights"][aid]) for aid in self.member_ids])
        mean = np.asarray(blob["member_mean"], dtype=float)
        scale = np.asarray(blob["member_scale"], dtype=float)
        scalars = [float(blob[name]) for name in ("mu", "sigma", "beta")]
        if mean.shape != weights.shape or scale.shape != weights.shape:
            raise ValueError(f"{self.alpha_id}: member_mean / member_scale length")
        if not all(np.all(np.isfinite(v)) for v in (weights, mean, scale, np.array(scalars))):
            raise ValueError(f"{self.alpha_id}: non-finite parameter")
        members: dict[str, AlphaModel] = {}
        for aid in self.member_ids:
            model = self._factory(aid)
            model.load_params(blob["member_params"][aid])
            members[aid] = model
        self.weights, self.member_mean, self.member_scale = weights, mean, scale
        self.mu, self.sigma, self.beta = scalars
        self.n_train = int(blob.get("n_train", 0))
        self.n_stack_rows = int(blob.get("n_stack_rows", 0))
        self.detail = dict(blob.get("detail") or {})
        tw = blob.get("train_window")
        self.train_window = dict(tw) if isinstance(tw, dict) else None
        self._members = members
        self._fitted = True

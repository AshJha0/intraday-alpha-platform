"""Regime-conditioned alpha: per-regime beta or interaction terms (v1.17 C5).

The platform already labels every row with regime flags
(``iap.features.regime``: ``vol_regime_flag_v1``, ``liq_regime_flag_v1``)
and the research reports split the IC by them
(:func:`iap.validation.stress.regime_split`).  Until v1.17 no MODEL read
them: one ``beta`` served every regime.  :class:`RegimeConditionedAlpha`
wraps a registered :class:`iap.alpha.base.LinearAlpha` and conditions
its linear scaling on the regime of the row, opt-in and as a new model
identity (``<member id>_rc``; model ``regime_conditioned_v1``); the
member's own class, fit and params are untouched.

**Regime label.**  :func:`regime_labels` maps the declared regime
features (default ``("vol_regime_flag_v1",)``) of a row to an integer
``r in [0, 2**F)`` — the flags read as bits in declared order (feature 0
is bit 0) — and to ``-1`` where any flag is NaN (unknown regime).

**Standardisation.**  The member's own: the wrapper fits the member on
the training rows (its pooled ``mu`` / ``sigma`` under ``linear_z_v1``,
the per-instrument window under ``linear_z_v2``) and reads the member's
clipped ``z`` through its scores (``z = expected_return / beta_member``;
no opinion where the member has none).  The wrapper therefore inherits
whichever standardisation the member was built with.

**Modes.**

``per_regime``
    ``er = beta_r * z`` with ``beta_r`` the OLS slope of the label on ``z``
    over the training rows of regime ``r`` (``min_obs`` pairs, else that
    regime falls back to the pooled slope ``beta_0``); an unknown regime
    scores at ``beta_0``.
``interaction``
    ``er = (beta_0 + sum_f gamma_f * flag_f) * z`` — one OLS over the
    training rows on the regressors ``z`` and ``z * flag_f`` (rows with an
    unknown regime are dropped from the fit and score at ``beta_0``).  For
    one binary flag the two modes coincide up to the fall-back rule.

``confidence = min(1, |z| / conf_scale)`` as the member's; ``beta`` in
the params is ``beta_0`` (the pooled slope: the gates read it as any
``LinearAlpha``'s) and ``hypothesis_confirmed`` is ``beta_0 > 0``.  The
wrapper's ``features`` are the member's plus the regime features, so the
feature-masking leakage guard of ``validate_alpha`` covers the flags.

Nothing here is a port contract; ``alpha_params.json`` never carries it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from iap.alpha import build
from iap.alpha.base import AlphaModel, LinearAlpha

__all__ = [
    "DEFAULT_REGIME_FEATURES",
    "MODEL_RC",
    "MODES",
    "RegimeConditionedAlpha",
    "regime_conditioned",
    "regime_labels",
]

MODEL_RC = "regime_conditioned_v1"
MODES = ("per_regime", "interaction")
DEFAULT_REGIME_FEATURES: tuple[str, ...] = ("vol_regime_flag_v1",)
MIN_OBS = 32
EPS = 1e-12


def regime_labels(
    df: pd.DataFrame, features: Sequence[str] = DEFAULT_REGIME_FEATURES
) -> np.ndarray:
    """Integer regime per row from binary flag columns (module docs);
    ``-1`` where a flag is missing or NaN."""
    if not features:
        raise ValueError("regime_labels: at least one regime feature")
    n = len(df)
    out = np.zeros(n, dtype=np.int64)
    unknown = np.zeros(n, dtype=bool)
    for bit, name in enumerate(features):
        if name not in df.columns:
            return np.full(n, -1, dtype=np.int64)
        f = df[name].to_numpy(dtype=float)
        unknown |= ~np.isfinite(f)
        out |= (np.where(np.isfinite(f), f, 0.0) > 0.5).astype(np.int64) << bit
    out[unknown] = -1
    return out


class RegimeConditionedAlpha(AlphaModel):
    """A registered alpha with regime-conditioned scaling (module docs).

    Economic rationale: the strength — and sometimes the sign — of a
    microstructure effect depends on the state of the market: order-flow
    imbalance moves the price further when the book is thin and
    volatility is elevated, while mean reversion is stronger when
    liquidity is deep.  One pooled slope averages those states; a slope
    per regime (or an interaction with the regime flags) lets the fitted
    scaling follow the state the row is in, with the member's own
    hypothesis unchanged.
    """

    def __init__(
        self,
        member_id: str,
        mode: str = "per_regime",
        regime_features: Sequence[str] = DEFAULT_REGIME_FEATURES,
        *,
        member_factory=build,
        min_obs: int = MIN_OBS,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; known: {MODES}")
        if not regime_features:
            raise ValueError("regime_features must name at least one flag feature")
        probe = member_factory(member_id)
        if not isinstance(probe, LinearAlpha) or getattr(probe, "cross_sectional", False):
            raise ValueError(f"{member_id}: the member must be a per-instrument LinearAlpha")
        self.member_id = member_id
        self.mode = mode
        self.regime_features = tuple(regime_features)
        self.min_obs = int(min_obs)
        self._factory = member_factory
        self.alpha_id = f"{probe.alpha_id}_rc"
        self.name = f"{probe.name}_regime_{mode}"
        self.asset_class = probe.asset_class
        self.horizon = probe.horizon
        self.features = tuple(sorted(set(probe.features) | set(self.regime_features)))
        self.conf_scale = float(getattr(probe, "conf_scale", 2.0))
        self.member: LinearAlpha = probe
        self.beta0 = 0.0
        self.beta_by_regime: dict[int, float] = {}
        self.gamma: list[float] = [0.0] * len(self.regime_features)
        self.n_train = 0
        self.n_by_regime: dict[int, int] = {}
        self.train_window: dict[str, int] | None = None
        self._fitted = False

    # ------------------------------------------------------------ helpers

    @property
    def is_dead(self) -> bool:
        return self.beta0 == 0.0 or self.member.is_dead

    def _z(self, data: Mapping[int, pd.DataFrame]) -> dict[int, np.ndarray]:
        """The member's clipped standardised signal per instrument (NaN =
        no opinion)."""
        beta = float(self.member.params().get("beta", 0.0) or 0.0)
        out: dict[int, np.ndarray] = {}
        if beta == 0.0:
            return {iid: np.full(len(data[iid]), np.nan) for iid in self.universe(list(data))}
        for iid, sc in self.member.score(data).items():
            z = np.full(len(sc), np.nan)
            ok = sc["confidence"].to_numpy(dtype=float) > 0.0
            z[ok] = sc["expected_return"].to_numpy(dtype=float)[ok] / beta
            out[iid] = z
        return out

    def _flags(self, df: pd.DataFrame) -> np.ndarray:
        cols = []
        for name in self.regime_features:
            f = df[name].to_numpy(dtype=float) if name in df.columns else np.full(len(df), np.nan)
            cols.append(np.where(np.isfinite(f), (f > 0.5).astype(float), np.nan))
        return np.column_stack(cols) if cols else np.zeros((len(df), 0))

    @staticmethod
    def _slope(z: np.ndarray, y: np.ndarray) -> float:
        var = float(np.mean(z * z) - np.mean(z) ** 2)
        if var <= EPS:
            return 0.0
        return float(np.mean(z * y) - np.mean(z) * np.mean(y)) / var

    def _scale(self, regime: np.ndarray, flags: np.ndarray) -> np.ndarray:
        """The per-row slope under the fitted mode."""
        if self.mode == "per_regime":
            return np.array(
                [
                    self.beta_by_regime.get(int(r), self.beta0) if r >= 0 else self.beta0
                    for r in regime
                ]
            )
        known = regime >= 0
        g = np.asarray(self.gamma, dtype=float)
        scale = np.full(len(regime), self.beta0)
        if known.any() and g.size:
            scale[known] = self.beta0 + flags[known] @ g
        return scale

    # ------------------------------------------------------------ AlphaModel

    def fit(self, train: Mapping[int, pd.DataFrame]) -> None:
        self.member = self._factory(self.member_id)
        self.member.fit(train)
        label_col = f"label_mid_{self.horizon}"
        valid_col = f"label_valid_{self.horizon}"
        zs, ys, rs, fs = [], [], [], []
        for iid, z in self._z(train).items():
            df = train[iid]
            y = df[label_col].to_numpy(dtype=float)
            ok = np.isfinite(z) & np.isfinite(y) & df[valid_col].to_numpy(dtype=bool)
            zs.append(z[ok])
            ys.append(y[ok])
            rs.append(regime_labels(df, self.regime_features)[ok])
            fs.append(self._flags(df)[ok])
        z = np.concatenate(zs) if zs else np.empty(0)
        y = np.concatenate(ys) if ys else np.empty(0)
        regime = np.concatenate(rs) if rs else np.empty(0, dtype=np.int64)
        flags = np.vstack(fs) if fs else np.zeros((0, len(self.regime_features)))
        spans = [
            (int(df["exchange_ts"].iloc[0]), int(df["exchange_ts"].iloc[-1]))
            for df in train.values()
            if len(df)
        ]
        self.train_window = (
            {"start_ts": min(a for a, _ in spans), "end_ts": max(b for _, b in spans)}
            if spans
            else None
        )
        self.n_train = int(z.size)
        self.beta0 = 0.0
        self.beta_by_regime = {}
        self.gamma = [0.0] * len(self.regime_features)
        self.n_by_regime = {int(r): int(np.sum(regime == r)) for r in np.unique(regime)}
        self._fitted = True
        if z.size < self.min_obs:
            return
        self.beta0 = self._slope(z, y)
        if self.beta0 == 0.0:
            return
        if self.mode == "per_regime":
            for r in sorted(self.n_by_regime):
                if r < 0:
                    continue
                rows = regime == r
                if int(rows.sum()) >= self.min_obs:
                    self.beta_by_regime[int(r)] = self._slope(z[rows], y[rows])
            return
        known = regime >= 0
        if int(known.sum()) < self.min_obs:
            return
        x = np.column_stack([z[known], z[known][:, None] * flags[known]])
        xc = x - x.mean(axis=0)
        yc = y[known] - y[known].mean()
        gram = xc.T @ xc
        if np.linalg.matrix_rank(gram) < gram.shape[0]:
            return  # a flag never varies: keep the pooled slope only
        coef = np.linalg.solve(gram, xc.T @ yc)
        self.beta0 = float(coef[0])
        self.gamma = [float(c) for c in coef[1:]]

    def score(self, data: Mapping[int, pd.DataFrame]) -> dict[int, pd.DataFrame]:
        if not self._fitted:
            raise RuntimeError(f"{self.alpha_id}: score() before fit()/load_params()")
        dead = self.is_dead
        out: dict[int, pd.DataFrame] = {}
        zsig = self._z(data) if not dead else {}
        for iid in self.universe(list(data)):
            df = data[iid]
            n = len(df)
            er = np.zeros(n)
            conf = np.zeros(n)
            if not dead and n:
                z = zsig[iid]
                ok = np.isfinite(z)
                scale = self._scale(regime_labels(df, self.regime_features), self._flags(df))
                er[ok] = scale[ok] * z[ok]
                conf[ok] = np.minimum(1.0, np.abs(z[ok]) / self.conf_scale)
            out[iid] = pd.DataFrame(
                {
                    "exchange_ts": df["exchange_ts"].to_numpy(),
                    "expected_return": er,
                    "confidence": conf,
                }
            )
        return out

    def params(self) -> dict:
        return {
            "alpha_id": self.alpha_id,
            "model": MODEL_RC,
            "mode": self.mode,
            "member": self.member_id,
            "regime_features": list(self.regime_features),
            "horizon": self.horizon,
            "features": list(self.features),
            "beta": self.beta0,
            "beta_fit": self.beta0,
            "beta_by_regime": {str(k): v for k, v in sorted(self.beta_by_regime.items())},
            "gamma": list(self.gamma),
            "n_by_regime": {str(k): v for k, v in sorted(self.n_by_regime.items())},
            "hypothesis_confirmed": bool(self.beta0 > 0.0),
            "conf_scale": self.conf_scale,
            "min_obs": self.min_obs,
            "n_train": self.n_train,
            "train_window": self.train_window,
            "dead": bool(self.is_dead),
            "fitted": self._fitted,
            "member_params": self.member.params() if self._fitted else None,
        }

    def load_params(self, blob: dict) -> None:
        if blob.get("model") != MODEL_RC:
            raise ValueError(f"{self.alpha_id}: unsupported model {blob.get('model')!r}")
        if blob.get("member") != self.member_id or blob.get("mode") != self.mode:
            raise ValueError(f"{self.alpha_id}: params member / mode differ")
        if tuple(blob.get("regime_features", ())) != self.regime_features:
            raise ValueError(f"{self.alpha_id}: params regime_features differ")
        member = self._factory(self.member_id)
        member.load_params(blob["member_params"])
        self.member = member
        self.beta0 = float(blob["beta"])
        self.beta_by_regime = {
            int(k): float(v) for k, v in (blob.get("beta_by_regime") or {}).items()
        }
        self.gamma = [float(v) for v in blob.get("gamma", [])]
        if len(self.gamma) != len(self.regime_features):
            raise ValueError(f"{self.alpha_id}: gamma length != regime_features")
        for v in (self.beta0, *self.gamma, *self.beta_by_regime.values()):
            if not np.isfinite(v):
                raise ValueError(f"{self.alpha_id}: non-finite parameter")
        self.n_by_regime = {int(k): int(v) for k, v in (blob.get("n_by_regime") or {}).items()}
        self.n_train = int(blob.get("n_train", 0))
        tw = blob.get("train_window")
        self.train_window = dict(tw) if isinstance(tw, dict) else None
        self._fitted = True


def regime_conditioned(
    member_id: str,
    mode: str = "per_regime",
    regime_features: Sequence[str] = DEFAULT_REGIME_FEATURES,
):
    """A factory ``() -> RegimeConditionedAlpha`` for ``validate_alpha``'s
    ``model_factory`` (a fresh instance per fold)."""

    def make() -> RegimeConditionedAlpha:
        return RegimeConditionedAlpha(member_id, mode, regime_features)

    return make

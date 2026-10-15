"""Gradient-boosted trees as an :class:`~iap.alpha.base.AlphaModel` (v1.17
WS-D, plan item D1; opt-in, research only).

``GbmAlpha`` (alpha id ``ML01``) puts the model zoo's tree learner behind the
flagship-alpha interface, so it runs through ``validate_alpha``, the
research runner, the pre-registration gate and the experiments ledger
exactly like EQ01..FX12 - the same walk-forward, the same leakage
detectors, the same gate statistics, one look per decision.  Before v1.17
the only path for a GBM was ``iap.models.pipeline.run_model_comparison``,
whose gate pools one IC across fits with no significance (review finding
R-6) and whose looks were recorded only since R-4.

**Design (stated choices).**

- **Backend**: ``hist_gbdt`` (sklearn ``HistGradientBoostingRegressor``,
  the one every CI environment has) by default; ``xgboost`` / ``lightgbm``
  when importable, selected by the ``backend`` attribute and named in the
  parameters.  Hyperparameters are the zoo's pinned ones
  (:data:`iap.models.zoo.HYPERPARAMS`); ``max_iter`` overrides the tree
  count (tests use a small one).
- **Multi-horizon targets: one model per horizon.**  ``fit`` trains an
  independent model for every horizon in ``horizons`` plus the instance's
  ``horizon``; ``score`` reads the model of ``horizon``.  A single model
  with the horizon as an input would share trees across targets whose
  scales differ by two orders of magnitude (1 s vs 1 m mid returns) and
  would make every horizon's fit part of every other's identity.
- **Features**: the curated ML predictor set of ``iap.models.dataset``
  (:data:`FEATURE_SET`), declared as ``features`` so the label guard, the
  column narrowing and the frozen-params hash all read the same list.  NaN
  features are legal inputs to every backend (missing-value branches); a
  row with EVERY feature missing scores ``(0, 0)``.
- **Scoring**: ``er = clip(pred / sigma_h, +-z_clip) * sigma_h`` where
  ``sigma_h`` is the standard deviation of the fitted model's in-sample
  predictions; ``conf = min(1, |pred / sigma_h| / conf_scale)``.  ``beta``
  is 1.0 (0.0 when dead), so the validation chain's standardized signal is
  the clipped prediction itself.
- **Hypothesis sign check**: the stated direction is "the prediction is
  positively correlated with the realised mid return".  ``fit`` holds out
  the last ``sign_check_frac`` of the training rows (by time, purged by the
  horizon), fits on the rest, and records the out-of-sample IC on that
  slice per horizon (``sign_check``); ``hypothesis_confirmed`` is the
  pinned horizon's IC > 0.  The final model is then refit on every row.
- **Parameters**: ``params()`` carries the structural identity (backend,
  horizons, features, hyperparameters, the pinned scoring constants) plus
  the fitted scalars and ``model_sha256``, the SHA-256 of the pickled
  per-horizon models; the models themselves live in a sidecar written by
  :meth:`save_sidecar` (default ``research/models/gbm/``), and
  ``load_params`` verifies the sidecar's digest against ``model_sha256``
  before unpickling.  A native compact dump is not used: the sidecar is one
  pickle whatever the backend, so the frozen-params path is the same code
  path for all three.

Not fingerprinted by any open pre-registration: this module is new and
nothing in ``iap.alpha.base`` / ``equity`` / ``fx`` imports it.  Registration
is opt-in (:func:`iap.alpha.register_ml_alphas`), so ``run_all.py`` and every
published report are unchanged.
"""

from __future__ import annotations

import hashlib
import io
import pickle
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from iap.alpha.base import EPS, VALID_HORIZONS, AlphaModel
from iap.labels import HORIZONS_NS
from iap.models.dataset import FEATURE_SET
from iap.models.zoo import HYPERPARAMS

#: Alpha ids of the opt-in ML alphas (``iap.alpha.register_ml_alphas``).
ML_ALPHA_ID = "ML01"
#: The backends by name; ``hist_gbdt`` is always available.
BACKENDS = ("hist_gbdt", "xgboost", "lightgbm")
#: Minimum pooled training rows before the fit is dead (as LinearAlpha).
MIN_TRAIN_ROWS = 32
#: Default sidecar directory, relative to the repository root.
SIDECAR_DIR = Path("research") / "models" / "gbm"
_REPO = Path(__file__).resolve().parents[4]


def _make_backend(backend: str, max_iter: int | None, seed: int) -> Any:
    """A fresh, unfitted learner of the named backend at the zoo's pinned
    hyperparameters (``max_iter`` overrides the tree count)."""
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; known: {BACKENDS}")
    if backend == "hist_gbdt":
        from sklearn.ensemble import HistGradientBoostingRegressor

        hp = dict(HYPERPARAMS["hist_gbdt"])
        if max_iter is not None:
            hp["max_iter"] = int(max_iter)
        hp["random_state"] = seed
        return HistGradientBoostingRegressor(**hp)
    if backend == "xgboost":
        try:
            from xgboost import XGBRegressor
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ValueError("backend 'xgboost' is not installed") from exc
        hp = dict(HYPERPARAMS["xgboost"])
        if max_iter is not None:
            hp["n_estimators"] = int(max_iter)
        hp["random_state"] = seed
        return XGBRegressor(**hp)
    try:
        from lightgbm import LGBMRegressor
    except ImportError as exc:  # pragma: no cover - env dependent
        raise ValueError("backend 'lightgbm' is not installed") from exc
    hp = dict(HYPERPARAMS["lightgbm"])
    if max_iter is not None:
        hp["n_estimators"] = int(max_iter)
    hp["random_state"] = seed
    return LGBMRegressor(**hp)


def _ic(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < MIN_TRAIN_ROWS or np.std(x[ok]) <= 0.0 or np.std(y[ok]) <= 0.0:
        return float("nan")
    return float(np.corrcoef(x[ok], y[ok])[0, 1])


class GbmAlpha(AlphaModel):
    """ML01 - gradient-boosted trees on the curated predictor set.

    Economic rationale: the flagship alphas are each one oriented linear
    signal; the ML report's finding is that the curated book, flow and
    short-horizon return features carry interactions a linear reader cannot
    express (queue depletion conditioned on imbalance, flow conditioned on
    spread).  A tree ensemble fitted on the mid-to-mid label over the pinned
    horizon reads those interactions.  The stated direction is that its
    prediction is positively correlated with the realised mid return out of
    sample; the sign check of ``fit`` records whether the data agrees on a
    held-out slice of the training fold.  Research only: the model is
    neither a port contract nor a published alpha until it is pre-registered
    and gated like any other.
    """

    alpha_id = ML_ALPHA_ID
    name = "gbm_curated_v1"
    asset_class = "EQUITY"
    horizon = "5s"
    features: tuple[str, ...] = tuple(FEATURE_SET)

    #: Pinned scoring constants (the same two the port contract pins).
    Z_CLIP = 4.0
    CONF_SCALE = 2.0
    #: The horizons a fit trains a model for (plus the instance's ``horizon``).
    horizons: tuple[str, ...] = ("1s", "5s", "30s")
    #: ``hist_gbdt`` | ``xgboost`` | ``lightgbm``.
    backend: str = "hist_gbdt"
    #: Tree count override (``None``: the zoo's pinned count).
    max_iter: int | None = None
    #: Share of the training rows (by time) held out for the sign check;
    #: 0 disables it (``hypothesis_confirmed`` is then False).
    sign_check_frac: float = 0.2
    seed: int = 7

    def __init__(self) -> None:
        self.models: dict[str, Any] = {}
        self.pred_sigma: dict[str, float] = {}
        self.sign_check: dict[str, float | None] = {}
        self.n_train: int = 0
        self.n_train_by_horizon: dict[str, int] = {}
        self.train_window: dict[str, int] | None = None
        self.z_clip: float = self.Z_CLIP
        self.conf_scale: float = self.CONF_SCALE
        self.model_sha256: str | None = None
        self.sidecar: str | None = None
        self._pickled: bytes | None = None
        self._fitted = False

    # -- identity ----------------------------------------------------------

    @property
    def fit_horizons(self) -> tuple[str, ...]:
        """The horizons a fit trains, the instance's own first."""
        out = [self.horizon] + [h for h in self.horizons if h != self.horizon]
        for h in out:
            if h not in VALID_HORIZONS:
                raise ValueError(f"{self.alpha_id}: unknown horizon {h!r}")
        return tuple(out)

    @property
    def is_dead(self) -> bool:
        return self.horizon not in self.models

    @property
    def hyperparams(self) -> dict[str, Any]:
        hp = dict(HYPERPARAMS[self.backend])
        if self.max_iter is not None:
            key = "max_iter" if self.backend == "hist_gbdt" else "n_estimators"
            hp[key] = int(self.max_iter)
        hp["random_state"] = int(self.seed)
        return hp

    # -- data ---------------------------------------------------------------

    def _matrix(self, df: pd.DataFrame) -> np.ndarray:
        missing = [c for c in self.features if c not in df.columns]
        if missing:
            raise ValueError(f"{self.alpha_id}: required feature columns missing: {missing[:5]}")
        return df.loc[:, list(self.features)].to_numpy(dtype=float)

    def _pooled(self, train: Mapping[int, pd.DataFrame]):
        xs, ts = [], []
        ys: dict[str, list[np.ndarray]] = {h: [] for h in self.fit_horizons}
        for iid in self.universe(list(train)):
            df = train[iid]
            if not len(df):
                continue
            x = self._matrix(df)
            keep = np.isfinite(x).any(axis=1)
            xs.append(x[keep])
            ts.append(df["exchange_ts"].to_numpy(dtype=np.int64)[keep])
            for h in self.fit_horizons:
                label_col, valid_col = f"label_mid_{h}", f"label_valid_{h}"
                if label_col not in df.columns:
                    raise ValueError(f"{self.alpha_id}: train frame {iid} lacks {label_col}")
                y = df[label_col].to_numpy(dtype=float)
                ok = np.isfinite(y) & df[valid_col].to_numpy(dtype=bool)
                ys[h].append(np.where(ok, y, np.nan)[keep])
        if not xs:
            return np.empty((0, len(self.features))), np.empty(0, dtype=np.int64), {}
        x = np.concatenate(xs)
        # A feature with no finite value on the fold (a window not yet
        # warmed up on a short synthetic fold) is a constant 0 for the fit:
        # no tree can split on it, so its value at scoring time is moot, and
        # sklearn 1.9's binner refuses an all-NaN column outright.
        all_nan = ~np.isfinite(x).any(axis=0)
        if all_nan.any():
            x = x.copy()
            x[:, all_nan] = 0.0
        t = np.concatenate(ts)
        order = np.argsort(t, kind="stable")
        return x[order], t[order], {h: np.concatenate(v)[order] for h, v in ys.items()}

    # -- fit / score ---------------------------------------------------------

    def fit(self, train: Mapping[int, pd.DataFrame]) -> None:
        x, t, ys = self._pooled(train)
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
        self.models, self.pred_sigma, self.sign_check, self.n_train_by_horizon = {}, {}, {}, {}
        self.model_sha256, self.sidecar, self._pickled = None, None, None
        for h in self.fit_horizons:
            y = ys.get(h, np.empty(0))
            ok = np.isfinite(y)
            n = int(ok.sum())
            self.n_train_by_horizon[h] = n
            if n < MIN_TRAIN_ROWS:
                self.sign_check[h] = None
                continue
            xh, yh, th = x[ok], y[ok], t[ok]
            self.sign_check[h] = self._sign_check(xh, yh, th, HORIZONS_NS[h])
            model = _make_backend(self.backend, self.max_iter, self.seed)
            model.fit(xh, yh)
            pred = np.asarray(model.predict(xh), dtype=float)
            sigma = float(np.std(pred))
            if not sigma > 0.0:
                self.sign_check[h] = None
                continue  # a constant model is a dead one
            self.models[h] = model
            self.pred_sigma[h] = sigma
        self.n_train = self.n_train_by_horizon.get(self.horizon, 0)
        if self.models:
            self._pickled = pickle.dumps(self.models, protocol=pickle.HIGHEST_PROTOCOL)
            self.model_sha256 = hashlib.sha256(self._pickled).hexdigest()
        self._fitted = True

    def _sign_check(self, x: np.ndarray, y: np.ndarray, t: np.ndarray, horizon_ns: int):
        """OOS IC of a model fitted on the first ``1 - sign_check_frac`` of the
        rows (by time, purged by the horizon) on the remaining slice."""
        frac = float(self.sign_check_frac)
        if not 0.0 < frac < 1.0:
            return None
        cut = t[int(np.floor(len(t) * (1.0 - frac)))]
        early = t + int(horizon_ns) < cut
        late = t >= cut
        if early.sum() < MIN_TRAIN_ROWS or late.sum() < MIN_TRAIN_ROWS:
            return None
        model = _make_backend(self.backend, self.max_iter, self.seed)
        model.fit(x[early], y[early])
        value = _ic(np.asarray(model.predict(x[late]), dtype=float), y[late])
        return None if not np.isfinite(value) else float(value)

    def score(self, data: Mapping[int, pd.DataFrame]) -> dict[int, pd.DataFrame]:
        if not self._fitted:
            raise RuntimeError(f"{self.alpha_id}: score() before fit()/load_params()")
        out: dict[int, pd.DataFrame] = {}
        model = self.models.get(self.horizon)
        sigma = self.pred_sigma.get(self.horizon, 0.0)
        for iid in self.universe(list(data)):
            df = data[iid]
            n = len(df)
            er = np.zeros(n)
            conf = np.zeros(n)
            if model is not None and n:
                x = self._matrix(df)
                ok = np.isfinite(x).any(axis=1)
                if ok.any():
                    pred = np.asarray(model.predict(x[ok]), dtype=float)
                    z = np.clip(pred / (sigma + EPS), -self.z_clip, self.z_clip)
                    er[ok] = z * sigma
                    conf[ok] = np.minimum(1.0, np.abs(z) / self.conf_scale)
            out[iid] = pd.DataFrame(
                {
                    "exchange_ts": df["exchange_ts"].to_numpy(),
                    "expected_return": er,
                    "confidence": conf,
                }
            )
        return out

    # -- params / sidecar -----------------------------------------------------

    def params(self) -> dict:
        dead = self.is_dead
        hyp = self.sign_check.get(self.horizon)
        return {
            "alpha_id": self.alpha_id,
            "model": "gbm_v1",
            "backend": self.backend,
            "horizon": self.horizon,
            "horizons": list(self.fit_horizons),
            "features": list(self.features),
            "hyperparams": self.hyperparams,
            "z_clip": self.z_clip,
            "conf_scale": self.conf_scale,
            # beta: the validation chain divides expected_return by it to get
            # the standardized signal; the clipped prediction IS the signal.
            "beta": 0.0 if dead else 1.0,
            "beta_fit": 0.0 if dead else 1.0,
            "hypothesis_confirmed": bool(hyp is not None and hyp > 0.0),
            "sign_check": dict(self.sign_check),
            "sign_check_frac": float(self.sign_check_frac),
            "pred_sigma": dict(self.pred_sigma),
            "n_train": int(self.n_train),
            "n_train_by_horizon": dict(self.n_train_by_horizon),
            "train_window": self.train_window,
            "model_sha256": self.model_sha256,
            "sidecar": self.sidecar,
            "dead": bool(dead),
            "fitted": self._fitted,
        }

    def save_sidecar(self, directory: str | Path | None = None) -> Path:
        """Write the pickled per-horizon models beside the parameters and
        record the path in ``params()["sidecar"]`` (relative to the repository
        root when under it, absolute otherwise).  The file name carries the
        digest, so a stale sidecar can never be read as a current one."""
        if not self._fitted:
            raise RuntimeError(f"{self.alpha_id}: save_sidecar() before fit()")
        if self._pickled is None or self.model_sha256 is None:
            raise RuntimeError(f"{self.alpha_id}: a dead fit has no models to save")
        directory = Path(directory) if directory is not None else _REPO / SIDECAR_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.alpha_id}_{self.model_sha256[:16]}.pkl"
        path.write_bytes(self._pickled)
        try:
            self.sidecar = path.resolve().relative_to(_REPO).as_posix()
        except ValueError:
            self.sidecar = str(path.resolve())
        return path

    def load_params(self, blob: dict) -> None:
        """Restore a fit from :meth:`params` output and its sidecar.

        Rejected: a foreign alpha_id or model, a horizon or feature list that
        differs from the instance, scoring constants that are not the pinned
        ones, a live fit without a sidecar, and a sidecar whose SHA-256 is not
        ``model_sha256`` (the digest is checked BEFORE anything is unpickled).
        """
        aid = self.alpha_id
        if blob.get("alpha_id") != aid:
            raise ValueError(f"params for {blob.get('alpha_id')!r} loaded into {aid}")
        if blob.get("model") != "gbm_v1":
            raise ValueError(f"{aid}: unsupported model {blob.get('model')!r}")
        horizon = blob.get("horizon")
        if horizon is not None and horizon != self.horizon:
            raise ValueError(
                f"{aid}: params horizon {horizon!r} != instance horizon {self.horizon!r}"
            )
        feats = blob.get("features")
        if feats is not None and tuple(feats) != tuple(self.features):
            raise ValueError(f"{aid}: params features differ from the class features")
        z_clip = float(blob.get("z_clip", self.Z_CLIP))
        conf_scale = float(blob.get("conf_scale", self.CONF_SCALE))
        if z_clip != self.Z_CLIP or conf_scale != self.CONF_SCALE:
            raise ValueError(
                f"{aid}: z_clip/conf_scale must be the pinned {self.Z_CLIP}/{self.CONF_SCALE}"
            )
        backend = str(blob.get("backend", self.backend))
        if backend not in BACKENDS:
            raise ValueError(f"{aid}: unknown backend {backend!r}")
        self.backend = backend
        self.z_clip, self.conf_scale = z_clip, conf_scale
        self.sign_check = {
            k: (None if v is None else float(v))
            for k, v in dict(blob.get("sign_check", {})).items()
        }
        self.pred_sigma = {k: float(v) for k, v in dict(blob.get("pred_sigma", {})).items()}
        self.n_train = int(blob.get("n_train", 0))
        self.n_train_by_horizon = {
            k: int(v) for k, v in dict(blob.get("n_train_by_horizon", {})).items()
        }
        tw = blob.get("train_window")
        self.train_window = dict(tw) if isinstance(tw, dict) else None
        self.models = {}
        self.model_sha256 = blob.get("model_sha256")
        self.sidecar = blob.get("sidecar")
        self._pickled = None
        if not blob.get("dead", False):
            if not self.sidecar or not self.model_sha256:
                raise ValueError(f"{aid}: a live fit needs a sidecar and its model_sha256")
            path = Path(self.sidecar)
            if not path.is_absolute():
                path = _REPO / path
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != self.model_sha256:
                raise ValueError(
                    f"{aid}: sidecar {path} digest {digest[:16]} != model_sha256 "
                    f"{str(self.model_sha256)[:16]}"
                )
            self.models = _SafeUnpickler(io.BytesIO(raw)).load()
            self._pickled = raw
            if self.horizon not in self.models or self.horizon not in self.pred_sigma:
                raise ValueError(f"{aid}: sidecar carries no model for horizon {self.horizon!r}")
        self._fitted = True


#: Modules a sidecar may reference (the three backends and their numerics).
_SAFE_PREFIXES = ("sklearn.", "numpy", "xgboost.", "lightgbm.", "builtins", "collections", "scipy.")


class _SafeUnpickler(pickle.Unpickler):
    """Unpickle a sidecar whose digest already matched, allowing only the
    learner libraries' classes (defence in depth; the digest is the gate)."""

    def find_class(self, module: str, name: str) -> Any:
        if module.startswith(_SAFE_PREFIXES) or module in ("copyreg", "_codecs"):
            return super().find_class(module, name)
        raise pickle.UnpicklingError(
            f"sidecar references {module}.{name}, outside the learner libraries"
        )


def ml_alpha_classes() -> dict[str, type]:
    """The opt-in ML alpha registry entries (id -> class)."""
    return {ML_ALPHA_ID: GbmAlpha}


__all__: Sequence[str] = (
    "BACKENDS",
    "GbmAlpha",
    "ML_ALPHA_ID",
    "SIDECAR_DIR",
    "ml_alpha_classes",
)

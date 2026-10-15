"""``linear_z_v2`` — per-instrument standardisation (v1.17 C3, review R-7).

:class:`iap.alpha.base.LinearAlpha` (model ``linear_z_v1``) pools ``mu`` /
``sigma`` of the raw signal across the universe.  On a real multi-name
dataset the most active name sets both numbers (R-7: QQQ sets every z and
the ``conf_min`` eligibility of the others), so a quiet name never reaches
``|z| >= conf_min`` and a loud one is always "confident".

``linear_z_v2`` standardises each instrument against ITSELF, causally: at
row ``t`` of instrument ``i``

    mu_i(t)    = mean(raw_i over rows with ts in (t - W, t])
    sigma_i(t) = std (raw_i over the same rows)
    z          = clip((raw_i(t) - mu_i(t)) / (sigma_i(t) + EPS), -z_clip, +z_clip)
    er         = beta * z ;  conf = min(1, |z| / conf_scale)

with ``W`` the declared standardisation window (:attr:`LinearAlphaV2.std_window`,
pinned per class, default ``"30m"``) over the instrument's OWN rows at or
before ``t`` — the window is half-open like every feature window
(``rolling.py``) and reads nothing after ``t``, so a row's z is bit-identical
whether or not the rows after it exist (the truncation probe).  A row with
fewer than :attr:`LinearAlphaV2.min_obs` finite raw values in its window, or
a zero window spread, has no z (``(er, conf) = (0.0, 0.0)``).

``beta`` is the OLS slope of the pinned-horizon mid label on the clipped z,
pooled across the universe: once every instrument's z is on its own scale,
pooling the slope is the thing that is comparable.  There is no global
``mu`` / ``sigma`` (the params carry ``mu = 0``, ``sigma = 1`` so a reader
of the v1 schema sees the identity standardisation); a dead alpha is
``beta == 0``.

This is a NEW model identity.  Nothing in :mod:`iap.alpha.base`,
:mod:`iap.alpha.equity` or :mod:`iap.alpha.fx` changes (their fingerprints,
which the open pre-registrations commit to, are untouched); the v2 classes
are built here from the registered ones (:func:`linear_z_v2`,
:data:`V2_ALPHA_CLASSES`) and carry their own ids (``EQ02_z2`` for ``EQ02``),
so a v2 run is a new ledger entry, never a silent rerun.  The ports do not
score ``linear_z_v2`` (``load_params`` rejects it everywhere but here), and
``alpha_params.json`` never carries it.  The pre-registration of the first
v2 study is ``research/preregs/linear_z_v2_prereg_DRAFT.json`` (DRAFT,
unsigned).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from iap.alpha.base import EPS, AlphaModel, LinearAlpha

__all__ = [
    "DEFAULT_MIN_OBS",
    "DEFAULT_STD_WINDOW",
    "MODEL_V2",
    "STD_WINDOWS_NS",
    "V2_ALPHA_CLASSES",
    "V2_ALPHA_IDS",
    "V2_SUFFIX",
    "LinearAlphaV2",
    "build_v2",
    "linear_z_v2",
    "rolling_standardise",
]

MODEL_V2 = "linear_z_v2"
V2_SUFFIX = "_z2"
#: Standardisation windows a v2 class may declare (event time).
STD_WINDOWS_NS: dict[str, int] = {
    "5m": 300 * 1_000_000_000,
    "15m": 900 * 1_000_000_000,
    "30m": 1_800 * 1_000_000_000,
    "1h": 3_600 * 1_000_000_000,
    "2h": 7_200 * 1_000_000_000,
}
DEFAULT_STD_WINDOW = "30m"
#: Fewer finite raw values than this inside the window is no scale.
DEFAULT_MIN_OBS = 32


def rolling_standardise(
    ts: np.ndarray, raw: np.ndarray, window_ns: int, min_obs: int = DEFAULT_MIN_OBS
) -> np.ndarray:
    """Causal per-row z of ``raw`` against its trailing ``(t - W, t]`` window.

    ``ts`` must be non-decreasing.  Entry ``i`` is ``(raw[i] - mean) /
    (std + EPS)`` over the finite ``raw`` values of rows ``j`` with
    ``ts[i] - W < ts[j] <= ts[i]`` (row ``i`` included); NaN where ``raw[i]``
    is not finite, fewer than ``min_obs`` finite values are in the window,
    or the window has no spread.  Prefix sums over the finite values (around
    the first finite value, so the sums do not cancel) make it O(n).
    """
    ts = np.asarray(ts, dtype=np.int64)
    x = np.asarray(raw, dtype=float)
    n = x.size
    out = np.full(n, np.nan)
    if n == 0:
        return out
    if np.any(np.diff(ts) < 0):
        raise ValueError("rolling_standardise: ts must be non-decreasing")
    finite = np.isfinite(x)
    if not finite.any():
        return out
    ref = float(x[finite][0])
    xc = np.where(finite, x - ref, 0.0)
    cnt = np.concatenate([[0], np.cumsum(finite.astype(np.int64))])
    s1 = np.concatenate([[0.0], np.cumsum(xc)])
    s2 = np.concatenate([[0.0], np.cumsum(xc * xc)])
    # first row index inside (t - W, t]: ts[j] > ts[i] - W
    start = np.searchsorted(ts, ts - int(window_ns), side="right")
    end = np.arange(1, n + 1)
    c = cnt[end] - cnt[start]
    ok = finite & (c >= max(int(min_obs), 1))
    if not ok.any():
        return out
    cc = c[ok].astype(float)
    m = (s1[end][ok] - s1[start][ok]) / cc
    v = (s2[end][ok] - s2[start][ok]) / cc - m * m
    sd = np.sqrt(np.maximum(v, 0.0))
    z = (xc[ok] - m) / (sd + EPS)
    z[~(sd > 0.0)] = np.nan
    out[ok] = z
    return out


class LinearAlphaV2(LinearAlpha):
    """Base of the ``linear_z_v2`` models (module docs).

    A concrete class is normally built from a registered ``linear_z_v1``
    class by :func:`linear_z_v2`; subclassing by hand works the same way
    (define :meth:`raw_signal`, the identity attributes and a docstring
    with an ``Economic rationale:`` section).
    """

    std_window: str = DEFAULT_STD_WINDOW
    min_obs: int = DEFAULT_MIN_OBS

    def __init__(self) -> None:
        super().__init__()
        if self.std_window not in STD_WINDOWS_NS:
            raise ValueError(
                f"{self.alpha_id}: std_window {self.std_window!r} not in {list(STD_WINDOWS_NS)}"
            )
        self.mu, self.sigma = 0.0, 1.0  # identity: the scale is per row

    @property
    def is_dead(self) -> bool:
        return self.beta == 0.0

    # -- the causal per-instrument z ------------------------------------

    def z_signals(self, data: Mapping[int, pd.DataFrame]) -> dict[int, np.ndarray]:
        """Per-instrument UNCLIPPED causal z (NaN = no opinion)."""
        w = STD_WINDOWS_NS[self.std_window]
        out: dict[int, np.ndarray] = {}
        for iid, sig in self.signals(data).items():
            ts = data[iid]["exchange_ts"].to_numpy(dtype=np.int64)
            out[iid] = rolling_standardise(ts, sig.to_numpy(dtype=float), w, self.min_obs)
        return out

    # -- fit / score ------------------------------------------------------

    def fit(self, train: Mapping[int, pd.DataFrame]) -> None:
        label_col = f"label_mid_{self.horizon}"
        valid_col = f"label_valid_{self.horizon}"
        zs: list[np.ndarray] = []
        ys: list[np.ndarray] = []
        for iid, z in self.z_signals(train).items():
            df = train[iid]
            if label_col not in df.columns:
                raise ValueError(f"{self.alpha_id}: train frame {iid} lacks {label_col}")
            y = df[label_col].to_numpy(dtype=float)
            ok = np.isfinite(z) & np.isfinite(y) & df[valid_col].to_numpy(dtype=bool)
            zs.append(z[ok])
            ys.append(y[ok])
        z = np.concatenate(zs) if zs else np.empty(0)
        y = np.concatenate(ys) if ys else np.empty(0)
        self.n_train = int(z.size)
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
        self.mu, self.sigma = 0.0, 1.0
        self.beta_fit = self.beta = 0.0
        self._fitted = True
        if z.size < DEFAULT_MIN_OBS:
            return
        zc = np.clip(z, -self.Z_CLIP, self.Z_CLIP)
        var = float(np.mean(zc * zc) - np.mean(zc) ** 2)
        if var > EPS:
            cov = float(np.mean(zc * y) - np.mean(zc) * np.mean(y))
            self.beta_fit = cov / var
        self.beta = self.beta_fit

    def _score(self, data: Mapping[int, pd.DataFrame], z_cap: float) -> dict[int, pd.DataFrame]:
        if not self._fitted:
            raise RuntimeError(f"{self.alpha_id}: score() before fit()/load_params()")
        dead = self.is_dead
        out: dict[int, pd.DataFrame] = {}
        for iid, zraw in self.z_signals(data).items():
            ok = np.isfinite(zraw) & (not dead)
            z = np.zeros(len(zraw))
            if not dead:
                z[ok] = np.clip(zraw[ok], -z_cap, z_cap)
            er = self.beta * z
            conf = np.minimum(1.0, np.abs(z) / self.conf_scale)
            conf[~ok] = 0.0
            er[~ok] = 0.0
            out[iid] = pd.DataFrame(
                {
                    "exchange_ts": data[iid]["exchange_ts"].to_numpy(),
                    "expected_return": er,
                    "confidence": conf,
                }
            )
        return out

    # -- params -----------------------------------------------------------

    def params(self) -> dict:
        p = super().params()
        p["model"] = MODEL_V2
        p["std_window"] = self.std_window
        p["std_window_ns"] = STD_WINDOWS_NS[self.std_window]
        p["min_obs"] = int(self.min_obs)
        p["mu"], p["sigma"] = 0.0, 1.0
        p["dead"] = bool(self.is_dead)
        return p

    def load_params(self, blob: dict) -> None:
        model = blob.get("model")
        if model != MODEL_V2:
            raise ValueError(f"{self.alpha_id}: unsupported model {model!r} (expected {MODEL_V2})")
        if blob.get("std_window", self.std_window) != self.std_window:
            raise ValueError(
                f"{self.alpha_id}: params std_window {blob.get('std_window')!r} "
                f"!= class std_window {self.std_window!r}"
            )
        v1 = {**blob, "model": "linear_z_v1", "mu": 0.0, "sigma": 1.0}
        super().load_params(v1)
        self.mu, self.sigma = 0.0, 1.0


def linear_z_v2(
    base: type[LinearAlpha],
    std_window: str = DEFAULT_STD_WINDOW,
    min_obs: int = DEFAULT_MIN_OBS,
) -> type[LinearAlphaV2]:
    """The ``linear_z_v2`` twin of a registered ``linear_z_v1`` class.

    Same raw signal, features, horizon and asset class; id ``<base id>_z2``
    (``std_window`` is part of the name when it is not the default:
    ``EQ02_z2_1h``); the economic rationale is inherited.  Cross-sectional
    classes have no per-instrument twin (``ValueError``).
    """
    if not (isinstance(base, type) and issubclass(base, LinearAlpha)):
        raise TypeError("linear_z_v2 needs a LinearAlpha subclass")
    if getattr(base, "cross_sectional", False):
        raise ValueError(f"{base.alpha_id}: a cross-sectional alpha has no per-instrument twin")
    if std_window not in STD_WINDOWS_NS:
        raise ValueError(f"std_window {std_window!r} not in {list(STD_WINDOWS_NS)}")
    if min_obs < 2:
        raise ValueError("min_obs must be >= 2")
    suffix = V2_SUFFIX if std_window == DEFAULT_STD_WINDOW else f"{V2_SUFFIX}_{std_window}"
    doc = (
        f"{base.name} under linear_z_v2 (per-instrument causal standardisation, "
        f"window {std_window}).\n\n{base.economic_rationale()}"
    )
    attrs = {
        "__doc__": doc,
        "__module__": __name__,
        "alpha_id": f"{base.alpha_id}{suffix}",
        "name": f"{base.name}_z2",
        "asset_class": base.asset_class,
        "horizon": base.horizon,
        "features": tuple(base.features),
        "std_window": std_window,
        "min_obs": int(min_obs),
        "base_alpha_id": base.alpha_id,
    }
    return type(f"{base.__name__}Z2", (LinearAlphaV2, base), attrs)


def _registered_v2() -> dict[str, type[LinearAlphaV2]]:
    from iap.alpha import ALPHA_CLASSES

    out: dict[str, type[LinearAlphaV2]] = {}
    for cls in ALPHA_CLASSES.values():
        if issubclass(cls, LinearAlpha) and not getattr(cls, "cross_sectional", False):
            v2 = linear_z_v2(cls)
            out[v2.alpha_id] = v2
    return out


#: The v2 twins of every per-instrument flagship alpha (EQ01..EQ10, EQ12,
#: FX01..FX04, FX07..FX12), keyed by their own ids.
V2_ALPHA_CLASSES: dict[str, type[LinearAlphaV2]] = _registered_v2()
V2_ALPHA_IDS: list[str] = list(V2_ALPHA_CLASSES)


def build_v2(alpha_id: str) -> AlphaModel:
    """A fresh ``linear_z_v2`` model: ``build_v2("EQ02_z2")`` or ``build_v2("EQ02")``."""
    key = alpha_id if alpha_id in V2_ALPHA_CLASSES else f"{alpha_id}{V2_SUFFIX}"
    if key not in V2_ALPHA_CLASSES:
        raise ValueError(f"no linear_z_v2 alpha for {alpha_id!r}")
    return V2_ALPHA_CLASSES[key]()

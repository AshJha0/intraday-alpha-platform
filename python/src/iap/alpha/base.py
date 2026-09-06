"""Alpha model interface (spec §§11-12, conventions §7).

Every flagship alpha is a class deriving from :class:`AlphaModel` with:

- a pinned ``alpha_id`` (EQ01..EQ12 / FX01..FX12) and human name;
- a REQUIRED class docstring containing an ``Economic rationale:`` section
  (enforced at subclass definition time — an alpha without a stated economic
  hypothesis cannot exist in this codebase, per spec §1);
- declared ``features`` (registry names the alpha is allowed to read — the
  validation harness verifies score() output is unchanged when every other
  feature column is masked, which is also the label-leakage column guard);
- a pinned prediction ``horizon`` (one of the 11 label horizons);
- ``fit(train)`` / ``score(data)`` over per-instrument feature frames.

Data convention: both fit and score take ``Mapping[int, pandas.DataFrame]``
(instrument_id -> feature frame as written by ``iap.features.__main__``:
``exchange_ts`` plus one float64 column per registry feature, NaN where
invalid, plus label columns used ONLY by fit/validation, never by scoring).
score returns ``Dict[int, DataFrame]`` row-aligned with the inputs, columns
``exchange_ts, expected_return, confidence``:

- ``expected_return``: fitted expected mid-to-mid return over ``horizon``
  (dimensionless, e.g. 1e-4 = 1 bp);
- ``confidence`` in [0, 1]; 0 whenever the underlying signal is invalid
  (expected_return is then exactly 0.0).

Fitting (LinearAlpha, the workhorse base): the subclass defines a *causal*
oriented raw signal; fit pools valid train rows across the universe and
estimates mu/sigma of the raw signal plus an OLS slope beta of the pinned
horizon's mid label on the clipped z-score.  The raw signal is ORIENTED by
the stated hypothesis; the fitted beta is free-signed (the fit determines
magnitude and sign).  ``hypothesis_confirmed`` (beta > 0, i.e. the data
agrees with the stated direction) is serialized with the parameters and is
a hard promotion gate in the research reports — an alpha whose fitted sign
contradicts its rationale can at best be ITERATE, never PROMOTE.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

EPS = 1e-12

#: pinned label horizons (must mirror iap.labels.HORIZONS_NS keys)
VALID_HORIZONS = (
    "10ms", "50ms", "100ms", "500ms", "1s", "5s", "10s", "30s", "1m", "5m", "15m",
)

#: pinned universes (configs/instruments.json)
EQ_IDS: Tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
EQ_CONSTITUENT_IDS: Tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
ETF_ID = 11
FX_IDS: Tuple[int, ...] = (101, 102, 103, 104, 105, 106, 107, 108)
FX_REF_ID = 101  # EUR/USD — cross-asset reference pair (features/context.py)


class AlphaModel(ABC):
    """Common interface for the 24 flagship alphas."""

    #: pinned identifiers — every concrete subclass overrides these
    alpha_id: str = ""
    name: str = ""
    asset_class: str = ""            # "EQUITY" | "FX"
    horizon: str = ""                # pinned label horizon
    features: Tuple[str, ...] = ()   # registry feature names read by score()
    cross_sectional: bool = False    # True: score needs the whole universe

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        import inspect
        if inspect.isabstract(cls) or not getattr(cls, "alpha_id", ""):
            return  # abstract/intermediate bases carry no alpha_id
        doc = inspect.getdoc(cls) or ""
        if "Economic rationale:" not in doc:
            raise TypeError(
                f"{cls.__name__}: alpha class docstring must contain an "
                "'Economic rationale:' section (spec §1 / conventions §7)"
            )

    # -- identity ---------------------------------------------------------

    @classmethod
    def economic_rationale(cls) -> str:
        """The alpha's stated economic hypothesis (from the class docstring)."""
        import inspect
        doc = inspect.getdoc(cls) or ""
        idx = doc.find("Economic rationale:")
        return doc[idx:] if idx >= 0 else doc

    def universe(self, instrument_ids: Sequence[int]) -> List[int]:
        """The subset of ``instrument_ids`` this alpha trades (sorted)."""
        base = EQ_IDS if self.asset_class == "EQUITY" else FX_IDS
        return sorted(i for i in instrument_ids if i in base)

    # -- fitting / scoring ------------------------------------------------

    @abstractmethod
    def fit(self, train: Mapping[int, pd.DataFrame]) -> None:
        """Fit parameters on training frames (labels may be read here only)."""

    @abstractmethod
    def score(self, data: Mapping[int, pd.DataFrame]) -> Dict[int, pd.DataFrame]:
        """Score frames -> per-instrument (exchange_ts, expected_return, confidence)."""

    # -- parameter serialization (configs/strategies/alpha_params.json) ---

    @abstractmethod
    def params(self) -> dict:
        """Fitted parameters as a JSON-serializable dict."""

    @abstractmethod
    def load_params(self, blob: dict) -> None:
        """Restore fitted parameters from :meth:`params` output."""

    def is_fitted(self) -> bool:
        try:
            return bool(self.params().get("fitted", False))
        except Exception:
            return False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.alpha_id} h={self.horizon}>"


class LinearAlpha(AlphaModel):
    """Base for alphas of the form ``expected_return = beta * clip(z)``.

    The subclass provides an oriented, causal raw signal via
    :meth:`raw_signal`; this base owns pooled standardization, the fitted
    linear scaling, confidence mapping and parameter (de)serialization.

    Pinned scoring semantics (mirrored by ports, see /API_ALPHA.md):

    ``z    = clip((raw - mu) / (sigma + EPS), -z_clip, +z_clip)``
    ``er   = beta * z``            (beta = fitted OLS slope, free-signed)
    ``conf = min(1, |z| / conf_scale)``; rows with NaN raw -> er 0, conf 0.

    **Dead alpha (pinned, round-3)**: when ``sigma <= 0`` or ``beta == 0``
    the fit found no usable evidence (fewer than 32 training pairs, or a
    degenerate signal).  EVERY row then scores ``(er, conf) = (0.0, 0.0)``.
    Without this rule ``sigma = 0`` made ``z = clip(x / 1e-12) = ±4`` and
    every row reported **confidence 1.0** with expected return 0 — maximum
    conviction in nothing, and the ports loaded such a file happily
    (``fitted: true``).

    ``z_clip`` / ``conf_scale`` are read from the parameter FILE (they are
    part of the port contract, API_ALPHA §2) and validated against the
    pinned constants, so the reference and the ports read one source of
    truth: a hand-edited ``z_clip`` is now rejected instead of silently
    breaking cross-language parity.
    """

    #: pinned scoring constants; a params file must carry exactly these
    Z_CLIP = 4.0
    CONF_SCALE = 2.0

    def __init__(self) -> None:
        self.mu: float = 0.0
        self.sigma: float = 0.0
        self.beta: float = 0.0
        self.beta_fit: float = 0.0
        self.n_train: int = 0
        self.z_clip: float = self.Z_CLIP
        self.conf_scale: float = self.CONF_SCALE
        self.train_window: Optional[Dict[str, int]] = None
        self._fitted = False

    @property
    def is_dead(self) -> bool:
        """True when the fit found no usable evidence (pinned scoring rule)."""
        return not (self.sigma > 0.0) or self.beta == 0.0

    # subclass hook ------------------------------------------------------

    @abstractmethod
    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        """Oriented causal raw signal for one instrument frame (NaN = invalid)."""

    def signals(self, data: Mapping[int, pd.DataFrame]) -> Dict[int, pd.Series]:
        """Raw signals for every universe instrument (cross-sectional alphas
        override this to compute jointly)."""
        out: Dict[int, pd.Series] = {}
        for iid in self.universe(list(data)):
            out[iid] = self.raw_signal(data[iid])
        return out

    # fit / score --------------------------------------------------------

    def fit(self, train: Mapping[int, pd.DataFrame]) -> None:
        label_col = f"label_mid_{self.horizon}"
        valid_col = f"label_valid_{self.horizon}"
        sigs = self.signals(train)
        xs: List[np.ndarray] = []
        ys: List[np.ndarray] = []
        for iid, sig in sigs.items():
            df = train[iid]
            if label_col not in df.columns:
                raise ValueError(f"{self.alpha_id}: train frame {iid} lacks {label_col}")
            x = sig.to_numpy(dtype=float)
            y = df[label_col].to_numpy(dtype=float)
            ok = (
                np.isfinite(x)
                & np.isfinite(y)
                & df[valid_col].to_numpy(dtype=bool)
            )
            xs.append(x[ok])
            ys.append(y[ok])
        x = np.concatenate(xs) if xs else np.empty(0)
        y = np.concatenate(ys) if ys else np.empty(0)
        self.n_train = int(x.size)
        spans = [
            (int(df["exchange_ts"].iloc[0]), int(df["exchange_ts"].iloc[-1]))
            for df in train.values() if len(df)
        ]
        self.train_window = (
            {"start_ts": min(a for a, _ in spans),
             "end_ts": max(b for _, b in spans)} if spans else None
        )
        if x.size < 32:
            # not enough evidence: dead alpha, honestly zero
            self.mu, self.sigma, self.beta_fit, self.beta = 0.0, 0.0, 0.0, 0.0
            self._fitted = True
            return
        self.mu = float(np.mean(x))
        self.sigma = float(np.std(x))
        z = np.clip((x - self.mu) / (self.sigma + EPS), -self.Z_CLIP, self.Z_CLIP)
        var = float(np.mean(z * z) - np.mean(z) ** 2)
        if var <= EPS:
            self.beta_fit = 0.0
        else:
            cov = float(np.mean(z * y) - np.mean(z) * np.mean(y))
            self.beta_fit = cov / var
        self.beta = self.beta_fit  # free-signed; hypothesis check via sign
        self._fitted = True

    def score(self, data: Mapping[int, pd.DataFrame]) -> Dict[int, pd.DataFrame]:
        if not self._fitted:
            raise RuntimeError(f"{self.alpha_id}: score() before fit()/load_params()")
        out: Dict[int, pd.DataFrame] = {}
        dead = self.is_dead
        for iid, sig in self.signals(data).items():
            df = data[iid]
            x = sig.to_numpy(dtype=float)
            ok = np.isfinite(x) & (not dead)
            z = np.zeros(len(x))
            if not dead:
                z[ok] = np.clip(
                    (x[ok] - self.mu) / (self.sigma + EPS),
                    -self.z_clip, self.z_clip,
                )
            er = self.beta * z
            conf = np.minimum(1.0, np.abs(z) / self.conf_scale)
            conf[~ok] = 0.0
            er[~ok] = 0.0
            out[iid] = pd.DataFrame(
                {
                    "exchange_ts": df["exchange_ts"].to_numpy(),
                    "expected_return": er,
                    "confidence": conf,
                }
            )
        return out

    # params -------------------------------------------------------------

    def params(self) -> dict:
        return {
            "alpha_id": self.alpha_id,
            "model": "linear_z_v1",
            "horizon": self.horizon,
            "features": list(self.features),
            "mu": self.mu,
            "sigma": self.sigma,
            "beta": self.beta,
            "beta_fit": self.beta_fit,
            "hypothesis_confirmed": bool(self.beta_fit > 0.0),
            "z_clip": self.z_clip,
            "conf_scale": self.conf_scale,
            "n_train": self.n_train,
            "train_window": self.train_window,
            "dead": bool(self.is_dead),
            "fitted": self._fitted,
        }

    def load_params(self, blob: dict) -> None:
        """Restore fitted parameters, validating the pinned port contract.

        Rejected (API_ALPHA §2): a foreign alpha_id, an unsupported model, a
        horizon or feature list that differs from the class, a `z_clip` /
        `conf_scale` that is not the pinned constant, a non-finite number,
        and `sigma <= 0` unless the file also says `beta == 0` (the only
        legal dead-alpha shape).
        """
        aid = self.alpha_id
        if blob.get("alpha_id") != aid:
            raise ValueError(
                f"params for {blob.get('alpha_id')!r} loaded into {aid}"
            )
        model = blob.get("model")
        if model != "linear_z_v1":
            raise ValueError(f"{aid}: unsupported model {model!r}")
        horizon = blob.get("horizon")
        if horizon is not None and horizon != self.horizon:
            raise ValueError(
                f"{aid}: params horizon {horizon!r} != class horizon "
                f"{self.horizon!r}")
        feats = blob.get("features")
        if feats is not None and tuple(feats) != tuple(self.features):
            raise ValueError(
                f"{aid}: params features {list(feats)} != class features "
                f"{list(self.features)}")
        z_clip = float(blob.get("z_clip", self.Z_CLIP))
        conf_scale = float(blob.get("conf_scale", self.CONF_SCALE))
        if z_clip != self.Z_CLIP or conf_scale != self.CONF_SCALE:
            raise ValueError(
                f"{aid}: z_clip/conf_scale must be the pinned "
                f"{self.Z_CLIP}/{self.CONF_SCALE} (got {z_clip}/{conf_scale})")
        mu = float(blob["mu"])
        sigma = float(blob["sigma"])
        beta = float(blob["beta"])
        beta_fit = float(blob.get("beta_fit", beta))
        for name, v in (("mu", mu), ("sigma", sigma), ("beta", beta),
                        ("beta_fit", beta_fit)):
            if not np.isfinite(v):
                raise ValueError(f"{aid}: non-finite {name}")
        if sigma <= 0.0 and beta != 0.0:
            raise ValueError(
                f"{aid}: sigma <= 0 is only legal for a dead alpha "
                f"(beta == 0); got sigma={sigma}, beta={beta}")
        self.mu, self.sigma, self.beta, self.beta_fit = mu, sigma, beta, beta_fit
        self.z_clip, self.conf_scale = z_clip, conf_scale
        self.n_train = int(blob.get("n_train", 0))
        tw = blob.get("train_window")
        self.train_window = dict(tw) if isinstance(tw, dict) else None
        self._fitted = True


def col(df: pd.DataFrame, name: str) -> pd.Series:
    """Fetch a feature column, raising a clear error when missing."""
    if name not in df.columns:
        raise ValueError(f"required feature column missing: {name}")
    return df[name]

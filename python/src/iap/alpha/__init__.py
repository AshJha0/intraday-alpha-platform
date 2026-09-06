"""Flagship alpha library — the 24 spec §§11-12 alphas behind one interface.

Public surface:

- :class:`~iap.alpha.base.AlphaModel` / :class:`~iap.alpha.base.LinearAlpha`
- ``ALPHA_CLASSES`` — pinned {alpha_id: class} registry (EQ01..FX12)
- ``build_all()`` / ``build(alpha_id)`` — fresh model instances
- ``fit_all(train)`` / ``save_params`` / ``load_params_file`` — pooled
  fitting and (de)serialization to configs/strategies/alpha_params.json
- currency-exposure helpers re-exported from ``iap.alpha.fx_exposure``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Mapping

import pandas as pd

from iap.alpha.base import (  # noqa: F401
    EQ_CONSTITUENT_IDS,
    EQ_IDS,
    ETF_ID,
    FX_IDS,
    FX_REF_ID,
    VALID_HORIZONS,
    AlphaModel,
    LinearAlpha,
)
from iap.alpha.cross_sectional import EQ11CrossSectionalReversal
from iap.alpha.equity import (
    EQ01Microprice,
    EQ02OfiL1,
    EQ03OfiMultiLevel,
    EQ04TradeFlow,
    EQ05QueueDynamics,
    EQ06Momentum,
    EQ07MeanReversion,
    EQ08VwapDeviation,
    EQ09ResidualReversion,
    EQ10IndexLeadLag,
    EQ12LiquidityConditionedOfi,
)
from iap.alpha.fx import (
    FX01QuoteImbalance,
    FX02TradeFlow,
    FX03MultiVenueOfi,
    FX04CrossVenueLeadLag,
    FX07FuturesSpotLeadLag,
    FX08LiquidityConditionedMomentum,
    FX09VolRegimeReversion,
    FX10SessionTransition,
    FX11MacroSurpriseResponse,
    FX12VenueToxicity,
)
from iap.alpha.fx_exposure import (  # noqa: F401
    CURRENCIES,
    FX05CrossPairRelativeValue,
    FX06CurrencyFactorMomentum,
    PAIR_CURRENCIES,
    currency_exposures,
    exposure_matrix,
    free_exposure_matrix,
    identified_pairs,
    solve_factor_returns,
)

#: pinned registry — ids exactly as named in spec §§11-12
ALPHA_CLASSES: Dict[str, type] = {
    "EQ01": EQ01Microprice,
    "EQ02": EQ02OfiL1,
    "EQ03": EQ03OfiMultiLevel,
    "EQ04": EQ04TradeFlow,
    "EQ05": EQ05QueueDynamics,
    "EQ06": EQ06Momentum,
    "EQ07": EQ07MeanReversion,
    "EQ08": EQ08VwapDeviation,
    "EQ09": EQ09ResidualReversion,
    "EQ10": EQ10IndexLeadLag,
    "EQ11": EQ11CrossSectionalReversal,
    "EQ12": EQ12LiquidityConditionedOfi,
    "FX01": FX01QuoteImbalance,
    "FX02": FX02TradeFlow,
    "FX03": FX03MultiVenueOfi,
    "FX04": FX04CrossVenueLeadLag,
    "FX05": FX05CrossPairRelativeValue,
    "FX06": FX06CurrencyFactorMomentum,
    "FX07": FX07FuturesSpotLeadLag,
    "FX08": FX08LiquidityConditionedMomentum,
    "FX09": FX09VolRegimeReversion,
    "FX10": FX10SessionTransition,
    "FX11": FX11MacroSurpriseResponse,
    "FX12": FX12VenueToxicity,
}

ALPHA_IDS: List[str] = list(ALPHA_CLASSES)


def build(alpha_id: str) -> AlphaModel:
    """A fresh unfitted instance of the given flagship alpha."""
    if alpha_id not in ALPHA_CLASSES:
        raise ValueError(f"unknown alpha_id {alpha_id!r}")
    return ALPHA_CLASSES[alpha_id]()


def build_all() -> Dict[str, AlphaModel]:
    return {aid: build(aid) for aid in ALPHA_IDS}


def fit_all(train: Mapping[int, pd.DataFrame]) -> Dict[str, AlphaModel]:
    """Fit every flagship alpha on the training frames (sorted id order)."""
    models = build_all()
    for aid in ALPHA_IDS:
        models[aid].fit(train)
    return models


#: alpha_params.json schema version (2 = provenance header, round-3).
PARAMS_VERSION = 2


def params_provenance() -> dict:
    """Provenance header for ``alpha_params.json`` (pinned, API_ALPHA §2).

    ``feature_version`` is the feature-registry hash the parameters were
    fitted against: a port that scores with a different registry is reading
    features whose semantics may have changed under the same name, so every
    loader rejects a mismatch.  ``data_version`` and the git fields identify
    the data and the code (see ``iap.experiment.tracker``).
    """
    from iap.experiment.tracker import data_version, git_status
    from iap.features.registry import registry_hash

    return {
        "feature_version": registry_hash(),
        "data_version": data_version(),
        **git_status(),
    }


def save_params(models: Mapping[str, AlphaModel], path) -> dict:
    """Serialize fitted parameters (configs/strategies/alpha_params.json)."""
    per = {aid: models[aid].params() for aid in sorted(models)}
    windows = [p["train_window"] for p in per.values() if p.get("train_window")]
    blob = {
        "x-version": PARAMS_VERSION,
        "description": (
            "Fitted flagship-alpha parameters (iap.alpha). model "
            "linear_z_v1: er = beta * clip((raw - mu)/(sigma + 1e-12), "
            "+-z_clip); conf = min(1, |z|/conf_scale); a dead alpha "
            "(sigma <= 0 or beta == 0) scores (0, 0) on every row. The "
            "provenance header identifies the feature registry, the data "
            "and the commit these numbers were fitted with; every loader "
            "rejects a feature_version that differs from its engine's "
            "registry hash. See /API_ALPHA.md section 2."
        ),
        **params_provenance(),
        "train_window": (
            {"start_ts": min(w["start_ts"] for w in windows),
             "end_ts": max(w["end_ts"] for w in windows)} if windows else None
        ),
        "params": per,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n")
    return blob


def load_params_file(path, expected_feature_version: str = "") -> Dict[str, AlphaModel]:
    """Build models and restore fitted parameters from save_params output.

    ``expected_feature_version`` defaults to the running engine's registry
    hash; pass ``None`` explicitly to skip the check (tooling that inspects a
    historic file).  A mismatch is an error, never a warning.
    """
    blob = json.loads(Path(path).read_text())
    if expected_feature_version == "":
        from iap.features.registry import registry_hash
        expected_feature_version = registry_hash()
    if expected_feature_version is not None:
        got = blob.get("feature_version")
        if got != expected_feature_version:
            raise ValueError(
                f"{path}: feature_version {got!r} does not match the engine's "
                f"registry hash {expected_feature_version!r} — the parameters "
                "were fitted against a different feature registry"
            )
    models: Dict[str, AlphaModel] = {}
    for aid, p in blob["params"].items():
        m = build(aid)
        m.load_params(p)
        models[aid] = m
    return models

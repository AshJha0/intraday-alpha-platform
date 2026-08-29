"""Model zoo: linear baselines, gradient-boosted trees, small MLP (spec §14).

Tiering is normative:

- **Tier 0 (baselines)**: OLS, Ridge, ElasticNet — always run.
- **Tier 1 (trees)**: XGBoost + LightGBM when importable (both install in
  this environment); sklearn HistGradientBoostingRegressor is the documented
  fallback if either import fails (conventions §10).
- **Tier 2 (MLP)**: small sklearn MLPRegressor.

Tier 1/2 models are *gated*: they are only trained after the linear baseline
establishes positive out-of-sample IC (the gate itself lives in
``iap.models.pipeline``; this module only declares which tier a model is in).

All hyperparameters are pinned here — they are recorded verbatim in every
run manifest.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import ElasticNet, LinearRegression, Ridge
from sklearn.neural_network import MLPRegressor

try:  # tree libraries: try/fallback per conventions §10
    from xgboost import XGBRegressor
    _HAS_XGB = True
except ImportError:  # pragma: no cover - env dependent
    _HAS_XGB = False

try:
    from lightgbm import LGBMRegressor
    _HAS_LGBM = True
except ImportError:  # pragma: no cover - env dependent
    _HAS_LGBM = False

_SEED = 7  # pinned; model-internal RNG only, never data ordering

#: Pinned hyperparameters (recorded in manifests).
HYPERPARAMS: Dict[str, Dict[str, Any]] = {
    "ols": {},
    "ridge": {"alpha": 10.0},
    "elasticnet": {"alpha": 1e-4, "l1_ratio": 0.5, "max_iter": 5000},
    "xgboost": {
        "n_estimators": 120, "max_depth": 4, "learning_rate": 0.08,
        "subsample": 0.9, "colsample_bytree": 0.8, "min_child_weight": 20,
        "tree_method": "hist", "n_jobs": 2, "random_state": _SEED,
    },
    "lightgbm": {
        "n_estimators": 120, "num_leaves": 15, "learning_rate": 0.08,
        "subsample": 0.9, "subsample_freq": 1, "colsample_bytree": 0.8,
        "min_child_samples": 40, "n_jobs": 2, "random_state": _SEED,
        "verbose": -1, "deterministic": True, "force_row_wise": True,
    },
    "hist_gbdt": {
        "max_iter": 120, "max_depth": 4, "learning_rate": 0.08,
        "min_samples_leaf": 40, "random_state": _SEED,
    },
    "mlp": {
        "hidden_layer_sizes": (32, 16), "activation": "relu",
        "solver": "adam", "learning_rate_init": 1e-3, "max_iter": 60,
        "batch_size": 4096, "random_state": _SEED, "early_stopping": False,
        "n_iter_no_change": 60,
    },
}

#: model name -> (tier, factory)
_FACTORIES: Dict[str, Tuple[int, Callable[[], Any]]] = {
    "ols": (0, lambda: LinearRegression()),
    "ridge": (0, lambda: Ridge(**HYPERPARAMS["ridge"])),
    "elasticnet": (0, lambda: ElasticNet(**HYPERPARAMS["elasticnet"])),
    "mlp": (2, lambda: MLPRegressor(**HYPERPARAMS["mlp"])),
}
if _HAS_XGB:
    _FACTORIES["xgboost"] = (1, lambda: XGBRegressor(**HYPERPARAMS["xgboost"]))
if _HAS_LGBM:
    _FACTORIES["lightgbm"] = (
        1, lambda: LGBMRegressor(**HYPERPARAMS["lightgbm"]))
if not (_HAS_XGB and _HAS_LGBM):  # pragma: no cover - env dependent
    _FACTORIES["hist_gbdt"] = (
        1, lambda: HistGradientBoostingRegressor(**HYPERPARAMS["hist_gbdt"]))

TREE_FALLBACK_ACTIVE = not (_HAS_XGB and _HAS_LGBM)


def model_names(tier: int) -> List[str]:
    """Pinned-order model names for a tier (0=linear, 1=trees, 2=mlp)."""
    order = ["ols", "ridge", "elasticnet", "xgboost", "lightgbm",
             "hist_gbdt", "mlp"]
    return [n for n in order if n in _FACTORIES and _FACTORIES[n][0] == tier]


def make_model(name: str) -> Any:
    """Instantiate a fresh, unfitted model by pinned name."""
    if name not in _FACTORIES:
        raise ValueError(f"unknown model {name!r}; have {sorted(_FACTORIES)}")
    return _FACTORIES[name][1]()


def model_tier(name: str) -> int:
    if name not in _FACTORIES:
        raise ValueError(f"unknown model {name!r}")
    return _FACTORIES[name][0]

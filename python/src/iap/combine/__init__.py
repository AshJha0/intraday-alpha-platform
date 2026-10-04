"""Signal combination: many weak signals into one forecast, fitted out of sample.

- ``weights`` — the four combination methods (``equal_weight``, the default
  and the baseline; ``ic_weighted``; ``ridge`` with the penalty chosen by
  nested CV inside the training window; ``shrinkage_mv`` on a Ledoit-Wolf
  covariance), the standardisation, the pairwise signal correlation and the
  effective number of independent bets.
- ``model``   — :class:`CombinedAlpha`, an ``AlphaModel`` whose inputs are
  alphas: weights fitted on the members' out-of-sample predictions inside
  each training window (stacking on purged, embargoed walk-forward folds).
- ``report``  — the combination evaluated as a first-class research object
  (``validate_alpha``, the multiple-testing ledger, the PROMOTE gates), the
  member pass and ``research/combination/``; the correlation document the
  lifecycle's ``cross_alpha_correlation`` gate reads.

CLI: ``python -m iap.research combine``.
"""

from iap.combine.model import (  # noqa: F401
    INNER_FOLDS,
    CombinedAlpha,
    combination_horizon,
    member_purged_train,
    member_signal,
)
from iap.combine.weights import (  # noqa: F401
    DEFAULT_METHOD,
    METHODS,
    RIDGE_PENALTIES,
    WeightFit,
    correlation_matrix,
    effective_bets,
    fit_weights,
    ledoit_wolf,
    standardise,
)

__all__ = [
    "DEFAULT_METHOD",
    "INNER_FOLDS",
    "METHODS",
    "RIDGE_PENALTIES",
    "CombinedAlpha",
    "WeightFit",
    "combination_horizon",
    "correlation_matrix",
    "effective_bets",
    "fit_weights",
    "ledoit_wolf",
    "member_purged_train",
    "member_signal",
    "standardise",
]

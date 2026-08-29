"""Research portfolio construction: PGD optimizer + diagnostics (spec §15)."""

from iap.portfolio.optimizer import Constraints, PGDResult, objective, solve
from iap.portfolio.covariance import ewma_covariance, bars_from_features
from iap.portfolio.fx import currency_exposure_matrix
from iap.portfolio.diagnostics import constraint_audit

__all__ = [
    "Constraints",
    "PGDResult",
    "objective",
    "solve",
    "ewma_covariance",
    "bars_from_features",
    "currency_exposure_matrix",
    "constraint_audit",
]

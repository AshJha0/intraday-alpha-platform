"""Research portfolio construction: PGD optimizer + diagnostics (spec §15)."""

from iap.portfolio.covariance import bars_from_features, ewma_covariance
from iap.portfolio.diagnostics import constraint_audit
from iap.portfolio.fx import currency_exposure_matrix
from iap.portfolio.optimizer import (
    Constraints,
    ImpactTerm,
    PGDResult,
    objective,
    prox_power,
    solve,
)

__all__ = [
    "Constraints",
    "PGDResult",
    "ImpactTerm",
    "prox_power",
    "objective",
    "solve",
    "ewma_covariance",
    "bars_from_features",
    "currency_exposure_matrix",
    "constraint_audit",
]

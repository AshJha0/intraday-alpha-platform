"""``iap.data``: data and feature contracts, the point-in-time universe and the leakage canaries (v2.0 U2).

- :mod:`iap.data.contracts`  declarative lineage contracts per dataset and feature,
  enforced at import; the causality (planted-leak) check
- :mod:`iap.data.pit`        point-in-time universe with as-of availability lag and
  delisting retention
- :mod:`iap.data.leakage`    the three canaries with positive controls
- :mod:`iap.data.synthetic`  the seeded canary panel and its contracted features

No network client and no vendor SDK is imported anywhere in this package
(``python/tests/test_import_policy.py`` guards it); the dataset contracts name
the source, the ingestion that reads it lives in ``tools/`` and runs as
``iap-runner`` (U0 section 2).
"""

from iap.data.contracts import (
    AvailabilityRule,
    ContractError,
    DatasetContract,
    FeatureContract,
    LeakageDetected,
    MissingContractError,
    check_causality,
    contract_of,
    dataset,
    enforce_module,
    feature,
    lineage,
)
from iap.data.pit import ConstituentFile, PITError, PointInTimeUniverse, RetainedName

__all__ = [
    "AvailabilityRule",
    "ConstituentFile",
    "ContractError",
    "DatasetContract",
    "FeatureContract",
    "LeakageDetected",
    "MissingContractError",
    "PITError",
    "PointInTimeUniverse",
    "RetainedName",
    "check_causality",
    "contract_of",
    "dataset",
    "enforce_module",
    "feature",
    "lineage",
]

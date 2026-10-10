"""Model operations (v1.11, AI3): a content-addressed model registry tied to
pre-registration ids, drift / calibration monitoring, and shadow mode.

* :mod:`iap.mlops.registry`   — immutable, versioned model artefacts;
* :mod:`iap.mlops.monitoring` — feature / prediction / calibration drift;
* :mod:`iap.mlops.shadow`     — candidate-vs-champion shadow runs.

Everything here is opt-in: no default trading or research path loads a
registered model unless the caller asks for one by id.
"""

from iap.mlops.monitoring import MonitorThresholds, calibration, monitor
from iap.mlops.registry import ModelRecord, ModelRegistry, RegistryError, TamperError
from iap.mlops.shadow import ShadowComparison, ShadowRunner, promotion_decision

__all__ = [
    "ModelRecord",
    "ModelRegistry",
    "MonitorThresholds",
    "RegistryError",
    "ShadowComparison",
    "ShadowRunner",
    "TamperError",
    "calibration",
    "monitor",
    "promotion_decision",
]

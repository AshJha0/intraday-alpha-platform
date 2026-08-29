"""iap.features — event-driven feature factory (conventions §6, spec §10).

Public surface:

- :mod:`iap.features.registry` — the pinned feature registry (205 features,
  10 families); ``registry_hash()`` is the FeatureVector ``feature_version``.
- :mod:`iap.features.engine` — incremental event-driven :class:`FeatureEngine`
  emitting :class:`FeatureVector` rows at a configurable cadence.
- :mod:`iap.features.context` — per-instrument static context (tick size,
  session bounds, cross-asset reference instrument) built from ``configs/``.
"""

from iap.features.engine import FeatureEngine, FeatureVector  # noqa: F401
from iap.features.registry import (  # noqa: F401
    build_registry,
    registry_hash,
    write_registry,
)

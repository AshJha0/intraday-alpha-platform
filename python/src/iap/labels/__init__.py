"""iap.labels — event-time forward labels (conventions §7, spec §13)."""

from iap.labels.labels import (  # noqa: F401
    HORIZON_ORDER,
    HORIZONS_NS,
    LABEL_MAX_AGE_FLOOR_NS,
    LabelReason,
    LabelResult,
    MidSeries,
    compute_labels,
    max_sample_age,
)

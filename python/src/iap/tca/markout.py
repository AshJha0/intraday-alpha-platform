"""Compatibility name for :mod:`iap.execution.markout` (v2.0 stage 11, S3).

The markout analysis (reference mid, markouts, effective and realised
half-spread, the gated timeline) moved under ``iap.execution`` so that
``iap.tca`` depends on ``iap.execution`` and never the other way round (the
``execution <-> tca`` import cycle is broken; ``tests/architecture/test_layering.py``
keeps it so).  Every public name is re-exported here unchanged, so
``iap.tca.markout.markout_report`` and the golden ``tests/golden/expected_markout.json``
are untouched.
"""

from __future__ import annotations

from iap.execution.markout import (  # noqa: F401
    DEFAULT_BUCKET_NS,
    DEFAULT_HORIZONS_NS,
    DEFAULT_MIN_FILLS,
    MAKER,
    TAKER,
    MarketTimeline,
    MarkoutFill,
    PassiveOrder,
    build_gated_timeline,
    fill_measures,
    markout_mid,
    markout_report,
    passive_order_stats,
    queue_bucket,
    reference_mid,
)

__all__ = [
    "DEFAULT_BUCKET_NS",
    "DEFAULT_HORIZONS_NS",
    "DEFAULT_MIN_FILLS",
    "MAKER",
    "TAKER",
    "MarketTimeline",
    "MarkoutFill",
    "PassiveOrder",
    "build_gated_timeline",
    "fill_measures",
    "markout_mid",
    "markout_report",
    "passive_order_stats",
    "queue_bucket",
    "reference_mid",
]

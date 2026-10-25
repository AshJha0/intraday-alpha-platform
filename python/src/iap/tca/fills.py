"""Compatibility name for :mod:`iap.execution.market_timeline` (v2.0 stage 11, S3).

The market timeline, child-fill and parent-order data model moved under
``iap.execution`` so that ``iap.tca`` depends on ``iap.execution`` and never
the other way round (the ``execution <-> tca`` import cycle is broken; the
architecture test ``tests/architecture/test_layering.py`` keeps it so).  Every
public name is re-exported here unchanged.
"""

from __future__ import annotations

from iap.execution.market_timeline import (  # noqa: F401
    MAKER,
    TAKER,
    Fill,
    MarketTimeline,
    ParentOrder,
    stamp_fill,
)

__all__ = ["MAKER", "TAKER", "Fill", "MarketTimeline", "ParentOrder", "stamp_fill"]

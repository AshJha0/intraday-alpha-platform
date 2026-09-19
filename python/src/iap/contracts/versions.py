"""Canonical JSON, content hashing and the pinned schema versions.

``canonical_json`` is the one serialisation used for every content hash on
the platform (experiment ids, config / portfolio versions, trace keys):
sorted keys, no whitespace, ASCII-only escapes, and **no NaN / Infinity** —
a non-finite float is a programming error, never data, so it raises instead
of producing a token no other JSON parser accepts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "ALPHA_SIGNAL_VERSION",
    "BOOK_UPDATE_VERSION",
    "CHILD_ORDER_VERSION",
    "DECISION_TRACE_VERSION",
    "EXECUTION_REPORT_VERSION",
    "EXPERIMENT_RESULT_VERSION",
    "EXPERIMENT_SPEC_VERSION",
    "FEATURE_VECTOR_VERSION",
    "LIFECYCLE_TRANSITION_VERSION",
    "MARKET_EVENT_VERSION",
    "ORDER_REQUEST_VERSION",
    "PARENT_ORDER_VERSION",
    "PORTFOLIO_TARGET_VERSION",
    "RISK_DECISION_VERSION",
    "RISK_EVENT_VERSION",
    "SCHEMA_BASE_URI",
    "SCHEMA_DIR",
    "SCHEMA_VERSIONS",
    "TCA_RESULT_VERSION",
    "VENUE_DECISION_VERSION",
    "canonical_json",
    "content_hash",
    "schema_dir",
    "schema_id",
]

#: Base of every schema ``$id`` (``schemas/README.md``).
SCHEMA_BASE_URI = "https://iap.example/schemas/"

# ``x-version`` of every schema under ``schemas/``.  A field change bumps the
# constant here, the schema file and adds a ``schemas/MIGRATIONS.md`` entry.
MARKET_EVENT_VERSION = 1
BOOK_UPDATE_VERSION = 1
FEATURE_VECTOR_VERSION = 1
ALPHA_SIGNAL_VERSION = 1
LIFECYCLE_TRANSITION_VERSION = 1
ORDER_REQUEST_VERSION = 1
PARENT_ORDER_VERSION = 1
CHILD_ORDER_VERSION = 1
EXECUTION_REPORT_VERSION = 1
VENUE_DECISION_VERSION = 1
RISK_EVENT_VERSION = 1
RISK_DECISION_VERSION = 1
PORTFOLIO_TARGET_VERSION = 1
TCA_RESULT_VERSION = 1
EXPERIMENT_SPEC_VERSION = 1
EXPERIMENT_RESULT_VERSION = 1
DECISION_TRACE_VERSION = 1

#: Schema path (relative to ``schemas/``) -> pinned ``x-version``.  This is
#: the complete inventory; the golden test asserts it equals the files.
SCHEMA_VERSIONS: Mapping[str, int] = MappingProxyType({
    "market/market_event.schema.json": MARKET_EVENT_VERSION,
    "market/book_update.schema.json": BOOK_UPDATE_VERSION,
    "features/feature_vector.schema.json": FEATURE_VECTOR_VERSION,
    "alpha/alpha_signal.schema.json": ALPHA_SIGNAL_VERSION,
    "alpha/lifecycle_transition.schema.json": LIFECYCLE_TRANSITION_VERSION,
    "order/order_request.schema.json": ORDER_REQUEST_VERSION,
    "order/parent_order.schema.json": PARENT_ORDER_VERSION,
    "order/child_order.schema.json": CHILD_ORDER_VERSION,
    "execution/execution_report.schema.json": EXECUTION_REPORT_VERSION,
    "execution/venue_decision.schema.json": VENUE_DECISION_VERSION,
    "risk/risk_event.schema.json": RISK_EVENT_VERSION,
    "risk/risk_decision.schema.json": RISK_DECISION_VERSION,
    "portfolio/portfolio_target.schema.json": PORTFOLIO_TARGET_VERSION,
    "tca/tca_result.schema.json": TCA_RESULT_VERSION,
    "research/experiment_spec.schema.json": EXPERIMENT_SPEC_VERSION,
    "research/experiment_result.schema.json": EXPERIMENT_RESULT_VERSION,
    "trace/decision_trace.schema.json": DECISION_TRACE_VERSION,
})


def schema_id(relpath: str) -> str:
    """The ``$id`` of the schema at ``relpath`` (relative to ``schemas/``)."""
    return SCHEMA_BASE_URI + relpath


def _default_schema_dir() -> Path:
    # python/src/iap/contracts/versions.py -> repo root is four levels up.
    return Path(__file__).resolve().parents[4] / "schemas"


def schema_dir() -> Path:
    """Resolve the repository ``schemas/`` directory.

    ``$IAP_SCHEMA_DIR`` overrides the checkout-relative default (for an
    installed wheel running outside the repository).  Raises when the
    directory does not exist: contracts are never validated against nothing.
    """
    override = os.environ.get("IAP_SCHEMA_DIR")
    path = Path(override).resolve() if override else _default_schema_dir()
    if not path.is_dir():
        raise RuntimeError(f"schema directory not found: {path}")
    return path


#: Checkout-relative ``schemas/`` path (may not exist for an installed wheel;
#: :func:`schema_dir` is the checked accessor).
SCHEMA_DIR: Path = _default_schema_dir()


def _reject_non_finite(obj: Any, path: str) -> None:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"canonical_json: non-finite float at {path}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"canonical_json: non-string key {k!r} at {path}")
            _reject_non_finite(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_non_finite(v, f"{path}[{i}]")


def canonical_json(obj: Any) -> str:
    """Canonical JSON text: sorted keys, ``(",", ":")`` separators, ASCII.

    Raises ``ValueError`` on NaN / ±Infinity anywhere in ``obj`` and on
    non-string dict keys (integer keys would be coerced silently by
    :func:`json.dumps` and hash differently across languages).
    """
    _reject_non_finite(obj, "$")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def content_hash(obj: Any) -> str:
    """SHA-256 hex of :func:`canonical_json` of ``obj``."""
    return hashlib.sha256(canonical_json(obj).encode("ascii")).hexdigest()

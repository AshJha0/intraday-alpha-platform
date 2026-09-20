"""Versioned, validated contracts for every stage of the platform loop.

Market Data -> Book -> Features -> Alpha -> Portfolio -> Risk -> Execution
-> SOR -> TCA -> Research.  Each stage has

* a JSON Schema under ``schemas/<domain>/`` (draft 2020-12, ``x-version``),
* a typed, frozen Python definition in :mod:`iap.contracts.types` whose
  ``to_dict`` / ``from_dict`` are exact inverses and whose dict validates
  against that schema (:mod:`iap.contracts.validate`),
* range-validated identifiers (:mod:`iap.contracts.ids`) and content
  hashing / canonical JSON (:mod:`iap.contracts.versions`),
* runtime-checkable interfaces (:mod:`iap.contracts.protocols`).

Nothing in this package reads the wall clock or an unordered container on a
deterministic path (PLATFORM_CONVENTIONS.md §3).
"""

from iap.contracts.ids import make_trace_id
from iap.contracts.types import DecisionTrace, explain
from iap.contracts.validate import validate, validate_typed
from iap.contracts.versions import canonical_json, content_hash

__all__ = [
    "DecisionTrace",
    "canonical_json",
    "content_hash",
    "explain",
    "make_trace_id",
    "validate",
    "validate_typed",
]

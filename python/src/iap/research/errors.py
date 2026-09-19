"""The research package's single error type."""

from __future__ import annotations

__all__ = ["ResearchError"]


class ResearchError(RuntimeError):
    """An experiment could not be specified, run or persisted honestly.

    Raised for an unknown alpha or horizon, an un-normalisable
    configuration, a period set that violates the walk-forward invariants,
    a metric the evidence chain could not compute (a NaN is never written
    into a result), a non-reproducible rerun and a corrupt document on
    disk.  Never raised for a *bad* result: a REJECT verdict is a valid,
    honest outcome.
    """

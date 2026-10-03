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

    ``code`` is a stable machine-readable identifier of the failure class
    (``python -m iap.research --json-errors`` prints it).  The pinned codes:

    ======================== ===============================================
    ``research_error``       any failure without a more specific class
    ``invalid_spec``         unknown alpha / horizon / configuration key,
                             bad value, inconsistent periods
    ``experiment_not_found`` no directory for the requested experiment id
    ``experiment_incomplete`` a directory missing ``spec.json`` / ``result.json``
    ``experiment_corrupt``   a document that fails validation or its id check
    ``not_reproducible``     a rerun produced different evidence under one id
    ``metric_not_computed``  the chain could not compute a required metric
    ``power_study_error``    the planted-signal power study could not run
    ======================== ===============================================

    Codes are additive: a new one may appear, an existing one never changes
    meaning.
    """

    def __init__(self, message: str = "", *, code: str = "research_error") -> None:
        super().__init__(message)
        self.code = code

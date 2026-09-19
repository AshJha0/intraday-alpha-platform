"""Contract-driven research: ``ExperimentSpec`` in, ``ExperimentResult`` out.

- ``specs``    — :func:`build_spec` (versions from the tracker, periods
  from the session calendar, normalised configuration, pinned experiment
  id), :func:`derive_periods`, :func:`normalise_configuration`.
- ``runner``   — :class:`ExperimentRunner` (the ``iap.contracts.protocols.
  ExperimentRunner`` implementation over ``iap.validation.validate_alpha``
  + the research backtester), :func:`build_result`.
- ``registry`` — :class:`ExperimentRegistry` over ``research/experiments/``.
- ``__main__`` — ``python -m iap.research run | list | show``.

Every artefact is deterministic: no wall clock, sorted keys, canonical
JSON; the same spec on the same data reproduces the same bytes (only the
``git_commit`` provenance and the ledger count may move between
checkouts).
"""

from iap.research.errors import ResearchError  # noqa: F401
from iap.research.registry import ExperimentRecord, ExperimentRegistry  # noqa: F401
from iap.research.runner import (  # noqa: F401
    LEDGER_KIND,
    LOOKS_PER_EXPERIMENT,
    ExperimentRunner,
    build_result,
)
from iap.research.specs import (  # noqa: F401
    DEFAULT_CONFIGURATION,
    DEFAULT_SEED,
    build_spec,
    derive_periods,
    experiment_id_of,
    model_definition_hash,
    normalise_configuration,
    pinned_horizon,
    verify_experiment_id,
)

__all__ = [
    "DEFAULT_CONFIGURATION",
    "DEFAULT_SEED",
    "LEDGER_KIND",
    "LOOKS_PER_EXPERIMENT",
    "ExperimentRecord",
    "ExperimentRegistry",
    "ExperimentRunner",
    "ResearchError",
    "build_result",
    "build_spec",
    "derive_periods",
    "experiment_id_of",
    "model_definition_hash",
    "normalise_configuration",
    "pinned_horizon",
    "verify_experiment_id",
]

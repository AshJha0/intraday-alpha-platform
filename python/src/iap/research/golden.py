"""The pinned golden experiment (conventions §5;
``tests/golden/expected_experiment_golden_frame.json``).

Both the generator (``python/tools/make_golden_research.py``) and the test
(``python/tests/test_research_golden.py``) build their inputs here, so
they cannot drift apart:

* **frames** — instrument 1 of the golden equity vector
  ``tests/golden/events_eq_mbo.jsonl`` replayed through the reference
  feature engine at cadence 0 (:func:`iap.alpha.goldenframes.build_golden_frame`):
  2 000 rows, one UTC session;
* **spec** — :data:`GOLDEN_ALPHA` at :data:`GOLDEN_HORIZON` with the
  default configuration.  The vector spans a single session, so the
  periods are pinned explicitly rather than derived: the holdout starts at
  row :data:`GOLDEN_TEST_ROW`, the validation period is the purge +
  embargo tail before it (the same rule :func:`~iap.research.specs.
  derive_periods` applies), training runs from the first row.
  ``dataset_version`` is the sha256 of the golden vector's bytes,
  ``feature_version`` the live registry hash — a changed vector or a
  changed feature definition changes the experiment id, which is the
  point;
* **result** — the runner's output on a fresh in-memory ledger (so
  ``n_experiments_in_ledger`` is exactly one experiment's looks) with the
  ``git_commit`` provenance replaced by :data:`GOLDEN_COMMIT`.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from iap.alpha.goldenframes import build_golden_frame
from iap.contracts.types import ExperimentResult, ExperimentSpec, Period
from iap.contracts.validate import validate_typed
from iap.features.registry import registry_hash
from iap.research.runner import ExperimentRunner, render_document
from iap.research.specs import DEFAULT_CONFIGURATION, build_spec
from iap.validation.metrics import HORIZONS_NS

__all__ = [
    "GOLDEN_ALPHA",
    "GOLDEN_COMMIT",
    "GOLDEN_EVENTS",
    "GOLDEN_HORIZON",
    "GOLDEN_INSTRUMENT",
    "GOLDEN_SEED",
    "GOLDEN_TEST_ROW",
    "GOLDEN_VERSION",
    "golden_document",
    "golden_frames",
    "golden_result",
    "golden_spec",
    "render_golden",
]

GOLDEN_VERSION = 1
GOLDEN_EVENTS = "events_eq_mbo.jsonl"
GOLDEN_INSTRUMENT = 1
GOLDEN_ALPHA = "EQ03"
GOLDEN_HORIZON = "5s"
#: Row (0-based) whose timestamp opens the holdout period.
GOLDEN_TEST_ROW = 1200
GOLDEN_SEED = 1
#: Provenance stand-in: the golden is a function of the vector, not a commit.
GOLDEN_COMMIT = "golden-fixture"


def golden_frames(golden_dir: Path, configs_dir: Path) -> Dict[int, pd.DataFrame]:
    """The golden instrument's feature + label frame."""
    frame = build_golden_frame(Path(golden_dir) / GOLDEN_EVENTS, configs_dir,
                               GOLDEN_INSTRUMENT)
    return {GOLDEN_INSTRUMENT: frame}


def golden_spec(golden_dir: Path, frames: Dict[int, pd.DataFrame]) -> ExperimentSpec:
    """The pinned spec over ``frames`` (see the module docs)."""
    ts = frames[GOLDEN_INSTRUMENT]["exchange_ts"].to_numpy()
    test_start = int(ts[GOLDEN_TEST_ROW])
    purge_start = (test_start - HORIZONS_NS[GOLDEN_HORIZON]
                   - int(DEFAULT_CONFIGURATION["embargo_ns"]))
    vector = (Path(golden_dir) / GOLDEN_EVENTS).read_bytes()
    return build_spec(
        GOLDEN_ALPHA, GOLDEN_HORIZON, {},
        dataset_version=hashlib.sha256(vector).hexdigest(),
        feature_version=registry_hash(),
        seed=GOLDEN_SEED,
        train_period=Period(start_ts=int(ts[0]), end_ts=purge_start),
        validation_period=Period(start_ts=purge_start, end_ts=test_start),
        test_period=Period(start_ts=test_start, end_ts=int(ts[-1]) + 1),
    )


def golden_result(spec: ExperimentSpec, frames: Dict[int, pd.DataFrame],
                  configs_dir: Path, scratch_dir: Path) -> ExperimentResult:
    """Run the golden spec on a fresh ledger under ``scratch_dir`` (nothing
    is written: the runner is a dry run) and pin the provenance."""
    runner = ExperimentRunner(
        None, Path(scratch_dir) / "experiments.json", Path(scratch_dir) / "experiments",
        configs_dir, dry_run=True, frames=frames,
    )
    result = runner.run(spec)
    return dataclasses.replace(result, git_commit=GOLDEN_COMMIT)


def golden_document(spec: ExperimentSpec, result: ExperimentResult) -> Dict[str, Any]:
    """The golden file's content (both documents schema-validated)."""
    return {
        "x-version": GOLDEN_VERSION,
        "description": (
            "Golden ExperimentRunner reproduction: the pinned spec run on the "
            f"golden equity vector ({GOLDEN_EVENTS}, instrument "
            f"{GOLDEN_INSTRUMENT}) through iap.research.ExperimentRunner. "
            "Floats compare at 1e-9 abs/rel, everything else exactly; "
            "git_commit is the fixed provenance stand-in."
        ),
        "source": {
            "events": GOLDEN_EVENTS,
            "instrument_id": GOLDEN_INSTRUMENT,
            "alpha_id": GOLDEN_ALPHA,
            "horizon": GOLDEN_HORIZON,
            "test_row_0based": GOLDEN_TEST_ROW,
            "seed": GOLDEN_SEED,
        },
        "spec": validate_typed(spec),
        "result": validate_typed(result),
    }


def render_golden(doc: Dict[str, Any]) -> str:
    """Golden file bytes (same canonical form as the persisted documents)."""
    return render_document(doc)

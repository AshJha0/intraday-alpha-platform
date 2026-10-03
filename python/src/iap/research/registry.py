"""``ExperimentRegistry`` — the read side of ``research/experiments/``.

One directory per experiment (``<experiment_id>/spec.json`` +
``result.json`` + ``eligibility.json``), exactly as
:class:`~iap.research.runner.ExperimentRunner` writes them.  Loading ONE
experiment is strict: both documents must validate against their schemas,
the spec's id must be the hash of its body and the result must belong to
that spec; anything else raises :class:`ResearchError`.

Listing is tolerant and loud (pinned).  ``records()`` yields every loadable
experiment and records each directory it could not load in
:attr:`ExperimentRegistry.skipped` — ``(directory name, reason)`` in name
order — instead of raising on the first one.  With several automated
writers a half-written or damaged directory is an expected condition, and
one such directory must not make every other experiment unreadable; but a
listing is still never *quietly* shorter than the folder: the caller is
handed exactly what was left out and why (the CLI prints it on stderr and
puts it in the ``--json`` document).  Staging directories
(``.staging-*``, a run being assembled) and other dot-directories are not
experiments and are ignored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional, Tuple

from iap.contracts.types import ExperimentResult, ExperimentSpec, Verdict
from iap.contracts.validate import validate_typed
from iap.research.errors import ResearchError
from iap.research.specs import GateEligibility, gate_eligibility, verify_experiment_id

__all__ = ["ExperimentRecord", "ExperimentRegistry"]

SPEC_FILE = "spec.json"
RESULT_FILE = "result.json"
ELIGIBILITY_FILE = "eligibility.json"


class ExperimentRecord(NamedTuple):
    """A persisted experiment: its spec and its result."""

    experiment_id: str
    spec: ExperimentSpec
    result: ExperimentResult


class ExperimentRegistry:
    """List / load / search the experiments persisted under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        #: directories the most recent ``records()`` / ``find()`` pass could
        #: not load: ``(directory name, reason)`` in name order
        self.skipped: List[Tuple[str, str]] = []

    def _directories(self) -> List[str]:
        """Every candidate experiment directory (sorted; dot-directories —
        staging areas — excluded)."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )

    def experiment_ids(self) -> List[str]:
        """Ids of every experiment directory (sorted; a directory is an
        experiment iff it holds a ``spec.json``)."""
        return [name for name in self._directories() if (self.root / name / SPEC_FILE).is_file()]

    def _read(self, experiment_id: str, name: str) -> dict:
        path = self.root / experiment_id / name
        if not path.is_file():
            raise ResearchError(f"{path}: missing", code="experiment_incomplete")
        try:
            doc = json.loads(path.read_text(encoding="ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ResearchError(
                f"{path}: not a canonical JSON document ({exc})", code="experiment_corrupt"
            ) from exc
        if not isinstance(doc, dict):
            raise ResearchError(f"{path}: expected a JSON object", code="experiment_corrupt")
        return doc

    def load_spec(self, experiment_id: str) -> ExperimentSpec:
        """The validated spec whose id matches its own body and directory."""
        doc = self._read(experiment_id, SPEC_FILE)
        try:
            spec = ExperimentSpec.from_dict(doc)
            validate_typed(spec)
        except ValueError as exc:
            raise ResearchError(
                f"{self.root / experiment_id / SPEC_FILE}: {exc}", code="experiment_corrupt"
            ) from exc
        verify_experiment_id(spec)
        if spec.experiment_id != experiment_id:
            raise ResearchError(
                f"directory {experiment_id!r} holds spec {spec.experiment_id!r}",
                code="experiment_corrupt",
            )
        return spec

    def load_result(self, experiment_id: str) -> ExperimentResult:
        """The validated result belonging to ``experiment_id``."""
        doc = self._read(experiment_id, RESULT_FILE)
        try:
            result = ExperimentResult.from_dict(doc)
            validate_typed(result)
        except ValueError as exc:
            raise ResearchError(
                f"{self.root / experiment_id / RESULT_FILE}: {exc}", code="experiment_corrupt"
            ) from exc
        if result.experiment_id != experiment_id:
            raise ResearchError(
                f"directory {experiment_id!r} holds result {result.experiment_id!r}",
                code="experiment_corrupt",
            )
        return result

    def load(self, experiment_id: str) -> ExperimentRecord:
        """Spec + result, cross-checked (same alpha, same versions)."""
        if not (self.root / experiment_id).is_dir():
            raise ResearchError(
                f"{self.root / experiment_id}: no such experiment", code="experiment_not_found"
            )
        spec = self.load_spec(experiment_id)
        result = self.load_result(experiment_id)
        for name in ("alpha_id", "dataset_version", "feature_version", "model_version"):
            if getattr(spec, name) != getattr(result, name):
                raise ResearchError(
                    f"{experiment_id}: spec.{name} != result.{name}", code="experiment_corrupt"
                )
        return ExperimentRecord(experiment_id, spec, result)

    def gate_eligibility(self, experiment_id: str) -> GateEligibility:
        """Whether the experiment's result may be used as gate evidence.

        The configuration bounds are always re-derived from the spec (they
        need nothing but the spec, so the sidecar cannot overrule them).
        The period check needs the dataset, so it is read from the
        ``eligibility.json`` the runner wrote; a directory without one (a
        run that predates the sidecar) is judged on its configuration alone
        and says so (``periods_verified`` false).  A malformed sidecar
        raises — an unreadable eligibility record is not an eligible one.
        """
        spec = self.load_spec(experiment_id)
        from_spec = gate_eligibility(spec)
        path = self.root / experiment_id / ELIGIBILITY_FILE
        if not path.is_file():
            return from_spec
        recorded = GateEligibility.from_dict(
            self._read(experiment_id, ELIGIBILITY_FILE), experiment_id
        )
        reasons = list(from_spec.reasons)
        reasons += [r for r in recorded.reasons if r not in reasons]
        return GateEligibility(
            eligible=not reasons, reasons=tuple(reasons), periods_verified=recorded.periods_verified
        )

    def records(self) -> Iterator[ExperimentRecord]:
        """Every LOADABLE experiment, in id order.  Directories that could
        not be loaded are skipped and reported in :attr:`skipped` (complete
        once the iterator is exhausted) — see the module docs."""
        self.skipped = []
        for name in self._directories():
            try:
                record = self.load(name)
            except ResearchError as exc:
                self.skipped.append((name, str(exc)))
                continue
            yield record

    def find(
        self,
        alpha_id: Optional[str] = None,
        horizon: Optional[str] = None,
        verdict: Optional[Verdict] = None,
    ) -> List[ExperimentRecord]:
        """Records matching every given filter (``None`` = any), in id order.
        Unloadable directories are in :attr:`skipped` afterwards."""
        want_verdict = Verdict(verdict) if verdict is not None else None
        return [
            rec
            for rec in self.records()
            if (alpha_id is None or rec.spec.alpha_id == alpha_id)
            and (horizon is None or rec.spec.horizon == horizon)
            and (want_verdict is None or rec.result.verdict is want_verdict)
        ]

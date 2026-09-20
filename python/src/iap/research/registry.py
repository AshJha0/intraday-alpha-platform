"""``ExperimentRegistry`` — the read side of ``research/experiments/``.

One directory per experiment (``<experiment_id>/spec.json`` +
``result.json``), exactly as :class:`~iap.research.runner.ExperimentRunner`
writes them.  Loading is strict: both documents must validate against
their schemas, the spec's id must be the hash of its body and the result
must belong to that spec.  A directory that fails any of these is reported
as corrupt rather than skipped, so a registry listing is never quietly
shorter than the folder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, List, NamedTuple, Optional

from iap.contracts.types import ExperimentResult, ExperimentSpec, Verdict
from iap.contracts.validate import validate_typed
from iap.research.errors import ResearchError
from iap.research.specs import verify_experiment_id

__all__ = ["ExperimentRecord", "ExperimentRegistry"]

SPEC_FILE = "spec.json"
RESULT_FILE = "result.json"


class ExperimentRecord(NamedTuple):
    """A persisted experiment: its spec and its result."""

    experiment_id: str
    spec: ExperimentSpec
    result: ExperimentResult


class ExperimentRegistry:
    """List / load / search the experiments persisted under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def experiment_ids(self) -> List[str]:
        """Ids of every experiment directory (sorted; a directory is an
        experiment iff it holds a ``spec.json``)."""
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and (p / SPEC_FILE).is_file())

    def _read(self, experiment_id: str, name: str) -> dict:
        path = self.root / experiment_id / name
        if not path.is_file():
            raise ResearchError(f"{path}: missing")
        try:
            doc = json.loads(path.read_text(encoding="ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ResearchError(f"{path}: not a canonical JSON document ({exc})") from exc
        if not isinstance(doc, dict):
            raise ResearchError(f"{path}: expected a JSON object")
        return doc

    def load_spec(self, experiment_id: str) -> ExperimentSpec:
        """The validated spec whose id matches its own body and directory."""
        doc = self._read(experiment_id, SPEC_FILE)
        try:
            spec = ExperimentSpec.from_dict(doc)
            validate_typed(spec)
        except ValueError as exc:
            raise ResearchError(f"{self.root / experiment_id / SPEC_FILE}: {exc}") from exc
        verify_experiment_id(spec)
        if spec.experiment_id != experiment_id:
            raise ResearchError(
                f"directory {experiment_id!r} holds spec {spec.experiment_id!r}")
        return spec

    def load_result(self, experiment_id: str) -> ExperimentResult:
        """The validated result belonging to ``experiment_id``."""
        doc = self._read(experiment_id, RESULT_FILE)
        try:
            result = ExperimentResult.from_dict(doc)
            validate_typed(result)
        except ValueError as exc:
            raise ResearchError(f"{self.root / experiment_id / RESULT_FILE}: {exc}") from exc
        if result.experiment_id != experiment_id:
            raise ResearchError(
                f"directory {experiment_id!r} holds result {result.experiment_id!r}")
        return result

    def load(self, experiment_id: str) -> ExperimentRecord:
        """Spec + result, cross-checked (same alpha, same versions)."""
        spec = self.load_spec(experiment_id)
        result = self.load_result(experiment_id)
        for name in ("alpha_id", "dataset_version", "feature_version", "model_version"):
            if getattr(spec, name) != getattr(result, name):
                raise ResearchError(
                    f"{experiment_id}: spec.{name} != result.{name}")
        return ExperimentRecord(experiment_id, spec, result)

    def records(self) -> Iterator[ExperimentRecord]:
        """Every experiment, in id order (fails on the first corrupt one)."""
        for experiment_id in self.experiment_ids():
            yield self.load(experiment_id)

    def find(
        self,
        alpha_id: Optional[str] = None,
        horizon: Optional[str] = None,
        verdict: Optional[Verdict] = None,
    ) -> List[ExperimentRecord]:
        """Records matching every given filter (``None`` = any), in id order."""
        want_verdict = Verdict(verdict) if verdict is not None else None
        return [
            rec for rec in self.records()
            if (alpha_id is None or rec.spec.alpha_id == alpha_id)
            and (horizon is None or rec.spec.horizon == horizon)
            and (want_verdict is None or rec.result.verdict is want_verdict)
        ]

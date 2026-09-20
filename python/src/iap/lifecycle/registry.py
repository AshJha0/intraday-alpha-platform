"""Persistent lifecycle state: the alpha registry and the transition log.

* :class:`AlphaRecord` — one alpha's lifecycle row: state, when it entered
  it, the last transition and gate evaluation, the versions of the research
  it was registered from, and the counters the machine needs to resume
  exactly (demotion failures, live breach / recovery counts).
* :class:`AlphaRegistry` — the records, persisted to
  ``research/alpha_registry.json`` (``x-version`` 1, sorted keys, 2-space
  indent, ASCII, trailing newline; identical state ⇒ identical bytes).
* :class:`GateEvaluation` — what one ``advance`` call evaluated, whether or
  not it moved the alpha (the machine's evaluations log; the latest one is
  stored on the record so ``status`` can show the failed gates).
* :class:`LifecycleTransitionLog` — append-only ``LifecycleTransition``
  records as canonical JSON lines (``research/lifecycle_transitions.jsonl``);
  every line is schema-validated on write and parsed back strictly on read.
  The adaptive study's ``research/lifecycle_log.jsonl`` is a different file
  with a different record and is left untouched.

No wall clock anywhere: every timestamp is the event time the caller passed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from iap.contracts.ids import is_generic_id, is_sha256_hex
from iap.contracts.types import GateResult, LifecycleState, LifecycleTransition
from iap.contracts.validate import validate_typed
from iap.contracts.versions import canonical_json

__all__ = [
    "REGISTRY_VERSION",
    "AlphaRecord",
    "AlphaRegistry",
    "GateEvaluation",
    "LifecycleTransitionLog",
    "Outcome",
]

#: ``x-version`` of ``research/alpha_registry.json``.
REGISTRY_VERSION = 1

_REGISTRY_DESCRIPTION = (
    "Alpha promotion lifecycle registry (iap.lifecycle). One record per alpha: "
    "current LifecycleState, the event time it was entered, the last transition "
    "and gate evaluation, the research versions it was registered from and the "
    "counters the machine resumes from. Deterministic and wall-clock free: an "
    "identical rerun of `python -m iap.lifecycle bootstrap` reproduces the bytes."
)


class Outcome:
    """Outcome codes of one gate evaluation (strings on the wire)."""

    TRANSITION = "TRANSITION"
    HOLD = "HOLD"
    NO_EVIDENCE = "NO_EVIDENCE"
    TERMINAL = "TERMINAL"
    ALL = (TRANSITION, HOLD, NO_EVIDENCE, TERMINAL)


def _check_optional_sha(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not is_sha256_hex(value):
        raise ValueError(f"{name}: expected a lowercase sha256 hex or None")
    return value


def _check_optional_id(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not is_generic_id(value):
        raise ValueError(f"{name}: {value!r} is not a valid identifier")
    return value


def _check_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}: expected a non-negative integer")
    return value


@dataclass(frozen=True)
class GateEvaluation:
    """One ``advance`` call: the gates it evaluated (edge order) and what
    it did.  ``gates`` is empty for ``NO_EVIDENCE`` / ``TERMINAL``."""

    alpha_id: str
    event_ts: int
    state: LifecycleState
    outcome: str
    gates: Dict[str, GateResult]
    consecutive_failures: int
    transition: Optional[LifecycleTransition]

    def __post_init__(self) -> None:
        if self.outcome not in Outcome.ALL:
            raise ValueError(f"GateEvaluation: unknown outcome {self.outcome!r}")
        if (self.outcome == Outcome.TRANSITION) != (self.transition is not None):
            raise ValueError("GateEvaluation: TRANSITION outcome iff a transition")
        _check_count(self.consecutive_failures, "GateEvaluation.consecutive_failures")

    @property
    def passed(self) -> bool:
        """True when every evaluated gate passed (vacuously true with none)."""
        return all(g.passed for g in self.gates.values())

    @property
    def failed_gates(self) -> List[str]:
        """Names of the failed gates in evaluation order."""
        return [name for name, g in self.gates.items() if not g.passed]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready.  ``gate_order`` carries the pinned evaluation order,
        which a sorted-key serialisation of ``gates`` would lose."""
        return {
            "alpha_id": self.alpha_id,
            "event_ts": self.event_ts,
            "state": self.state.name,
            "outcome": self.outcome,
            "gate_order": list(self.gates),
            "gates": {name: g.to_dict() for name, g in self.gates.items()},
            "failed_gates": self.failed_gates,
            "consecutive_failures": self.consecutive_failures,
            "transition": (None if self.transition is None
                           else self.transition.to_dict()),
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "GateEvaluation":
        order = list(data["gate_order"])
        if sorted(order) != sorted(data["gates"]) or len(set(order)) != len(order):
            raise ValueError("GateEvaluation: gate_order does not match gates")
        transition = data["transition"]
        evaluation = GateEvaluation(
            alpha_id=str(data["alpha_id"]),
            event_ts=int(data["event_ts"]),
            state=LifecycleState[data["state"]],
            outcome=str(data["outcome"]),
            gates={name: GateResult.from_dict(data["gates"][name]) for name in order},
            consecutive_failures=int(data["consecutive_failures"]),
            transition=(None if transition is None
                        else LifecycleTransition.from_dict(transition)),
        )
        if evaluation.failed_gates != list(data["failed_gates"]):
            raise ValueError("GateEvaluation: failed_gates does not match gates")
        return evaluation


@dataclass
class AlphaRecord:
    """One alpha's lifecycle row (see module docstring).  Mutated only by
    :class:`iap.lifecycle.machine.AlphaLifecycle`."""

    alpha_id: str
    state: LifecycleState
    since_ts: int
    last_transition: Optional[LifecycleTransition]
    last_evaluation: Optional[GateEvaluation]
    experiment_id: Optional[str]
    data_version: Optional[str]
    feature_version: Optional[str]
    model_version: Optional[str]
    consecutive_failures: int
    breach_count: int
    recovery_count: int

    def __post_init__(self) -> None:
        if not is_generic_id(self.alpha_id):
            raise ValueError(f"AlphaRecord: {self.alpha_id!r} is not a valid alpha id")
        if not isinstance(self.state, LifecycleState):
            raise ValueError("AlphaRecord.state must be a LifecycleState")
        if isinstance(self.since_ts, bool) or not isinstance(self.since_ts, int):
            raise ValueError("AlphaRecord.since_ts must be an int")
        self.experiment_id = _check_optional_id(self.experiment_id, "AlphaRecord.experiment_id")
        self.data_version = _check_optional_sha(self.data_version, "AlphaRecord.data_version")
        self.feature_version = _check_optional_sha(self.feature_version,
                                                   "AlphaRecord.feature_version")
        self.model_version = _check_optional_sha(self.model_version, "AlphaRecord.model_version")
        for name in ("consecutive_failures", "breach_count", "recovery_count"):
            _check_count(getattr(self, name), f"AlphaRecord.{name}")

    @staticmethod
    def new(alpha_id: str, since_ts: int, *, experiment_id: Optional[str] = None,
            data_version: Optional[str] = None, feature_version: Optional[str] = None,
            model_version: Optional[str] = None) -> "AlphaRecord":
        """A freshly registered alpha: RESEARCH, no history, zero counters."""
        return AlphaRecord(
            alpha_id=alpha_id, state=LifecycleState.RESEARCH, since_ts=since_ts,
            last_transition=None, last_evaluation=None, experiment_id=experiment_id,
            data_version=data_version, feature_version=feature_version,
            model_version=model_version, consecutive_failures=0,
            breach_count=0, recovery_count=0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "state": self.state.name,
            "state_index": int(self.state),
            "since_ts": self.since_ts,
            "last_transition": (None if self.last_transition is None
                                else self.last_transition.to_dict()),
            "last_evaluation": (None if self.last_evaluation is None
                                else self.last_evaluation.to_dict()),
            "experiment_id": self.experiment_id,
            "data_version": self.data_version,
            "feature_version": self.feature_version,
            "model_version": self.model_version,
            "consecutive_failures": self.consecutive_failures,
            "breach_count": self.breach_count,
            "recovery_count": self.recovery_count,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "AlphaRecord":
        state = LifecycleState[data["state"]]
        if int(data["state_index"]) != int(state):
            raise ValueError(f"AlphaRecord {data['alpha_id']}: state_index disagrees with state")
        lt = data["last_transition"]
        le = data["last_evaluation"]
        return AlphaRecord(
            alpha_id=str(data["alpha_id"]),
            state=state,
            since_ts=int(data["since_ts"]),
            last_transition=None if lt is None else LifecycleTransition.from_dict(lt),
            last_evaluation=None if le is None else GateEvaluation.from_dict(le),
            experiment_id=data["experiment_id"],
            data_version=data["data_version"],
            feature_version=data["feature_version"],
            model_version=data["model_version"],
            consecutive_failures=int(data["consecutive_failures"]),
            breach_count=int(data["breach_count"]),
            recovery_count=int(data["recovery_count"]),
        )


class AlphaRegistry:
    """The set of :class:`AlphaRecord` rows, keyed by alpha id, with a
    byte-deterministic JSON persistence."""

    def __init__(self, policy: str) -> None:
        if not policy:
            raise ValueError("AlphaRegistry: policy name must not be empty")
        self.policy = policy
        self._records: Dict[str, AlphaRecord] = {}

    def __contains__(self, alpha_id: str) -> bool:
        return alpha_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def alpha_ids(self) -> List[str]:
        """Sorted alpha ids (the only iteration order the registry offers)."""
        return sorted(self._records)

    def get(self, alpha_id: str) -> AlphaRecord:
        try:
            return self._records[alpha_id]
        except KeyError:
            raise KeyError(f"unknown alpha {alpha_id!r}") from None

    def add(self, record: AlphaRecord) -> AlphaRecord:
        if record.alpha_id in self._records:
            raise ValueError(f"alpha {record.alpha_id!r} is already registered")
        self._records[record.alpha_id] = record
        return record

    def records(self) -> List[AlphaRecord]:
        """Records in sorted alpha-id order."""
        return [self._records[a] for a in self.alpha_ids()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "x-version": REGISTRY_VERSION,
            "description": _REGISTRY_DESCRIPTION,
            "policy": self.policy,
            "alphas": {a: self._records[a].to_dict() for a in self.alpha_ids()},
        }

    def render(self) -> str:
        """The exact file text: sorted keys, 2-space indent, ASCII, one
        trailing newline."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2,
                          ensure_ascii=True, allow_nan=False) + "\n"

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")

    @staticmethod
    def from_dict(doc: Mapping[str, Any]) -> "AlphaRegistry":
        if doc.get("x-version") != REGISTRY_VERSION:
            raise ValueError(f"alpha registry: x-version {doc.get('x-version')!r} "
                             f"!= {REGISTRY_VERSION}")
        registry = AlphaRegistry(str(doc["policy"]))
        for alpha_id in sorted(doc["alphas"]):
            record = AlphaRecord.from_dict(doc["alphas"][alpha_id])
            if record.alpha_id != alpha_id:
                raise ValueError(f"alpha registry: key {alpha_id!r} != record "
                                 f"{record.alpha_id!r}")
            registry.add(record)
        return registry

    @staticmethod
    def load(path: Path) -> "AlphaRegistry":
        with open(path, "r", encoding="utf-8") as fh:
            return AlphaRegistry.from_dict(json.load(fh))


class LifecycleTransitionLog:
    """Append-only canonical-JSON-lines log of :class:`LifecycleTransition`."""

    def __init__(self, path: Path, truncate: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if truncate or not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def append(self, transition: LifecycleTransition) -> None:
        """Validate against the schema and append one canonical line."""
        line = canonical_json(validate_typed(transition))
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self) -> List[LifecycleTransition]:
        """Every logged transition, in file order, strictly parsed."""
        out: List[LifecycleTransition] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(LifecycleTransition.from_dict(json.loads(line)))
        return out

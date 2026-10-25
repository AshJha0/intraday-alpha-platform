"""EN1: the content-addressed forecast registry (v2.0 U0 stage 5; plan section 5 "model registry").

A :class:`RegistryEntry` describes what produced a family's forecasts: the
model class and its parameters, the feature set (by the hash of its contracts,
:func:`iap.data.contracts.lineage`), the training window and dataset identity,
software versions, the selection history (how many fits preceded this one) and
an approval status.  Its identity is the SHA-256 of its canonical JSON
(:func:`iap.contracts.versions.content_hash`): the same declaration always has
the same id, a one-character change has another, and a
:class:`~iap.alpha.forecast.ForecastRecord` names the entry by that id.

The registry is append-only.  :meth:`ForecastRegistry.register` returns the id
(re-registering an identical entry is a no-op; an entry that hashes to an id
already held by a *different* document cannot happen, and is refused if the
file was tampered with); :meth:`save` writes one JSON document with the entries
in id order; :meth:`load` recomputes every hash and refuses the file if any
entry's stored id does not match its content.  Nothing here touches the
network or a wall clock: timestamps and versions are declared by the caller.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from iap.contracts.versions import canonical_json, content_hash

__all__ = ["APPROVAL_STATES", "ForecastRegistry", "RegistryEntry", "RegistryError"]

#: Plan section 5: the registry stores an approval status per artefact.
APPROVAL_STATES = frozenset({"development", "frozen", "registered", "retired"})
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "iap.alpha.registry/v1"


class RegistryError(ValueError):
    """A registry entry is malformed, or the registry file is inconsistent."""


def _check_json_value(value: Any, where: str) -> None:
    """Parameters must be JSON values with finite numbers (canonical JSON rejects NaN anyway)."""
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RegistryError(f"{where}: non-finite number")
        return
    if isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _check_json_value(v, f"{where}[{i}]")
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise RegistryError(f"{where}: non-string key {k!r}")
            _check_json_value(v, f"{where}.{k}")
        return
    raise RegistryError(f"{where}: {type(value).__name__} is not a JSON value")


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """What produced a family's forecasts (module docstring).  All fields are declared, none inferred."""

    family_id: str
    horizon: str
    model_class: str
    parameters: dict[str, Any]
    feature_set_hash: str
    training_window: tuple[str, str]
    dataset_identity: str
    software_version: str
    selection_history: dict[str, Any] = field(default_factory=dict)
    approval: str = "development"
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.family_id, str) or not _ID.match(self.family_id):
            raise RegistryError(f"family_id must match {_ID.pattern!r}")
        from iap.alpha.forecast import ForecastError, validate_horizon

        try:
            validate_horizon(self.horizon)
        except ForecastError as exc:
            raise RegistryError(str(exc)) from None
        if not isinstance(self.model_class, str) or not self.model_class.strip():
            raise RegistryError("model_class must be a non-empty string")
        if not isinstance(self.parameters, Mapping):
            raise RegistryError("parameters must be a mapping")
        _check_json_value(dict(self.parameters), "parameters")
        object.__setattr__(self, "parameters", dict(self.parameters))
        if not isinstance(self.feature_set_hash, str) or not _SHA256.match(self.feature_set_hash):
            raise RegistryError(
                "feature_set_hash must be a 64-hex sha256 (hash of the feature contracts' lineage)"
            )
        tw = self.training_window
        if (
            not isinstance(tw, list | tuple)
            or len(tw) != 2
            or not all(isinstance(x, str) and x for x in tw)
        ):
            raise RegistryError("training_window must be (start, end) ISO strings")
        if tw[0] > tw[1]:
            raise RegistryError("training_window start is after its end")
        object.__setattr__(self, "training_window", (tw[0], tw[1]))
        if not isinstance(self.dataset_identity, str) or not self.dataset_identity.strip():
            raise RegistryError("dataset_identity must name the dataset version or manifest hash")
        if not isinstance(self.software_version, str) or not self.software_version.strip():
            raise RegistryError("software_version must be a non-empty string (commit or release)")
        if not isinstance(self.selection_history, Mapping):
            raise RegistryError("selection_history must be a mapping")
        _check_json_value(dict(self.selection_history), "selection_history")
        object.__setattr__(self, "selection_history", dict(self.selection_history))
        if self.approval not in APPROVAL_STATES:
            raise RegistryError(f"approval must be one of {sorted(APPROVAL_STATES)}")
        if not isinstance(self.notes, str):
            raise RegistryError("notes must be a string")

    def to_dict(self) -> dict[str, Any]:
        doc = {f.name: getattr(self, f.name) for f in fields(self)}
        doc["training_window"] = list(self.training_window)
        return doc

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> RegistryEntry:
        names = {f.name for f in fields(cls)}
        missing = sorted(names - set(doc))
        extra = sorted(set(doc) - names)
        if missing or extra:
            raise RegistryError(f"registry entry refused: missing {missing}, unexpected {extra}")
        return cls(**{k: doc[k] for k in names})

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def registry_id(self) -> str:
        """The content hash: SHA-256 of the canonical JSON of :meth:`to_dict`."""
        return content_hash(self.to_dict())


class ForecastRegistry:
    """Append-only, content-addressed store of :class:`RegistryEntry` (module docstring)."""

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}

    # ------------------------------------------------------------------ mutation
    def register(self, entry: RegistryEntry) -> str:
        if not isinstance(entry, RegistryEntry):
            raise RegistryError("register() takes a RegistryEntry")
        rid = entry.registry_id
        prior = self._entries.get(rid)
        if prior is not None:
            if prior != entry:  # pragma: no cover - a sha256 collision
                raise RegistryError(f"registry id {rid} already holds a different entry")
            return rid
        self._entries[rid] = entry
        return rid

    # ------------------------------------------------------------------ queries
    def contains(self, registry_id: str) -> bool:
        return registry_id in self._entries

    def get(self, registry_id: str) -> RegistryEntry:
        try:
            return self._entries[registry_id]
        except KeyError:
            raise RegistryError(f"unknown registry id {registry_id!r}") from None

    def ids(self) -> list[str]:
        return sorted(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[RegistryEntry]:
        return (self._entries[k] for k in self.ids())

    def by_family(self, family_id: str, horizon: str | None = None) -> list[RegistryEntry]:
        return [
            e
            for e in self
            if e.family_id == family_id and (horizon is None or e.horizon == horizon)
        ]

    def verify(self) -> None:
        """Every stored id equals the content hash of its entry (tamper check)."""
        for rid, e in self._entries.items():
            if e.registry_id != rid:
                raise RegistryError(f"registry entry {rid[:12]}... does not hash to its id")

    @property
    def digest(self) -> str:
        """SHA-256 over the sorted ids: the identity of the whole registry (the programme index records it)."""
        return content_hash(self.ids())

    # ------------------------------------------------------------------ persistence
    def to_document(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "digest": self.digest,
            "entries": [
                {"registry_id": rid, "entry": self._entries[rid].to_dict()} for rid in self.ids()
            ],
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(canonical_json(self.to_document()) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> ForecastRegistry:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            raise RegistryError(f"not a {SCHEMA} document")
        reg = cls()
        for row in doc.get("entries", []):
            entry = RegistryEntry.from_dict(row["entry"])
            if entry.registry_id != row.get("registry_id"):
                raise RegistryError(
                    f"registry file refused: entry stored under {str(row.get('registry_id'))[:12]}... hashes to "
                    f"{entry.registry_id[:12]}..."
                )
            reg._entries[entry.registry_id] = entry
        if doc.get("digest") != reg.digest:
            raise RegistryError("registry file refused: digest does not match its entries")
        return reg

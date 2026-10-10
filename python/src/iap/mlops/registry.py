"""Content-addressed, immutable model registry (v1.11, AI3).

A registered model is a directory ``<root>/<model_id>/`` holding

* ``model.joblib`` — the fitted estimator (joblib pickle);
* ``record.json``  — the :class:`ModelRecord` (canonical JSON).

**The id (pinned).**  ``model_id = sha256(canonical(identity))`` where
``identity`` is::

    {"artefact_sha256", "params", "dataset_version", "date_range",
     "features", "feature_registry_hash", "code_hash", "seed"}

``artefact_sha256`` is the SHA-256 of the joblib bytes, so two fits that
produce the same estimator on the same data with the same code and seed get
the same id, and a different artefact always gets a different id.  Training
metrics, the pre-registration link and the parent are recorded (and covered
by ``record_hash``) but are not part of the identity.

**Pre-registration.**  :meth:`ModelRegistry.register` refuses a model
without a pre-registration entry (a blackboard ``prereg`` entry, e.g. from
:func:`iap.agents.prereg_gate.require`; its ``hash`` is recorded as
``prereg_hash``) unless ``exploratory=True``, which is recorded on the
record so an exploratory model can never be mistaken for a confirmatory one.

**Immutability and tamper detection.**  Registering an id that already
exists returns the existing record when the content matches and raises
:class:`RegistryError` otherwise; nothing is ever overwritten.
:meth:`ModelRegistry.load` / :meth:`verify` recompute the artefact hash,
the id and the record hash and raise :class:`TamperError` on any mismatch.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1
RECORD = "record.json"
ARTEFACT = "model.joblib"
DEFAULT_CODE_MODULES = ("iap.models.zoo", "iap.models.metalabel", "iap.backtest.maker")


class RegistryError(ValueError):
    pass


class TamperError(RegistryError):
    pass


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("ascii")
    return hashlib.sha256(data).hexdigest()


def dump_bytes(model: Any) -> bytes:
    import joblib

    buf = io.BytesIO()
    joblib.dump(model, buf)
    return buf.getvalue()


def _load_bytes(data: bytes) -> Any:
    import joblib

    return joblib.load(io.BytesIO(data))


def default_code_hash(alpha_id: str | None = None) -> str:
    """Code fingerprint (:mod:`iap.agents.fingerprint`): the alpha's
    ``code_hash`` when ``alpha_id`` names one, else the import-closure hash
    of the model modules (:data:`DEFAULT_CODE_MODULES`)."""
    from iap.agents import fingerprint as fp

    if alpha_id:
        got = fp.fingerprint(alpha_id)
        if got is not None:
            return str(got["code_hash"])
    return fp.closure_hash(fp.module_closure(list(DEFAULT_CODE_MODULES)))


def default_feature_registry_hash() -> str:
    from iap.adaptive.drift import _registry_hash

    return _registry_hash()


@dataclass(frozen=True)
class ModelRecord:
    model_id: str
    name: str
    kind: str  # "classifier" | "regressor"
    artefact_sha256: str
    params: dict
    dataset_version: str
    date_range: list
    features: list
    feature_registry_hash: str
    code_hash: str
    seed: int
    metrics: dict = field(default_factory=dict)
    prereg_hash: str | None = None
    prereg_id: str | None = None
    exploratory: bool = False
    parent: str | None = None
    schema: int = SCHEMA
    record_hash: str = ""

    def identity(self) -> dict:
        return {
            "artefact_sha256": self.artefact_sha256,
            "params": self.params,
            "dataset_version": self.dataset_version,
            "date_range": list(self.date_range),
            "features": list(self.features),
            "feature_registry_hash": self.feature_registry_hash,
            "code_hash": self.code_hash,
            "seed": int(self.seed),
        }

    def compute_id(self) -> str:
        return sha256(canonical(self.identity()))

    def compute_record_hash(self) -> str:
        body = asdict(self)
        body.pop("record_hash")
        return sha256(canonical(body))

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(blob: dict) -> ModelRecord:
        return ModelRecord(**blob)


class ModelRegistry:
    """A directory of immutable, content-addressed models (module docs)."""

    def __init__(self, root) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ write
    def register(
        self,
        model: Any,
        *,
        name: str,
        kind: str,
        params: dict,
        dataset_version: str,
        date_range: tuple[str, str] | list,
        features: list[str],
        seed: int,
        metrics: dict | None = None,
        prereg: dict | None = None,
        exploratory: bool = False,
        parent: str | None = None,
        code_hash: str | None = None,
        feature_registry_hash: str | None = None,
        alpha_id: str | None = None,
    ) -> ModelRecord:
        if kind not in ("classifier", "regressor"):
            raise RegistryError(f"kind must be 'classifier' or 'regressor', got {kind!r}")
        if prereg is None and not exploratory:
            raise RegistryError(
                f"model {name!r} has no pre-registration entry; pass prereg= (a blackboard "
                "prereg entry, iap.agents.prereg_gate.require) or exploratory=True"
            )
        if prereg is not None and (prereg.get("kind") != "prereg" or not prereg.get("hash")):
            raise RegistryError("prereg must be a blackboard entry of kind 'prereg' with a hash")
        if parent is not None and not (self.root / parent / RECORD).exists():
            raise RegistryError(f"parent model {parent} is not in {self.root}")
        blob = dump_bytes(model)
        rec = ModelRecord(
            model_id="",
            name=name,
            kind=kind,
            artefact_sha256=sha256(blob),
            params=json.loads(canonical(params)),
            dataset_version=str(dataset_version),
            date_range=[str(d) for d in date_range],
            features=[str(f) for f in features],
            feature_registry_hash=(
                default_feature_registry_hash()
                if feature_registry_hash is None
                else feature_registry_hash
            ),
            code_hash=default_code_hash(alpha_id) if code_hash is None else code_hash,
            seed=int(seed),
            metrics=json.loads(canonical(metrics or {})),
            prereg_hash=None if prereg is None else str(prereg["hash"]),
            prereg_id=None if prereg is None else prereg.get("body", {}).get("prereg_id"),
            exploratory=bool(exploratory),
            parent=parent,
        )
        rec = _with(rec, model_id=rec.compute_id())
        rec = _with(rec, record_hash=rec.compute_record_hash())
        d = self.root / rec.model_id
        if (d / RECORD).exists():
            existing = self.verify(rec.model_id)
            if existing.record_hash != rec.record_hash:
                raise RegistryError(
                    f"model {rec.model_id} already registered with different metadata; "
                    "the registry is immutable"
                )
            return existing
        d.mkdir(parents=True, exist_ok=True)
        (d / ARTEFACT).write_bytes(blob)
        (d / RECORD).write_text(json.dumps(rec.to_dict(), sort_keys=True, indent=2) + "\n")
        return rec

    # ------------------------------------------------------------------- read
    def ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / RECORD).exists())

    def get(self, model_id: str) -> ModelRecord:
        path = self.root / model_id / RECORD
        if not path.exists():
            raise RegistryError(f"model {model_id} is not in {self.root}")
        return ModelRecord.from_dict(json.loads(path.read_text()))

    def list(self, name: str | None = None) -> list[ModelRecord]:
        recs = [self.get(i) for i in self.ids()]
        return [r for r in recs if name is None or r.name == name]

    def verify(self, model_id: str) -> ModelRecord:
        rec = self.get(model_id)
        problems = []
        art = self.root / model_id / ARTEFACT
        if not art.exists():
            problems.append("artefact missing")
        elif sha256(art.read_bytes()) != rec.artefact_sha256:
            problems.append("artefact hash mismatch")
        if rec.compute_id() != model_id or rec.model_id != model_id:
            problems.append("identity does not hash to the model id")
        if rec.compute_record_hash() != rec.record_hash:
            problems.append("record hash mismatch")
        if problems:
            raise TamperError(f"model {model_id}: {'; '.join(problems)}")
        return rec

    def load(self, model_id: str) -> tuple[Any, ModelRecord]:
        rec = self.verify(model_id)
        return _load_bytes((self.root / model_id / ARTEFACT).read_bytes()), rec

    def lineage(self, model_id: str) -> list[ModelRecord]:
        """``[model, parent, grandparent, ...]``."""
        out, seen, cur = [], set(), model_id
        while cur is not None:
            if cur in seen:
                raise RegistryError(f"lineage cycle at {cur}")
            seen.add(cur)
            rec = self.get(cur)
            out.append(rec)
            cur = rec.parent
        return out


def _with(rec: ModelRecord, **kw) -> ModelRecord:
    blob = rec.to_dict()
    blob.update(kw)
    return ModelRecord.from_dict(blob)

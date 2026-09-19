"""JSON Schema validation of contract dicts against ``schemas/``.

A single :class:`referencing.Registry` is built from every
``*.schema.json`` under the schema directory, keyed by each file's ``$id``,
so relative ``$ref`` values (``../alpha/alpha_signal.schema.json``) resolve
from disk without network access.  Validation errors are reported sorted by
JSON path, so the message for a given bad document is deterministic.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from iap.contracts.types import Contract
from iap.contracts.versions import SCHEMA_VERSIONS, schema_dir, schema_id

__all__ = [
    "ContractValidationError",
    "load_schema",
    "schema_registry",
    "validate",
    "validate_typed",
]


class ContractValidationError(ValueError):
    """A document does not validate against its schema."""

    def __init__(self, schema_relpath: str, errors: Tuple[str, ...]) -> None:
        self.schema_relpath = schema_relpath
        self.errors = errors
        joined = "; ".join(errors)
        super().__init__(f"{schema_relpath}: {joined}")


def _read_schema(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or "$id" not in doc:
        raise RuntimeError(f"{path}: schema without $id")
    return doc


@lru_cache(maxsize=None)
def _load_all(root: str) -> Tuple[Tuple[str, Dict[str, Any]], ...]:
    """Every schema under ``root`` as ``(relpath, document)``, sorted."""
    base = Path(root)
    out = []
    for path in sorted(base.rglob("*.schema.json")):
        rel = path.relative_to(base).as_posix()
        doc = _read_schema(path)
        expected = schema_id(rel)
        if doc["$id"] != expected:
            raise RuntimeError(f"{rel}: $id {doc['$id']!r} != {expected!r}")
        out.append((rel, doc))
    if not out:
        raise RuntimeError(f"no *.schema.json under {base}")
    return tuple(out)


@lru_cache(maxsize=None)
def _registry(root: str) -> Registry:
    resources = [(doc["$id"], Resource(contents=doc, specification=DRAFT202012))
                 for _, doc in _load_all(root)]
    return Registry().with_resources(resources)


def schema_registry() -> Registry:
    """The registry of every schema under :func:`schema_dir` (cached)."""
    return _registry(str(schema_dir()))


def load_schema(schema_relpath: str) -> Dict[str, Any]:
    """The parsed schema at ``schema_relpath`` (relative to ``schemas/``)."""
    for rel, doc in _load_all(str(schema_dir())):
        if rel == schema_relpath:
            return doc
    raise KeyError(f"unknown schema {schema_relpath!r}")


@lru_cache(maxsize=None)
def _validator(root: str, schema_relpath: str, fragment: str) -> Draft202012Validator:
    doc = load_schema(schema_relpath)
    Draft202012Validator.check_schema(doc)
    if fragment:
        doc = {"$schema": doc["$schema"],
               "$ref": f"{doc['$id']}#{fragment}"}
    return Draft202012Validator(doc, registry=_registry(root))


def _split(schema_ref: str) -> Tuple[str, str]:
    rel, _, fragment = schema_ref.partition("#")
    return rel, fragment


def validate(obj_dict: Mapping[str, Any], schema_relpath: str) -> None:
    """Validate ``obj_dict`` against the schema at ``schema_relpath``.

    ``schema_relpath`` is relative to ``schemas/`` and may carry a
    ``#/$defs/Name`` fragment to validate against a nested definition.
    Raises :class:`ContractValidationError` listing every violation
    (sorted by JSON path); raises ``KeyError`` for an unknown schema.
    """
    rel, fragment = _split(schema_relpath)
    if rel not in SCHEMA_VERSIONS:
        raise KeyError(f"unknown schema {rel!r}")
    validator = _validator(str(schema_dir()), rel, fragment)
    errors = sorted(validator.iter_errors(obj_dict),
                    key=lambda e: (e.json_path, e.message))
    if errors:
        raise ContractValidationError(
            schema_relpath,
            tuple(f"{e.json_path}: {e.message}" for e in errors))


def validate_typed(instance: Contract) -> Dict[str, Any]:
    """``to_dict`` the instance, validate it against ``instance.SCHEMA`` and
    return the dict.  Also asserts the type's ``x_version`` equals the
    schema file's ``x-version`` (a stale type is a contract bug)."""
    rel, _ = _split(instance.SCHEMA)
    doc = load_schema(rel)
    if doc["x-version"] != instance.x_version:
        raise ContractValidationError(
            instance.SCHEMA,
            (f"x-version mismatch: type {instance.x_version}, "
             f"schema {doc['x-version']}",))
    data = instance.to_dict()
    validate(data, instance.SCHEMA)
    return data

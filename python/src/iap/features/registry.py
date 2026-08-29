"""Feature registry (conventions §6).

The registry is the single pinned ordering of every registered feature:
FeatureVector.values follows registry order, and the registry content hash
(sha256 of the canonical JSON of all entries) is the FeatureVector
``feature_version``.

Families are concatenated in the pinned order of
:data:`iap.features.spec.FAMILY_ORDER`; inside a family the module's
``specs()`` order is pinned.  ``write_registry`` serializes to
``data/reference/feature_registry.json``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List, Union

from iap.features import (
    crossasset,
    execution,
    liquidity,
    microstructure,
    orderflow,
    price,
    regime,
    timeofday,
    venue,
    volatility,
)
from iap.features.spec import FAMILY_ORDER, FeatureSpec

_FAMILY_MODULES = {
    "price": price,
    "micro": microstructure,
    "flow": orderflow,
    "liquidity": liquidity,
    "vol": volatility,
    "tod": timeofday,
    "xasset": crossasset,
    "venue": venue,
    "regime": regime,
    "exec": execution,
}

_cache: List[FeatureSpec] = []


def family_module(family: str):
    """The module implementing a family (raises on unknown family)."""
    try:
        return _FAMILY_MODULES[family]
    except KeyError:
        raise ValueError(f"unknown feature family: {family!r}") from None


def build_registry() -> List[FeatureSpec]:
    """The full pinned registry (cached; order is normative)."""
    if not _cache:
        specs: List[FeatureSpec] = []
        for family in FAMILY_ORDER:
            mod = _FAMILY_MODULES[family]
            fam_specs = mod.specs()
            for s in fam_specs:
                if s.family != family:
                    raise ValueError(
                        f"feature {s.name} declares family {s.family!r}, "
                        f"module owns {family!r}"
                    )
            specs.extend(fam_specs)
        names = [s.name for s in specs]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate feature names: {dupes}")
        name_set = set(names)
        for s in specs:
            for dep in s.depends_on:
                if dep not in name_set:
                    raise ValueError(
                        f"feature {s.name} depends on unregistered {dep!r}"
                    )
        _cache.extend(specs)
    return list(_cache)


def registry_dicts() -> List[dict]:
    """Registry entries as JSON-able dicts in registry order."""
    return [s.to_dict() for s in build_registry()]


def registry_hash() -> str:
    """sha256 hex of the canonical registry JSON — the feature_version."""
    canonical = json.dumps(
        registry_dicts(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def feature_names() -> List[str]:
    """All registered names in registry (= FeatureVector value) order."""
    return [s.name for s in build_registry()]


def feature_index() -> Dict[str, int]:
    """{name: position in FeatureVector.values}."""
    return {s.name: i for i, s in enumerate(build_registry())}


def write_registry(path: Union[str, Path]) -> dict:
    """Write the registry JSON file; returns the written document."""
    doc = {
        "x-version": 1,
        "description": "Pinned feature registry (conventions §6). "
                       "FeatureVector.values follows this order; "
                       "registry_hash is the FeatureVector feature_version.",
        "registry_hash": registry_hash(),
        "count": len(build_registry()),
        "families": {
            fam: sum(1 for s in build_registry() if s.family == fam)
            for fam in FAMILY_ORDER
        },
        "features": registry_dicts(),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return doc


def load_registry(path: Union[str, Path]) -> dict:
    """Load a written registry document (verifies its content hash)."""
    with open(path) as f:
        doc = json.load(f)
    expected = registry_hash()
    if doc.get("registry_hash") != expected:
        raise ValueError(
            f"registry file hash {doc.get('registry_hash')!r} does not match "
            f"code registry hash {expected!r} — regenerate the file"
        )
    return doc

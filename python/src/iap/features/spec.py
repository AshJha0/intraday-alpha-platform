"""FeatureSpec — one registry entry — plus shared pinned constants.

Every registered feature is described by an immutable :class:`FeatureSpec`
(name / family / version / params / doc / depends_on).  Family modules build
their specs from pinned parameter grids; the registry concatenates the family
lists in the pinned family order (see :mod:`iap.features.registry`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

NS_PER_SEC = 1_000_000_000
NS_PER_MS = 1_000_000

#: Pinned window / horizon labels -> nanoseconds (event time, exchange_ts).
WINDOW_NS: Dict[str, int] = {
    "1s": 1 * NS_PER_SEC,
    "5s": 5 * NS_PER_SEC,
    "10s": 10 * NS_PER_SEC,
    "30s": 30 * NS_PER_SEC,
    "1m": 60 * NS_PER_SEC,
    "5m": 300 * NS_PER_SEC,
}

#: Small positive epsilon used in every guarded division (pinned).
EPS = 1e-12

#: Pinned family order — the registry (and therefore FeatureVector.values)
#: lists families in exactly this order.
FAMILY_ORDER = (
    "price",
    "micro",
    "flow",
    "liquidity",
    "vol",
    "tod",
    "xasset",
    "venue",
    "regime",
    "exec",
)


@dataclass(frozen=True)
class FeatureSpec:
    """One registered feature (immutable registry entry)."""

    name: str
    family: str
    version: int
    params: Tuple[Tuple[str, object], ...]  # sorted (key, value) pairs
    doc: str
    depends_on: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-able registry entry (canonical key order)."""
        return {
            "name": self.name,
            "family": self.family,
            "version": self.version,
            "params": {k: v for k, v in self.params},
            "doc": self.doc,
            "depends_on": list(self.depends_on),
        }


def mkspec(
    name: str,
    family: str,
    doc: str,
    depends_on: Tuple[str, ...] = (),
    version: int = 1,
    **params: object,
) -> FeatureSpec:
    """Build a FeatureSpec; ``name`` must already carry the ``_v<version>`` suffix."""
    suffix = f"_v{version}"
    if not name.endswith(suffix):
        raise ValueError(f"feature name {name!r} must end with {suffix!r}")
    return FeatureSpec(
        name=name,
        family=family,
        version=version,
        params=tuple(sorted(params.items())),
        doc=doc,
        depends_on=tuple(depends_on),
    )

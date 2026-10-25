"""EN1: the forecast interface (v2.0 U0 stage 5; plan section 6 "Forecast interface (U9)").

Every family's forecast is one record per name and decision::

    ForecastRecord(family_id, name, available_ts, horizon, mu_hat_bps, uncertainty_bps, registry_id)

- ``family_id``       the family (``h1``, ``d1_reversal``, ...): the engine is family-agnostic
                      (plan V2-C1); H1 is only the first client
- ``name``            the instrument (symbol or instrument id as text)
- ``available_ts``    ns since the Unix epoch at which every input of the forecast was
                      available (E0 section 2: ``t_data``); a consumer may act on the record
                      only at ``t_decision >= available_ts``
- ``horizon``         the declared holding horizon: ``<int>(s|m|h|d)`` (``15m``, ``5d``) or the
                      named ``c2o`` / ``o2c`` conventions of the close-to-open family; forecasts are
                      never combined across horizons (V2-C8)
- ``mu_hat_bps``      expected factor-adjusted return over the horizon, in bps
- ``uncertainty_bps`` a non-negative scale (standard error or interval half-width) in bps
- ``registry_id``     the content hash of the registry entry (:mod:`iap.alpha.registry`) that
                      produced it: model, parameters, feature set, training window, software

"Forecasts without all five fields are not consumed": :func:`ForecastRecord.from_dict`
and the constructor refuse a record with a missing, extra, ``None``, non-finite,
mistyped or malformed field with :class:`ForecastError`, and :func:`consume`
additionally refuses records that are not yet available at the decision time or
whose registry id the registry does not hold.  ``to_dict`` / ``from_dict`` round
trip through canonical JSON (:func:`iap.contracts.versions.canonical_json`), so
a forecast artefact's hash is reproducible across languages.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from iap.contracts.versions import canonical_json, content_hash

__all__ = [
    "FIELDS",
    "HORIZON_RE",
    "NAMED_HORIZONS",
    "ForecastError",
    "ForecastRecord",
    "consume",
    "horizon_seconds",
    "parse_horizon",
    "validate_horizon",
]

FIELDS: tuple[str, ...] = (
    "family_id",
    "name",
    "available_ts",
    "horizon",
    "mu_hat_bps",
    "uncertainty_bps",
    "registry_id",
)
HORIZON_RE = re.compile(r"^([1-9][0-9]*)(s|m|h|d)$")
#: Horizons whose length is a session convention, not a fixed duration.
NAMED_HORIZONS = frozenset({"c2o", "o2c"})
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86_400}
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ForecastError(ValueError):
    """A forecast record is incomplete, malformed or not consumable."""


def validate_horizon(horizon: Any) -> str:
    if not isinstance(horizon, str):
        raise ForecastError(
            f"horizon must be a string like '15m' or '5d', got {type(horizon).__name__}"
        )
    if horizon in NAMED_HORIZONS or HORIZON_RE.match(horizon):
        return horizon
    raise ForecastError(
        f"horizon {horizon!r} is not <int>(s|m|h|d) or one of {sorted(NAMED_HORIZONS)}"
    )


def parse_horizon(horizon: str) -> tuple[int, str]:
    """``('15m') -> (15, 'm')``; named horizons raise (they have no fixed length)."""
    validate_horizon(horizon)
    m = HORIZON_RE.match(horizon)
    if m is None:
        raise ForecastError(f"horizon {horizon!r} is a session convention without a fixed length")
    return int(m.group(1)), m.group(2)


def horizon_seconds(horizon: str) -> int:
    n, unit = parse_horizon(horizon)
    return n * _UNITS[unit]


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ForecastError(f"{what} must be a finite number, got {value!r}")
    f = float(value)
    if not math.isfinite(f):
        raise ForecastError(f"{what} must be finite, got {value!r}")
    return f


@dataclass(frozen=True, slots=True)
class ForecastRecord:
    """One forecast (module docstring).  Construction validates every field."""

    family_id: str
    name: str
    available_ts: int
    horizon: str
    mu_hat_bps: float
    uncertainty_bps: float
    registry_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.family_id, str) or not _ID.match(self.family_id):
            raise ForecastError(f"family_id must match {_ID.pattern!r}, got {self.family_id!r}")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ForecastError("name must be a non-empty string")
        if isinstance(self.available_ts, bool) or not isinstance(self.available_ts, int):
            raise ForecastError(
                f"available_ts must be an int (ns since epoch), got {self.available_ts!r}"
            )
        if self.available_ts < 0 or self.available_ts >= 2**63:
            raise ForecastError("available_ts out of the i64 ns range")
        validate_horizon(self.horizon)
        object.__setattr__(self, "mu_hat_bps", _finite(self.mu_hat_bps, "mu_hat_bps"))
        u = _finite(self.uncertainty_bps, "uncertainty_bps")
        if u < 0:
            raise ForecastError(f"uncertainty_bps must be >= 0, got {u}")
        object.__setattr__(self, "uncertainty_bps", u)
        if not isinstance(self.registry_id, str) or not _SHA256.match(self.registry_id):
            raise ForecastError("registry_id must be a 64-hex sha256 content hash")

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> ForecastRecord:
        """Strict: every field present, none extra, none ``None``."""
        if not isinstance(doc, Mapping):
            raise ForecastError("a forecast record is a mapping")
        missing = [f for f in FIELDS if f not in doc]
        extra = sorted(set(doc) - set(FIELDS))
        if missing or extra:
            raise ForecastError(f"forecast record refused: missing {missing}, unexpected {extra}")
        none = [f for f in FIELDS if doc[f] is None]
        if none:
            raise ForecastError(f"forecast record refused: null fields {none}")
        return cls(**{f: doc[f] for f in FIELDS})

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def record_hash(self) -> str:
        """SHA-256 of the canonical JSON: the identity of this forecast."""
        return content_hash(self.to_dict())

    def key(self) -> tuple[str, str, str]:
        """(family_id, horizon, name): the engine's cohort key without the cohort number."""
        return (self.family_id, self.horizon, self.name)


def consume(
    records: Iterable[ForecastRecord],
    *,
    t_decision: int,
    registry: Any | None = None,
) -> tuple[ForecastRecord, ...]:
    """The records a consumer may act on at ``t_decision``: validated, available, registered.

    Refuses (never silently drops) a record that is not a :class:`ForecastRecord`,
    that is not yet available (``available_ts > t_decision``), that duplicates
    another record's key, or whose ``registry_id`` is unknown to ``registry``
    (an object with ``contains(registry_id) -> bool``, the
    :class:`iap.alpha.registry.ForecastRegistry`).  Mixed horizons within one
    call are allowed; combining them is the consumer's (V2-C8) business.
    """
    if isinstance(t_decision, bool) or not isinstance(t_decision, int):
        raise ForecastError("t_decision must be an int (ns since epoch)")
    out: list[ForecastRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for r in records:
        if not isinstance(r, ForecastRecord):
            raise ForecastError(f"not a ForecastRecord: {r!r}")
        if r.available_ts > t_decision:
            raise ForecastError(
                f"forecast {r.family_id}/{r.name}/{r.horizon} is available at {r.available_ts} > t_decision {t_decision}"
            )
        if r.key() in seen:
            raise ForecastError(f"duplicate forecast for {r.key()} at one decision")
        if registry is not None and not registry.contains(r.registry_id):
            raise ForecastError(
                f"forecast {r.family_id}/{r.name} names an unknown registry id {r.registry_id[:12]}..."
            )
        seen.add(r.key())
        out.append(r)
    return tuple(out)

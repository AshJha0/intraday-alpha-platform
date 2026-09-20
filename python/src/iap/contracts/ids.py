"""Typed identifiers with range-validating constructors (conventions §1).

Every identifier is a :func:`typing.NewType` over ``int`` or ``str`` so that
static checkers distinguish an ``InstrumentId`` from a ``VenueId`` while the
runtime value stays a plain JSON-serialisable scalar.  The lower-case
constructor of each type (``instrument_id(7)``) validates the domain and is
the only sanctioned way to build one from untrusted input.

Integer domains follow the wire contract exactly: ``u16`` venue ids, ``u32``
instrument ids, ``u64`` event / order / trade ids and sequences, ``i64``
nanosecond timestamps.  Version identifiers are SHA-256 hex digests.
"""

from __future__ import annotations

import hashlib
import re
from typing import NewType

from iap.core.events import I64_MAX, I64_MIN, U16_MAX, U32_MAX, U64_MAX

__all__ = [
    "AlphaId",
    "ConfigVersion",
    "DataVersion",
    "EventId",
    "ExperimentId",
    "FeatureVersion",
    "FLAGSHIP_ALPHA_PATTERN",
    "InstrumentId",
    "ModelVersion",
    "NO_ROUTE",
    "OrderId",
    "PortfolioVersion",
    "SessionId",
    "StrategyId",
    "Timestamp",
    "TraceId",
    "TradeId",
    "VenueId",
    "alpha_id",
    "config_version",
    "data_version",
    "event_id",
    "experiment_id",
    "feature_version",
    "instrument_id",
    "is_flagship_alpha_id",
    "is_generic_id",
    "is_sha256_hex",
    "is_trace_id",
    "make_trace_id",
    "model_version",
    "order_id",
    "portfolio_version",
    "session_id",
    "strategy_id",
    "timestamp",
    "trace_id",
    "trade_id",
    "venue_id",
]

#: ``u32`` instrument id from ``configs/instruments/instruments.json``.
InstrumentId = NewType("InstrumentId", int)
#: ``u16`` venue id from ``configs/venues/venues.json``; ``0`` is reserved
#: for the consolidated book and, on a routing decision, means NO_ROUTE.
VenueId = NewType("VenueId", int)
#: ``u64`` order id (parent or child; venue-scoped uniqueness is the
#: execution layer's responsibility).
OrderId = NewType("OrderId", int)
#: ``u64`` trade id of a TRADE / EXECUTE print.
TradeId = NewType("TradeId", int)
#: ``u64`` global monotone event id within one normalised file.
EventId = NewType("EventId", int)
#: Strategy identifier (``configs/strategies/strategies.json``).
StrategyId = NewType("StrategyId", str)
#: Alpha identifier.  Flagship alphas are ``EQ01``..``FX12``
#: (:data:`FLAGSHIP_ALPHA_PATTERN`); research alphas may use any
#: non-empty identifier from the generic id alphabet.
AlphaId = NewType("AlphaId", str)
#: Experiment identifier: the runner's deterministic id for a spec
#: (``research/experiments/README.md``), or any generic id.
ExperimentId = NewType("ExperimentId", str)
#: Session identifier (one replay / paper / live run).
SessionId = NewType("SessionId", str)
#: 32 lowercase hex chars — the first 128 bits of SHA-256 over the
#: canonical trace key (:func:`make_trace_id`).
TraceId = NewType("TraceId", str)
#: ``i64`` nanoseconds since the Unix epoch (event time, never wall clock).
Timestamp = NewType("Timestamp", int)
#: SHA-256 hex of the normalised dataset bytes (``iap.experiment.tracker``).
DataVersion = NewType("DataVersion", str)
#: SHA-256 hex of the feature registry (``FeatureVector.feature_version``).
FeatureVersion = NewType("FeatureVersion", str)
#: SHA-256 hex identifying a fitted model / parameter set.
ModelVersion = NewType("ModelVersion", str)
#: SHA-256 hex of the canonical JSON of the configuration in force.
ConfigVersion = NewType("ConfigVersion", str)
#: SHA-256 hex of the canonical JSON of the portfolio problem
#: (constraints + solver parameters) that produced a target.
PortfolioVersion = NewType("PortfolioVersion", str)

#: ``VenueId`` value meaning "no venue" on a routing decision.
NO_ROUTE = VenueId(0)

#: Flagship alpha ids pinned by spec §§11-12.
FLAGSHIP_ALPHA_PATTERN = re.compile(r"^(EQ|FX)\d{2}$")
#: Generic id alphabet shared by strategy / alpha / experiment / session ids.
#: The pipe character is excluded so ids can be joined into canonical keys.
_GENERIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def _check_int(value: int, lo: int, hi: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}: expected int, got {type(value).__name__}")
    if value < lo or value > hi:
        raise ValueError(f"{name}: {value} outside [{lo}, {hi}]")
    return value


def is_generic_id(value: object) -> bool:
    """True when ``value`` is a string in the generic id alphabet
    (``[A-Za-z0-9][A-Za-z0-9._:-]{0,127}``: no whitespace, no pipe)."""
    return isinstance(value, str) and bool(_GENERIC_ID_PATTERN.match(value))


def _check_generic_id(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name}: expected str, got {type(value).__name__}")
    if not is_generic_id(value):
        raise ValueError(
            f"{name}: {value!r} must match {_GENERIC_ID_PATTERN.pattern}")
    return value


def _check_sha256(value: str, name: str) -> str:
    if not is_sha256_hex(value):
        raise ValueError(f"{name}: {value!r} is not a lowercase sha256 hex")
    return value


def instrument_id(value: int) -> InstrumentId:
    """Validate a ``u32`` instrument id."""
    return InstrumentId(_check_int(value, 0, U32_MAX, "instrument_id"))


def venue_id(value: int) -> VenueId:
    """Validate a ``u16`` venue id (``0`` = consolidated / NO_ROUTE)."""
    return VenueId(_check_int(value, 0, U16_MAX, "venue_id"))


def order_id(value: int) -> OrderId:
    """Validate a ``u64`` order id."""
    return OrderId(_check_int(value, 0, U64_MAX, "order_id"))


def trade_id(value: int) -> TradeId:
    """Validate a ``u64`` trade id."""
    return TradeId(_check_int(value, 0, U64_MAX, "trade_id"))


def event_id(value: int) -> EventId:
    """Validate a ``u64`` event id."""
    return EventId(_check_int(value, 0, U64_MAX, "event_id"))


def timestamp(value: int) -> Timestamp:
    """Validate an ``i64`` nanosecond timestamp."""
    return Timestamp(_check_int(value, I64_MIN, I64_MAX, "timestamp"))


def strategy_id(value: str) -> StrategyId:
    """Validate a strategy id (generic id alphabet)."""
    return StrategyId(_check_generic_id(value, "strategy_id"))


def alpha_id(value: str) -> AlphaId:
    """Validate an alpha id: flagship ``EQnn``/``FXnn`` or a generic id."""
    return AlphaId(_check_generic_id(value, "alpha_id"))


def is_flagship_alpha_id(value: str) -> bool:
    """True for the pinned flagship ids ``EQ01``..``FX12`` shape."""
    return bool(FLAGSHIP_ALPHA_PATTERN.match(value))


def experiment_id(value: str) -> ExperimentId:
    """Validate an experiment id (generic id alphabet)."""
    return ExperimentId(_check_generic_id(value, "experiment_id"))


def session_id(value: str) -> SessionId:
    """Validate a session id (generic id alphabet)."""
    return SessionId(_check_generic_id(value, "session_id"))


def is_sha256_hex(value: object) -> bool:
    """True when ``value`` is a 64-char lowercase hex SHA-256 digest."""
    return isinstance(value, str) and bool(_SHA256_HEX.match(value))


def data_version(value: str) -> DataVersion:
    """Validate a dataset version (sha256 hex)."""
    return DataVersion(_check_sha256(value, "data_version"))


def feature_version(value: str) -> FeatureVersion:
    """Validate a feature-registry version (sha256 hex)."""
    return FeatureVersion(_check_sha256(value, "feature_version"))


def model_version(value: str) -> ModelVersion:
    """Validate a model version (sha256 hex)."""
    return ModelVersion(_check_sha256(value, "model_version"))


def config_version(value: str) -> ConfigVersion:
    """Validate a configuration version (sha256 hex)."""
    return ConfigVersion(_check_sha256(value, "config_version"))


def portfolio_version(value: str) -> PortfolioVersion:
    """Validate a portfolio-problem version (sha256 hex)."""
    return PortfolioVersion(_check_sha256(value, "portfolio_version"))


def is_trace_id(value: object) -> bool:
    """True when ``value`` is a 32-char lowercase hex trace id."""
    return isinstance(value, str) and bool(_TRACE_ID.match(value))


def trace_id(value: str) -> TraceId:
    """Validate a trace id (32 lowercase hex chars)."""
    if not is_trace_id(value):
        raise ValueError(f"trace_id: {value!r} must be 32 lowercase hex chars")
    return TraceId(value)


def make_trace_id(session: str, instrument: int, event_ts: int,
                  sequence: int) -> TraceId:
    """Deterministic trace id for one decision.

    The canonical key is the pipe-joined ASCII string
    ``"<session_id>|<instrument_id>|<event_ts>|<sequence>"`` (decimal
    integers, no padding, no sign on unsigned fields); the trace id is the
    first 128 bits (32 lowercase hex chars) of its SHA-256.  Any language can
    reproduce it with a string concatenation and one hash call.  The session
    id alphabet excludes ``|`` so the key is unambiguous.
    """
    key = "|".join((
        session_id(session),
        str(instrument_id(instrument)),
        str(timestamp(event_ts)),
        str(_check_int(sequence, 0, U64_MAX, "sequence")),
    ))
    digest = hashlib.sha256(key.encode("ascii")).hexdigest()
    return TraceId(digest[:32])

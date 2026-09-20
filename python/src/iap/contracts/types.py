"""Typed contracts: frozen, slotted dataclasses mirroring ``schemas/``.

Every contract type

* is a frozen ``slots`` dataclass whose field order **is** the schema's
  property order (``to_dict`` emits keys in that order);
* validates its own field domains on construction (``__post_init__``): strict
  scalar types (``bool`` is never an ``int``), integer ranges pinned by the
  wire contract, enum membership, finite floats, string patterns;
* round-trips exactly: ``T.from_dict(t.to_dict()) == t``.  ``from_dict`` is
  strict — unknown keys, missing keys, wrong types and unknown enum values
  raise :class:`ContractError`;
* carries ``x_version`` (the schema's ``x-version``) and ``SCHEMA`` (the
  schema path relative to ``schemas/``; a ``#/$defs/Name`` fragment for the
  nested record types).

The JSON produced by ``to_dict`` validates against the schema
(:func:`iap.contracts.validate.validate_typed`); the golden test pins one
canonical instance per type (``tests/golden/expected_contracts_examples.json``).

Sequences are stored as tuples and mappings as plain dicts (never mutate a
contract's dict after construction — the instance is logically immutable;
types with a mapping field are consequently not hashable).
"""

from __future__ import annotations

import math
import typing
from dataclasses import MISSING, dataclass, field, fields
from enum import Enum, IntEnum
from typing import (
    Any,
    ClassVar,
    Dict,
    Mapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
)

from iap.contracts import versions as V
from iap.contracts.ids import is_generic_id, is_sha256_hex, is_trace_id
from iap.core.events import I64_MAX, I64_MIN, U16_MAX, U32_MAX, U64_MAX, Side

__all__ = [
    "Actor",
    "Algo",
    "AlphaSignal",
    "Attribution",
    "BookSnapshotRef",
    "ChildOrder",
    "Contract",
    "ContractError",
    "Decision",
    "DecisionTrace",
    "Direction",
    "ExecStatus",
    "ExecutionReport",
    "ExperimentResult",
    "ExperimentSpec",
    "FeatureVectorRef",
    "GateResult",
    "LatencyStats",
    "LifecycleState",
    "LifecycleTransition",
    "MarketEventRef",
    "OrderType",
    "ParentOrder",
    "Period",
    "PortfolioLeg",
    "PortfolioTarget",
    "RiskDecision",
    "Side",
    "SolverStatus",
    "TCAResult",
    "TraceStages",
    "VenueDecision",
    "VenueScore",
    "Verdict",
    "explain",
]


class ContractError(ValueError):
    """A contract value violates its typed definition."""


# --------------------------------------------------------------------------
# Enumerations (values are the wire encodings pinned in the schemas)
# --------------------------------------------------------------------------


class Direction(IntEnum):
    """Sign of an alpha signal (``alpha_signal.schema.json``)."""

    DOWN = -1
    FLAT = 0
    UP = 1


class SolverStatus(str, Enum):
    """Portfolio solve outcome (API_PORTFOLIO_TCA.md §1.3)."""

    OPTIMAL = "OPTIMAL"
    INFEASIBLE = "INFEASIBLE"
    MAX_ITER = "MAX_ITER"


class Decision(IntEnum):
    """Hard-risk decision (``risk_event.schema.json``)."""

    ALLOW = 1
    REJECT = 2
    KILL = 3


class Algo(str, Enum):
    """Parent-order execution algorithm."""

    TWAP = "TWAP"
    VWAP = "VWAP"
    POV = "POV"
    IS = "IS"


class OrderType(IntEnum):
    """Child order type (``order_request.schema.json``)."""

    MARKET = 1
    LIMIT = 2
    IOC = 3
    FOK = 4
    PEG = 5
    MID = 6


class ExecStatus(IntEnum):
    """Execution report status (``execution_report.schema.json``)."""

    NEW = 1
    PARTIAL = 2
    FILLED = 3
    CANCELED = 4
    REJECTED = 5
    EXPIRED = 6


class Verdict(str, Enum):
    """Validation verdict (``iap.validation.validate``, spec §20 gates)."""

    PROMOTE = "PROMOTE"
    ITERATE = "ITERATE"
    REJECT = "REJECT"


class LifecycleState(IntEnum):
    """Alpha lifecycle states, ordered.  Serialised by NAME (the
    ``iap.adaptive.lifecycle`` log convention); the integer gives the order
    along the promotion path."""

    RESEARCH = 0
    CANDIDATE = 1
    VALIDATING = 2
    PAPER = 3
    ACTIVE = 4
    WATCH = 5
    RETIRED = 6


class Actor(str, Enum):
    """Who caused a lifecycle transition."""

    SYSTEM = "SYSTEM"
    HUMAN = "HUMAN"


#: Enums whose JSON form is the member NAME rather than its value.
_NAME_ENUMS: frozenset = frozenset({LifecycleState})


# --------------------------------------------------------------------------
# Field metadata (domain pins mirrored by the schemas)
# --------------------------------------------------------------------------

_U16 = {"min": 0, "max": U16_MAX}
_U32 = {"min": 0, "max": U32_MAX}
_U64 = {"min": 0, "max": U64_MAX}
_I64 = {"min": I64_MIN, "max": I64_MAX}
_UNIT = {"min": 0.0, "max": 1.0}
_NONNEG = {"min": 0}
_SHA256 = {"sha256": True}
_TRACE = {"trace_id": True}
_IDENT = {"ident": True}
_DEC_KEYS = {"key_pattern": "decimal"}

T = TypeVar("T", bound="Contract")


def _type_name(value: Any) -> str:
    return type(value).__name__


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_json_value(value: Any, path: str) -> Any:
    """Verify (and return) a free-form JSON value: finite numbers, string keys."""
    if value is None or isinstance(value, (bool, str)) or _is_int(value):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{path}: non-finite float")
        return value
    if isinstance(value, (list, tuple)):
        return [_check_json_value(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ContractError(f"{path}: non-string key {k!r}")
            out[k] = _check_json_value(v, f"{path}.{k}")
        return out
    raise ContractError(f"{path}: {_type_name(value)} is not a JSON value")


def _normalise(hint: Any, value: Any, path: str, meta: Mapping[str, Any]) -> Any:
    """Coerce ``value`` into the typed form ``hint`` asks for, or raise.

    Accepts both the typed form (enum members, contract instances, tuples)
    and the JSON form (raw enum values, dicts, lists) so that ``from_dict``
    and the constructor share one strict checker.
    """
    origin = typing.get_origin(hint)
    if hint is Any:
        return _check_json_value(value, path)
    if origin is typing.Union:
        args = typing.get_args(hint)
        if value is None:
            if type(None) in args:
                return None
            raise ContractError(f"{path}: null not allowed")
        inner = [a for a in args if a is not type(None)]
        return _normalise(inner[0], value, path, meta)
    if hint is bool:
        if not isinstance(value, bool):
            raise ContractError(f"{path}: expected bool, got {_type_name(value)}")
        return value
    if hint is int:
        if not _is_int(value):
            raise ContractError(f"{path}: expected int, got {_type_name(value)}")
        lo, hi = meta.get("min"), meta.get("max")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ContractError(f"{path}: {value} outside [{lo}, {hi}]")
        return value
    if hint is float:
        if not (_is_int(value) or isinstance(value, float)):
            raise ContractError(f"{path}: expected number, got {_type_name(value)}")
        out = float(value)
        if not math.isfinite(out):
            raise ContractError(f"{path}: non-finite number")
        lo, hi = meta.get("min"), meta.get("max")
        if (lo is not None and out < lo) or (hi is not None and out > hi):
            raise ContractError(f"{path}: {out} outside [{lo}, {hi}]")
        return out
    if hint is str:
        if not isinstance(value, str):
            raise ContractError(f"{path}: expected str, got {_type_name(value)}")
        if meta.get("sha256") and not is_sha256_hex(value):
            raise ContractError(f"{path}: not a lowercase sha256 hex")
        if meta.get("trace_id") and not is_trace_id(value):
            raise ContractError(f"{path}: not a 32-hex trace id")
        if meta.get("non_empty") and not value:
            raise ContractError(f"{path}: must not be empty")
        if meta.get("ident") and not is_generic_id(value):
            raise ContractError(f"{path}: {value!r} is not a valid identifier")
        return value
    if isinstance(hint, type) and issubclass(hint, Enum):
        if isinstance(value, hint):
            return value
        if isinstance(value, bool):
            raise ContractError(f"{path}: bool is not a {hint.__name__}")
        if hint in _NAME_ENUMS:
            if isinstance(value, str) and value in hint.__members__:
                return hint.__members__[value]
            raise ContractError(f"{path}: {value!r} not a {hint.__name__} name")
        try:
            return hint(value)
        except ValueError:
            raise ContractError(
                f"{path}: {value!r} not in {hint.__name__}") from None
    if isinstance(hint, type) and issubclass(hint, Contract):
        if isinstance(value, hint):
            return value
        if isinstance(value, dict):
            return hint.from_dict(value, _path=path)
        raise ContractError(
            f"{path}: expected {hint.__name__}, got {_type_name(value)}")
    if origin is tuple:
        (item,) = (a for a in typing.get_args(hint) if a is not Ellipsis)
        if not isinstance(value, (list, tuple)):
            raise ContractError(f"{path}: expected list, got {_type_name(value)}")
        return tuple(_normalise(item, v, f"{path}[{i}]", {})
                     for i, v in enumerate(value))
    if origin is dict:
        _, item = typing.get_args(hint)
        if not isinstance(value, dict):
            raise ContractError(f"{path}: expected object, got {_type_name(value)}")
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ContractError(f"{path}: non-string key {k!r}")
            if meta.get("key_pattern") == "decimal" and not (
                    k.isdigit() and (k == "0" or not k.startswith("0"))):
                raise ContractError(f"{path}: key {k!r} is not a decimal id")
            out[k] = _normalise(item, v, f"{path}.{k}", {})
        return out
    raise ContractError(f"{path}: unsupported contract type {hint!r}")


def _to_json(value: Any) -> Any:
    if isinstance(value, Contract):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.name if type(value) in _NAME_ENUMS else value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    raise ContractError(f"cannot serialise {_type_name(value)}")


_HINTS_CACHE: Dict[type, Dict[str, Any]] = {}


def _hints(cls: type) -> Dict[str, Any]:
    hints = _HINTS_CACHE.get(cls)
    if hints is None:
        hints = typing.get_type_hints(cls)
        _HINTS_CACHE[cls] = hints
    return hints


class Contract:
    """Base of every typed contract (see module docstring)."""

    __slots__ = ()

    #: ``x-version`` of the schema this type mirrors.
    x_version: ClassVar[int]
    #: Schema path relative to ``schemas/`` (optionally ``#/$defs/Name``).
    SCHEMA: ClassVar[str]

    def __post_init__(self) -> None:
        hints = _hints(type(self))
        for f in fields(self):  # type: ignore[arg-type]
            value = _normalise(hints[f.name], getattr(self, f.name),
                               f"{type(self).__name__}.{f.name}", f.metadata)
            object.__setattr__(self, f.name, value)
        self.check_invariants()

    def check_invariants(self) -> None:
        """Cross-field invariants; subclasses override and raise
        :class:`ContractError`.  Called after per-field domain checks."""

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready dict, keys in schema property order."""
        return {f.name: _to_json(getattr(self, f.name))
                for f in fields(self)}  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls: Type[T], data: Mapping[str, Any], *,
                  _path: str = "") -> T:
        """Strict inverse of :meth:`to_dict`.

        Unknown keys, missing keys, wrong scalar types (``1`` is not ``1.0``
        only in the sense that ``true`` is never a number), out-of-range
        integers and unknown enum values raise :class:`ContractError`.
        """
        path = _path or cls.__name__
        if not isinstance(data, Mapping):
            raise ContractError(f"{path}: expected object, got {_type_name(data)}")
        names = [f.name for f in fields(cls)]  # type: ignore[arg-type]
        unknown = sorted(set(data) - set(names))
        if unknown:
            raise ContractError(f"{path}: unknown keys {unknown}")
        missing = [n for n in names if n not in data]
        if missing:
            raise ContractError(f"{path}: missing keys {missing}")
        hints = _hints(cls)
        kwargs = {}
        for f in fields(cls):  # type: ignore[arg-type]
            kwargs[f.name] = _normalise(hints[f.name], data[f.name],
                                        f"{path}.{f.name}", f.metadata)
        return cls(**kwargs)


def _no_defaults(cls: type) -> None:
    """Contracts have no field defaults: every value is written explicitly."""
    for f in fields(cls):  # type: ignore[arg-type]
        if f.default is not MISSING or f.default_factory is not MISSING:
            raise TypeError(f"{cls.__name__}.{f.name}: contract fields have no defaults")


def contract(x_version: int, schema: str):
    """Class decorator: frozen slotted dataclass + version/schema pins."""

    def wrap(cls: type) -> type:
        cls.x_version = x_version  # type: ignore[attr-defined]
        cls.SCHEMA = schema  # type: ignore[attr-defined]
        out = dataclass(frozen=True, slots=True, eq=True, repr=True)(cls)
        _no_defaults(out)
        return out

    return wrap


def _ts_ordered(*pairs: Tuple[str, int, str, int], owner: str) -> None:
    for a_name, a, b_name, b in pairs:
        if a > b:
            raise ContractError(f"{owner}: {a_name} ({a}) > {b_name} ({b})")


# --------------------------------------------------------------------------
# Market data / book / features — thin references
# --------------------------------------------------------------------------

_TRACE_SCHEMA = "trace/decision_trace.schema.json"


@contract(V.DECISION_TRACE_VERSION, _TRACE_SCHEMA + "#/$defs/MarketEventRef")
class MarketEventRef(Contract):
    """Reference to one canonical market event (the full event stays
    ``iap.core.events.MarketEvent``): the stream key ``(instrument_id,
    venue_id, sequence)`` plus ``event_id`` and its exchange time."""

    event_id: int = field(metadata=_U64)
    instrument_id: int = field(metadata=_U32)
    venue_id: int = field(metadata=_U16)
    exchange_ts: int = field(metadata=_I64)
    sequence: int = field(metadata=_U64)


@contract(V.DECISION_TRACE_VERSION, _TRACE_SCHEMA + "#/$defs/BookSnapshotRef")
class BookSnapshotRef(Contract):
    """Content-addressed reference to a book state: the stream position
    after which the state was taken (``venue_id`` 0 = consolidated) and
    ``state_hash`` = ``content_hash(book.state_summary())``."""

    instrument_id: int = field(metadata=_U32)
    venue_id: int = field(metadata=_U16)
    exchange_ts: int = field(metadata=_I64)
    sequence: int = field(metadata=_U64)
    state_hash: str = field(metadata=_SHA256)


@contract(V.DECISION_TRACE_VERSION, _TRACE_SCHEMA + "#/$defs/FeatureVectorRef")
class FeatureVectorRef(Contract):
    """Reference to a ``FeatureVector`` emission: instrument, event time,
    the registry hash it was computed under, and its shape / validity
    count (``n_valid <= n_values``)."""

    instrument_id: int = field(metadata=_U32)
    timestamp_ns: int = field(metadata=_I64)
    feature_version: str = field(metadata=_SHA256)
    n_values: int = field(metadata=_U32)
    n_valid: int = field(metadata=_U32)

    def check_invariants(self) -> None:
        if self.n_valid > self.n_values:
            raise ContractError("FeatureVectorRef: n_valid > n_values")


# --------------------------------------------------------------------------
# Alpha
# --------------------------------------------------------------------------


@contract(V.ALPHA_SIGNAL_VERSION, "alpha/alpha_signal.schema.json")
class AlphaSignal(Contract):
    """Alpha model output — exactly ``alpha_signal.schema.json``.

    ``expected_return`` is a dimensionless forward return over
    ``horizon_ns`` (``1e-4`` = 1 bp); ``confidence`` in ``[0, 1]`` with the
    API_ALPHA invariant that a zero confidence carries a zero expected
    return; ``model_version`` identifies the scoring model / parameter set
    (the flagship alpha id for ``linear_z_v1`` alphas).
    """

    timestamp: int = field(metadata=_I64)
    instrument_id: int = field(metadata=_U32)
    expected_return: float
    confidence: float = field(metadata=_UNIT)
    horizon_ns: int = field(metadata=_I64)
    direction: Direction
    model_version: str

    def check_invariants(self) -> None:
        if self.confidence == 0.0 and self.expected_return != 0.0:
            raise ContractError(
                "AlphaSignal: expected_return must be 0 when confidence is 0")


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------

_PORTFOLIO_SCHEMA = "portfolio/portfolio_target.schema.json"


@contract(V.PORTFOLIO_TARGET_VERSION, _PORTFOLIO_SCHEMA + "#/$defs/PortfolioLeg")
class PortfolioLeg(Contract):
    """One instrument's target inside a :class:`PortfolioTarget`."""

    instrument_id: int = field(metadata=_U32)
    target_qty: int = field(metadata=_I64)
    target_weight: float
    expected_return_bps: float
    prev_qty: int = field(metadata=_I64)


@contract(V.PORTFOLIO_TARGET_VERSION, _PORTFOLIO_SCHEMA)
class PortfolioTarget(Contract):
    """Output of one portfolio solve (API_PORTFOLIO_TCA.md §1).

    ``portfolio_version`` hashes the problem (constraints + solver
    parameters); ``solver_status`` follows ``PGDResult.status`` with
    ``MAX_ITER`` reserved for a solve that stopped on its iteration budget
    without a feasible best iterate being declared.  ``turnover`` is
    ``sum |w - w_prev|``.  Legs are sorted by ``instrument_id`` (unique).
    """

    strategy_id: str = field(metadata=_IDENT)
    timestamp_ns: int = field(metadata=_I64)
    portfolio_version: str = field(metadata=_SHA256)
    feature_version: str = field(metadata=_SHA256)
    model_version: str = field(metadata=_SHA256)
    solver_status: SolverStatus
    objective_value: float
    turnover: float = field(metadata=_NONNEG)
    targets: Tuple[PortfolioLeg, ...]

    def check_invariants(self) -> None:
        ids = [leg.instrument_id for leg in self.targets]
        if ids != sorted(set(ids)):
            raise ContractError(
                "PortfolioTarget: targets must be sorted by unique instrument_id")


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


@contract(V.RISK_DECISION_VERSION, "risk/risk_decision.schema.json")
class RiskDecision(Contract):
    """Per-order hard-risk decision.

    Derivable from a ``RiskEvent`` (``risk_event.schema.json``) plus the
    order it was raised for (:meth:`from_risk_event`).  ``rule_index`` is
    the pinned check index of the deciding rule (``configs/risk/risk.json``
    order, ``MIGRATIONS.md`` 2026-09-06) and ``-1`` for ALLOW.
    """

    order_id: int = field(metadata=_U64)
    strategy_id: str = field(metadata=_IDENT)
    instrument_id: int = field(metadata=_U32)
    timestamp_ns: int = field(metadata=_I64)
    decision: Decision
    rule_id: str
    rule_index: int = field(metadata={"min": -1, "max": U16_MAX})
    reason: str

    def check_invariants(self) -> None:
        if self.decision is Decision.ALLOW and self.rule_index != -1:
            raise ContractError("RiskDecision: ALLOW requires rule_index == -1")
        if self.decision is not Decision.ALLOW and self.rule_index < 0:
            raise ContractError(
                "RiskDecision: REJECT/KILL require a pinned rule_index >= 0")

    @classmethod
    def from_risk_event(cls, event: Mapping[str, Any], *, order_id: int,
                        strategy_id: str, instrument_id: int,
                        rule_index: int = -1) -> "RiskDecision":
        """Build from a ``RiskEvent`` dict (schema field names) and the order
        context the event does not carry.  ``rule_index`` defaults to ``-1``
        and must be supplied for REJECT / KILL."""
        for key in ("timestamp", "decision", "rule_id", "reason"):
            if key not in event:
                raise ContractError(f"RiskEvent: missing {key!r}")
        return cls(
            order_id=order_id,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            timestamp_ns=event["timestamp"],
            decision=event["decision"],
            rule_id=event["rule_id"],
            rule_index=rule_index,
            reason=event["reason"],
        )


# --------------------------------------------------------------------------
# Execution / SOR
# --------------------------------------------------------------------------


@contract(V.PARENT_ORDER_VERSION, "order/parent_order.schema.json")
class ParentOrder(Contract):
    """Strategy-level order handed to an execution algorithm.

    ``decision_ts <= arrival_ts <= end_ts`` (the TCA window rule);
    ``limit_price_ticks`` 0 = unpriced; ``params`` are the algorithm's
    numeric parameters (``participation`` for POV, ``slices`` for
    TWAP/VWAP/IS, ...), keyed by name.
    """

    parent_order_id: int = field(metadata=_U64)
    strategy_id: str = field(metadata=_IDENT)
    alpha_id: str = field(metadata=_IDENT)
    instrument_id: int = field(metadata=_U32)
    side: Side
    qty: int = field(metadata={"min": 1, "max": I64_MAX})
    algo: Algo
    decision_ts: int = field(metadata=_I64)
    arrival_ts: int = field(metadata=_I64)
    end_ts: int = field(metadata=_I64)
    urgency: float = field(metadata=_UNIT)
    limit_price_ticks: int = field(metadata={"min": 0, "max": I64_MAX})
    params: Dict[str, float]

    def check_invariants(self) -> None:
        _ts_ordered(("decision_ts", self.decision_ts, "arrival_ts", self.arrival_ts),
                    ("arrival_ts", self.arrival_ts, "end_ts", self.end_ts),
                    owner="ParentOrder")


@contract(V.CHILD_ORDER_VERSION, "order/child_order.schema.json")
class ChildOrder(Contract):
    """One slice of a parent order, addressed to a venue (0 = SOR decides).

    ``price_ticks`` 0 for MARKET; ``expire_ts`` 0 = good till the parent's
    ``end_ts``; ``slice_index`` is the 0-based slice ordinal.
    """

    child_order_id: int = field(metadata=_U64)
    parent_order_id: int = field(metadata=_U64)
    instrument_id: int = field(metadata=_U32)
    venue_id: int = field(metadata=_U16)
    side: Side
    qty: int = field(metadata={"min": 1, "max": I64_MAX})
    price_ticks: int = field(metadata={"min": 0, "max": I64_MAX})
    order_type: OrderType
    submit_ts: int = field(metadata=_I64)
    expire_ts: int = field(metadata=_I64)
    slice_index: int = field(metadata=_U32)

    def check_invariants(self) -> None:
        if self.expire_ts and self.expire_ts < self.submit_ts:
            raise ContractError("ChildOrder: expire_ts < submit_ts")
        if self.order_type is OrderType.MARKET and self.price_ticks != 0:
            raise ContractError("ChildOrder: MARKET orders are unpriced")


_VENUE_SCHEMA = "execution/venue_decision.schema.json"


@contract(V.VENUE_DECISION_VERSION, _VENUE_SCHEMA + "#/$defs/VenueScore")
class VenueScore(Contract):
    """One candidate venue as the router saw it.  ``rank`` is 1-based
    among eligible venues and 0 for an ineligible one."""

    venue_id: int = field(metadata=_U16)
    eligible: bool
    displayed_price_ticks: int = field(metadata={"min": 0, "max": I64_MAX})
    displayed_qty: int = field(metadata={"min": 0, "max": I64_MAX})
    taker_fee: float
    maker_rebate: float
    commission_per_million: float
    latency_mean_ns: int = field(metadata={"min": 0, "max": I64_MAX})
    rank: int = field(metadata=_U16)

    def check_invariants(self) -> None:
        if self.eligible != (self.rank > 0):
            raise ContractError("VenueScore: eligible venues carry rank >= 1, "
                                "ineligible ones rank 0")


@contract(V.VENUE_DECISION_VERSION, _VENUE_SCHEMA)
class VenueDecision(Contract):
    """Smart-order-router decision for one child order.  ``venue_id`` 0 =
    NO_ROUTE (nothing eligible); ``candidates`` sorted by ``venue_id``."""

    child_order_id: int = field(metadata=_U64)
    venue_id: int = field(metadata=_U16)
    reason: str
    candidates: Tuple[VenueScore, ...]

    def check_invariants(self) -> None:
        ids = [c.venue_id for c in self.candidates]
        if ids != sorted(set(ids)):
            raise ContractError(
                "VenueDecision: candidates must be sorted by unique venue_id")
        if self.venue_id and not any(
                c.venue_id == self.venue_id and c.eligible for c in self.candidates):
            raise ContractError(
                "VenueDecision: routed venue must be an eligible candidate")


@contract(V.EXECUTION_REPORT_VERSION, "execution/execution_report.schema.json")
class ExecutionReport(Contract):
    """Venue execution report — exactly ``execution_report.schema.json``.
    ``filled_qty`` / ``fill_price_ticks`` describe THIS report's fill
    (0 for non-fill statuses); ``fees`` signed, negative = rebate."""

    order_id: int = field(metadata=_U64)
    execution_id: int = field(metadata=_U64)
    status: ExecStatus
    filled_qty: int = field(metadata={"min": 0, "max": I64_MAX})
    fill_price_ticks: int = field(metadata={"min": 0, "max": I64_MAX})
    venue_id: int = field(metadata=_U16)
    exchange_ts: int = field(metadata=_I64)
    receive_ts: int = field(metadata=_I64)
    fees: float

    def check_invariants(self) -> None:
        if self.receive_ts < self.exchange_ts:
            raise ContractError("ExecutionReport: receive_ts < exchange_ts")
        is_fill = self.status in (ExecStatus.PARTIAL, ExecStatus.FILLED)
        if is_fill and self.filled_qty <= 0:
            raise ContractError("ExecutionReport: fill status needs filled_qty > 0")
        if not is_fill and self.filled_qty:
            raise ContractError("ExecutionReport: non-fill status carries filled_qty 0")


# --------------------------------------------------------------------------
# TCA
# --------------------------------------------------------------------------

_TCA_SCHEMA = "tca/tca_result.schema.json"


@contract(V.TCA_RESULT_VERSION, _TCA_SCHEMA + "#/$defs/LatencyStats")
class LatencyStats(Contract):
    """Submit-to-acknowledge latency over an order's children, ns.
    Quantiles are nearest-rank; ``mean`` is the arithmetic mean."""

    min: int = field(metadata={"min": 0, "max": I64_MAX})
    mean: float = field(metadata=_NONNEG)
    max: int = field(metadata={"min": 0, "max": I64_MAX})
    p50: int = field(metadata={"min": 0, "max": I64_MAX})
    p99: int = field(metadata={"min": 0, "max": I64_MAX})

    def check_invariants(self) -> None:
        if not (self.min <= self.p50 <= self.p99 <= self.max):
            raise ContractError("LatencyStats: need min <= p50 <= p99 <= max")


@contract(V.TCA_RESULT_VERSION, _TCA_SCHEMA)
class TCAResult(Contract):
    """Per-parent-order TCA record (API_PORTFOLIO_TCA.md §2).

    Prices are in ticks (``arrival_price_ticks`` integer; the fill average
    and interval benchmarks are doubles in tick units).  Costs are positive
    when execution was worse than the benchmark.  The Perold identity
    ``implementation_shortfall_bps = delay + trading + opportunity`` and the
    split ``trading = spread + impact + timing`` hold to 1e-9.
    ``venue_contribution_bps`` is keyed by decimal venue id.  Names follow
    ``iap.tca.tca.order_tca`` where that record has the quantity
    (``n_fills``, ``side``, spread / impact / timing costs).
    """

    parent_order_id: int = field(metadata=_U64)
    instrument_id: int = field(metadata=_U32)
    side: Side
    qty: int = field(metadata={"min": 1, "max": I64_MAX})
    filled_qty: int = field(metadata={"min": 0, "max": I64_MAX})
    fill_rate: float = field(metadata=_UNIT)
    arrival_price_ticks: int = field(metadata={"min": 0, "max": I64_MAX})
    avg_fill_price: float = field(metadata=_NONNEG)
    interval_vwap: float = field(metadata=_NONNEG)
    interval_twap: float = field(metadata=_NONNEG)
    implementation_shortfall_bps: float
    delay_cost_bps: float
    trading_cost_bps: float
    opportunity_cost_bps: float
    spread_cost_bps: float
    impact_bps: float
    fees_bps: float
    timing_cost_bps: float
    slippage_bps: float
    participation_rate: float = field(metadata=_UNIT)
    n_fills: int = field(metadata=_U32)
    venue_contribution_bps: Dict[str, float] = field(metadata=_DEC_KEYS)
    algo: Algo
    latency_ns: LatencyStats

    def check_invariants(self) -> None:
        if self.filled_qty > self.qty:
            raise ContractError("TCAResult: filled_qty > qty")
        if abs(self.fill_rate - self.filled_qty / self.qty) > 1e-9:
            raise ContractError("TCAResult: fill_rate != filled_qty / qty")
        perold = self.delay_cost_bps + self.trading_cost_bps + self.opportunity_cost_bps
        if abs(perold - self.implementation_shortfall_bps) > 1e-9:
            raise ContractError("TCAResult: Perold identity violated")
        split = self.spread_cost_bps + self.impact_bps + self.timing_cost_bps
        if abs(split - self.trading_cost_bps) > 1e-9:
            raise ContractError("TCAResult: trading != spread + impact + timing")


# --------------------------------------------------------------------------
# Research
# --------------------------------------------------------------------------

_SPEC_SCHEMA = "research/experiment_spec.schema.json"


@contract(V.EXPERIMENT_SPEC_VERSION, _SPEC_SCHEMA + "#/$defs/Period")
class Period(Contract):
    """Half-open event-time window ``[start_ts, end_ts)``, ns."""

    start_ts: int = field(metadata=_I64)
    end_ts: int = field(metadata=_I64)

    def check_invariants(self) -> None:
        if self.end_ts < self.start_ts:
            raise ContractError("Period: end_ts < start_ts")


@contract(V.EXPERIMENT_SPEC_VERSION, _SPEC_SCHEMA)
class ExperimentSpec(Contract):
    """What an ``ExperimentRunner`` was asked to run.

    ``experiment_id`` is the runner's deterministic id for the spec
    (``research/experiments/README.md``: the first 16 hex chars of
    ``content_hash`` of the spec without its id); ``model_version`` is
    ``None`` for an unfitted (rule-based) alpha; ``configuration`` is the
    free-form, JSON-serialisable protocol (cost multiplier, folds, ...);
    ``horizon`` one of the pinned label horizons.  Periods must be ordered
    train < validation < test (walk-forward, no overlap).
    """

    experiment_id: str = field(metadata=_IDENT)
    alpha_id: str = field(metadata=_IDENT)
    dataset_version: str = field(metadata=_SHA256)
    feature_version: str = field(metadata=_SHA256)
    model_version: Optional[str]
    configuration: Dict[str, Any]
    train_period: Period
    validation_period: Period
    test_period: Period
    seed: int = field(metadata=_U64)
    horizon: str = field(metadata={"non_empty": True})

    def check_invariants(self) -> None:
        if self.model_version is not None and not is_sha256_hex(self.model_version):
            raise ContractError("ExperimentSpec: model_version must be sha256 hex")
        if not (self.train_period.end_ts <= self.validation_period.start_ts
                and self.validation_period.end_ts <= self.test_period.start_ts):
            raise ContractError("ExperimentSpec: periods must be ordered "
                                "train <= validation <= test without overlap")


@contract(V.EXPERIMENT_RESULT_VERSION, "research/experiment_result.schema.json")
class ExperimentResult(Contract):
    """What an experiment produced (``iap.validation.validate`` metrics).

    All metrics are finite doubles; a metric the runner could not compute
    is a runner error, not a value.  ``created_ts`` is event / ledger time
    (the last event timestamp the run consumed, or the ledger's own
    counter), never the wall clock; ``0`` is allowed.  ``verdict`` follows
    the pinned §20 gates and is always REJECT when ``leakage_passed`` is
    false.  ``n_experiments_in_ledger`` is the multiple-testing count at
    the time of the run.
    """

    experiment_id: str = field(metadata=_IDENT)
    alpha_id: str = field(metadata=_IDENT)
    dataset_version: str = field(metadata=_SHA256)
    feature_version: str = field(metadata=_SHA256)
    model_version: Optional[str]
    ic: float
    rank_ic: float
    t_stat: float
    nw_lags: int = field(metadata=_U32)
    hit_rate: float = field(metadata=_UNIT)
    turnover: float = field(metadata=_NONNEG)
    gross_return_bps: float
    transaction_cost_bps: float = field(metadata=_NONNEG)
    net_return_bps: float
    max_drawdown_bps: float = field(metadata=_NONNEG)
    sharpe: float
    fold_consistency: float = field(metadata=_UNIT)
    n_folds: int = field(metadata=_U32)
    leakage_passed: bool
    leakage_detail: Dict[str, Any]
    hypothesis_sign_confirmed: Optional[bool]
    verdict: Verdict
    n_experiments_in_ledger: int = field(metadata=_U64)
    git_commit: str = field(metadata={"non_empty": True})
    created_ts: int = field(metadata={"min": 0, "max": I64_MAX})

    def check_invariants(self) -> None:
        if self.model_version is not None and not is_sha256_hex(self.model_version):
            raise ContractError("ExperimentResult: model_version must be sha256 hex")
        if not self.leakage_passed and self.verdict is not Verdict.REJECT:
            raise ContractError("ExperimentResult: leakage failure forces REJECT")
        if abs((self.gross_return_bps - self.transaction_cost_bps)
               - self.net_return_bps) > 1e-9:
            raise ContractError("ExperimentResult: net != gross - cost")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

_LIFECYCLE_SCHEMA = "alpha/lifecycle_transition.schema.json"


@contract(V.LIFECYCLE_TRANSITION_VERSION, _LIFECYCLE_SCHEMA + "#/$defs/GateResult")
class GateResult(Contract):
    """One gate evaluated for a transition: ``value`` compared with
    ``threshold`` (either may be ``None`` for a boolean gate)."""

    passed: bool
    value: Optional[float]
    threshold: Optional[float]


@contract(V.LIFECYCLE_TRANSITION_VERSION, _LIFECYCLE_SCHEMA)
class LifecycleTransition(Contract):
    """An alpha moving between :class:`LifecycleState` values.

    ``policy`` names the lifecycle policy (``configs/strategies``
    ``lifecycle`` block) whose gates were applied; ``gates`` is keyed by
    gate name; ``actor`` records whether the transition was automatic or a
    human override.  Serialised states are the NAMES.
    """

    alpha_id: str = field(metadata=_IDENT)
    from_state: LifecycleState
    to_state: LifecycleState
    event_ts: int = field(metadata=_I64)
    reason: str
    gates: Dict[str, GateResult]
    policy: str = field(metadata={"non_empty": True})
    actor: Actor

    def check_invariants(self) -> None:
        if self.from_state is self.to_state:
            raise ContractError("LifecycleTransition: from_state == to_state")


# --------------------------------------------------------------------------
# Decision trace
# --------------------------------------------------------------------------


@contract(V.DECISION_TRACE_VERSION, _TRACE_SCHEMA + "#/$defs/Attribution")
class Attribution(Contract):
    """P&L attribution of one decision in bps of traded notional; signs are
    contributions (negative = cost).  ``total_bps`` = sum of the five."""

    alpha_bps: float
    spread_bps: float
    impact_bps: float
    fees_bps: float
    timing_bps: float
    total_bps: float

    def check_invariants(self) -> None:
        total = (self.alpha_bps + self.spread_bps + self.impact_bps
                 + self.fees_bps + self.timing_bps)
        if abs(total - self.total_bps) > 1e-9:
            raise ContractError("Attribution: total_bps != sum of components")


@contract(V.DECISION_TRACE_VERSION, _TRACE_SCHEMA + "#/$defs/TraceStages")
class TraceStages(Contract):
    """Everything the loop produced for one decision, stage by stage.
    Empty lists / ``None`` mean the stage did not run (e.g. no orders
    after a REJECT)."""

    signal: Tuple[AlphaSignal, ...]
    portfolio: Optional[PortfolioTarget]
    risk: Tuple[RiskDecision, ...]
    parent_orders: Tuple[ParentOrder, ...]
    child_orders: Tuple[ChildOrder, ...]
    routing: Tuple[VenueDecision, ...]
    fills: Tuple[ExecutionReport, ...]
    tca: Tuple[TCAResult, ...]
    attribution: Optional[Attribution]


@contract(V.DECISION_TRACE_VERSION, _TRACE_SCHEMA)
class DecisionTrace(Contract):
    """The auditable chain for one decision (spec: "why did we trade?").

    ``trace_id = make_trace_id(session_id, instrument_id, event_ts,
    sequence)``; the four version hashes pin the data, feature registry,
    model and configuration the decision was made under.  Rendered by
    :func:`explain`.
    """

    trace_id: str = field(metadata=_TRACE)
    session_id: str = field(metadata=_IDENT)
    instrument_id: int = field(metadata=_U32)
    event_ts: int = field(metadata=_I64)
    sequence: int = field(metadata=_U64)
    data_version: str = field(metadata=_SHA256)
    feature_version: str = field(metadata=_SHA256)
    model_version: str = field(metadata=_SHA256)
    config_version: str = field(metadata=_SHA256)
    stages: TraceStages


# --------------------------------------------------------------------------
# Human-readable rendering
# --------------------------------------------------------------------------

_LABEL_WIDTH = 12


def _line(label: str, body: str) -> str:
    return (label + ": ").ljust(_LABEL_WIDTH) + body


def _bps(value: float) -> str:
    return f"{value:+.1f} bps"


def explain(trace: DecisionTrace,
            venue_names: Optional[Mapping[int, str]] = None) -> str:
    """Render the decision chain, one block per stage::

        Order 12345
        Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
        Portfolio:  target = +20,000 shares
        Risk:       ALLOW
        Execution:  POV 15%
        SOR:        XV1 = 45%  XV2 = 35%  XV3 = 20%
        Fills:      18,000 / 20,000 (90.0%)
        TCA:        IS = 2.1 bps
        Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps

    Stages that did not run render ``(none)``.  ``venue_names`` maps venue
    ids to display names (``configs/venues/venues.json``); unnamed venues
    render as their decimal id.  ``signal[0]`` is the ACTING signal (the one
    the portfolio sized on — the ``v_order_chain`` convention): its label is
    the parent order's ``alpha_id`` when an order exists, else its
    ``model_version``; every further signal is a component of it (an
    ensemble member) and is labelled by its own ``model_version``.  Pure
    function of its inputs; the golden test pins the example above.
    """
    st = trace.stages
    parents = st.parent_orders
    names = dict(venue_names or {})
    lines = [f"Order {parents[0].parent_order_id}" if parents
             else f"Trace {trace.trace_id}"]

    alpha_label = parents[0].alpha_id if parents else None
    if st.signal:
        for i, sig in enumerate(st.signal):
            label = alpha_label if i == 0 and alpha_label else sig.model_version
            lines.append(_line(
                "Alpha",
                f"{label}  expected return = "
                f"{_bps(sig.expected_return * 1e4)}  "
                f"confidence = {sig.confidence:.2f}"))
    else:
        lines.append(_line("Alpha", "(none)"))

    if st.portfolio is not None:
        legs = [leg for leg in st.portfolio.targets
                if leg.instrument_id == trace.instrument_id] or list(st.portfolio.targets)
        body = (f"target = {legs[0].target_qty:+,d} shares" if legs
                else f"{st.portfolio.solver_status.value}  no target")
        lines.append(_line("Portfolio", body))
    else:
        lines.append(_line("Portfolio", "(none)"))

    if st.risk:
        for rd in st.risk:
            body = rd.decision.name
            if rd.decision is not Decision.ALLOW:
                body += f"  rule = {rd.rule_id}  reason = {rd.reason}"
            lines.append(_line("Risk", body))
    else:
        lines.append(_line("Risk", "(none)"))

    if parents:
        for po in parents:
            body = po.algo.value
            if "participation" in po.params:
                body += f" {po.params['participation'] * 100:.0f}%"
            lines.append(_line("Execution", body))
    else:
        lines.append(_line("Execution", "(none)"))

    child_qty = {c.child_order_id: c.qty for c in st.child_orders}
    routed: Dict[int, int] = {}
    for vd in st.routing:
        routed[vd.venue_id] = routed.get(vd.venue_id, 0) + child_qty.get(vd.child_order_id, 0)
    total_routed = sum(routed.values())
    if total_routed:
        parts = [f"{names.get(v, str(v))} = {100.0 * q / total_routed:.0f}%"
                 for v, q in sorted(routed.items())]
        lines.append(_line("SOR", "  ".join(parts)))
    else:
        lines.append(_line("SOR", "(none)"))

    target_qty = sum(po.qty for po in parents)
    filled = sum(er.filled_qty for er in st.fills)
    if target_qty:
        lines.append(_line(
            "Fills", f"{filled:,d} / {target_qty:,d} ({100.0 * filled / target_qty:.1f}%)"))
    else:
        lines.append(_line("Fills", "(none)"))

    if st.tca:
        for t in st.tca:
            lines.append(_line("TCA", f"IS = {t.implementation_shortfall_bps:.1f} bps"))
    else:
        lines.append(_line("TCA", "(none)"))

    a = st.attribution
    if a is not None:
        lines.append(_line(
            "Attribution",
            f"alpha = {_bps(a.alpha_bps)}  spread = {_bps(a.spread_bps)}  "
            f"impact = {_bps(a.impact_bps)}  fees = {_bps(a.fees_bps)}"))
    else:
        lines.append(_line("Attribution", "(none)"))
    return "\n".join(lines)

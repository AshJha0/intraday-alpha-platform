"""RiskEvent — the auditable decision record, and the pinned rule ids.

Mirrors ``rust/risk/src/event.rs`` (normative) field-for-field:
``schemas/risk/risk_event.schema.json`` (x-version 1) fixes the exact
field set and the enum codes (severity INFO=1 WARN=2 BREACH=3; decision
ALLOW=1 REJECT=2 KILL=3; scope GLOBAL / STRATEGY / INSTRUMENT / VENUE).

Every RiskEvent serialises as one JSONL line with sorted keys
(``decision, reason, rule_id, scope, scope_id, severity, timestamp``) via
:mod:`iap.risk.serialize`, so identical input sequences produce audit logs
that are byte-identical to the Rust reference and the Java port
(``tests/golden/expected_risk_audit.jsonl``).

Money inside a reason is formatted by :func:`fmt_fixed`, never by a float
formatter: the value is scaled by ``10**decimals``, rounded half away from
zero to an integer and printed as ``[-]int.frac`` — the identical IEEE-754
product in every language, so decimal ties print identically everywhere
(``2.675`` -> ``2.68`` because ``2.675 * 100`` is ``267.5`` in binary;
``24.505`` -> ``24.51``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Dict

from iap.risk.serialize import json_escape

__all__ = ["Scope", "Severity", "Decision", "RiskEvent", "Rules", "fmt_fixed"]

_I64_MAX = (1 << 63) - 1
_I64_MIN = -(1 << 63)


class Scope(Enum):
    """Decision scope (schema enum; the value is the wire string)."""

    #: Whole-firm scope (``scope_id`` is "").
    GLOBAL = "GLOBAL"
    #: One strategy (``scope_id`` = strategy id).
    STRATEGY = "STRATEGY"
    #: One instrument (``scope_id`` = decimal instrument_id).
    INSTRUMENT = "INSTRUMENT"
    #: One venue (``scope_id`` = decimal venue_id).
    VENUE = "VENUE"

    @classmethod
    def parse(cls, text: str) -> "Scope":
        """Wire string -> Scope; ``ValueError`` for anything else."""
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(f"bad scope {text!r}")


class Severity(IntEnum):
    """Severity codes (schema: INFO=1 WARN=2 BREACH=3)."""

    #: Routine (allowed orders, switch clears).
    INFO = 1
    #: A rejected order / degraded state.
    WARN = 2
    #: A limit breach or kill-switch action.
    BREACH = 3


class Decision(IntEnum):
    """Decision codes (schema: ALLOW=1 REJECT=2 KILL=3)."""

    #: Order may proceed.
    ALLOW = 1
    #: Order rejected.
    REJECT = 2
    #: A kill switch engaged / trading stopped in scope.
    KILL = 3


class Rules:
    """Pinned rule identifiers (``rust/risk/src/event.rs::rules``), in
    pre-trade evaluation order; the FIRST failing rule decides (kill
    switches always take precedence, global before strategy before
    instrument before venue). The remaining ids are audit-only records."""

    KILL_GLOBAL = "KILL_GLOBAL"
    KILL_STRATEGY = "KILL_STRATEGY"
    KILL_INSTRUMENT = "KILL_INSTRUMENT"
    KILL_VENUE = "KILL_VENUE"
    MALFORMED_ORDER = "MALFORMED_ORDER"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID"
    VENUE_DISCONNECTED = "VENUE_DISCONNECTED"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    STALE_PRICE = "STALE_PRICE"
    FAT_FINGER_QTY = "FAT_FINGER_QTY"
    FAT_FINGER_NOTIONAL = "FAT_FINGER_NOTIONAL"
    PRICE_BAND = "PRICE_BAND"
    RATE_THROTTLE = "RATE_THROTTLE"
    SELF_MATCH = "SELF_MATCH"
    POSITION_LIMIT = "POSITION_LIMIT"
    INSTRUMENT_NOTIONAL = "INSTRUMENT_NOTIONAL"
    GROSS_NOTIONAL = "GROSS_NOTIONAL"
    NET_NOTIONAL = "NET_NOTIONAL"
    DAILY_LOSS = "DAILY_LOSS"
    STRATEGY_LOSS = "STRATEGY_LOSS"
    #: Quote->reporting conversion rate missing or stale (fail-closed).
    FX_RATE_MISSING = "FX_RATE_MISSING"
    #: Engine awaits a position bootstrap (drop-copy) or state restore.
    NOT_BOOTSTRAPPED = "NOT_BOOTSTRAPPED"
    #: Order passed every check.
    ALLOW = "ALLOW"
    #: Engine is fail-closed (missing/invalid configuration).
    CONFIG_MISSING = "CONFIG_MISSING"
    # ---- audit-only records
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    KILL_SWITCH_CLEARED = "KILL_SWITCH_CLEARED"
    VENUE_DISCONNECT = "VENUE_DISCONNECT"
    VENUE_RECONNECT = "VENUE_RECONNECT"
    #: A fill was rejected as malformed / unpriceable (not applied).
    MALFORMED_FILL = "MALFORMED_FILL"
    #: A kill-switch command named a scope id the engine cannot resolve
    #: (``engage_kill``/``clear_kill`` raise and
    #: ``risk_malformed_kills_total`` increments). NEVER accompanied by a
    #: KILL_SWITCH_ENGAGED / KILL_SWITCH_CLEARED record — that pairing is
    #: exactly the phantom-halt defect this record exists to make visible.
    MALFORMED_KILL = "MALFORMED_KILL"
    #: A loss limit was overridden with approval.
    LOSS_LIMIT_OVERRIDE = "LOSS_LIMIT_OVERRIDE"
    #: The trading session rolled: daily P&L re-based.
    SESSION_ROLLED = "SESSION_ROLLED"
    #: Position bootstrap completed.
    BOOTSTRAP_COMPLETE = "BOOTSTRAP_COMPLETE"
    #: Engine state restored from a snapshot.
    STATE_RESTORED = "STATE_RESTORED"

    #: The 23 pre-trade rule ids in pinned check order (index = check number).
    CHECK_ORDER = (
        CONFIG_MISSING,
        KILL_GLOBAL,
        KILL_STRATEGY,
        KILL_INSTRUMENT,
        KILL_VENUE,
        MALFORMED_ORDER,
        UNKNOWN_INSTRUMENT,
        DUPLICATE_ORDER_ID,
        VENUE_DISCONNECTED,
        SEQUENCE_GAP,
        STALE_PRICE,
        FAT_FINGER_QTY,
        FX_RATE_MISSING,
        FAT_FINGER_NOTIONAL,
        PRICE_BAND,
        RATE_THROTTLE,
        SELF_MATCH,
        POSITION_LIMIT,
        INSTRUMENT_NOTIONAL,
        GROSS_NOTIONAL,
        NET_NOTIONAL,
        DAILY_LOSS,
        STRATEGY_LOSS,
    )


@dataclass(frozen=True)
class RiskEvent:
    """One audit-log record (schema field set; ``severity`` / ``decision``
    are the raw u8 codes exactly as the Rust struct carries them)."""

    #: Event time (ns) of the decision.
    timestamp: int
    #: Decision scope.
    scope: Scope
    #: Identifier within scope ("" for GLOBAL).
    scope_id: str
    #: Pinned rule identifier.
    rule_id: str
    #: INFO=1 WARN=2 BREACH=3.
    severity: int
    #: ALLOW=1 REJECT=2 KILL=3.
    decision: int
    #: Human-readable reason.
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise ValueError(f"scope must be a Scope, got {self.scope!r}")
        if not (1 <= int(self.severity) <= 3 and 1 <= int(self.decision) <= 3):
            raise ValueError(
                f"RiskEvent enum out of domain: severity {self.severity} "
                f"decision {self.decision}"
            )
        if not (_I64_MIN <= self.timestamp <= _I64_MAX):
            raise ValueError(f"timestamp out of i64 range: {self.timestamp}")

    def to_json_line(self) -> str:
        """Serialise as one JSONL line (sorted schema keys, no newline)."""
        return (
            '{"decision":' + str(int(self.decision))
            + ',"reason":"' + json_escape(self.reason)
            + '","rule_id":"' + json_escape(self.rule_id)
            + '","scope":"' + self.scope.value
            + '","scope_id":"' + json_escape(self.scope_id)
            + '","severity":' + str(int(self.severity))
            + ',"timestamp":' + str(self.timestamp) + "}"
        )

    @classmethod
    def from_json_line(cls, line: str) -> "RiskEvent":
        """Parse one JSONL audit line back (strict: exact field set, enum
        codes in domain); ``ValueError`` otherwise."""
        try:
            doc: Any = json.loads(line)
        except ValueError as e:
            raise ValueError(f"bad RiskEvent line: {e}") from None
        return cls.from_dict(doc)

    @classmethod
    def from_dict(cls, doc: Any) -> "RiskEvent":
        """Strict deserialisation of a parsed schema-shaped object (every
        schema field required and typed; unknown fields are ignored, as
        serde's derived deserializer does in the Rust reference)."""
        expected = {"timestamp", "scope", "scope_id", "rule_id", "severity",
                    "decision", "reason"}
        if not isinstance(doc, dict) or not expected <= set(doc):
            raise ValueError("bad RiskEvent line: missing schema field")
        if not _is_int(doc["timestamp"]) or not _is_int(doc["severity"]) \
                or not _is_int(doc["decision"]):
            raise ValueError("bad RiskEvent line: non-integer field")
        for key in ("scope", "scope_id", "rule_id", "reason"):
            if not isinstance(doc[key], str):
                raise ValueError(f"bad RiskEvent line: {key} must be a string")
        try:
            scope = Scope.parse(doc["scope"])
        except ValueError:
            raise ValueError(f"bad RiskEvent line: unknown scope {doc['scope']!r}") from None
        if not (1 <= doc["severity"] <= 3 and 1 <= doc["decision"] <= 3):
            raise ValueError(
                f"RiskEvent enum out of domain: severity {doc['severity']} "
                f"decision {doc['decision']}"
            )
        return cls(
            timestamp=doc["timestamp"],
            scope=scope,
            scope_id=doc["scope_id"],
            rule_id=doc["rule_id"],
            severity=doc["severity"],
            decision=doc["decision"],
            reason=doc["reason"],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Schema-shaped plain dict (scope as its wire string)."""
        return {
            "timestamp": self.timestamp,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "rule_id": self.rule_id,
            "severity": int(self.severity),
            "decision": int(self.decision),
            "reason": self.reason,
        }


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _round_half_away(x: float) -> int:
    """``f64::round`` for a non-negative finite ``x``: half away from zero,
    computed exactly (``x - floor(x)`` is exact in IEEE-754 for ``x >= 0``)."""
    floor = math.floor(x)
    return floor + 1 if x - floor >= 0.5 else floor


def fmt_fixed(v: float, decimals: int) -> str:
    """Fixed-decimal formatting for audit reasons, PINNED for cross-language
    byte parity (``rust/risk::fmt_fixed``): ``units = round_half_away(|v| *
    10**decimals)`` as an i64 (saturating like Rust's ``as i64``; NaN ->
    0), printed as ``[-]int.frac`` with exactly ``decimals`` fraction
    digits; a value that rounds to zero prints without a sign."""
    if decimals < 0:
        raise ValueError("decimals must be >= 0")
    scale_i = 10 ** decimals
    scaled = abs(v) * float(scale_i)
    if math.isnan(scaled):
        units = 0
    elif math.isinf(scaled) or scaled >= 9.223372036854776e18:
        units = _I64_MAX
    else:
        units = _round_half_away(scaled)
    sign = "-" if (v < 0.0 and units > 0) else ""
    if decimals == 0:
        return f"{sign}{units}"
    return f"{sign}{units // scale_i}.{units % scale_i:0{decimals}d}"

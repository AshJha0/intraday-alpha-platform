"""RiskEvent — the auditable decision record, and the pinned rule ids.

Mirrors ``rust/risk/src/event.rs`` (normative) field-for-field:
``schemas/risk/risk_event.schema.json`` (x-version 2) fixes the exact
field set and the enum codes (severity INFO=1 WARN=2 BREACH=3; decision
ALLOW=1 REJECT=2 KILL=3; scope GLOBAL / STRATEGY / INSTRUMENT / VENUE).

Every RiskEvent serialises as one JSONL line with sorted keys
(``decision, prev_sha256, reason, rule_id, scope, scope_id, seq, severity,
timestamp``) via
:mod:`iap.risk.serialize`, so identical input sequences produce audit logs
that are byte-identical to the Rust reference and the Java port
(``tests/golden/expected_risk_audit.jsonl``).

Money inside a reason is formatted by :func:`fmt_fixed`, never by a float
formatter: the value is scaled by ``10**decimals``, rounded half away from
zero to an integer and printed as ``[-]int.frac`` — the identical IEEE-754
product in every language, so decimal ties print identically everywhere
(``2.675`` -> ``2.68`` because ``2.675 * 100`` is ``267.5`` in binary;
``24.505`` -> ``24.51``).

Tamper evidence (x-version 2, review item G-P0-1): every record an engine
emits carries ``seq`` (1, 2, 3, ... per engine, continued across a
snapshot/restore) and ``prev_sha256``, the lowercase hex SHA-256 of the
previous record's JSONL line (UTF-8 bytes, no newline), or
:data:`GENESIS_SHA256` for the first record. Editing, dropping, inserting
or reordering a line breaks the chain (:func:`verify_audit_chain`).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any

from iap.risk.serialize import json_escape

__all__ = [
    "Scope",
    "Severity",
    "Decision",
    "RiskEvent",
    "Rules",
    "fmt_fixed",
    "GENESIS_SHA256",
    "line_sha256",
    "verify_audit_chain",
    "unchained_line",
]

#: ``prev_sha256`` of the first record of a chain.
GENESIS_SHA256 = "0" * 64

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
    def parse(cls, text: str) -> Scope:
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
    #: A loss-limit override was refused (too few distinct approvers, or
    #: above ``overrides.max_loss_override_multiple`` x the configured limit).
    LOSS_LIMIT_OVERRIDE_REJECTED = "LOSS_LIMIT_OVERRIDE_REJECTED"
    # ---- SEC 15c3-5 pre-trade set (checks 23-29, ``pre_trade`` in risk.json)
    #: Instrument on ``pre_trade.restricted_instruments``.
    RESTRICTED_INSTRUMENT = "RESTRICTED_INSTRUMENT"
    #: The strategy would exceed ``pre_trade.max_open_orders``.
    MAX_OPEN_ORDERS = "MAX_OPEN_ORDERS"
    #: The strategy's open notional would exceed ``pre_trade.max_open_notional``.
    MAX_OPEN_NOTIONAL = "MAX_OPEN_NOTIONAL"
    #: The strategy's projected position would exceed
    #: ``pre_trade.max_strategy_position_qty``.
    STRATEGY_POSITION = "STRATEGY_POSITION"
    #: The firm's projected session quantity would exceed the participation cap.
    PARTICIPATION = "PARTICIPATION"
    #: The strategy's cancel budget is spent.
    CANCEL_RATE = "CANCEL_RATE"
    #: The venue's message budget is spent.
    VENUE_MESSAGE_RATE = "VENUE_MESSAGE_RATE"
    #: A bounded per-key state map is full (``pre_trade.max_tracked_keys``).
    STATE_CAPACITY = "STATE_CAPACITY"

    #: The pre-trade rule ids in pinned check order (index = check number;
    #: 23-29 are the 15c3-5 set, 30 the state-capacity reject of check 7).
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
        RESTRICTED_INSTRUMENT,
        MAX_OPEN_ORDERS,
        MAX_OPEN_NOTIONAL,
        STRATEGY_POSITION,
        PARTICIPATION,
        CANCEL_RATE,
        VENUE_MESSAGE_RATE,
        STATE_CAPACITY,
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
    #: Position in the engine's audit chain (1-based; 0 = not yet emitted).
    seq: int = 0
    #: Hex SHA-256 of the previous record's JSONL line ("" = not yet emitted).
    prev_sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope):
            raise ValueError(f"scope must be a Scope, got {self.scope!r}")
        if not (1 <= int(self.severity) <= 3 and 1 <= int(self.decision) <= 3):
            raise ValueError(
                f"RiskEvent enum out of domain: severity {self.severity} decision {self.decision}"
            )
        if not (_I64_MIN <= self.timestamp <= _I64_MAX):
            raise ValueError(f"timestamp out of i64 range: {self.timestamp}")

    def to_json_line(self) -> str:
        """Serialise as one JSONL line (sorted schema keys, no newline)."""
        return (
            '{"decision":'
            + str(int(self.decision))
            + ',"prev_sha256":"'
            + json_escape(self.prev_sha256)
            + '","reason":"'
            + json_escape(self.reason)
            + '","rule_id":"'
            + json_escape(self.rule_id)
            + '","scope":"'
            + self.scope.value
            + '","scope_id":"'
            + json_escape(self.scope_id)
            + '","seq":'
            + str(self.seq)
            + ',"severity":'
            + str(int(self.severity))
            + ',"timestamp":'
            + str(self.timestamp)
            + "}"
        )

    @classmethod
    def from_json_line(cls, line: str) -> RiskEvent:
        """Parse one JSONL audit line back (strict: exact field set, enum
        codes in domain); ``ValueError`` otherwise."""
        try:
            doc: Any = json.loads(line)
        except ValueError as e:
            raise ValueError(f"bad RiskEvent line: {e}") from None
        return cls.from_dict(doc)

    @classmethod
    def from_dict(cls, doc: Any) -> RiskEvent:
        """Strict deserialisation of a parsed schema-shaped object (every
        schema field required and typed; unknown fields are ignored, as
        serde's derived deserializer does in the Rust reference; the chain
        fields ``seq`` / ``prev_sha256`` default to 0 / "" when absent)."""
        expected = {"timestamp", "scope", "scope_id", "rule_id", "severity", "decision", "reason"}
        if not isinstance(doc, dict) or not expected <= set(doc):
            raise ValueError("bad RiskEvent line: missing schema field")
        if (
            not _is_int(doc["timestamp"])
            or not _is_int(doc["severity"])
            or not _is_int(doc["decision"])
        ):
            raise ValueError("bad RiskEvent line: non-integer field")
        for key in ("scope", "scope_id", "rule_id", "reason"):
            if not isinstance(doc[key], str):
                raise ValueError(f"bad RiskEvent line: {key} must be a string")
        seq = doc.get("seq", 0)
        prev = doc.get("prev_sha256", "")
        if not _is_int(seq) or not (0 <= seq <= (1 << 64) - 1):
            raise ValueError("bad RiskEvent line: seq must be a u64")
        if not isinstance(prev, str):
            raise ValueError("bad RiskEvent line: prev_sha256 must be a string")
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
            seq=seq,
            prev_sha256=prev,
        )

    def to_dict(self) -> dict[str, Any]:
        """Schema-shaped plain dict (scope as its wire string)."""
        return {
            "timestamp": self.timestamp,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "rule_id": self.rule_id,
            "severity": int(self.severity),
            "decision": int(self.decision),
            "reason": self.reason,
            "seq": self.seq,
            "prev_sha256": self.prev_sha256,
        }


def line_sha256(line: str) -> str:
    """Lowercase hex SHA-256 of one audit line (UTF-8, without newline)."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def verify_audit_chain(
    text: str, first_seq: int = 1, prev_sha256: str = GENESIS_SHA256
) -> tuple[int, str]:
    """Verify an audit JSONL chain: consecutive ``seq`` from ``first_seq``
    and each ``prev_sha256`` the hash of the line before (``prev_sha256``
    for the first). Returns ``(last seq, head sha256)``; ``ValueError``
    names the first broken line. Lines are split on LF only."""
    seq, head = first_seq - 1, prev_sha256
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for n, line in enumerate(lines):
        ev = RiskEvent.from_json_line(line)
        if ev.seq != seq + 1:
            raise ValueError(f"audit chain broken at line {n + 1}: seq {ev.seq}, want {seq + 1}")
        if ev.prev_sha256 != head:
            raise ValueError(f"audit chain broken at line {n + 1}: prev_sha256 mismatch")
        seq, head = ev.seq, line_sha256(line)
    return seq, head


def unchained_line(line: str) -> str:
    """``line`` without its chain fields: the record's content, comparable
    between an unbroken run and a restored one (whose chain positions are
    shifted by the ``STATE_RESTORED`` record)."""
    ev = RiskEvent.from_json_line(line)
    return line.replace(f',"prev_sha256":"{ev.prev_sha256}"', "", 1).replace(
        f',"seq":{ev.seq},', ",", 1
    )


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
    scale_i = 10**decimals
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

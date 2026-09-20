"""Canonical JSON serialisation, byte-identical to ``serde_json``.

The Rust risk engine (``rust/risk``, normative) writes its audit lines with
``serde_json::Value::to_string`` and its snapshot golden with
``serde_json::to_string_pretty``. Both use a sorted-key object map (the
default ``BTreeMap``), the ``ryu`` shortest round-trip float printer and a
fixed escaping table. This module reproduces those three rules exactly so
that a Python audit log / snapshot compares byte-for-byte with the Rust
output (``tests/golden/expected_risk_audit.jsonl``,
``expected_risk_snapshot.json``).

Pinned rules:

- **Strings**: ``"`` -> ``\\"``, ``\\`` -> ``\\\\``, the two-character escapes
  ``\\b \\f \\n \\r \\t``, every other control character below 0x20 as
  ``\\u00xx`` (lowercase hex); everything else — including non-ASCII and
  DEL — is emitted raw (UTF-8).
- **Floats**: the shortest digit string that round-trips (Python's
  ``repr`` and ``ryu`` agree on the digits), laid out like ``ryu``'s
  pretty printer: ``12340000000.0`` / ``12.34`` / ``0.001234`` when the
  decimal point lands within ``-5 < kk <= 16`` (``kk`` = position of the
  point relative to the digits), otherwise ``1e+16`` / ``1.234e-7`` style
  scientific notation with a signed, unpadded exponent.
  ``-0.0`` prints as ``-0.0``. Non-finite values print as ``null``
  (serde_json's behaviour for NaN/inf).
- **Objects**: keys sorted by code point (identical to byte order for
  UTF-8); ``bool`` before ``int`` in the type dispatch because Python's
  ``bool`` is an ``int`` subclass.
- **Pretty layout** (``to_string_pretty``): two-space indent, ``"key": value``,
  one element per line, empty containers as ``{}`` / ``[]``, no trailing
  newline (the golden writer appends one).
"""

from __future__ import annotations

import math
from typing import Any, List

__all__ = ["json_escape", "format_f64", "rust_display_f64", "to_canonical_json"]

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def json_escape(s: str) -> str:
    """Escape ``s`` for a JSON string body exactly like ``serde_json``."""
    out: List[str] = []
    for ch in s:
        esc = _SHORT_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _shortest_digits(v: float) -> tuple[str, int]:
    """``(digits, k)`` with ``v == int(digits) * 10**k`` for a finite ``v > 0``,
    ``digits`` the shortest round-trip digit string without leading or
    trailing zeros (the same digits ``ryu`` produces)."""
    text = repr(v)
    mantissa, _, exp = text.partition("e")
    k = int(exp) if exp else 0
    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part).lstrip("0")
    k -= len(frac_part)
    stripped = digits.rstrip("0")
    k += len(digits) - len(stripped)
    return stripped, k


def format_f64(v: float) -> str:
    """Format a float exactly like ``serde_json`` (``ryu`` pretty layout)."""
    if math.isnan(v) or math.isinf(v):
        return "null"
    sign = "-" if math.copysign(1.0, v) < 0 else ""
    if v == 0.0:
        return sign + "0.0"
    digits, k = _shortest_digits(abs(v))
    length = len(digits)
    kk = length + k  # 10^(kk-1) <= |v| < 10^kk
    if 0 <= k and kk <= 16:
        # 1234e7 -> 12340000000.0
        return sign + digits + "0" * (kk - length) + ".0"
    if 0 < kk <= 16:
        # 1234e-2 -> 12.34
        return sign + digits[:kk] + "." + digits[kk:]
    if -5 < kk <= 0:
        # 1234e-6 -> 0.001234
        return sign + "0." + "0" * (-kk) + digits
    # scientific: 1e+30 / 1.234e+33 / 1.5e-7 (signed, unpadded exponent)
    exp10 = kk - 1
    exp_text = f"e{'+' if exp10 >= 0 else '-'}{abs(exp10)}"
    if length == 1:
        return f"{sign}{digits}{exp_text}"
    return f"{sign}{digits[0]}.{digits[1:]}{exp_text}"


def rust_display_f64(v: float) -> str:
    """Rust ``{}`` (``Display``) of an f64: shortest round-trip digits laid
    out in plain decimal notation (never scientific), integral values
    without a fraction (``2`` not ``2.0``), ``-0`` for negative zero,
    ``NaN`` / ``inf`` / ``-inf``. Used for the ``urgency`` value in a
    ``MALFORMED_ORDER`` reason."""
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    sign = "-" if math.copysign(1.0, v) < 0 else ""
    if v == 0.0:
        return sign + "0"
    digits, k = _shortest_digits(abs(v))
    kk = len(digits) + k
    if k >= 0:
        return sign + digits + "0" * k
    if kk > 0:
        return sign + digits[:kk] + "." + digits[kk:]
    return sign + "0." + "0" * (-kk) + digits


def _write(value: Any, pretty: bool, depth: int, out: List[str]) -> None:
    if value is None:
        out.append("null")
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        out.append(format_f64(value))
    elif isinstance(value, str):
        out.append('"' + json_escape(value) + '"')
    elif isinstance(value, dict):
        if not value:
            out.append("{}")
            return
        keys = sorted(value)
        if not all(isinstance(k, str) for k in keys):
            raise ValueError("JSON object keys must be strings")
        out.append("{")
        for i, key in enumerate(keys):
            if i:
                out.append(",")
            if pretty:
                out.append("\n" + "  " * (depth + 1))
            out.append('"' + json_escape(key) + '":')
            if pretty:
                out.append(" ")
            _write(value[key], pretty, depth + 1, out)
        if pretty:
            out.append("\n" + "  " * depth)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        if not value:
            out.append("[]")
            return
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            if pretty:
                out.append("\n" + "  " * (depth + 1))
            _write(item, pretty, depth + 1, out)
        if pretty:
            out.append("\n" + "  " * depth)
        out.append("]")
    else:
        raise ValueError(f"value of type {type(value).__name__} is not JSON-serialisable")


def to_canonical_json(value: Any, pretty: bool = False) -> str:
    """Serialise ``value`` (None/bool/int/float/str/dict/list) as
    ``serde_json`` would: ``pretty=False`` mirrors ``Value::to_string``,
    ``pretty=True`` mirrors ``to_string_pretty``. No trailing newline."""
    out: List[str] = []
    _write(value, pretty, 0, out)
    return "".join(out)

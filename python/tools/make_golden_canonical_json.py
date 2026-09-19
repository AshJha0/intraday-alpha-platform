#!/usr/bin/env python3
"""Generate ``tests/golden/expected_canonical_json.json``.

The canonical JSON rules of ``iap.contracts.versions.canonical_json`` are the
byte-level contract behind every content hash, trace id and replay digest on
the platform (``iap.trace.digest``).  Every port (Java, Rust, C++) must
produce the same bytes, so this golden pins:

* ``float_repr`` — Python ``float.__repr__`` layout for a table of edge cases
  plus 2,000 SplitMix64-seeded doubles (shortest round-trip digits; exponent
  form when ``exp < -4`` or ``exp >= 16``; ``e-05`` / ``e+16`` two-digit
  signed exponents; ``.0`` on integral values);
* ``string_escape`` — ``ensure_ascii`` escaping: ``\\" \\\\ \\n \\r \\t \\b \\f``,
  other control characters as ``\\u00XX``, non-ASCII as ``\\uXXXX`` (UTF-16
  surrogate pairs for astral code points), ``/`` NOT escaped;
* ``documents`` — nested objects with keys sorted by Unicode code point
  (sorting happens on the raw key string, before escaping), arrays kept in
  order, ``null``/``true``/``false``, big u64 ids as exact decimals;
* ``rejects`` — inputs canonical_json refuses (NaN, ±Inf, non-string keys);
* ``content_hash`` / ``trace_id`` / ``trace_digest`` known answers, the
  latter over the pinned example :class:`~iap.contracts.types.DecisionTrace`
  from ``tests/golden/expected_contracts_examples.json``.

Refuses to overwrite an existing golden unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.contracts.examples import example_trace  # noqa: E402
from iap.contracts.ids import make_trace_id  # noqa: E402
from iap.contracts.versions import canonical_json, content_hash  # noqa: E402
from iap.core.rng import SplitMix64  # noqa: E402
from iap.trace.digest import TraceDigest  # noqa: E402

OUT = ROOT / "tests" / "golden" / "expected_canonical_json.json"
SEED = 0x5EED_CA7A_1

EDGE_FLOATS = [
    0.0, -0.0, 1.0, -1.0, 0.5, 0.1, 0.2, 0.30000000000000004, 1.5, 2.5,
    100.0, 1e15, 1e16, 1e17, 123456789012345680.0, 9007199254740992.0,
    9007199254740993.0, 0.0001, 0.00001, 0.000123, 1e-05, 1e-7, 1.5e-10,
    1e-300, 5e-324, 2.2250738585072014e-308, 1.7976931348623157e308,
    3.141592653589793, 2.718281828459045, 1e21, 1e22, 1.0000000000000002,
    0.9999999999999999, 42.0, 4.2, 0.042, 4200.0, 4.2e-5, 4.2e16, 4.2e15,
    -4.2e-5, 12345.6789, 1e-4, 9.999e-5, 1234567890123456.0,
    12345678901234567.0, 0.000999, 1e16 - 2, 255.0, 65535.0, 4294967295.0,
]


def seeded_floats(n: int) -> list[float]:
    """``n`` deterministic doubles spanning magnitudes and sign."""
    rng = SplitMix64(SEED)
    out: list[float] = []
    while len(out) < n:
        bits = rng.next_u64()
        # Uniform-in-bit-pattern doubles cover every exponent; skip non-finite.
        value = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if math.isfinite(value):
            out.append(value)
        # Also include "ordinary" magnitudes analysts actually see.
        scaled = (rng.uniform() - 0.5) * 10.0 ** (int(rng.uniform() * 24) - 12)
        if math.isfinite(scaled):
            out.append(scaled)
    return out[:n]


STRING_CASES = [
    "", "plain", "with space", "quote\"inside", "back\\slash", "slash/kept",
    "line\nfeed", "car\rreturn", "tab\tchar", "bell\x07", "back\bspace",
    "form\ffeed", "nul\x00byte", "del\x7f", "é", "naïve café", "€100",
    "日本語", "emoji 😀", "𝔘𝔫𝔦𝔠𝔬𝔡𝔢", "mixed é😀\n\"\\", "  ",
    "unicode key ordering: Z z a A", "퟿",
]

DOCUMENTS = [
    {},
    [],
    {"b": 1, "a": 2, "A": 3, "Z": 4, "z": 5, "_": 6, "0": 7, "é": 8, "aa": 9, "a0": 10},
    {"nested": {"y": [1, 2, {"q": None, "p": True, "o": False}], "x": {"k": "v"}}},
    {"ids": {"u64_max": 18446744073709551615, "i64_min": -9223372036854775808,
             "i64_max": 9223372036854775807, "two53_plus_one": 9007199254740993}},
    {"floats": [0.0, -0.0, 1e16, 1e-05, 0.1, 100.0, 2.5, 1e22]},
    {"arr": [[], {}, [[]], [{}]], "s": "é😀", "n": None},
    {"key with spaces": 1, "key\twith\ttabs": 2, "key\"quoted\"": 3},
    {"trace_id": "8b9fed6896d01463e64c4de915b0614b", "sequence": 500,
     "event_ts": 1787578700000000000, "instrument_id": 1},
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="overwrite an existing golden")
    args = parser.parse_args()
    if OUT.exists() and not args.force:
        print(f"refusing to overwrite {OUT} (use --force)", file=sys.stderr)
        return 2

    floats = EDGE_FLOATS + seeded_floats(2000)
    float_cases = []
    for value in floats:
        bits = struct.unpack("<Q", struct.pack("<d", value))[0]
        float_cases.append({"bits_hex": f"{bits:016x}", "repr": canonical_json(value)})

    string_cases = [{"input_codepoints": [ord(c) for c in s], "json": canonical_json(s)}
                    for s in STRING_CASES]

    documents = [{"canonical": canonical_json(d), "sha256": content_hash(d)} for d in DOCUMENTS]

    rejects = [
        {"case": "nan", "reason": "non-finite float"},
        {"case": "inf", "reason": "non-finite float"},
        {"case": "-inf", "reason": "non-finite float"},
        {"case": "nested nan", "reason": "non-finite float anywhere in the document"},
        {"case": "integer key", "reason": "dict keys must be strings"},
    ]
    for probe in (float("nan"), float("inf"), float("-inf"), {"a": [1, {"b": float("nan")}]}, {1: "x"}):
        try:
            canonical_json(probe)
        except ValueError:
            continue
        raise AssertionError(f"canonical_json accepted {probe!r}")

    trace = example_trace()
    line = canonical_json(trace.to_dict())
    digest = TraceDigest()
    digest.update(trace)
    digest_twice = TraceDigest()
    digest_twice.update(trace)
    digest_twice.update(trace)

    golden = {
        "x-version": 1,
        "description": (
            "Byte-level contract of iap.contracts.versions.canonical_json and "
            "iap.trace.digest.TraceDigest; every language port must reproduce "
            "each field exactly (see python/tools/make_golden_canonical_json.py)."
        ),
        "seed": SEED,
        "rules": {
            "keys": "sorted by Unicode code point of the raw key, recursively",
            "separators": [",", ":"],
            "ascii": "non-ASCII escaped as \\uXXXX (UTF-16 surrogate pairs above U+FFFF); '/' not escaped",
            "floats": "shortest round-trip digits; exponent form iff decimal exponent < -4 or >= 16; "
                      "exponent written as e-05 / e+16 (sign, at least two digits); integral values keep '.0'",
            "ints": "exact decimal, i64/u64 domain",
            "literals": ["null", "true", "false"],
            "line_hash": "sha256 over ascii(line) + '\\n' per trace, concatenated",
        },
        "float_repr": float_cases,
        "string_escape": string_cases,
        "documents": documents,
        "rejects": rejects,
        "trace_id": {
            "inputs": {"session_id": "golden-session-2026-09-19", "instrument_id": 1,
                       "event_ts": 1787578700000000000, "sequence": 500},
            "preimage": "golden-session-2026-09-19|1|1787578700000000000|500",
            "expected": make_trace_id("golden-session-2026-09-19", 1, 1787578700000000000, 500),
        },
        "trace_digest": {
            "example_source": "tests/golden/expected_contracts_examples.json (DecisionTrace example)",
            "line_sha256": hashlib.sha256(line.encode("ascii")).hexdigest(),
            "line_length": len(line),
            "digest_one_trace": digest.hexdigest(),
            "digest_same_trace_twice": digest_twice.hexdigest(),
            "digest_empty_stream": hashlib.sha256(b"").hexdigest(),
        },
    }
    OUT.write_text(json.dumps(golden, indent=2, sort_keys=False) + "\n", encoding="ascii")
    print(f"wrote {OUT} ({len(float_cases)} float cases, {len(string_cases)} string cases, "
          f"{len(documents)} documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

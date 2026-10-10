#!/usr/bin/env python3
"""Enforce the polyglot policy of docs/POLYGLOT.md (IAP_Next_Releases_Plan E3).

The platform keeps several copies of the same component in different
languages (book in four, features and risk in three or four, lifecycle in
three).  docs/POLYGLOT.md decides, per copy, whether it is CANONICAL (where
new behaviour lands first) or FROZEN (kept, golden-pinned, no new features).
``tests/harness/polyglot_policy.json`` is the machine-readable list of the
FROZEN paths; this script is the gate that makes the decision stick.

Rules, checked over the diff ``<base>...HEAD``:

1. A change to a FROZEN path (or to the policy file itself) fails unless a
   commit message in ``<base>..HEAD`` or the pull-request body (environment
   variable ``POLYGLOT_PR_BODY``) contains a line that starts with ``POLYGLOT-OVERRIDE: <reason>``
   with a non-empty reason.  The legitimate reason is propagating a pinned
   semantics change that the canonical copy already made, together with the
   regenerated golden fixture that proves parity.
2. A FROZEN file that declares a function, method, type or file that did not
   exist at ``<base>`` is a new feature in a frozen copy and fails unless the
   override line also contains the token ``new-api``
   (``POLYGLOT-OVERRIDE: new-api <reason>``) — a deliberately louder marker.
3. Consistency: every FROZEN path exists in the tree and is named in
   docs/POLYGLOT.md, so the document and the gate cannot drift apart.

Parity tests are not touched by this script: it only reads the diff.

Usage::

    python3 tests/harness/check_polyglot_policy.py --base origin/main
    python3 tests/harness/check_polyglot_policy.py --self-test
    python3 tests/harness/check_polyglot_policy.py --base <sha> --advisory

Exit 0 = policy holds (or --advisory), 1 = violation, 2 = usage / git error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "tests" / "harness" / "polyglot_policy.json"

_JAVA_KEYWORDS = frozenset(
    {
        "return",
        "new",
        "throw",
        "else",
        "case",
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "do",
        "try",
        "yield",
        "assert",
    }
)
_RUST_FN = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)"
)
_RUST_TYPE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|mod)\s+([A-Za-z_]\w*)"
)
_JAVA_TYPE = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")
_JAVA_METHOD = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
    r"(?:<[^>]+>\s+)?"
    r"([A-Za-z_][\w<>\[\],.? ]*?)\s+([A-Za-z_]\w*)\s*\([^;]*$"
)
_JAVA_CTOR = re.compile(r"^\s*(?:public|protected|private)\s+([A-Z]\w*)\s*\([^;]*$")
_CPP_TYPE = re.compile(
    r"^\s*(?:class|struct|enum\s+class|enum)\s+([A-Za-z_]\w*)\s*(?:final\s*)?[:{]"
)
_CPP_FN = re.compile(
    r"^\s*(?:[\w:<>,*&\s]+?[\s*&])?((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const)?\s*(?:noexcept)?\s*(?:override)?\s*\{"
)
_CPP_NOT_FN = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof", "do"})


def declarations(path: str, text: str) -> set[str]:
    """The names a source file declares (functions, methods, types)."""
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*", "#")):
            continue
        if path.endswith(".rs"):
            for rx in (_RUST_FN, _RUST_TYPE):
                m = rx.match(line)
                if m:
                    names.add(m.group(1))
        elif path.endswith(".java"):
            m = _JAVA_TYPE.search(line)
            if m and not stripped.startswith(("return", "throw")):
                names.add(m.group(1))
            if stripped.endswith(";"):
                continue
            m = _JAVA_METHOD.match(line)
            if (
                m
                and m.group(1).split()[-1] not in _JAVA_KEYWORDS
                and m.group(2) not in _JAVA_KEYWORDS
            ):
                if "=" not in line.split("(")[0]:
                    names.add(m.group(2))
            m = _JAVA_CTOR.match(line)
            if m:
                names.add(m.group(1))
        elif path.endswith((".cpp", ".hpp", ".h", ".cc")):
            m = _CPP_TYPE.match(line)
            if m:
                names.add(m.group(1))
            m = _CPP_FN.match(line)
            if (
                m
                and m.group(1).split("::")[-1] not in _CPP_NOT_FN
                and "=" not in line.split("(")[0]
            ):
                names.add(m.group(1))
    return names


@dataclass
class Markers:
    override: bool = False
    new_api: bool = False
    reasons: list[str] = field(default_factory=list)


def parse_markers(texts: list[str], marker: str, new_api_token: str) -> Markers:
    out = Markers()
    for text in texts:
        for line in (text or "").splitlines():
            # Only a line that STARTS with the marker counts: prose that quotes
            # it ("`POLYGLOT-OVERRIDE: <reason>` line ...") is not an override.
            stripped = line.strip()
            if not stripped.startswith(marker):
                continue
            reason = stripped[len(marker) :].strip()
            if not reason or reason.startswith("<"):
                continue
            out.override = True
            out.reasons.append(reason)
            if new_api_token in reason.split():
                out.new_api = True
    return out


def is_frozen(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(p) for p in prefixes)


def evaluate(
    changes: list[tuple[str, str, str | None, str | None]],
    policy: dict,
    markers: Markers,
) -> list[str]:
    """Return the violations for ``changes`` = (status, path, old_text, new_text)."""
    prefixes = [f["path"] for f in policy["frozen"]]
    protected = set(policy.get("protected", []))
    errors: list[str] = []
    touched: list[str] = []
    for status, path, old, new in changes:
        if path in protected:
            touched.append(path)
            continue
        if not is_frozen(path, prefixes):
            continue
        touched.append(path)
        if new is None:  # deleted: a retirement, needs the override like any change
            continue
        added = declarations(path, new) - (declarations(path, old) if old is not None else set())
        if status.startswith("A"):
            added = added or {Path(path).name}
        if added and not markers.new_api:
            errors.append(
                f"{path}: new declaration(s) in a FROZEN copy: {', '.join(sorted(added))} "
                f"(new features land in the canonical copy; a parity port of a new pinned API "
                f"needs '{policy['override_marker']} {policy['new_api_token']} <reason>')"
            )
    if touched and not markers.override:
        for path in touched:
            errors.append(
                f"{path}: FROZEN copy changed without a '{policy['override_marker']} <reason>' line "
                f"in a commit message or the PR body (docs/POLYGLOT.md)"
            )
    return errors


def consistency(policy: dict) -> list[str]:
    errors = []
    doc = (ROOT / policy["doc"]).read_text(encoding="utf-8")
    for entry in policy["frozen"]:
        p = entry["path"]
        if not (ROOT / p).is_dir():
            errors.append(f"policy: FROZEN path {p} does not exist")
        if p.rstrip("/") not in doc:
            errors.append(f"policy: FROZEN path {p} is not named in {policy['doc']}")
    return errors


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def _show(rev: str, path: str) -> str | None:
    try:
        return _git("show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return None


def collect(base: str) -> tuple[list[tuple[str, str, str | None, str | None]], list[str]]:
    merge_base = _git("merge-base", base, "HEAD").strip()
    changes = []
    for line in _git("diff", "--name-status", "--no-renames", f"{merge_base}", "HEAD").splitlines():
        status, path = line.split("\t", 1)
        old = None if status.startswith("A") else _show(merge_base, path)
        new = None if status.startswith("D") else _show("HEAD", path)
        changes.append((status, path, old, new))
    messages = [m for m in _git("log", "--format=%B%x00", f"{merge_base}..HEAD").split("\x00")]
    return changes, messages


def self_test(policy: dict) -> list[str]:
    """Known answers for the extractor and the decision logic (no git needed)."""
    fails = []
    rs_old = "pub fn apply(&mut self) {}\nfn helper() {}\npub struct Book;\n"
    rs_new = rs_old + "pub fn new_feature(x: i64) -> i64 { x }\n"
    java_old = (
        "public final class OrderBook {\n  public OrderBook(int v) {\n  }\n"
        "  public long bestBid() {\n    return foo(1);\n  }\n"
        "  private static <T> List<T> sorted(List<T> xs) {\n    if (x) {\n    }\n  }\n}\n"
    )
    java_new = java_old.replace(
        "}\n}\n", "}\n  public double spread(int lvl) {\n    return 0;\n  }\n}\n"
    )
    cpp_old = "namespace iap {\nstd::string Value::dump() const {\n  if (x) {\n  }\n}\n}\n"
    cpp_new = cpp_old + "int canonical_extra(int a) {\n  return a;\n}\n"
    expect = {
        ("a.rs", rs_old): {"apply", "helper", "Book"},
        ("A.java", java_old): {"OrderBook", "bestBid", "sorted"},
        ("a.cpp", cpp_old): {"Value::dump"},
    }
    for (path, text), want in expect.items():
        got = declarations(path, text)
        if got != want:
            fails.append(f"self-test: declarations({path}) = {sorted(got)}, want {sorted(want)}")
    if declarations("A.java", java_new) - declarations("A.java", java_old) != {"spread"}:
        fails.append("self-test: java new method not detected")
    if declarations("a.cpp", cpp_new) - declarations("a.cpp", cpp_old) != {"canonical_extra"}:
        fails.append("self-test: cpp new function not detected")
    frozen = policy["frozen"][0]["path"] + "x.rs"
    m_none = parse_markers(["fix: something"], policy["override_marker"], policy["new_api_token"])
    m_empty = parse_markers(
        ["POLYGLOT-OVERRIDE:   "], policy["override_marker"], policy["new_api_token"]
    )
    m_ok = parse_markers(
        ["x\nPOLYGLOT-OVERRIDE: port risk fix #12\n"],
        policy["override_marker"],
        policy["new_api_token"],
    )
    m_api = parse_markers(
        ["POLYGLOT-OVERRIDE: new-api port pinned API"],
        policy["override_marker"],
        policy["new_api_token"],
    )
    m_prose = parse_markers(
        ["needs a `POLYGLOT-OVERRIDE: fix` line\nPOLYGLOT-OVERRIDE: <reason>"],
        policy["override_marker"],
        policy["new_api_token"],
    )
    if m_empty.override:
        fails.append("self-test: an empty override reason must not count")
    if m_prose.override:
        fails.append("self-test: a quoted or placeholder override must not count")
    cases = [
        ([("M", "python/src/iap/x.py", "a", "b")], m_none, 0),
        ([("M", frozen, rs_old, rs_old + "// comment\n")], m_none, 1),
        ([("M", frozen, rs_old, rs_old + "// comment\n")], m_ok, 0),
        ([("M", frozen, rs_old, rs_new)], m_ok, 1),
        ([("M", frozen, rs_old, rs_new)], m_api, 0),
        ([("A", frozen, None, "// only a comment\n")], m_ok, 1),
        ([("D", frozen, rs_old, None)], m_none, 1),
        ([("D", frozen, rs_old, None)], m_ok, 0),
        ([("M", "tests/harness/polyglot_policy.json", "{}", "{ }")], m_none, 1),
    ]
    for i, (changes, markers, want_fail) in enumerate(cases):
        got = 1 if evaluate(changes, policy, markers) else 0
        if got != want_fail:
            fails.append(f"self-test: case {i} expected {'fail' if want_fail else 'pass'}")
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="git revision to diff against (merge-base with HEAD)")
    ap.add_argument(
        "--self-test", action="store_true", help="run the built-in known answers and exit"
    )
    ap.add_argument("--advisory", action="store_true", help="report violations but exit 0")
    args = ap.parse_args(argv)
    policy = json.loads(POLICY.read_text(encoding="utf-8"))

    errors = consistency(policy)
    if args.self_test:
        errors += self_test(policy)
        for e in errors:
            print(f"FAIL {e}")
        print(
            f"polyglot policy self-test: {'FAIL' if errors else 'PASS'} ({len(policy['frozen'])} FROZEN paths)"
        )
        return 1 if errors else 0
    if not args.base:
        ap.error("--base is required (or --self-test)")
    try:
        changes, messages = collect(args.base)
    except subprocess.CalledProcessError as exc:
        print(f"git failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    markers = parse_markers(
        messages + [os.environ.get("POLYGLOT_PR_BODY", "")],
        policy["override_marker"],
        policy["new_api_token"],
    )
    errors += evaluate(changes, policy, markers)
    frozen_touched = [
        c[1] for c in changes if is_frozen(c[1], [f["path"] for f in policy["frozen"]])
    ]
    print(
        f"polyglot policy: {len(changes)} changed file(s), {len(frozen_touched)} in FROZEN copies"
    )
    for reason in markers.reasons:
        print(f"  override: {reason}")
    for e in errors:
        print(f"{'WARN' if args.advisory else 'FAIL'} {e}")
    if errors and not args.advisory:
        return 1
    print(
        "polyglot policy: PASS" if not errors else "polyglot policy: violations reported (advisory)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

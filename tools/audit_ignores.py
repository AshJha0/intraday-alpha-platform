#!/usr/bin/env python3
"""tools/audit_ignores.py — time-boxed supply-chain audit ignores (G-P1-7, v1.14).

The blocking audits of the ``supply-chain`` CI job (pip-audit, cargo audit,
trivy) read their ignores from ONE reviewed file,
``.github/audit-ignores.json``::

    {"ignores": [{"tool": "pip-audit" | "cargo-audit" | "trivy",
                  "id": "GHSA-... / PYSEC-... / RUSTSEC-... / CVE-...",
                  "expires": "YYYY-MM-DD",
                  "reason": "why it does not apply / what unblocks the fix"}]}

Every entry needs all four fields; an entry past its ``expires`` date fails
the job (renew it with a new reason, or fix the dependency).  This prints
the active ids of one tool in the form that tool takes:

    --tool pip-audit    ->  --ignore-vuln ID ...      (pip-audit arguments)
    --tool cargo-audit  ->  --ignore ID ...           (cargo audit arguments)
    --tool trivy        ->  one ID per line           (a .trivyignore file)

``--check`` validates the file only.  Exit codes: 0 ok, 1 invalid/expired.

Usage:
    python tools/audit_ignores.py --check
    python tools/audit_ignores.py --tool pip-audit [--today 2026-10-11]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / ".github" / "audit-ignores.json"
TOOLS = ("pip-audit", "cargo-audit", "trivy")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,63}$")


def problems(doc: object, today: dt.date) -> list[str]:
    if not isinstance(doc, dict) or not isinstance(doc.get("ignores"), list):
        return ['the file must be {"ignores": [...]}']
    out = []
    for i, e in enumerate(doc["ignores"]):
        if not isinstance(e, dict):
            out.append(f"entry {i}: not an object")
            continue
        if e.get("tool") not in TOOLS:
            out.append(f"entry {i}: tool must be one of {TOOLS}")
        if not isinstance(e.get("id"), str) or not _ID.match(e["id"]):
            out.append(f"entry {i}: bad id {e.get('id')!r}")
        if not str(e.get("reason", "")).strip():
            out.append(f"entry {i} ({e.get('id')}): no reason")
        try:
            expires = dt.date.fromisoformat(str(e.get("expires")))
        except ValueError:
            out.append(f"entry {i} ({e.get('id')}): expires must be YYYY-MM-DD")
            continue
        if expires < today:
            out.append(f"entry {i} ({e.get('id')}): expired on {expires} - fix or renew")
    return out


def args_for(doc: dict, tool: str) -> list[str]:
    ids = sorted({e["id"] for e in doc["ignores"] if e["tool"] == tool})
    if tool == "pip-audit":
        return [x for i in ids for x in ("--ignore-vuln", i)]
    if tool == "cargo-audit":
        return [x for i in ids for x in ("--ignore", i)]
    return ids


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", type=Path, default=PATH)
    ap.add_argument("--tool", choices=TOOLS)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (default: today, UTC)")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.UTC).date()
    doc = json.loads(a.file.read_text(encoding="utf-8"))
    bad = problems(doc, today)
    for p in bad:
        print(f"error: {a.file.name}: {p}", file=sys.stderr)
    if bad:
        return 1
    if a.tool:
        sep = "\n" if a.tool == "trivy" else " "
        print(sep.join(args_for(doc, a.tool)))
    elif a.check:
        print(f"{len(doc['ignores'])} audit ignores, none expired")
    return 0


if __name__ == "__main__":
    sys.exit(main())

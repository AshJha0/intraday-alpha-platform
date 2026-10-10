"""Post-hoc verification of a finding (AI1): the LLM never computes numbers.

A finding is ``title`` + ``text`` + ``refs``.  It is accepted only if

1. every reference resolves (:func:`iap.agents.citations.resolve`), at least
   one is a ``report:`` of the current session, and
2. every number written in the title or text appears in an artefact the
   finding cites - as a numeric leaf of the report JSON or the cited board
   entry, or a number inside one of their string leaves - up to the rounding
   the text shows (``0.412`` matches a stored ``0.41187``; ``0.42`` does not).
   A trailing ``%`` also matches the stored value times 100.

Identifiers are not numbers: digits glued to letters (``EQ02``, ``10s``,
``FX07``) and the citation strings themselves are skipped.  A number the
model derived (a difference, a ratio, a mean) is in no artefact and the
finding is rejected - which is the point.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from iap.agents import citations
from iap.agents.mcp_server import FREE_TEXT

NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.:/-])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(%?)(?![A-Za-z0-9_])"
)


def numbers_in_text(text: str, refs: Iterable[str] = ()) -> list[tuple[str, bool]]:
    """``(token, is_percent)`` for every number written in ``text``."""
    for r in refs:
        text = text.replace(r, " ")
    out = []
    for m in NUMBER.finditer(text):
        out.append((m.group(1), m.group(2) == "%"))
    return out


def leaves(obj: Any) -> list[float]:
    """Every number stored in ``obj``: numeric leaves and numbers inside
    strings - except free-text fields (``hypothesis``, ``text``, ``note``...),
    which anyone could have written: a number planted in prose is not
    evidence, so it cannot verify a finding."""
    out: list[float] = []
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            if math.isfinite(float(v)):
                out.append(float(v))
        elif isinstance(v, str):
            out.extend(float(t) for t, _ in numbers_in_text(v))
        elif isinstance(v, dict):
            stack.extend(x for k, x in v.items() if k not in FREE_TEXT)
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
    return out


def _decimals(tok: str) -> int:
    mant = re.split(r"[eE]", tok)[0]
    return len(mant.split(".")[1]) if "." in mant else 0


def matches(tok: str, pct: bool, pool: Iterable[float]) -> bool:
    x = float(tok)
    if "e" in tok.lower():
        tol = abs(x) * 0.5 * 10 ** (-_decimals(tok)) + 1e-15
    else:
        tol = 0.5 * 10 ** (-_decimals(tok)) + 1e-12
    for v in pool:
        cands = (v, v * 100.0) if pct else (v,)
        if any(abs(c - x) <= tol for c in cands):
            return True
    return False


def verify_finding(
    title: str, text: str, refs: list[str], root: Path, session_id: str
) -> list[str]:
    """Problems with a finding; ``[]`` means it may be filed."""
    problems: list[str] = []
    if not refs:
        return ["a finding must cite at least one artefact"]
    bad = citations.unresolved(refs, root)
    if bad:
        problems.append(f"unresolvable citations: {bad}")
    if not any(r.startswith(f"report:{session_id}.") for r in refs):
        problems.append("a finding must cite a gated-run report of this session (report:...)")
    pool: list[float] = []
    for r in refs:
        doc = citations.artefact(r, root)
        if doc is not None:
            pool.extend(leaves(doc))
    unverified = [
        tok + ("%" if pct else "")
        for tok, pct in numbers_in_text(f"{title}\n{text}", refs)
        if not matches(tok, pct, pool)
    ]
    if unverified:
        problems.append(
            f"numbers not found in any cited artefact: {unverified} - copy numbers "
            "verbatim from tool results and cite the artefact that holds them"
        )
    return problems

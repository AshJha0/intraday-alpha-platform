"""Post-hoc verification of a finding (AI1): the LLM never computes numbers.

A finding is ``title`` + ``text`` + ``refs``.  It is accepted only if

1. every reference resolves (:func:`iap.agents.citations.resolve`), at least
   one is a ``report:`` of the current session, and
2. every number written in the title or text appears in the EVIDENCE of an
   artefact the finding cites, under exact-decimal matching (v1.13, G-P2-2):

   * evidence is the numeric leaves (and numbers inside string leaves) of the
     ``metrics`` / ``result`` / ``results`` / ``stats`` / ``statistics``
     subtrees and the ``t_threshold`` field - not metadata such as ``seq``,
     ``ts``, ``expected_sign``, ``n``, counts or ids, which would otherwise
     verify any small integer;
   * a number written WITH a decimal point matches a stored value that rounds
     to it at the precision shown, in decimal arithmetic on the value's
     shortest repr (``0.412`` matches a stored ``0.41187``; ``0.42`` does
     not); half-even and half-up rounding are both accepted;
   * a number written WITHOUT a decimal point (``3``, ``12%``) must equal a
     stored value exactly - no +/-0.5 tolerance;
   * a trailing ``%`` matches the stored value times 100 (exactly, in decimal);
   * a LABELLED statistic (``IC 0.041``, ``t = 5.3``, ``Sharpe 1.2``, ``hit
     rate 0.53``) must match a field of that statistic (``gate_ic``,
     ``t_stat``, ...), not merely any number in the pool.

3. the text uses no number WORDS (``three``, ``twice``, ``half``, ``dozen``...):
   a quantity written in words cannot be checked, so it is refused.

Identifiers are not numbers: digits glued to letters (``EQ02``, ``10s``,
``FX07``) and the citation strings themselves are skipped.  A number the
model derived (a difference, a ratio, a mean) is in no artefact and the
finding is rejected - which is the point.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from iap.agents import citations
from iap.agents.mcp_server import FREE_TEXT

NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.:/-])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(%?)(?![A-Za-z0-9_])"
)

#: subtrees of a cited artefact whose numbers are evidence (G-P2-2)
EVIDENCE_KEYS = frozenset({"metrics", "result", "results", "stats", "statistics"})
#: single evidence fields outside those subtrees
EVIDENCE_FIELDS = frozenset({"t_threshold"})

#: quantities written as words cannot be verified, so they are refused
NUMBER_WORDS = frozenset(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
    "sixty seventy eighty ninety hundred thousand million billion trillion dozen "
    "dozens half halves quarter quarters third thirds twice thrice double doubled "
    "triple tripled quadruple quadrupled tenfold hundredfold".split()
)
#: a word standing alone: letters glued to ``_`` or digits are an identifier
#: (``ic_first_half``, a field name copied from a report), not a number word
_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]+(?![A-Za-z0-9_])")

#: label in the text -> predicate on a field name (the statistic it names)
_LABEL = re.compile(
    r"(?i)\b(gate\s+ic|rank\s*ic|ic|t[-_ ]?stat(?:istic)?|t|sharpe(?:\s+ratio)?|hit[-_ ]?rate)"
    r"\s*(?:=|:|of|is|was)?\s*$"
)


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _family(label: str) -> str:
    lab = re.sub(r"[\s_-]+", "", label.lower())
    if "ic" in lab and not lab.startswith("t"):
        return "ic"
    if lab.startswith("sharpe"):
        return "sharpe"
    if lab.startswith("hit"):
        return "hit"
    return "t"


def field_in_family(field: str, family: str) -> bool:
    toks = _tokens(field)
    if family == "ic":
        return "ic" in toks or "rankic" in toks
    if family == "sharpe":
        return any(t.startswith("sharpe") for t in toks)
    if family == "hit":
        return "hit" in toks or "hitrate" in toks
    return ("t" in toks or "tstat" in toks) and "threshold" not in toks


def numbers_in_text(text: str, refs: Iterable[str] = ()) -> list[tuple[str, bool]]:
    """``(token, is_percent)`` for every number written in ``text``."""
    return [(tok, pct) for tok, pct, _ in _numbers_with_labels(text, refs)]


def _numbers_with_labels(text: str, refs: Iterable[str] = ()) -> list[tuple[str, bool, str | None]]:
    for r in refs:
        text = text.replace(r, " ")
    out: list[tuple[str, bool, str | None]] = []
    for m in NUMBER.finditer(text):
        lab = _LABEL.search(text[max(0, m.start() - 24) : m.start()])
        out.append((m.group(1), m.group(2) == "%", _family(lab.group(1)) if lab else None))
    return out


def number_words(text: str) -> list[str]:
    """Number words used in ``text`` (refused: they cannot be verified)."""
    return sorted({w.lower() for w in _WORD.findall(text) if w.lower() in NUMBER_WORDS})


def leaves(obj: Any) -> list[float]:
    """Every number stored in ``obj``: numeric leaves and numbers inside
    strings - except free-text fields (``hypothesis``, ``text``, ``note``...),
    which anyone could have written: a number planted in prose is not
    evidence, so it cannot verify a finding."""
    return [v for _, v in _keyed_leaves(obj, None)]


def _keyed_leaves(obj: Any, key: str | None) -> list[tuple[str | None, float]]:
    out: list[tuple[str | None, float]] = []
    stack: list[tuple[str | None, Any]] = [(key, obj)]
    while stack:
        k, v = stack.pop()
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            if math.isfinite(float(v)):
                out.append((k, v))
        elif isinstance(v, str):
            out.extend((k, float(t)) for t, _ in numbers_in_text(v))
        elif isinstance(v, dict):
            stack.extend((str(kk), x) for kk, x in v.items() if kk not in FREE_TEXT)
        elif isinstance(v, (list, tuple)):
            stack.extend((k, x) for x in v)
    return out


def evidence(obj: Any) -> list[tuple[str | None, float]]:
    """``(field name, value)`` for every number in the evidence subtrees of
    ``obj`` (module docs): metadata is not evidence."""
    out: list[tuple[str | None, float]] = []
    stack = [obj]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            for k, x in v.items():
                if k in FREE_TEXT:
                    continue
                if k in EVIDENCE_KEYS or k in EVIDENCE_FIELDS:
                    out.extend(_keyed_leaves(x, str(k)))
                else:
                    stack.append(x)
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
    return out


def _dec(v: float | int) -> Decimal:
    return Decimal(v) if isinstance(v, int) else Decimal(repr(float(v)))


def matches(tok: str, pct: bool, pool: Iterable[float]) -> bool:
    """Exact-decimal match of a written number against stored values."""
    try:
        want = Decimal(tok)
    except InvalidOperation:
        return False
    mant = re.split(r"[eE]", tok)[0]
    exact = "." not in mant
    quantum = Decimal(1).scaleb(cast(int, want.as_tuple().exponent))
    for v in pool:
        if isinstance(v, bool) or not math.isfinite(float(v)):
            continue
        d = _dec(v)
        cands = (d, d * 100) if pct else (d,)
        for c in cands:
            if exact:
                if c == want:
                    return True
            elif any(
                c.quantize(quantum, rounding=r) == want for r in (ROUND_HALF_EVEN, ROUND_HALF_UP)
            ):
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
    pool: list[tuple[str | None, float]] = []
    for r in refs:
        doc = citations.artefact(r, root)
        if doc is not None:
            pool.extend(evidence(doc))
    body = f"{title}\n{text}"
    words = number_words(body)
    if words:
        problems.append(
            f"numbers written as words cannot be verified: {words} - write the "
            "number verbatim from a cited artefact"
        )
    unverified = []
    for tok, pct, family in _numbers_with_labels(body, refs):
        cands = [v for k, v in pool if family is None or (k and field_in_family(k, family))]
        if not matches(tok, pct, cands):
            unverified.append(tok + ("%" if pct else "") + (f" ({family})" if family else ""))
    if unverified:
        problems.append(
            f"numbers not found in any cited artefact: {unverified} - copy numbers "
            "verbatim from tool results and cite the artefact that holds them"
        )
    return problems

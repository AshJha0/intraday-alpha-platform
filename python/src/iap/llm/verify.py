"""Post-hoc verification of a finding (AI1): the LLM never computes numbers.

A finding is ``title`` + ``text`` + ``refs``.  It is accepted only if

1. every reference resolves (:func:`iap.governance.citations.resolve`), at least
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
     not); half-even and half-up rounding are both accepted.  Any written
     precision is handled: a precision beyond the default 28-digit decimal
     context is compared, never raised;
   * a number written WITHOUT a decimal point (``3``, ``12%``) must equal a
     stored value exactly - no +/-0.5 tolerance;
   * a percent sign (``12%``, ``12 %`` or ``12pct``) matches the stored value
     times 100 (exactly, in decimal).  It matches the stored value itself
     only when the field is named as a percentage (a ``pct`` / ``percent``
     token, e.g. ``turnover_pct``): ``0.53%`` does not verify a stored 0.53;
   * a LABELLED statistic (``IC 0.041``, ``t = 5.3``, ``Sharpe 1.2``, ``hit
     rate 0.53``) must match a field of that statistic (``gate_ic``,
     ``t_stat``, ``information_coefficient``...), not merely any number in
     the pool.  A list of labels followed by the same number of numbers
     (``IC and t of 0.041 and 5.3``) binds them in order.

3. the text uses no number WORDS (``three``, ``twice``, ``half``, ``dozen``,
   and the phrases ``a pair of`` / ``a couple of``): a quantity written in
   words cannot be checked, so it is refused.  The fixed terms
   ``half-life`` and ``one-/two-sided`` / ``-tailed`` are names, not
   quantities.
4. no number is disguised with look-alike letters (``O.41``, ``0.4l``): a
   decimal token mixing digits with ``O``/``o``/``l``/``I`` is refused.

How a number is read (v2.x, from the offline golden set): the text is first
normalised - invisible format characters (zero-width space, word joiner,
soft hyphen, bidi marks) are removed, Unicode compatibility forms are folded
(NFKC: full-width and mathematical digits become ASCII), the Unicode minus
becomes ``-``, and thousands separators (``1,234``, ``1 234`` with a
no-break space) are joined.  A number is then read when it stands alone, or
directly after ``/``, after a ``:`` that follows a letter or space
(``IC:0.99``), or after a ``-`` that follows a digit (the upper end of
``0.41-0.99``, read as positive).  A number glued to a unit suffix
(``bp``, ``bps``, ``x``, ``ms``, ``us``, ``ns``) is a quantity and is
checked (``99bps``, ``1.5x``).

Identifiers are not numbers: digits glued to other letters (``EQ02``,
``10s``, ``FX07``), digits after a letter and hyphen (``EQ-02``), a number
followed by ``.`` and letters (the ticker ``0700.HK``), and the citation
strings themselves are skipped.  A number the model derived (a difference, a
ratio, a mean) is in no artefact and the finding is rejected - which is the
point.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, cast

from iap.governance import citations
from iap.governance.mcp_server import FREE_TEXT

NUMBER = re.compile(
    r"(?:(?<![A-Za-z0-9_.:/\-])(?P<sign>[-+]?)"
    r"|(?<=[A-Za-z\s)\]]:)(?P<csign>[-+]?)"
    r"|(?<=/)|(?<=\d-))"
    r"(?P<num>(?>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?))"
    r"(?P<pct>[  ]?%|pct)?"
    r"(?P<unit>bps|bp|x|ms|us|ns)?"
    r"(?![A-Za-z0-9_])(?!\.[A-Za-z])"
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
#: fixed terms that contain a number word but state no quantity
_TERMS = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:half-li(?:fe|ves)|(?:one|two)-(?:sided|tailed))\b")
#: quantity phrases outside the word list
_PHRASES = re.compile(r"(?i)\b(?:a|one)\s+(pair|couple|brace)\s+of\b")
#: a decimal disguised with look-alike letters (``O.41``, ``0.4l``)
_CONFUSABLE = re.compile(
    r"(?<![A-Za-z0-9_.])(?=[0-9OolI.]*\d)(?=[0-9OolI.]*[OolI])"
    r"[0-9OolI]+\.[0-9OolI]+(?![A-Za-z0-9_])"
)

_LAB = r"(?:gate\s+ic|rank\s*ic|ic|t[-_ ]?stat(?:istic)?|t|sharpe(?:\s+ratio)?|hit[-_ ]?rate)"
#: label in the text -> predicate on a field name (the statistic it names)
_LABEL = re.compile(rf"(?i)\b({_LAB})\s*(?:=|:|of|is|was)?\s*$")
#: ``IC and t of`` / ``IC, t of``: a list of labels bound to numbers in order
_LABEL_LIST = re.compile(
    rf"(?i)\b{_LAB}(?:\s*(?:,|\band\b)\s*(?:and\s+)?{_LAB}\b)+\s*(?:=|:|of|is|was|were|are)?\s*"
)
_ONE_LABEL = re.compile(rf"(?i)\b{_LAB}\b")
_LIST_GAP = re.compile(r"(?i)^\s*(?:,\s*and|,|and)\s*$")

_INVISIBLE = re.compile(r"[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_MINUS = str.maketrans({"−": "-", "﹣": "-", "➖": "-"})
_THOUSANDS = re.compile(r"(?<![\d.,])(\d{1,3})((?:[,  ]\d{3})+)(?![\d,])")
_PCT_FIELD = frozenset({"pct", "percent", "percentage"})


def _join(m: re.Match[str]) -> str:
    return m.group(1) + re.sub(r"[,  ]", "", m.group(2))


def normalise(text: str) -> str:
    """The text the number rules read (module docs: how a number is read)."""
    t = _INVISIBLE.sub("", str(text))
    t = _THOUSANDS.sub(_join, t)
    t = unicodedata.normalize("NFKC", t)
    t = _THOUSANDS.sub(_join, t)
    return t.translate(_MINUS)


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
        return "ic" in toks or "rankic" in toks or {"information", "coefficient"} <= set(toks)
    if family == "sharpe":
        return any(t.startswith("sharpe") for t in toks)
    if family == "hit":
        return "hit" in toks or "hitrate" in toks
    return ("t" in toks or "tstat" in toks) and "threshold" not in toks


def numbers_in_text(text: str, refs: Iterable[str] = ()) -> list[tuple[str, bool]]:
    """``(token, is_percent)`` for every number written in ``text``."""
    return [(tok, pct) for tok, pct, _ in _numbers_with_labels(text, refs)]


def _strip(text: str, refs: Iterable[str]) -> str:
    for r in refs:
        text = text.replace(r, " ")
    return normalise(text)


def _list_bindings(text: str, found: list[re.Match[str]]) -> dict[int, str]:
    """Start offset -> family for numbers that follow a label list."""
    out: dict[int, str] = {}
    for lm in _LABEL_LIST.finditer(text):
        fams = [_family(x) for x in _ONE_LABEL.findall(lm.group(0))]
        after = [m for m in found if m.start() >= lm.end()]
        if not after or text[lm.end() : after[0].start()].strip():
            continue
        chain = [after[0]]
        for prev, nxt in zip(after, after[1:], strict=False):
            if len(chain) == len(fams) or not _LIST_GAP.match(text[prev.end() : nxt.start()]):
                break
            chain.append(nxt)
        if len(chain) == len(fams):
            out.update({m.start(): f for m, f in zip(chain, fams, strict=True)})
    return out


def _numbers_with_labels(text: str, refs: Iterable[str] = ()) -> list[tuple[str, bool, str | None]]:
    text = _strip(text, refs)
    found = list(NUMBER.finditer(text))
    bound = _list_bindings(text, found)
    out: list[tuple[str, bool, str | None]] = []
    for m in found:
        sign = m.group("sign") or m.group("csign") or ""
        if m.start() in bound:
            fam: str | None = bound[m.start()]
        else:
            lab = _LABEL.search(text[max(0, m.start() - 24) : m.start()])
            fam = _family(lab.group(1)) if lab else None
        out.append((sign + m.group("num"), m.group("pct") is not None, fam))
    return out


def number_words(text: str) -> list[str]:
    """Number words used in ``text`` (refused: they cannot be verified)."""
    t = _TERMS.sub(" ", normalise(text))
    words = {w.lower() for w in _WORD.findall(t) if w.lower() in NUMBER_WORDS}
    words |= {m.group(1).lower() for m in _PHRASES.finditer(t)}
    return sorted(words)


def disguised_numbers(text: str, refs: Iterable[str] = ()) -> list[str]:
    """Decimals written with look-alike letters (refused)."""
    return [m.group(0) for m in _CONFUSABLE.finditer(_strip(text, refs))]


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
    """Exact-decimal match of a written number against stored values
    (``pct``: against the stored value times 100)."""
    try:
        want = Decimal(tok)
    except InvalidOperation:
        return False
    mant = re.split(r"[eE]", tok)[0]
    exact = "." not in mant
    with localcontext() as ctx:
        ctx.prec = 1000
        quantum = Decimal(1).scaleb(cast(int, want.as_tuple().exponent))
        for v in pool:
            if isinstance(v, bool) or not math.isfinite(float(v)):
                continue
            c = _dec(v) * 100 if pct else _dec(v)
            if exact:
                if c == want:
                    return True
                continue
            for r in (ROUND_HALF_EVEN, ROUND_HALF_UP):
                try:
                    if c.quantize(quantum, rounding=r) == want:
                        return True
                except InvalidOperation:
                    break  # precision beyond any sane context: no match
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
    disguised = disguised_numbers(body, refs)
    if disguised:
        problems.append(f"numbers written with look-alike letters: {disguised}")
    unverified = []
    for tok, pct, family in _numbers_with_labels(body, refs):
        keyed = [(k, v) for k, v in pool if family is None or (k and field_in_family(k, family))]
        ok = matches(tok, pct, [v for _, v in keyed])
        if not ok and pct:
            raw = [v for k, v in keyed if k and _PCT_FIELD & set(_tokens(k))]
            ok = matches(tok, False, raw)
        if not ok:
            unverified.append(tok + ("%" if pct else "") + (f" ({family})" if family else ""))
    if unverified:
        problems.append(
            f"numbers not found in any cited artefact: {unverified} - copy numbers "
            "verbatim from tool results and cite the artefact that holds them"
        )
    return problems

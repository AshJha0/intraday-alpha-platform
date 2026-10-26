"""Check an agent's closing summary against what actually happened (v1.15, A-P1-4).

The live eval (docs/RESEARCH_NOTES.md) found a model ending a session
with *"Finding filed."* after its finding had been rejected.  This module is a
deterministic check of the closing text (the ``finish`` summary plus the
model's last plain-text output) against the session's tool log:

* an ACTION claim - filed a finding, proposed / pre-registered, drafted,
  signed off, promoted, recorded an outcome, ran a confirmatory test, ran a
  study - is supported only if the tool log holds an ACCEPTED (``ok``) call of
  the tool that performs that action.  Tools an agent does not hold
  (``sign_off``, ``promote_alpha``, ``record_outcome``, ``run_confirmatory``)
  can never be accepted, so such a claim is always unsupported;
* a NUMERIC claim must match, under :mod:`iap.llm.verify`'s exact-decimal
  rules, the evidence of a report of this session or a number an accepted
  tool returned.

Claims are found per clause (split on sentence punctuation, commas and
"but").  A clause with a negation, a refusal word or a modal (``not``,
``could not``, ``rejected``, ``must``, ``a human`` ...) states no action,
so "could not file the finding" is not a claim.  The matcher is lexical and
deliberately conservative: a false flag marks a report for review, it never
blocks anything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.llm import verify

#: claim kind -> (clause pattern(s) that ALL must match, tools whose accepted call supports it)
_W = r"(?<![A-Za-z])"
_E = r"(?![A-Za-z])"
CLAIMS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "signed_off",
        (rf"{_W}signed[- ]?off{_E}|{_W}sign-?off (?:was |is )?(?:done|given|recorded){_E}",),
        ("sign_off",),
    ),
    ("promoted", (rf"{_W}promoted{_E}",), ("promote_alpha",)),
    (
        "ran_confirmatory",
        (
            rf"{_W}confirmatory{_E}",
            rf"{_W}(?:ran|run|executed|performed|conducted|completed|did){_E}",
        ),
        ("run_confirmatory",),
    ),
    (
        "outcome_recorded",
        (rf"{_W}outcomes?{_E}", rf"{_W}(?:recorded|logged|wrote|written|entered|marked){_E}"),
        ("record_outcome",),
    ),
    (
        "finding_filed",
        (
            rf"{_W}findings?{_E}",
            rf"{_W}(?:filed|submitted|posted|logged|lodged|published|recorded|wrote|written){_E}",
        ),
        ("file_finding",),
    ),
    ("proposed", (rf"{_W}(?:proposed|pre-?registered|registered){_E}",), ("preregister",)),
    ("drafted", (rf"{_W}drafted{_E}",), ("preregister", "propose_hypothesis")),
    (
        "ran_study",
        (
            rf"{_W}(?:ran|executed|performed|conducted){_E}",
            rf"{_W}(?:study|studies|test|run|backtest){_E}",
        ),
        ("run_gated_study",),
    ),
)
_COMPILED = tuple((k, tuple(re.compile(p, re.I) for p in ps), tools) for k, ps, tools in CLAIMS)

#: a clause with any of these does not assert that the action happened
NEGATION = re.compile(
    r"(?<![A-Za-z])(?:not|no|never|cannot|can't|couldn't|didn't|wasn't|weren't|won't|"
    r"isn't|aren't|haven't|hasn't|unable|refused|rejected|declined|denied|blocked|failed|"
    r"fails|without|instead|rather|pending|awaiting|nor|neither|will|would|should|could|"
    r"can|must|may|might|need|needs|requires?|only|human|owner|reviewer|if|once|until|"
    r"before|tried|attempted|attempt)(?![A-Za-z])|n't(?![A-Za-z])",
    re.I,
)
_CLAUSE = re.compile(r"[.;!?\n,:]+|\s(?:but|however|although|though)\s", re.I)


def clauses(text: str) -> list[str]:
    # keep decimals intact: split on '.' only when not between digits
    protected = re.sub(r"(?<=\d)\.(?=\d)", "\x00", text)
    return [c.replace("\x00", ".").strip() for c in _CLAUSE.split(protected) if c.strip()]


def action_claims(text: str) -> list[dict[str, str]]:
    """``{"kind", "clause"}`` for every affirmative action claim in ``text``."""
    out = []
    for c in clauses(text):
        if NEGATION.search(c):
            continue
        for kind, pats, _tools in _COMPILED:
            if all(p.search(c) for p in pats):
                out.append({"kind": kind, "clause": c})
                break  # one claim per clause, most specific kind first
    return out


def _accepted(log: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(r["tool"]) for r in log if r.get("ok")}


def _refs(log: Iterable[Mapping[str, Any]]) -> list[str]:
    text = json.dumps([r.get("output") for r in log if r.get("ok")], default=str)
    return sorted(set(re.findall(r"(?:report|board):[A-Za-z0-9_.:-]+", text)))


def _pool(
    log: Sequence[Mapping[str, Any]], root: Path, session_id: str
) -> list[tuple[str | None, float]]:
    from iap.agents import citations

    pool: list[tuple[str | None, float]] = []
    rdir = root / "research" / "agents" / "llm_sessions" / session_id / "reports"
    if rdir.is_dir():
        for p in sorted(rdir.glob("*.json")):
            pool.extend(verify.evidence(json.loads(p.read_text(encoding="utf-8"))))
    for ref in _refs(log):
        doc = citations.artefact(ref, root)
        if doc is not None:
            pool.extend(verify.evidence(doc))
    for r in log:
        if r.get("ok"):
            pool.extend(verify._keyed_leaves(r.get("output"), None))
    return pool


def check_summary(
    text: str, log: Sequence[Mapping[str, Any]], root: Path, session_id: str
) -> dict[str, Any]:
    """``{"summary_claims_verified": bool, "unsupported_claims": [...], "claims": [...]}``."""
    ok_tools = _accepted(log)
    tools_of = {k: t for k, _p, t in CLAIMS}
    claims = action_claims(text)
    unsupported: list[dict[str, str]] = []
    for c in claims:
        if not ok_tools & set(tools_of[c["kind"]]):
            unsupported.append(
                {
                    "kind": c["kind"],
                    "claim": c["clause"][:200],
                    "why": f"no accepted {' / '.join(tools_of[c['kind']])} call in the tool log",
                }
            )
    refs = _refs(log)
    pool = _pool(log, root, session_id)
    for tok, pct, family in verify._numbers_with_labels(text, refs):
        cands = [v for k, v in pool if family is None or (k and verify.field_in_family(k, family))]
        if not verify.matches(tok, pct, cands):
            num = tok + ("%" if pct else "")
            unsupported.append(
                {
                    "kind": "number",
                    "claim": num + (f" ({family})" if family else ""),
                    "why": "number not in any report of this session or accepted tool result",
                }
            )
    return {
        "summary_claims_verified": not unsupported,
        "unsupported_claims": unsupported,
        "claims": [c["kind"] for c in claims],
    }

"""Offline measurement of the numeric-claim verifier (:mod:`iap.llm.verify`).

The plain idea: a guardrail that is only asserted by a handful of unit tests
has no error rate.  This runner replays a hand-labelled golden set through
:func:`iap.llm.verify.verify_finding` and counts how often it disagrees with
the label a careful reviewer set (before running it) from the documented
rule.

Formally, with ``R`` the cases labelled ``reject`` and ``A`` those labelled
``accept``::

    false-accept rate = #{c in R : verifier accepts c} / |R|
    false-reject rate = #{c in A : verifier rejects c} / |A|

each reported with a 95% Wilson score interval, overall and per category.
A verifier crash counts as a rejection (the finding is not filed) and is
listed separately.

Worked example: 60 reject-labelled cases of which 6 are accepted give a
false-accept rate of 0.1 with Wilson interval about [0.047, 0.201].

No model, network or key is involved: every case is a small artefact written
to a temporary research tree and a finding text.  ``--check`` fails when
either rate rises above the committed result file.

    python -m iap.llm.verifier_eval [--check] [--cases PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from iap.llm.verify import verify_finding

ROOT = Path(__file__).resolve().parents[4]
CASES = ROOT / "research" / "agents" / "evals" / "verifier_golden.json"
RESULT = ROOT / "research" / "agents" / "evals" / "verifier_golden_result.json"
#: cases written and labelled before the verifier fixes ran on them
HELDOUT = ROOT / "research" / "agents" / "evals" / "verifier_heldout.json"
SESSION = "eval"
REF = f"report:{SESSION}.r1"
LABELS = ("accept", "reject")
CASE_KEYS = ("id", "category", "evidence", "title", "text", "label", "reason")
#: rates are compared with this slack so float noise never trips the gate
EPS = 1e-12


def wilson(k: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """95% Wilson score interval for ``k`` successes in ``n`` trials
    (``(0.0, 1.0)`` when ``n == 0``)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, mid - half), min(1.0, mid + half))


def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else 0.0), "wilson95": [round(lo, 6), round(hi, 6)]}


def validate_cases(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The case list, or ``ValueError`` naming the first schema problem."""
    cases = doc.get("cases") if isinstance(doc, Mapping) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("case file needs a non-empty 'cases' list")
    seen: set[str] = set()
    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            raise ValueError(f"case {i} is not an object")
        missing = [k for k in CASE_KEYS if k not in c]
        if missing:
            raise ValueError(f"case {c.get('id', i)} lacks {missing}")
        if c["label"] not in LABELS:
            raise ValueError(f"case {c['id']}: label must be one of {LABELS}")
        if c["id"] in seen:
            raise ValueError(f"duplicate case id {c['id']}")
        if not isinstance(c["evidence"], dict) or not str(c["reason"]).strip():
            raise ValueError(f"case {c['id']}: evidence must be an object and reason non-empty")
        seen.add(c["id"])
    return cases


def judge(case: Mapping[str, Any], root: Path) -> tuple[str, list[str]]:
    """``('accept'|'reject'|'error', problems)`` for one case."""
    path = root / "research" / "agents" / "llm_sessions" / SESSION / "reports" / "r1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case["evidence"]), encoding="utf-8")
    try:
        problems = verify_finding(case["title"], case["text"], [REF], root, SESSION)
    except Exception as exc:  # a crash is a finding of its own
        return "error", [f"{type(exc).__name__}: {exc}"]
    return ("reject" if problems else "accept"), problems


def _rates(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    rej = [r for r in rows if r["label"] == "reject"]
    acc = [r for r in rows if r["label"] == "accept"]
    return {
        "cases": len(rows),
        "false_accept": rate(sum(r["verdict"] == "accept" for r in rej), len(rej)),
        "false_reject": rate(sum(r["verdict"] != "accept" for r in acc), len(acc)),
    }


def evaluate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for c in cases:
            verdict, problems = judge(c, Path(tmp))
            rows.append({**c, "verdict": verdict, "problems": problems})
    cats = sorted({r["category"] for r in rows})
    disagreements = [
        {
            "id": r["id"],
            "category": r["category"],
            "label": r["label"],
            "verdict": r["verdict"],
            "title": r["title"],
            "text": r["text"],
            "reason": r["reason"],
            "problems": r["problems"],
            "classification": r.get("disagreement"),
        }
        for r in rows
        if (r["verdict"] == "accept") != (r["label"] == "accept")
    ]
    mislabelled = {
        d["id"] for d in disagreements if (d["classification"] or {}).get("kind") == "mislabel"
    }
    # a mislabelled case is scored under the corrected label
    corrected = [
        {**r, "label": "reject" if r["label"] == "accept" else "accept"}
        if r["id"] in mislabelled
        else r
        for r in rows
    ]
    return {
        "schema": "iap.verifier_golden_result/1",
        "what": "offline measurement of iap.llm.verify.verify_finding against hand labels; "
        "no model is involved",
        "overall": _rates(rows),
        "overall_net_of_mislabels": _rates(corrected),
        "per_category": {k: _rates(r for r in rows if r["category"] == k) for k in cats},
        "errors": [r["id"] for r in rows if r["verdict"] == "error"],
        "defects": sorted(
            d["id"] for d in disagreements if (d["classification"] or {}).get("kind") == "defect"
        ),
        "mislabels": sorted(mislabelled),
        "unclassified": sorted(d["id"] for d in disagreements if not d["classification"]),
        "disagreements": disagreements,
    }


def gate(new: Mapping[str, Any], old: Mapping[str, Any]) -> list[str]:
    """Reasons the new result is worse than the committed one."""
    out = []
    for key in ("false_accept", "false_reject"):
        a, b = new["overall"][key]["rate"], old["overall"][key]["rate"]
        if a > b + EPS:
            out.append(f"{key} rate rose from {b:.4f} to {a:.4f}")
    if new.get("unclassified"):
        out.append(f"disagreements without a defect/mislabel classification: {new['unclassified']}")
    if "heldout" in new and "heldout" in old:
        out += [f"held-out: {m}" for m in gate(new["heldout"], old["heldout"])]
    elif "heldout" in old:
        out.append("held-out set missing from the new run")
    return out


def _fmt(name: str, r: Mapping[str, Any]) -> str:
    fa, fr = r["false_accept"], r["false_reject"]
    return (
        f"{name:<20} n={r['cases']:<4} false-accept {fa['k']}/{fa['n']} = {fa['rate']:.3f} "
        f"[{fa['wilson95'][0]:.3f}, {fa['wilson95'][1]:.3f}]   false-reject {fr['k']}/{fr['n']} "
        f"= {fr['rate']:.3f} [{fr['wilson95'][0]:.3f}, {fr['wilson95'][1]:.3f}]"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m iap.llm.verifier_eval")
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument(
        "--heldout", type=Path, default=HELDOUT, help="held-out case file ('none' to skip)"
    )
    ap.add_argument("--out", type=Path, default=RESULT)
    ap.add_argument("--check", action="store_true", help="fail if a rate rose; do not write")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cases = validate_cases(json.loads(a.cases.read_text(encoding="utf-8")))
    res = evaluate(cases)
    if a.heldout is not None and a.heldout.name != "none":
        res["heldout"] = evaluate(validate_cases(json.loads(a.heldout.read_text(encoding="utf-8"))))
    for tag, r in [("golden", res), ("held-out", res.get("heldout"))]:
        if r is None:
            continue
        print(f"== {tag}")
        print(_fmt("overall", r["overall"]))
        print(_fmt("net of mislabels", r["overall_net_of_mislabels"]))
        if tag == "golden":
            for k, rr in r["per_category"].items():
                print(_fmt(k, rr))
        for d in r["disagreements"]:
            kind = (d["classification"] or {}).get("kind", "UNCLASSIFIED")
            print(
                f"  {d['id']} [{d['category']}] label={d['label']} verifier={d['verdict']} "
                f"({kind}): {d['text']!a}"
            )
    if a.check:
        if not a.out.is_file():
            print(f"no committed result at {a.out}")
            return 1
        bad = gate(res, json.loads(a.out.read_text(encoding="utf-8")))
        for b in bad:
            print("FAIL:", b)
        return 1 if bad else 0
    a.out.write_text(
        json.dumps(res, indent=1, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

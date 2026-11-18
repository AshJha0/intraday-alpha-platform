"""CLI: ``python -m iap.memory {search,prior-tests,eval}``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from iap.memory.search import EVAL_RESULT, KINDS, REPO_ROOT, Hit, Memory, load_spec, run_eval


def _print_hits(title: str, hits: list[Hit]) -> None:
    print(title)
    if not hits:
        print("  (none)")
    for r, h in enumerate(hits, 1):
        print(f"  {r:2d}. {h.score:7.3f}  {h.citation}  [{h.heading}]")
        print(f"      {h.snippet}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(prog="python -m iap.memory", description=__doc__)
    ap.add_argument(
        "--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search the research record")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=10)
    s.add_argument(
        "--kind", action="append", choices=KINDS, help="limit to a chunk kind (repeatable)"
    )
    s.add_argument("--json", action="store_true", help="JSON with snippets wrapped as untrusted")

    p = sub.add_parser("prior-tests", help="nearest ledger entries and result documents for a test")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--alpha")
    p.add_argument("--horizon")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--json", action="store_true")

    e = sub.add_parser("eval", help="recall@1/5/10 and MRR on the committed questions")
    e.add_argument("--check", action="store_true", help="fail if recall@5 is below the floor")
    e.add_argument("--no-write", action="store_true", help=f"do not write {EVAL_RESULT}")

    a = ap.parse_args(argv)
    mem = Memory(a.root)

    if a.cmd == "search":
        hits = mem.search(a.query, k=a.k, kinds=a.kind)
        if a.json:
            print(json.dumps({"query": a.query, "hits": [h.as_json() for h in hits]}, indent=2))
        else:
            _print_hits(f"search: {a.query}", hits)
        return 0

    if a.cmd == "prior-tests":
        try:
            res = mem.prior_tests(alpha=a.alpha, horizon=a.horizon, text=a.text, k=a.k)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if a.json:
            out = {k: [h.as_json() for h in v] for k, v in res.items()}
            out["query"] = {"alpha": a.alpha, "horizon": a.horizon, "text": a.text}
            print(json.dumps(out, indent=2))
        else:
            _print_hits(f"ledger entries ({len(res['ledger'])})", res["ledger"])
            _print_hits("result documents", res["documents"])
        return 0

    result = run_eval(mem, load_spec(a.root))
    if not a.no_write:
        (a.root / EVAL_RESULT).write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        f"n={result['n_questions']} recall@1={result['recall_at_1']} recall@5={result['recall_at_5']} "
        f"recall@10={result['recall_at_10']} MRR={result['mrr']} floor@5={result['recall_at_5_floor']}"
    )
    print(f"missed at 5: {', '.join(result['missed_at_5']) or 'none'}")
    if a.check and result["recall_at_5"] < result["recall_at_5_floor"]:
        print("FAIL: recall@5 below the floor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

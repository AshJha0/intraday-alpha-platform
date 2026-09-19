"""``python -m iap.store`` — build, explain, query.

    python -m iap.store build   [--db data/store/iap.sqlite] [--repo-root .]
    python -m iap.store explain [--db ...] <parent_order_id>
    python -m iap.store sql     [--db ...] "<query>"

``build`` applies the DDL and imports every research artefact that exists,
then prints a deterministic count table (one row per table, sorted) and
the importers' warnings on stderr.  ``sql`` prints one canonical JSON
line per row.  Output never contains a wall-clock value.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from iap.contracts.versions import canonical_json
from iap.store.db import Store
from iap.store.importers import default_repo_root, import_all

__all__ = ["DEFAULT_DB", "build_parser", "main", "render_counts"]

#: Default database path, relative to the repository root (git-ignored).
DEFAULT_DB = Path("data") / "store" / "iap.sqlite"


def render_counts(counts: "dict[str, int]") -> str:
    """The count table: ``table`` left-justified, rows right-justified."""
    width = max(len("table"), *(len(t) for t in counts)) if counts else len("table")
    digits = max(len("rows"), *(len(str(n)) for n in counts.values())) if counts else len("rows")
    lines = [f"{'table'.ljust(width)}  {'rows'.rjust(digits)}"]
    lines += [f"{t.ljust(width)}  {str(n).rjust(digits)}" for t, n in sorted(counts.items())]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=None,
                        help=f"SQLite file (default <repo-root>/{DEFAULT_DB.as_posix()})")
    common.add_argument("--repo-root", default=None,
                        help="repository root holding configs/ and research/ "
                             "(default: resolved from the package location)")
    parser = argparse.ArgumentParser(prog="python -m iap.store", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", parents=[common],
                   help="apply the DDL and import every artefact present")
    ex = sub.add_parser("explain", parents=[common],
                        help="render the decision chain of a parent order")
    ex.add_argument("parent_order_id", type=int)
    q = sub.add_parser("sql", parents=[common],
                       help="run one query; one canonical JSON line per row")
    q.add_argument("query")
    return parser


def _db_path(args: argparse.Namespace, root: Path) -> Path:
    return Path(args.db) if args.db is not None else root / DEFAULT_DB


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.repo_root) if args.repo_root is not None else default_repo_root()
    db = _db_path(args, root)

    if args.command == "build":
        db.parent.mkdir(parents=True, exist_ok=True)
        with Store.open(db) as store:
            store.init()
            reports = import_all(store, root)
            counts = store.counts()
        warnings: List[str] = [f"{step}: {w}" for step, rep in reports.items()
                               for w in rep.warnings]
        print(render_counts(counts))
        for line in warnings:
            print(f"warning: {line}", file=sys.stderr)
        return 0

    if not db.is_file():
        print(f"error: no store at {db} (run `python -m iap.store build` first)",
              file=sys.stderr)
        return 2
    with Store.open(db) as store:
        if args.command == "explain":
            try:
                print(store.explain(args.parent_order_id))
            except KeyError as exc:
                print(f"error: {exc.args[0]}", file=sys.stderr)
                return 1
            return 0
        try:
            for row in store.query(args.query):
                print(canonical_json(row))
        except BrokenPipeError:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

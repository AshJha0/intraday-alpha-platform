"""``python -m iap.store`` — build, scorecard, explain, query.

    python -m iap.store build     [--db data/store/iap.sqlite] [--repo-root .]
                                  [--rebuild] [--dataset-version V] [--methods M]
                                  [--ledger PATH ...]
    python -m iap.store scorecard [--db ...] [--dataset-version V] [--methods M]
                                  [--all-scopes]
    python -m iap.store explain   [--db ...] <parent_order_id>
    python -m iap.store sql       [--db ...] [--dataset-version V] [--methods M] "<query>"

``build`` applies the DDL and imports every research artefact that exists,
then prints a deterministic count table (one row per table, sorted) and
the importers' warnings on stderr.  It records the CURRENT research scope —
the dataset of ``configs/strategies/alpha_params.json`` and the default
method bundle, or what ``--dataset-version`` / ``--methods`` name — which the
``*_current`` views filter to; ``--ledger`` indexes a further
multiple-testing ledger (the one an ingested dataset keeps in its own
directory) under the scopes its entries state.  A database written under
another data-model ``x-version`` is refused (exit code 2) and left
untouched: the store is derived, so the migration is ``build --rebuild``,
which deletes the file and builds it again.

``scorecard`` prints ``v_alpha_scorecard`` as one canonical JSON line per
(alpha, scope) row: the current scope by default, the scope
``--dataset-version`` / ``--methods`` select (either alone keeps the current
value of the other), or every scope with ``--all-scopes``.  A dataset may be
given by a unique prefix of its hash.

``sql`` prints one canonical JSON line per row; the named parameters
``:dataset_version`` and ``:methods`` are bound to the selected scope (the
current one by default), so a query can be written once and pointed at
another scope from the command line.  ``sql``, ``scorecard`` and ``explain``
open the file **read-only** (``file:...?mode=ro``), so a statement that
writes fails with exit code 1 and the index can only change through
``build``.  ``sql`` takes exactly ONE statement: several statements
separated by ``;`` are refused (exit code 1) rather than half-run.  Every
SQLite failure exits 1 with the engine's own message; the "opened
read-only" hint is added only when the failure IS a write to the read-only
file, never to a syntax error or an unknown table.  Output never contains a
wall-clock value.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from iap.contracts.versions import canonical_json
from iap.store.db import Store, StoreVersionError
from iap.store.importers import default_repo_root, import_all

__all__ = [
    "DEFAULT_DB",
    "build_parser",
    "main",
    "render_counts",
    "select_scope",
    "sql_error_message",
]

#: Default database path, relative to the repository root (git-ignored).
DEFAULT_DB = Path("data") / "store" / "iap.sqlite"


def render_counts(counts: dict[str, int]) -> str:
    """The count table: ``table`` left-justified, rows right-justified."""
    width = max(len("table"), *(len(t) for t in counts)) if counts else len("table")
    digits = max(len("rows"), *(len(str(n)) for n in counts.values())) if counts else len("rows")
    lines = [f"{'table'.ljust(width)}  {'rows'.rjust(digits)}"]
    lines += [f"{t.ljust(width)}  {str(n).rjust(digits)}" for t, n in sorted(counts.items())]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db", default=None, help=f"SQLite file (default <repo-root>/{DEFAULT_DB.as_posix()})"
    )
    common.add_argument(
        "--repo-root",
        default=None,
        help="repository root holding configs/ and research/ "
        "(default: resolved from the package location)",
    )
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument(
        "--dataset-version",
        default=None,
        help="dataset of the scope (build: the full content hash; reads: a unique "
        "prefix is enough); default: the current scope's",
    )
    scope.add_argument(
        "--methods",
        default=None,
        help="research method bundle of the scope (v2, legacy_v1); default: the current scope's",
    )
    parser = argparse.ArgumentParser(
        prog="python -m iap.store",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser(
        "build", parents=[common, scope], help="apply the DDL and import every artefact present"
    )
    b.add_argument(
        "--rebuild",
        action="store_true",
        help="delete an existing database first (the migration from another x-version)",
    )
    b.add_argument(
        "--ledger",
        action="append",
        default=[],
        metavar="PATH",
        help="a further multiple-testing ledger to index (repeatable)",
    )
    sc = sub.add_parser(
        "scorecard",
        parents=[common, scope],
        help="the alpha scorecard of one scope (default: the current one)",
    )
    sc.add_argument("--all-scopes", action="store_true", help="every scope, not one")
    ex = sub.add_parser(
        "explain", parents=[common], help="render the decision chain of a parent order"
    )
    ex.add_argument("parent_order_id", type=int)
    q = sub.add_parser(
        "sql",
        parents=[common, scope],
        help="run one query; one canonical JSON line per row "
        "(:dataset_version and :methods are bound to the selected scope)",
    )
    q.add_argument("query")
    return parser


def sql_error_message(exc: sqlite3.Error) -> str:
    """The stderr line for a failed ``sql`` statement.

    SQLite reports a write to a ``mode=ro`` file as "attempt to write a
    readonly database"; only that failure gets the rebuild hint.  A
    multi-statement string (``sqlite3.ProgrammingError`` from ``execute``)
    is named for what it is.
    """
    text = str(exc)
    if "readonly database" in text.lower():
        return f"error: {text} (the store is opened read-only; use `build` to rebuild it)"
    if isinstance(exc, sqlite3.ProgrammingError) and "one statement" in text.lower():
        return f"error: `sql` runs exactly one statement; several were given ({text})"
    return f"error: {text}"


def select_scope(
    store: Store, dataset_version: str | None, methods: str | None
) -> tuple[str | None, str | None]:
    """The scope a read command addresses: the store's current scope with
    whichever part the command line names replaced.  ``dataset_version`` may
    be a prefix; it must select exactly one dataset the store knows
    (``ValueError`` otherwise).  Either part is ``None`` when it is neither
    named nor recorded."""
    current = store.current_scope() or (None, None)
    dataset = current[0]
    if dataset_version is not None:
        known = sorted(
            {r["dataset_version"] for r in store.query("SELECT dataset_version FROM ledger_scopes")}
            | {
                r["dataset_version"]
                for r in store.query("SELECT DISTINCT dataset_version FROM experiment_results")
            }
            | ({current[0]} if current[0] is not None else set())
        )
        matches = [d for d in known if d.startswith(dataset_version)]
        if len(matches) != 1:
            what = "no dataset" if not matches else f"{len(matches)} datasets"
            raise ValueError(
                f"--dataset-version {dataset_version!r} selects {what}; known: {known}"
            )
        dataset = matches[0]
    return dataset, current[1] if methods is None else methods


def _db_path(args: argparse.Namespace, root: Path) -> Path:
    return Path(args.db) if args.db is not None else root / DEFAULT_DB


def _print_rows(rows: list[dict]) -> None:
    for row in rows:
        print(canonical_json(row))


def _build(args: argparse.Namespace, root: Path, db: Path) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    if args.rebuild and db.is_file():
        db.unlink()
    with Store.open(db) as store:
        try:
            store.init()
        except StoreVersionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        reports = import_all(
            store,
            root,
            dataset_version=args.dataset_version,
            methods=args.methods,
            extra_ledgers=tuple(args.ledger),
        )
        counts = store.counts()
    warnings: list[str] = [f"{step}: {w}" for step, rep in reports.items() for w in rep.warnings]
    print(render_counts(counts))
    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.repo_root) if args.repo_root is not None else default_repo_root()
    db = _db_path(args, root)

    if args.command == "build":
        return _build(args, root, db)

    if not db.is_file():
        print(f"error: no store at {db} (run `python -m iap.store build` first)", file=sys.stderr)
        return 2
    # explain / scorecard / sql never write: the file is opened read-only
    # (mode=ro), so an arbitrary statement cannot alter the index (rebuild it
    # with `build`).
    with Store.open(db, read_only=True) as store:
        try:
            store.check_version()
        except StoreVersionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.command == "explain":
            try:
                print(store.explain(args.parent_order_id))
            except KeyError as exc:
                print(f"error: {exc.args[0]}", file=sys.stderr)
                return 1
            return 0
        try:
            dataset, methods = select_scope(store, args.dataset_version, args.methods)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            if args.command == "scorecard":
                if args.all_scopes:
                    rows = store.query(
                        "SELECT * FROM v_alpha_scorecard "
                        "ORDER BY alpha_id, dataset_version, methods"
                    )
                else:
                    rows = store.query(
                        "SELECT * FROM v_alpha_scorecard "
                        "WHERE dataset_version = ? AND methods = ? ORDER BY alpha_id",
                        (dataset, methods),
                    )
                _print_rows(rows)
                return 0
            params = {
                name: value
                for name, value in (("dataset_version", dataset), ("methods", methods))
                if f":{name}" in args.query
            }
            _print_rows(store.query(args.query, params))
        except (sqlite3.Error, sqlite3.Warning) as exc:
            # sqlite3.Warning: Python < 3.12 raises it (not ProgrammingError)
            # for a multi-statement string.
            if isinstance(exc, sqlite3.Warning):
                print(
                    f"error: `sql` runs exactly one statement; several were given ({exc})",
                    file=sys.stderr,
                )
            else:
                print(sql_error_message(exc), file=sys.stderr)
            return 1
        except BrokenPipeError:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

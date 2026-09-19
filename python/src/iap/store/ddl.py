"""The platform DDL (``schemas/sql/iap_v1.sql``): load, split, apply.

The DDL is written to run unchanged on SQLite 3 and PostgreSQL >= 13 (the
rules are listed at the top of the file and in ``docs/DATA_MODEL.md``
section 5).  This module knows nothing engine-specific: it strips ``--``
comments, splits on statement-terminating semicolons and hands each
statement to a DB-API cursor.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Tuple

from iap.contracts.versions import schema_dir

__all__ = [
    "DDL_X_VERSION",
    "ddl_path",
    "load_ddl",
    "split_statements",
    "statements",
    "table_names",
    "view_names",
    "apply",
]

#: ``x-version`` of the data model this package reads and writes.
DDL_X_VERSION = 1

_DDL_NAME = "iap_v1.sql"
_CREATE_TABLE = re.compile(r"^\s*CREATE TABLE IF NOT EXISTS\s+(\w+)", re.IGNORECASE)
_CREATE_VIEW = re.compile(r"^\s*CREATE VIEW\s+(\w+)", re.IGNORECASE)


def ddl_path() -> Path:
    """``<repo>/schemas/sql/iap_v1.sql`` (honours ``$IAP_SCHEMA_DIR``)."""
    return schema_dir() / "sql" / _DDL_NAME


def load_ddl() -> str:
    """The DDL text, exactly as on disk."""
    with open(ddl_path(), "r", encoding="utf-8") as fh:
        return fh.read()


def split_statements(sql: str) -> Tuple[str, ...]:
    """Split DDL text into statements.

    ``--`` comments are removed first (the DDL never puts ``--`` inside a
    string literal); statements end at a ``;`` outside single quotes.
    Whitespace-only fragments are dropped.  Pure function of its input.
    """
    lines = []
    for raw in sql.splitlines():
        in_quote = False
        cut = len(raw)
        for i, ch in enumerate(raw):
            if ch == "'":
                in_quote = not in_quote
            elif ch == "-" and not in_quote and raw.startswith("--", i):
                cut = i
                break
        lines.append(raw[:cut])
    text = "\n".join(lines)

    out = []
    buf = []
    in_quote = False
    for ch in text:
        if ch == "'":
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        raise ValueError("DDL: trailing statement without terminating ';'")
    return tuple(out)


def statements() -> Tuple[str, ...]:
    """The statements of the on-disk DDL, in file order."""
    return split_statements(load_ddl())


def table_names(sql: str) -> Tuple[str, ...]:
    """Table names created by ``sql``, in definition order."""
    return tuple(m.group(1) for stmt in split_statements(sql)
                 for m in [_CREATE_TABLE.match(stmt)] if m)


def view_names(sql: str) -> Tuple[str, ...]:
    """View names created by ``sql``, in definition order."""
    return tuple(m.group(1) for stmt in split_statements(sql)
                 for m in [_CREATE_VIEW.match(stmt)] if m)


def apply(conn: sqlite3.Connection) -> int:
    """Execute every DDL statement on ``conn`` inside one transaction.

    Idempotent: ``IF NOT EXISTS`` tables/indexes, ``DROP VIEW IF EXISTS``
    + ``CREATE VIEW``, and a guarded ``schema_version`` seed row.  Returns
    the number of statements executed.  The caller owns commit/rollback
    semantics only when it has opened a transaction itself; otherwise the
    statements run in one ``BEGIN``/``COMMIT`` pair here.
    """
    stmts = statements()
    own_tx = not conn.in_transaction
    cur = conn.cursor()
    try:
        if own_tx:
            cur.execute("BEGIN")
        for stmt in stmts:
            cur.execute(stmt)
        if own_tx:
            cur.execute("COMMIT")
    except Exception:
        if own_tx:
            cur.execute("ROLLBACK")
        raise
    finally:
        cur.close()
    return len(stmts)

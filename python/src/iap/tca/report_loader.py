"""Versioned ExecutionReport file loader (REL-05, Level 1).

Reads recorded venue execution reports from JSON or CSV into the canonical
:class:`iap.contracts.types.ExecutionReport` and, with a market timeline, into
the research TCA :class:`iap.tca.fills.ParentOrder` ("TcaParent") that the TCA
functions consume.  A file loader only: no broker client, no network.

File schema ``iap.execution_reports`` version 1
-----------------------------------------------
* JSON: ``{"schema": "iap.execution_reports", "version": 1, "reports": [row, ...]}``
* CSV: first line ``#iap.execution_reports/1``, then a header with the columns
  below and one row per report.

Row columns (all required, integers except ``fees``): ``order_id``,
``execution_id``, ``status`` (a name such as ``PARTIAL`` or the integer code),
``filled_qty``, ``fill_price_ticks``, ``venue_id``, ``exchange_ts``,
``receive_ts`` (ns since the Unix epoch), ``fees`` (float, negative = rebate).

Rejected deterministically with :class:`ReportFileError` (the first violation
in file order): an unknown schema, version or column, a bad value, a duplicate
``execution_id`` (ids are unique per file), a per-order timestamp regression
(``exchange_ts`` or ``receive_ts`` going backwards in ``execution_id`` order),
and a report after a terminal status.  The parent quantity is only known to
:func:`parent_from_reports`, which rejects over-fills.  Returned reports are
sorted by ``(receive_ts, execution_id)``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from iap.contracts.types import ExecStatus, ExecutionReport
from iap.tca.fills import MAKER, TAKER, MarketTimeline, ParentOrder, stamp_fill

SCHEMA = "iap.execution_reports"
VERSION = 1
COLUMNS = (
    "order_id",
    "execution_id",
    "status",
    "filled_qty",
    "fill_price_ticks",
    "venue_id",
    "exchange_ts",
    "receive_ts",
    "fees",
)
_TERMINAL = frozenset(
    {ExecStatus.FILLED, ExecStatus.CANCELED, ExecStatus.REJECTED, ExecStatus.EXPIRED}
)
_FILLS = frozenset({ExecStatus.PARTIAL, ExecStatus.FILLED})


class ReportFileError(ValueError):
    """The report file violates the schema or a sequence rule."""


def _status(v: Any, row: int) -> ExecStatus:
    if isinstance(v, bool):
        raise ReportFileError(f"row {row}: bad status {v!r}")
    try:
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            return ExecStatus(int(v))
        return ExecStatus[str(v).strip().upper()]
    except (KeyError, ValueError):
        raise ReportFileError(f"row {row}: bad status {v!r}") from None


def _int(v: Any, name: str, row: int) -> int:
    if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
        raise ReportFileError(f"row {row}: {name} must be an integer, got {v!r}")
    try:
        return int(v.strip()) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        raise ReportFileError(f"row {row}: {name} must be an integer, got {v!r}") from None


def _row_to_report(raw: Mapping[str, Any], row: int) -> ExecutionReport:
    extra = set(raw) - set(COLUMNS)
    missing = [c for c in COLUMNS if c not in raw]
    if extra or missing:
        raise ReportFileError(f"row {row}: unknown columns {sorted(extra)}, missing {missing}")
    try:
        fees = float(raw["fees"])
    except (TypeError, ValueError):
        raise ReportFileError(f"row {row}: fees must be a number, got {raw['fees']!r}") from None
    if fees != fees or fees in (float("inf"), float("-inf")):
        raise ReportFileError(f"row {row}: fees must be finite")
    ints = {c: _int(raw[c], c, row) for c in COLUMNS if c not in ("status", "fees")}
    try:
        return ExecutionReport(status=_status(raw["status"], row), fees=fees, **ints)
    except ReportFileError:
        raise
    except Exception as exc:  # contract domain or invariant failure
        raise ReportFileError(f"row {row}: {exc}") from None


def validate_sequence(reports: Sequence[ExecutionReport]) -> list[ExecutionReport]:
    """Check ids, per-order timestamp order and terminal states; return sorted."""
    seen: set[int] = set()
    for i, r in enumerate(reports):
        if r.execution_id in seen:
            raise ReportFileError(f"row {i}: duplicate execution_id {r.execution_id}")
        seen.add(r.execution_id)
    by_order: dict[int, list[ExecutionReport]] = {}
    for r in sorted(reports, key=lambda x: x.execution_id):
        by_order.setdefault(r.order_id, []).append(r)
    for oid, rs in sorted(by_order.items()):
        for prev, cur in zip(rs, rs[1:], strict=False):
            if prev.status in _TERMINAL:
                raise ReportFileError(
                    f"order {oid}: execution {cur.execution_id} follows terminal "
                    f"{prev.status.name} (execution {prev.execution_id})"
                )
            if cur.exchange_ts < prev.exchange_ts or cur.receive_ts < prev.receive_ts:
                raise ReportFileError(
                    f"order {oid}: timestamps go backwards between executions "
                    f"{prev.execution_id} and {cur.execution_id}"
                )
    return sorted(reports, key=lambda r: (r.receive_ts, r.execution_id))


def parse_json(text: str) -> list[ExecutionReport]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReportFileError(f"invalid JSON: {exc}") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or doc.get("version") != VERSION:
        raise ReportFileError(f"expected schema {SCHEMA!r} version {VERSION}")
    rows = doc.get("reports")
    if not isinstance(rows, list) or set(doc) - {"schema", "version", "reports"}:
        raise ReportFileError("expected exactly schema, version and a reports list")
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ReportFileError(f"row {i}: not an object")
    return validate_sequence([_row_to_report(raw, i) for i, raw in enumerate(rows)])


def parse_csv(text: str) -> list[ExecutionReport]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != f"#{SCHEMA}/{VERSION}":
        raise ReportFileError(f"first line must be '#{SCHEMA}/{VERSION}'")
    rd = csv.DictReader(io.StringIO("\n".join(lines[1:])))
    if tuple(rd.fieldnames or ()) != COLUMNS:
        raise ReportFileError(f"CSV header must be exactly {','.join(COLUMNS)}")
    rows = []
    for i, raw in enumerate(rd):
        if None in raw or None in raw.values():
            raise ReportFileError(f"row {i}: wrong number of fields")
        rows.append(_row_to_report(raw, i))
    return validate_sequence(rows)


def load_execution_reports(path: str | Path) -> list[ExecutionReport]:
    """Load ``.json`` or ``.csv`` (by suffix); see the module docstring."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in (".json", ".csv"):
        raise ReportFileError(f"unsupported suffix {p.suffix!r} (use .json or .csv)")
    text = p.read_text(encoding="utf-8")
    return parse_json(text) if suffix == ".json" else parse_csv(text)


def dump_json(reports: Sequence[ExecutionReport]) -> str:
    """Canonical JSON of ``reports`` (round-trips through :func:`parse_json`)."""
    rows = [
        {
            "order_id": r.order_id,
            "execution_id": r.execution_id,
            "status": r.status.name,
            "filled_qty": r.filled_qty,
            "fill_price_ticks": r.fill_price_ticks,
            "venue_id": r.venue_id,
            "exchange_ts": r.exchange_ts,
            "receive_ts": r.receive_ts,
            "fees": r.fees,
        }
        for r in reports
    ]
    return json.dumps({"schema": SCHEMA, "version": VERSION, "reports": rows}, indent=1) + "\n"


def parent_from_reports(
    *,
    parent_id: int,
    instrument_id: int,
    side: int,
    qty_target: int,
    decision_ts: int,
    arrival_ts: int,
    end_ts: int,
    reports: Sequence[ExecutionReport],
    timeline: MarketTimeline,
    tick_size: float,
    makers: frozenset[int] = frozenset(),
) -> ParentOrder:
    """Build the TCA ``ParentOrder`` ("TcaParent") from a parent's fill reports.

    ``reports`` are the reports of this parent's children only (the caller owns
    the child -> parent map).  Order ids in ``makers`` filled passively; every
    other fill is stamped TAKER (an ``ExecutionReport`` carries no liquidity
    flag, so state this assumption wherever the result is used).  Rejects an
    over-fill (``sum filled_qty > qty_target``) and a fill outside
    ``[arrival_ts, end_ts]``.
    """
    parent = ParentOrder(
        order_id=parent_id,
        instrument_id=instrument_id,
        side=side,
        qty_target=qty_target,
        decision_ts=decision_ts,
        arrival_ts=arrival_ts,
        end_ts=end_ts,
    )
    for r in sorted(reports, key=lambda x: (x.exchange_ts, x.execution_id)):
        if r.status not in _FILLS:
            continue
        if not arrival_ts <= r.exchange_ts <= end_ts:
            raise ReportFileError(
                f"execution {r.execution_id}: fill at {r.exchange_ts} outside the parent window"
            )
        parent.fills.append(
            stamp_fill(
                timeline,
                r.exchange_ts,
                r.fill_price_ticks * tick_size,
                r.filled_qty,
                side,
                MAKER if r.order_id in makers else TAKER,
            )
        )
    if parent.qty_filled > qty_target:
        raise ReportFileError(f"parent {parent_id}: filled {parent.qty_filled} > {qty_target}")
    return parent

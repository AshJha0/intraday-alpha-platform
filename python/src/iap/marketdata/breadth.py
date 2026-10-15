"""Breadth universe: the pre-registered selection rule and its one-pass
measurement on a full-market ITCH day (v1.17, A3).

The rule (owner decision 2026-10-15): **the top N Nasdaq-listed symbols by
displayed continuous-session volume on one selection day, ETFs flagged,
test securities excluded**.  "Displayed volume" is the sum of the shares
executed against displayed orders (ITCH ``E``, and printable ``C``) between
the start (``Q``) and the end (``M``) of regular market hours; opening /
closing crosses (``Q`` messages) and non-displayed prints (``P``) are
counted beside it but do not rank.  A security whose directory row says
``authenticity == "T"`` (a test security) is excluded; an ETP
(``etp_flag == "Y"``) is kept and flagged.  Everything else is recorded
(market category, round lot, rank, the three volumes), so the choice is
auditable and survivorship is explicit: the universe is fixed on the
selection day and never re-picked from later days.

    python -m iap.marketdata breadth-universe --input 01302019.NASDAQ_ITCH50.gz \\
        --date 2019-01-30 --top 100 --out research/breadth/universe.json

One streaming pass (``Itch50Reader`` with no symbol filter, no book, no
mapping: about 9,000 counters) over the file; nothing is downloaded.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
from pathlib import Path

from iap.marketdata.itch50 import (
    CrossTrade,
    Itch50Reader,
    OrderExecuted,
    OrderExecutedWithPrice,
    StockDirectory,
    SystemEvent,
    Trade,
)

__all__ = ["RULE", "RULE_VERSION", "measure_volumes", "select_universe", "main"]

RULE_VERSION = "breadth_universe/v1"
RULE = (
    "top N Nasdaq-listed symbols by displayed continuous-session volume (ITCH E + printable C "
    "shares between system events Q and M) on the selection day; ETFs (etp_flag Y) kept and "
    "flagged; test securities (authenticity T) excluded; crosses and hidden prints reported, "
    "not ranked; fixed on the selection day (no re-selection on later days)"
)


def measure_volumes(path: str | Path, limit_messages: int | None = None) -> dict:
    """``{symbol: {...}}`` for every symbol with a directory row: displayed,
    cross and hidden share volumes (regular hours for displayed), the
    directory attributes, plus the pass's counters."""
    reader = Itch50Reader(path, symbols=None, limit_messages=limit_messages)
    by_locate: dict[int, dict] = {}
    orders: dict[int, int] = {}  # order_ref -> locate (executions carry no locate match)
    regular = False
    events: list[dict] = []
    for m in reader:
        kind = type(m)
        if kind is SystemEvent:
            events.append({"code": m.event_code, "ts": m.ts})
            if m.event_code == "Q":
                regular = True
            elif m.event_code == "M":
                regular = False
            continue
        if kind is StockDirectory:
            by_locate[m.locate] = {
                "symbol": m.stock,
                "displayed_volume": 0,
                "displayed_volume_all_hours": 0,
                "cross_volume": 0,
                "hidden_volume": 0,
                "etp": m.etp_flag == "Y",
                "test_security": m.authenticity == "T",
                "market_category": m.market_category,
                "round_lot_size": m.round_lot_size,
                "financial_status": m.financial_status,
            }
            continue
        row = by_locate.get(m.locate)
        if row is None:
            continue
        if kind is OrderExecuted or (kind is OrderExecutedWithPrice and m.printable == "Y"):
            row["displayed_volume_all_hours"] += m.shares
            if regular:
                row["displayed_volume"] += m.shares
        elif kind is CrossTrade:
            row["cross_volume"] += m.shares
        elif kind is Trade:
            row["hidden_volume"] += m.shares
    del orders
    return {
        "symbols": {row["symbol"]: row for row in by_locate.values()},
        "messages_read": reader.messages_read,
        "bytes_read": reader.bytes_read,
        "counts": {k: v for k, v in sorted(reader.counts.items()) if v},
        "system_events": events,
        "limit_reached": reader.limit_reached,
    }


def select_universe(
    measured: dict, top: int, *, date: str, source: str, sha256: str | None
) -> dict:
    """Apply :data:`RULE` to a measurement; the universe document."""
    rows = [r for r in measured["symbols"].values() if not r["test_security"]]
    excluded = [r["symbol"] for r in measured["symbols"].values() if r["test_security"]]
    rows.sort(key=lambda r: (-r["displayed_volume"], r["symbol"]))
    ranked = []
    for i, r in enumerate(rows, start=1):
        ranked.append({"rank": i, **r})
    chosen = ranked[:top]
    return {
        "schema": "iap.breadth_universe/v1",
        "rule_version": RULE_VERSION,
        "rule": RULE,
        "selection_date": date,
        "source_file": source,
        "source_sha256": sha256,
        "top": int(top),
        "measured_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "messages_read": measured["messages_read"],
        "limit_reached": measured["limit_reached"],
        "n_symbols_with_directory": len(measured["symbols"]),
        "n_test_securities_excluded": len(excluded),
        "n_etfs_in_universe": sum(1 for r in chosen if r["etp"]),
        "symbols": [r["symbol"] for r in chosen],
        "etfs": [r["symbol"] for r in chosen if r["etp"]],
        "universe": chosen,
        "next_50": ranked[top : top + 50],
        "survivorship_note": (
            "the universe is the selection day's top N; a symbol that later delists, "
            "halts or changes id stays in the list and is reported as missing on that day"
        ),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m iap.marketdata breadth-universe", description=__doc__.splitlines()[0]
    )
    p.add_argument("--input", required=True, help="the selection day's full-market ITCH 5.0 file")
    p.add_argument("--date", required=True, help="selection date, YYYY-MM-DD")
    p.add_argument("--top", type=int, default=100)
    p.add_argument("--out", required=True, help="universe JSON to write")
    p.add_argument("--limit-messages", type=int, default=None, help="smoke run over the head only")
    p.add_argument("--no-hash", action="store_true", help="do not sha256 the input file")
    a = p.parse_args(argv)
    if a.top < 1:
        p.error("--top must be >= 1")
    src = Path(a.input)
    measured = measure_volumes(src, a.limit_messages)
    doc = select_universe(
        measured, a.top, date=a.date, source=src.name, sha256=None if a.no_hash else _sha256(src)
    )
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"wrote {out}: {len(doc['symbols'])} symbols ({doc['n_etfs_in_universe']} ETFs) of "
        f"{doc['n_symbols_with_directory']} with a directory row; "
        f"{doc['n_test_securities_excluded']} test securities excluded; "
        f"{measured['messages_read']:,} messages read"
        + (" (LIMIT REACHED: a smoke run, not a selection)" if measured["limit_reached"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

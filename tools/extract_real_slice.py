#!/usr/bin/env python3
"""Cut a small, committed, real-session sample slice for the CI guards (v1.17 F4/F5).

The latency and memory guards need a workload that is REAL (a Nasdaq ITCH day
with its deep book, not the synthetic generator) yet small enough to commit
(docs/REAL_DATA.md: vendor data is licensed and never committed; a few
megabytes of derived canonical events of one symbol are not the vendor file).
This tool writes one such slice as a self-contained *ingested dataset*
directory (``dataset.json`` + ``normalized/*.normalized.iap1`` + ``configs/``
+ ``reference/``) so that every consumer of a real dataset — the C++
``bench_tick_to_trade``, ``iap.mvp.real``, ``python -m iap.features``, the
execution replay — reads it unchanged.

Content of the slice, in file order:

1. a **book-reconstruction prefix**: one ADD per order resting on the
   symbol's book at the window start ``T0`` (``qty`` = the remaining qty at
   ``T0``, side / price = its state at ``T0``, ``exchange_ts`` /
   ``receive_ts`` = the timestamps of its original ADD), sorted by original
   (exchange_ts, sequence).  So the first window event sees the real book,
   not an empty one;
2. the **window**: the next ``--events`` canonical events of the symbol from
   ``T0`` on, byte-identical to the normalized stream except for
   ``event_id`` and ``sequence``.

``event_id`` and ``sequence`` are renumbered 1..N over the whole slice: the
normaliser numbers sequences per instrument contiguously and the order book
declares a gap (and goes stale) on any hole, so a cut stream must be
contiguous again.  The original sequence range of the window is recorded.

Provenance (``dataset.json`` ``sessions[<date>].slice`` and ``SLICE.md``):
source dataset name + ``dataset_version`` + normalized file sha256, the
vendor file name + sha256 the dataset was ingested from, day, symbol,
instrument / venue ids, ``T0`` (UTC ns and local time), the window's
original sequence range, prefix / window counts, book depth at ``T0`` and
at the end of the slice, output sha256 and byte size, and the exact command.

    PYTHONPATH=python/src python tools/extract_real_slice.py \
        --dataset data/real/ds3_20190130 --date 2019-01-30 --symbol AAPL \
        --start 09:30:00 --events 64000 --out benchmarks/slices/itch50_20190130_AAPL

Nothing here changes a registered result: the slice is a benchmark workload,
listed nowhere as a research dataset.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.core.codec import iter_iap1, sha256_events_iap1, sha256_file, write_iap1  # noqa: E402
from iap.core.events import EventType, MarketEvent  # noqa: E402
from iap.marketdata.ingest import MANIFEST_NAME, MANIFEST_VERSION  # noqa: E402
from iap.orderbook.book import OrderBook  # noqa: E402

EXTRACTOR_VERSION = 1
MAX_BYTES_DEFAULT = 5_000_000


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _levels(book: OrderBook) -> tuple[int, int]:
    keys = list(book._levels)  # noqa: SLF001 (diagnostic read of the level map)
    bids = sum(1 for k in keys if k[0] == 0)
    return bids, len(keys) - bids


def extract(
    dataset: Path,
    date: str,
    symbol: str,
    start_local: str,
    n_events: int,
    out: Path,
    max_bytes: int,
    argv: list[str],
) -> dict:
    manifest = _load(dataset / MANIFEST_NAME)
    if manifest.get("kind") != "real":
        raise SystemExit(f"{dataset}: not a real dataset")
    if date not in manifest.get("sessions", {}):
        raise SystemExit(f"{dataset}: no session {date}")
    inst_doc = _load(dataset / "configs" / "instruments" / "instruments.json")
    rows = [r for r in inst_doc["instruments"] if r["symbol"] == symbol]
    if len(rows) != 1:
        raise SystemExit(f"{dataset}: symbol {symbol} not in the universe")
    row = rows[0]
    iid = int(row["instrument_id"])
    session = inst_doc["sessions"][row["asset_class"]]
    tz = ZoneInfo(session["timezone"])
    t0_local = datetime.fromisoformat(f"{date}T{start_local}").replace(tzinfo=tz)
    t0 = int(t0_local.timestamp()) * 1_000_000_000
    venue_id = int(manifest["source"]["venue_id"])

    files = manifest["normalized"]["files"]
    names = sorted(n for n in files if n.endswith(".normalized.iap1"))
    if len(names) != 1:
        raise SystemExit(f"{dataset}: expected one normalized .iap1, found {names}")
    source = dataset / "normalized" / names[0]
    source_sha = sha256_file(source)
    if source_sha != files[names[0]]:
        raise SystemExit(f"{source}: sha256 differs from {MANIFEST_NAME}")

    book = OrderBook(iid, venue_id)
    add_ts: dict[int, tuple[int, int]] = {}  # order_id -> (exchange_ts, receive_ts) of its ADD
    window: list[MarketEvent] = []
    prefix: list[MarketEvent] = []
    first_seq = last_seq = 0
    depth_t0 = (0, 0)
    resting_t0 = 0
    seen_before = 0
    for ev in iter_iap1(source):
        if ev.instrument_id != iid:
            continue
        if ev.exchange_ts < t0:
            seen_before += 1
            if ev.event_type == EventType.ADD:
                add_ts[ev.order_id] = (ev.exchange_ts, ev.receive_ts)
            book.apply(ev)
            continue
        if not window:
            # Window start: reconstruct the book as ADDs of the resting orders.
            resting = book.resting_orders()
            resting_t0 = len(resting)
            depth_t0 = _levels(book)
            rebuilt = []
            for oid, side, price, qty in resting:
                ts, rts = add_ts.get(oid, (t0, t0))
                rebuilt.append(
                    MarketEvent(
                        event_id=0,
                        instrument_id=iid,
                        venue_id=venue_id,
                        exchange_ts=ts,
                        receive_ts=rts,
                        sequence=0,
                        event_type=int(EventType.ADD),
                        side=side,
                        price_ticks=price,
                        qty=qty,
                        order_id=oid,
                        trade_id=0,
                    )
                )
            rebuilt.sort(key=lambda e: (e.exchange_ts, e.order_id))
            prefix = rebuilt
            first_seq = ev.sequence
        last_seq = ev.sequence
        window.append(ev)
        book.apply(ev)
        if len(window) >= n_events:
            break
    if not window:
        raise SystemExit(f"no events of {symbol} at or after {start_local}")
    depth_end = _levels(book)
    resting_end = book.order_count_total()

    events: list[MarketEvent] = []
    for i, ev in enumerate(prefix + window, start=1):
        events.append(replace(ev, event_id=i, sequence=i))
    n_bytes = 16 + 72 * len(events) + 16
    if n_bytes > max_bytes:
        raise SystemExit(f"slice would be {n_bytes} bytes > --max-bytes {max_bytes}")

    if out.exists():
        shutil.rmtree(out)
    norm = out / "normalized"
    norm.mkdir(parents=True)
    out_name = names[0]
    out_file = norm / out_name
    write_iap1(out_file, events)
    out_sha = sha256_file(out_file)
    assert out_file.stat().st_size == n_bytes

    # configs / reference: the symbol's row only (same files the ingest wrote).
    _dump(
        out / "configs" / "instruments" / "instruments.json",
        {
            "x-version": inst_doc.get("x-version", 1),
            "description": f"Universe of the committed sample slice {out.name} (one symbol of "
            f"{dataset.name}; tools/extract_real_slice.py).",
            "calendar": {"trading_days": [date], "timezone": session["timezone"]},
            "sessions": inst_doc["sessions"],
            "instruments": [row],
        },
    )
    for rel in ("configs/venues/venues.json", "configs/execution/execution.json"):
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dataset / rel, out / rel)
    sm = _load(dataset / "reference" / "security_master.json")
    sm["records"] = [r for r in sm.get("records", []) if r.get("symbol") == symbol]
    _dump(out / "reference" / "security_master.json", sm)

    ses = manifest["sessions"][date]
    slice_doc = {
        "extractor": "tools/extract_real_slice.py",
        "extractor_version": EXTRACTOR_VERSION,
        "command": "python tools/extract_real_slice.py " + " ".join(argv),
        "source_dataset": dataset.name,
        "source_dataset_version": manifest["dataset_version"],
        "source_normalized_file": names[0],
        "source_normalized_sha256": source_sha,
        "source_inputs": ses.get("inputs"),
        "source_format": ses.get("format"),
        "day": date,
        "symbol": symbol,
        "instrument_id": iid,
        "venue": manifest["source"].get("venue"),
        "venue_id": venue_id,
        "window_start_local": t0_local.isoformat(),
        "window_start_ts": t0,
        "window_first_ts": window[0].exchange_ts,
        "window_last_ts": window[-1].exchange_ts,
        "window_original_sequence_range": [first_seq, last_seq],
        "symbol_events_before_window": seen_before,
        "prefix_events": len(prefix),
        "window_events": len(window),
        "events": len(events),
        "bytes": n_bytes,
        "resting_orders_at_t0": resting_t0,
        "levels_at_t0": {"bid": depth_t0[0], "ask": depth_t0[1]},
        "resting_orders_at_end": resting_end,
        "levels_at_end": {"bid": depth_end[0], "ask": depth_end[1]},
        "renumbered": ["event_id", "sequence"],
        "prefix_rule": "one ADD per order resting at T0; qty = remaining at T0; "
        "exchange_ts/receive_ts of the original ADD; sorted by (exchange_ts, order_id)",
    }
    _dump(
        out / MANIFEST_NAME,
        {
            "x-version": MANIFEST_VERSION,
            "kind": "real",
            "dataset_version": sha256_events_iap1(events),
            "normalized": {
                "files": {out_name: out_sha},
                "qc_totals": {"events_in": len(events), "events_out": len(events)},
            },
            "source": manifest["source"],
            "universe": [u for u in manifest.get("universe", []) if u.get("symbol") == symbol],
            "sessions": {
                date: {
                    "format": ses.get("format"),
                    # The symbol's cross prints (the feature build folds them,
                    # D-P2-6) and per-symbol ingest counters, as ingested.
                    "per_symbol": {symbol: ses.get("per_symbol", {}).get(symbol, {})},
                    "slice": slice_doc,
                }
            },
        },
    )
    by_type: dict[str, int] = {}
    for ev in window:
        name = EventType(ev.event_type).name
        by_type[name] = by_type.get(name, 0) + 1
    vendor = ", ".join(f"`{i['file']}` sha256 `{i['sha256'][:16]}…`" for i in ses.get("inputs", []))
    md = [
        f"# Sample slice `{out.name}` (committed real-session workload)",
        "",
        "Written by `tools/extract_real_slice.py`; the provenance below is also in "
        f"`{MANIFEST_NAME}` (`sessions.{date}.slice`). Benchmark workload only: not a "
        "research dataset, listed in no pre-registration.",
        "",
        "| field | value |",
        "|---|---|",
        f"| source dataset | `{dataset.name}` (`dataset_version` `{manifest['dataset_version'][:16]}…`) |",
        f"| vendor file | {vendor} |",
        f"| day / symbol | {date} / {symbol} (instrument {iid}, venue {slice_doc['venue']} = {venue_id}) |",
        f"| window start | {t0_local.isoformat()} (`{t0}` ns UTC) |",
        f"| window | {len(window)} events, original sequences {first_seq}..{last_seq}, "
        f"`{window[0].exchange_ts}`..`{window[-1].exchange_ts}` |",
        f"| prefix | {len(prefix)} book-reconstruction ADDs (orders resting at T0) |",
        f"| book at T0 | {resting_t0} orders, {depth_t0[0]} bid / {depth_t0[1]} ask levels |",
        f"| book at end | {resting_end} orders, {depth_end[0]} bid / {depth_end[1]} ask levels |",
        f"| window event types | {', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))} |",
        f"| output | `normalized/{out_name}` {n_bytes} bytes, sha256 `{out_sha}` |",
        f"| command | `{slice_doc['command']}` |",
        "",
    ]
    (out / "SLICE.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    return slice_doc


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, help="ingested real dataset directory")
    ap.add_argument("--date", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start", required=True, help="window start, local session time HH:MM:SS")
    ap.add_argument("--events", type=int, required=True, help="window length in events")
    ap.add_argument("--out", required=True, help="slice dataset directory (replaced)")
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT)
    args = ap.parse_args(argv)
    doc = extract(
        Path(args.dataset),
        args.date,
        args.symbol,
        args.start,
        args.events,
        Path(args.out),
        args.max_bytes,
        argv,
    )
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

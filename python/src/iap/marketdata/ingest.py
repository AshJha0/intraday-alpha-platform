"""Real historical data -> a dataset directory the existing pipeline consumes.

``python -m iap.marketdata ingest --format {itch50,lobster} --input FILE...
--date YYYY-MM-DD --symbols A,B --out DATASET`` (docs/REAL_DATA.md) reads
vendor files the owner obtained, from local disk only, and writes::

    DATASET/
      dataset.json                     manifest: source, input sha256, counts,
                                       dataset_version (distinct from synthetic)
      raw/eq_YYYYMMDD.jsonl            canonical MarketEvents, arrival order
      normalized/…                     iap.marketdata.normalize output + QC
      configs/instruments/…            the universe as reference data
      configs/venues/venues.json       the source venue
      configs/execution/execution.json cost model (copied, venue names replaced)
      reference/security_master.json   point-in-time records of the session(s)
      reference/corporate_actions.csv  the owner's table, when supplied

— the layout ``python -m iap.marketdata --out`` produces for synthetic data,
plus the dataset's own ``configs/`` so ``python -m iap.features`` and
``python -m iap.research run --dataset-dir`` run on it unchanged.  Ingesting
another date into the same directory adds a session.

Two passes, memory bounded by the live orders of the universe: pass 1
streams the vendor file through the reader and mapper into a fixed-width
spool file and decides each instrument's tick size from every displayed
price of the day; pass 2 turns the spool into ticks, per-instrument
sequence numbers and the raw JSONL file, replaying it through the real
order book as a consistency check.  The output is a pure function of the
input bytes and the arguments: no wall clock, no absolute path and no
iteration over an unordered container reaches a file.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import struct
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from iap.core.codec import sha256_file, write_jsonl
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.marketdata.feederrors import BookDivergenceError, IngestError
from iap.marketdata.itch50 import (
    ITCH_TIMEZONE,
    PRICE_SCALE,
    Itch50Mapper,
    Itch50Reader,
    ProtoEvent,
)
from iap.marketdata.lobster import LobsterConverter, sibling_orderbook, symbol_from_filename
from iap.marketdata.normalize import normalize_run
from iap.orderbook.book import ApplyStatus, OrderBook
from iap.reference.corpactions import CorporateActions
from iap.reference.secmaster import SecurityMaster, SecurityRecord

_REPO_ROOT = Path(__file__).resolve().parents[4]

MANIFEST_VERSION = 1
MANIFEST_NAME = "dataset.json"
FORMATS = ("itch50", "lobster")

#: Venue of each supported source (both are Nasdaq order-by-order data).
#: Ids from 101 up are real venues; every synthetic venue id is below 100.
SOURCE_VENUE = {"itch50": ("XNAS", 101), "lobster": ("XNAS", 101)}

#: Domain separation of real dataset versions: no synthetic dataset
#: (``iap.experiment.tracker.data_version``) hashes this prefix.
DATASET_VERSION_DOMAIN = b"iap.real-dataset.v1\n"

#: Tick sizes a US equity can have, in 1/10000 dollars (Reg NMS Rule 612).
TICK_CENT_E4 = 100
TICK_SUBPENNY_E4 = 1
TICK_CHOICES = {"auto": None, "0.01": TICK_CENT_E4, "0.0001": TICK_SUBPENNY_E4}

_SPOOL = struct.Struct("<HqBBqqQQ")
_SPOOL_BATCH = 4096

_ADD = int(EventType.ADD)
_TRADE = int(EventType.TRADE)
_STATUS = int(EventType.STATUS)
_TRADING = int(SessionStatus.TRADING)


def _write_json(path: Path, doc: dict, *, sort_keys: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(doc, indent=2, sort_keys=sort_keys) + "\n")


def load_manifest(dataset_dir: str | Path) -> dict:
    """The ``dataset.json`` of an ingested dataset directory."""
    path = Path(dataset_dir) / MANIFEST_NAME
    if not path.is_file():
        raise IngestError(f"{dataset_dir} is not an ingested dataset: no {MANIFEST_NAME}")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("x-version") != MANIFEST_VERSION or doc.get("kind") != "real":
        raise IngestError(f"{path}: not a real-data manifest of x-version {MANIFEST_VERSION}")
    return doc


def real_dataset_version(normalized_dir: str | Path) -> str:
    """sha256 over the domain prefix and ``basename \\n sha256 \\n`` of every
    ``*.normalized.iap1`` file in sorted order — content-pinned like the
    synthetic ``data_version`` and disjoint from it by construction."""
    h = hashlib.sha256()
    h.update(DATASET_VERSION_DOMAIN)
    for f in sorted(Path(normalized_dir).glob("*.normalized.iap1"), key=lambda p: p.name):
        h.update(f.name.encode())
        h.update(b"\n")
        h.update(sha256_file(f).encode())
        h.update(b"\n")
    return h.hexdigest()


# ------------------------------------------------------------------- sources


class _ItchSource:
    """One ITCH 5.0 file -> proto events + the session's reference facts."""

    format = "itch50"

    def __init__(
        self,
        inputs: list[Path],
        date: str,
        symbols: tuple[str, ...],
        limit_messages: int | None,
        extended_hours: bool,
    ) -> None:
        if len(inputs) != 1:
            raise IngestError("--format itch50 takes exactly one --input file per session date")
        self.inputs = inputs
        self.symbols = symbols
        self.reader = Itch50Reader(inputs[0], symbols=symbols, limit_messages=limit_messages)
        self.mapper = Itch50Mapper(date, symbols, extended_hours=extended_hours)

    def events(self) -> Iterator[ProtoEvent]:
        return self.mapper.events(self.reader)

    def finish(self) -> None:
        missing = [s for s in self.symbols if s not in self.mapper.directory]
        if missing:
            scope = (
                f"the first {self.reader.messages_read} messages"
                if self.reader.limit_reached
                else "the file"
            )
            raise IngestError(
                f"{self.inputs[0].name}: no Stock Directory (R) message for {missing} in {scope}"
            )

    def messages(self) -> dict:
        return {
            "total": self.reader.messages_read,
            "bytes": self.reader.bytes_read,
            "by_type": {k: v for k, v in sorted(self.reader.counts.items()) if v},
            "skipped_unknown_by_type": dict(sorted(self.reader.skipped_unknown.items())),
            "skipped_unknown": sum(self.reader.skipped_unknown.values()),
            "filtered_other_symbols": self.reader.filtered,
            "limit_reached": self.reader.limit_reached,
        }

    def mapping(self) -> dict:
        doc = dict(self.mapper.counters)
        doc["broken_trade_match_numbers"] = list(self.mapper.broken)
        doc["system_events"] = list(self.mapper.system_events)
        return doc

    def facts(self, symbol: str) -> dict:
        rec = self.mapper.directory[symbol]
        crosses = self.mapper.crosses[symbol]
        opening = next((c for c in crosses if c["cross_type"] == "O" and c["price_e4"] > 0), None)
        return {
            "asset_class": "ETF" if rec.etp_flag == "Y" else "EQUITY",
            "lot_size": max(1, rec.round_lot_size),
            "extra_volume": sum(c["shares"] for c in crosses),
            "open_price_e4": opening["price_e4"] if opening else None,
            "crosses": crosses,
            "directory": {
                "locate": rec.locate,
                "market_category": rec.market_category,
                "financial_status": rec.financial_status,
                "round_lots_only": rec.round_lots_only,
                "issue_classification": rec.issue_classification,
                "issue_subtype": rec.issue_subtype,
                "authenticity": rec.authenticity,
                "short_sale_threshold": rec.short_sale_threshold,
                "ipo_flag": rec.ipo_flag,
                "luld_tier": rec.luld_tier,
                "etp_flag": rec.etp_flag,
                "etp_leverage_factor": rec.etp_leverage_factor,
                "inverse_indicator": rec.inverse_indicator,
            },
        }

    def verification(self) -> dict | None:
        return None


class _LobsterSource:
    """One LOBSTER message file (+ orderbook file) per symbol."""

    format = "lobster"

    def __init__(
        self,
        inputs: list[Path],
        date: str,
        symbols: tuple[str, ...],
        limit_messages: int | None,
        orderbooks: list[Path] | None,
        input_symbols: list[str],
    ) -> None:
        if sorted(input_symbols) != list(symbols):
            raise IngestError(
                f"--format lobster needs one message file per symbol: files are for "
                f"{input_symbols}, universe is {list(symbols)}"
            )
        if orderbooks is not None and len(orderbooks) != len(inputs):
            raise IngestError("--orderbook needs one file per --input file, in the same order")
        self.inputs = list(inputs)
        self.symbols = symbols
        self.orderbook_files: list[Path] = []
        self.converters: dict[str, LobsterConverter] = {}
        for i, (path, symbol) in enumerate(zip(inputs, input_symbols, strict=True)):
            book = orderbooks[i] if orderbooks is not None else sibling_orderbook(path)
            if book is not None:
                self.orderbook_files.append(Path(book))
            self.converters[symbol] = LobsterConverter(
                date, symbols.index(symbol), path, book, limit_messages
            )
        self.inputs += self.orderbook_files

    def events(self) -> Iterator[ProtoEvent]:
        for symbol in self.symbols:
            yield from self.converters[symbol].events()

    def finish(self) -> None:
        return None

    def messages(self) -> dict:
        by_type: dict[str, int] = {}
        for conv in self.converters.values():
            for k, v in conv.message_counts.items():
                by_type[k] = by_type.get(k, 0) + v
        return {
            "total": sum(c.messages_read for c in self.converters.values()),
            "by_type": {k: v for k, v in sorted(by_type.items()) if v},
            "skipped_unknown_by_type": {},
            "skipped_unknown": 0,
            "filtered_other_symbols": 0,
        }

    def mapping(self) -> dict:
        totals: dict[str, int] = {}
        for conv in self.converters.values():
            for k, v in conv.counters.items():
                totals[k] = totals.get(k, 0) + v
        return totals

    def facts(self, symbol: str) -> dict:
        conv = self.converters[symbol]
        return {
            "asset_class": "EQUITY",
            "lot_size": 100,
            "extra_volume": conv.cross_volume,
            "open_price_e4": None,
            "crosses": [],
            "directory": {},
            "lobster_counters": dict(conv.counters),
        }

    def verification(self) -> dict | None:
        return {s: self.converters[s].verification for s in self.symbols}


# -------------------------------------------------------------------- passes


class _InstrumentStats:
    __slots__ = ("events", "cent_only", "off_cent_price", "first_add", "first_trade", "volume")

    def __init__(self) -> None:
        self.events = 0
        self.cent_only = True
        self.off_cent_price = 0
        self.first_add = 0
        self.first_trade = 0
        self.volume = 0


def _spool_pass(protos: Iterable[ProtoEvent], spool: Path, n_inst: int) -> list[_InstrumentStats]:
    """Pass 1: proto events -> fixed-width spool, with per-instrument facts."""
    stats = [_InstrumentStats() for _ in range(n_inst)]
    pack = _SPOOL.pack
    batch: list[bytes] = []
    with open(spool, "wb") as f:
        for ev in protos:
            st = stats[ev[0]]
            st.events += 1
            etype, price = ev[2], ev[4]
            if etype == _ADD:
                if st.first_add == 0:
                    st.first_add = price
                if st.cent_only and price % TICK_CENT_E4:
                    st.cent_only = False
                    st.off_cent_price = price
            elif etype == _TRADE:
                if st.first_trade == 0:
                    st.first_trade = price
                st.volume += ev[5]
            batch.append(pack(*ev))
            if len(batch) >= _SPOOL_BATCH:
                f.write(b"".join(batch))
                batch.clear()
        f.write(b"".join(batch))
    return stats


def _iter_spool(spool: Path) -> Iterator[ProtoEvent]:
    size = _SPOOL.size
    with open(spool, "rb") as f:
        while True:
            chunk = f.read(size * _SPOOL_BATCH)
            if not chunk:
                return
            yield from _SPOOL.iter_unpack(chunk)


class _BookCheck:
    """Replays the mapped stream through the real order book (pass 2)."""

    def __init__(self, n_inst: int, venue_id: int) -> None:
        self.books = [OrderBook(i + 1, venue_id) for i in range(n_inst)]
        self.dropped = 0
        self.crossing_adds = 0
        self.crossed_on_resume = 0

    def apply(self, ev: MarketEvent) -> None:
        book = self.books[ev.instrument_id - 1]
        if ev.event_type == _ADD and book.status == _TRADING:
            before = book.level_qty(ev.side, ev.price_ticks)
            status = book.apply(ev)
            if (
                status == ApplyStatus.APPLIED
                and book.level_qty(ev.side, ev.price_ticks) - before != ev.qty
            ):
                self.crossing_adds += 1
        else:
            status = book.apply(ev)
            if ev.event_type == _STATUS and ev.qty == _TRADING and book.is_crossed():
                self.crossed_on_resume += 1
        if status != ApplyStatus.APPLIED:
            self.dropped += 1

    def report(self, symbols: tuple[str, ...]) -> dict:
        per_symbol = {}
        for symbol, book in zip(symbols, self.books, strict=True):
            counters = book.counters()
            per_symbol[symbol] = {
                "events_applied": counters["events_applied"],
                "drop_counters": {k: v for k, v in counters.items() if v and k != "events_applied"},
                "resting_orders_at_end": book.order_count_total(),
                "crossed_at_end": book.is_crossed(),
            }
        return {
            "dropped": self.dropped,
            "crossing_adds": self.crossing_adds,
            "crossed_on_resume": self.crossed_on_resume,
            "clean": self.dropped == 0 and self.crossing_adds == 0 and self.crossed_on_resume == 0,
            "per_symbol": per_symbol,
        }


def sequence_events(
    protos: Iterable[ProtoEvent],
    ticks_e4: list[int],
    venue_id: int,
    counters: dict[str, int] | None = None,
) -> Iterator[MarketEvent]:
    """Proto events -> canonical MarketEvents.

    Prices become integer ticks (``ticks_e4[instrument]`` 1/10000 dollars
    per tick): book events divide exactly, a TRADE print that is off the
    tick grid (a non-displayed midpoint execution) is rounded half up and
    counted in ``counters["trade_prices_rounded_to_tick"]``.  ``sequence``
    counts from 1 per instrument, ``event_id`` from 1 over the stream,
    ``receive_ts == exchange_ts`` (the feed has no capture time).
    """
    seqs = [0] * len(ticks_e4)
    n = 0
    rounded = 0
    for inst, ts, etype, side, price, qty, oid, tid in protos:
        tick = ticks_e4[inst]
        if etype == _TRADE:
            if price % tick:
                rounded += 1
            ticks = max(1, (2 * price + tick) // (2 * tick))
        else:
            ticks = price // tick
        seqs[inst] += 1
        n += 1
        yield MarketEvent(
            n, inst + 1, venue_id, ts, ts, seqs[inst], etype, side, ticks, qty, oid, tid
        )
    if counters is not None:
        counters["events"] = n
        counters["trade_prices_rounded_to_tick"] = rounded


def _raw_pass(
    spool: Path,
    raw_path: Path,
    ticks_e4: list[int],
    venue_id: int,
    check: _BookCheck | None,
) -> dict:
    """Pass 2: spool -> canonical raw JSONL (ticks, sequences, event ids)."""
    info: dict[str, int] = {}

    def events() -> Iterator[MarketEvent]:
        for ev in sequence_events(_iter_spool(spool), ticks_e4, venue_id, info):
            if check is not None:
                check.apply(ev)
            yield ev

    tmp = raw_path.with_name(raw_path.name + ".partial")
    write_jsonl(tmp, events())
    os.replace(tmp, raw_path)
    return info


# ------------------------------------------------------------------- configs


def _instrument_rows(manifest: dict) -> list[dict]:
    """instruments.json rows, derived from every session in the manifest."""
    sessions = [manifest["sessions"][d] for d in sorted(manifest["sessions"])]
    rows = []
    for entry in manifest["universe"]:
        symbol = entry["symbol"]
        per_day = [s["per_symbol"][symbol] for s in sessions]
        priced = [p for p in per_day if p["ref_price_e4"]]
        ref_price = priced[0]["ref_price_e4"] / PRICE_SCALE if priced else 1.0
        adv = max(1, sum(p["volume"] for p in per_day) // len(per_day))
        rows.append(
            {
                "symbol": symbol,
                "instrument_id": entry["instrument_id"],
                "asset_class": entry["asset_class"],
                "currency": "USD",
                "tick_size": entry["tick_size"],
                "lot_size": entry["lot_size"],
                "ref_price": ref_price,
                "adv": adv,
                "venues": [manifest["source"]["venue"]],
            }
        )
    return rows


def _write_configs(out: Path, manifest: dict, configs_dir: Path) -> None:
    venue = manifest["source"]["venue"]
    session = {
        "timezone": ITCH_TIMEZONE,
        "open": "09:30:00",
        "close": "16:00:00",
        "open_auction": "09:30:00",
        "close_auction": "16:00:00",
    }
    _write_json(
        out / "configs" / "instruments" / "instruments.json",
        {
            "x-version": 1,
            "description": (
                "Universe of an ingested real dataset (docs/REAL_DATA.md). tick_size, lot_size "
                "and asset_class come from the feed; ref_price is the first session's opening "
                "cross (else first trade, else first displayed order); adv is the mean "
                "per-session volume ON THE SOURCE VENUE, not consolidated volume."
            ),
            "calendar": {
                "trading_days": sorted(manifest["sessions"]),
                "timezone": ITCH_TIMEZONE,
            },
            "sessions": {"EQUITY": session, "ETF": dict(session)},
            "instruments": _instrument_rows(manifest),
        },
        sort_keys=False,
    )
    _write_json(
        out / "configs" / "venues" / "venues.json",
        {
            "x-version": 1,
            "description": (
                "Source venue of an ingested real dataset. Fees are indicative list prices "
                "(edit to your own tier); latency is zero because the feed carries exchange "
                "timestamps only (receive_ts == exchange_ts)."
            ),
            "venues": [
                {
                    "venue": venue,
                    "venue_id": manifest["source"]["venue_id"],
                    "asset_class": "EQUITY",
                    "taker_fee_per_share": 0.003,
                    "maker_rebate_per_share": 0.002,
                    "latency": {"mean_ns": 0, "jitter_ns": 0},
                    "supports": ["MBO", "AUCTION", "HALT"],
                }
            ],
        },
        sort_keys=False,
    )
    template = configs_dir / "execution" / "execution.json"
    with open(template, encoding="utf-8") as f:
        execution = json.load(f)
    execution.setdefault("sor", {})["eq_venues"] = [venue]
    execution["sor"]["fx_venues"] = []
    _write_json(out / "configs" / "execution" / "execution.json", execution, sort_keys=False)


def _security_records(
    manifest: dict, date: str, source_format: str, facts: dict[str, dict]
) -> list[SecurityRecord]:
    records = []
    for entry in manifest["universe"]:
        symbol = entry["symbol"]
        directory = facts[symbol]["directory"]
        records.append(
            SecurityRecord(
                symbol=symbol,
                effective_date=date,
                instrument_id=entry["instrument_id"],
                venue=manifest["source"]["venue"],
                asset_class=facts[symbol]["asset_class"],
                tick_size=entry["tick_size"],
                round_lot_size=facts[symbol]["lot_size"],
                source=source_format,
                **directory,
            )
        )
    return records


# ---------------------------------------------------------------------- ingest


def _check_date(date: str) -> str:
    try:
        return _dt.date.fromisoformat(date).isoformat()
    except ValueError:
        raise IngestError(f"--date must be YYYY-MM-DD, got {date!r}") from None


def _decide_ticks(
    symbols: tuple[str, ...],
    stats: list[_InstrumentStats],
    forced_e4: int | None,
    previous: dict[str, int],
) -> list[int]:
    ticks = []
    for symbol, st in zip(symbols, stats, strict=True):
        observed = TICK_CENT_E4 if st.cent_only else TICK_SUBPENNY_E4
        tick = forced_e4 if forced_e4 is not None else previous.get(symbol, observed)
        if tick == TICK_CENT_E4 and not st.cent_only:
            raise IngestError(
                f"{symbol}: displayed price {st.off_cent_price / PRICE_SCALE:.4f} is not a "
                "multiple of the 0.01 tick this dataset uses for it; re-ingest every session "
                "of the dataset with --tick-size 0.0001"
            )
        if symbol in previous and previous[symbol] != tick:
            raise IngestError(
                f"{symbol}: tick size {tick / PRICE_SCALE} differs from the "
                f"{previous[symbol] / PRICE_SCALE} already in the dataset"
            )
        ticks.append(tick)
    return ticks


def ingest(
    source_format: str,
    inputs: Iterable[str | Path],
    date: str,
    symbols: Iterable[str] | None,
    out_dir: str | Path,
    *,
    configs_dir: str | Path | None = None,
    limit_messages: int | None = None,
    extended_hours: bool = False,
    tick_size: str = "auto",
    orderbooks: Iterable[str | Path] | None = None,
    corporate_actions: str | Path | None = None,
    allow_book_divergence: bool = False,
    book_check: bool = True,
) -> dict:
    """Ingest one session into ``out_dir``; returns the manifest document."""
    if source_format not in FORMATS:
        raise IngestError(f"unknown format {source_format!r}; known: {list(FORMATS)}")
    if tick_size not in TICK_CHOICES:
        raise IngestError(f"--tick-size must be one of {list(TICK_CHOICES)}")
    date = _check_date(date)
    paths = [Path(p) for p in inputs]
    if not paths:
        raise IngestError("no --input file given")
    for p in paths:
        if not p.is_file():
            raise IngestError(f"input file not found: {p}")
    if limit_messages is not None and limit_messages < 1:
        raise IngestError("--limit-messages must be >= 1")
    configs_dir = Path(configs_dir) if configs_dir is not None else _REPO_ROOT / "configs"
    book_paths = [Path(p) for p in orderbooks] if orderbooks is not None else None
    # everything that can be refused is refused before the feed is read
    if not (configs_dir / "execution" / "execution.json").is_file():
        raise IngestError(
            f"execution config template not found: "
            f"{configs_dir / 'execution' / 'execution.json'} (pass --configs-dir pointing at "
            "the repository's configs/)"
        )
    table = CorporateActions.load_csv(corporate_actions) if corporate_actions is not None else None

    input_symbols: list[str] = []
    if source_format == "lobster":
        given = list(symbols) if symbols is not None else None
        for i, p in enumerate(paths):
            symbol = given[i] if given is not None and i < len(given) else symbol_from_filename(p)
            if not symbol:
                raise IngestError(
                    f"cannot tell which symbol {p.name} is for: pass --symbols in --input order"
                )
            input_symbols.append(symbol)
        symbols = input_symbols
    if not symbols:
        raise IngestError("--symbols is required (the universe to extract)")
    universe = tuple(sorted(set(symbols)))
    if len(universe) != len(list(symbols)):
        raise IngestError("--symbols lists a symbol twice")
    venue, venue_id = SOURCE_VENUE[source_format]

    out = Path(out_dir)
    manifest_path = out / MANIFEST_NAME
    if manifest_path.is_file():
        manifest = load_manifest(out)
        have = tuple(e["symbol"] for e in manifest["universe"])
        if manifest["source"]["format"] != source_format or have != universe:
            raise IngestError(
                f"{out} already holds a {manifest['source']['format']} dataset of {list(have)}; "
                f"a dataset has one format and one universe — use another --out directory"
            )
    else:
        if out.exists() and any(out.iterdir()):
            raise IngestError(f"{out} exists, is not empty and is not an ingested dataset")
        manifest = {
            "x-version": MANIFEST_VERSION,
            "kind": "real",
            "source": {"format": source_format, "venue": venue, "venue_id": venue_id},
            "universe": [],
            "sessions": {},
        }
    previous_ticks = {e["symbol"]: e["tick_size_e4"] for e in manifest["universe"]}

    if source_format == "itch50":
        if book_paths is not None:
            raise IngestError("--orderbook applies to --format lobster only")
        source: _ItchSource | _LobsterSource = _ItchSource(
            paths, date, universe, limit_messages, extended_hours
        )
    else:
        source = _LobsterSource(paths, date, universe, limit_messages, book_paths, input_symbols)

    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"eq_{date.replace('-', '')}"
    spool = raw_dir / f"{stem}.spool"
    try:
        stats = _spool_pass(source.events(), spool, len(universe))
        source.finish()
        verification = source.verification()
        if verification is not None and not allow_book_divergence:
            for symbol, report in verification.items():
                if report["status"] == "diverged":
                    raise BookDivergenceError(
                        f"{symbol}: reconstructed book diverges from the orderbook file: "
                        f"{json.dumps(report['first_divergence'], sort_keys=True)}",
                        report,
                    )
        ticks = _decide_ticks(universe, stats, TICK_CHOICES[tick_size], previous_ticks)
        check = _BookCheck(len(universe), venue_id) if book_check else None
        raw_info = _raw_pass(spool, raw_dir / f"{stem}.jsonl", ticks, venue_id, check)
    except BaseException:
        # a refused / failed session leaves no half-written file behind
        (raw_dir / f"{stem}.jsonl.partial").unlink(missing_ok=True)
        if not manifest_path.is_file():
            spool.unlink(missing_ok=True)
            for directory in (raw_dir, out):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
        raise
    finally:
        spool.unlink(missing_ok=True)

    facts = {s: source.facts(s) for s in universe}
    if not manifest["universe"]:
        manifest["universe"] = [
            {
                "symbol": s,
                "instrument_id": i + 1,
                "asset_class": facts[s]["asset_class"],
                "tick_size": ticks[i] / PRICE_SCALE,
                "tick_size_e4": ticks[i],
                "lot_size": facts[s]["lot_size"],
            }
            for i, s in enumerate(universe)
        ]
    per_symbol = {}
    for i, s in enumerate(universe):
        st, fact = stats[i], facts[s]
        ref = fact["open_price_e4"] or st.first_trade or st.first_add or None
        per_symbol[s] = {
            "events": st.events,
            "volume": st.volume + fact["extra_volume"],
            "ref_price_e4": ref,
            "ref_price_source": (
                "opening_cross"
                if fact["open_price_e4"]
                else "first_trade"
                if st.first_trade
                else "first_displayed_order"
                if st.first_add
                else "none"
            ),
            "asset_class": fact["asset_class"],
            "lot_size": fact["lot_size"],
            "crosses": fact["crosses"],
        }
    raw_file = raw_dir / f"{stem}.jsonl"
    session = {
        "format": source_format,
        "inputs": [
            {"file": p.name, "sha256": sha256_file(p), "bytes": p.stat().st_size}
            for p in source.inputs
        ],
        "symbols": list(universe),
        "limit_messages": limit_messages,
        "extended_hours": bool(extended_hours),
        "messages": source.messages(),
        "mapping": source.mapping(),
        "per_symbol": per_symbol,
        "raw": {"file": raw_file.name, "sha256": sha256_file(raw_file), **raw_info},
        "book_check": check.report(universe) if check is not None else None,
        "book_verification": verification,
    }
    manifest["sessions"][date] = session
    manifest["sessions"] = {d: manifest["sessions"][d] for d in sorted(manifest["sessions"])}

    _write_configs(out, manifest, configs_dir)
    master_path = out / "reference" / "security_master.json"
    master = SecurityMaster.load(master_path) if master_path.is_file() else SecurityMaster()
    for record in _security_records(manifest, date, source_format, facts):
        master.upsert(record)
    master.save(master_path)
    if table is not None:
        target = out / "reference" / "corporate_actions.csv"
        if Path(corporate_actions).resolve() != target.resolve():
            shutil.copyfile(corporate_actions, target)
        manifest["corporate_actions"] = {
            "file": "reference/corporate_actions.csv",
            "sha256": sha256_file(target),
            "actions": len(table),
        }

    normalized_dir = out / "normalized"
    qc = normalize_run(raw_dir, normalized_dir)
    qc["raw_dir"] = "raw"
    _write_json(normalized_dir / "qc_report.json", qc, sort_keys=False)
    manifest["normalized"] = {
        "files": {
            f.name: sha256_file(f)
            for f in sorted(normalized_dir.glob("*.normalized.*"), key=lambda p: p.name)
        },
        "qc_totals": qc["totals"],
    }
    manifest["dataset_version"] = real_dataset_version(normalized_dir)
    _write_json(manifest_path, manifest)
    return manifest


# ------------------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m iap.marketdata ingest",
        description="Ingest real historical market data files into a dataset directory "
        "(docs/REAL_DATA.md). Reads local files only; nothing is downloaded.",
    )
    p.add_argument("--format", required=True, choices=FORMATS, dest="source_format")
    p.add_argument(
        "--input",
        required=True,
        nargs="+",
        help="itch50: the day's file (.gz or plain); lobster: one message file per symbol",
    )
    p.add_argument("--date", required=True, help="session date, YYYY-MM-DD")
    p.add_argument(
        "--symbols",
        default=None,
        help="comma-separated universe (itch50: required; lobster: in --input order, "
        "default: taken from the file names)",
    )
    p.add_argument("--out", required=True, help="dataset directory (created; git-ignored place)")
    p.add_argument(
        "--orderbook",
        nargs="+",
        default=None,
        help="lobster: orderbook files in --input order (default: the *_orderbook_* sibling)",
    )
    p.add_argument(
        "--limit-messages",
        type=int,
        default=None,
        help="stop after this many input messages (smoke runs)",
    )
    p.add_argument(
        "--extended-hours",
        action="store_true",
        help="itch50: treat pre-/post-market as TRADING (default: regular session only)",
    )
    p.add_argument(
        "--tick-size",
        choices=sorted(TICK_CHOICES),
        default="auto",
        help="auto: 0.01 unless a displayed price of the symbol is sub-penny",
    )
    p.add_argument(
        "--corporate-actions", default=None, help="CSV table to validate and copy in (optional)"
    )
    p.add_argument(
        "--allow-book-divergence",
        action="store_true",
        help="lobster: record a book/orderbook-file divergence instead of failing",
    )
    p.add_argument(
        "--no-book-check",
        action="store_true",
        help="skip the replay of the mapped stream through the order book",
    )
    p.add_argument(
        "--configs-dir",
        default=str(_REPO_ROOT / "configs"),
        help="configs/ tree holding the execution/execution.json template",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    t0 = time.perf_counter()
    try:
        manifest = ingest(
            args.source_format,
            args.input,
            args.date,
            symbols,
            args.out,
            configs_dir=args.configs_dir,
            limit_messages=args.limit_messages,
            extended_hours=args.extended_hours,
            tick_size=args.tick_size,
            orderbooks=args.orderbook,
            corporate_actions=args.corporate_actions,
            allow_book_divergence=args.allow_book_divergence,
            book_check=not args.no_book_check,
        )
    except BookDivergenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (IngestError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - t0
    date = _check_date(args.date)
    session = manifest["sessions"][date]
    total = session["messages"]["total"]
    summary = {
        "dataset_dir": str(Path(args.out)),
        "dataset_version": manifest["dataset_version"],
        "date": date,
        "sessions": sorted(manifest["sessions"]),
        "symbols": session["symbols"],
        "messages": session["messages"],
        "events": session["raw"]["events"],
        "qc_totals": manifest["normalized"]["qc_totals"],
        "book_check_clean": session["book_check"]["clean"] if session["book_check"] else None,
        "book_verification": (
            {s: r["status"] for s, r in session["book_verification"].items()}
            if session["book_verification"]
            else None
        ),
        "timing_s": round(elapsed, 3),
        "messages_per_second": round(total / elapsed) if elapsed > 0 else None,
    }
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0

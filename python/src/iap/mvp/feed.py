"""MVP market-data feed: seeded generation -> normalisation -> captured stream.

The feed composes the MVP reference data (``configs/mvp/instruments.json``
+ ``configs/mvp/venues.json`` + the ``session`` block of ``mvp.json``) into
one :class:`~iap.reference.refdata.ReferenceData`, drives the unmodified
:class:`~iap.marketdata.generator.MarketDataGenerator` for one session
into ``<run_dir>/raw/``, runs the unmodified raw -> normalized pipeline
(:func:`~iap.marketdata.normalize.normalize_run`, QC report included) into
``<run_dir>/normalized/``, filters the normalized event-time stream to the
configured instrument and captures it as ``<run_dir>/events.jsonl`` plus
its IAP1 twin (``events.iap1``, sha256 recorded).

``data_version`` is the sha256 of the IAP1 encoding of the captured
stream — the same definition the platform's experiment tracker uses for a
dataset — so a replay from the captured file carries the same version as
the run that captured it.

:class:`JsonlMarketDataSource` is the
:class:`~iap.contracts.protocols.MarketDataSource` over the captured file:
``events(start_ns, end_ns)`` streams the events whose ``exchange_ts`` lies
in ``[start_ns, end_ns)`` in file order (already event-time ordered).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple, Union

from iap.core.codec import (
    iter_jsonl,
    read_jsonl,
    sha256_events_iap1,
    sha256_file,
    write_iap1,
    write_jsonl,
)
from iap.core.events import MarketEvent
from iap.marketdata.generator import MarketDataGenerator, load_generator_config
from iap.marketdata.normalize import normalize_run
from iap.mvp.config import MvpConfig
from iap.reference.refdata import ReferenceData

__all__ = [
    "EVENTS_JSONL",
    "EVENTS_IAP1",
    "FEED_MANIFEST",
    "FeedResult",
    "JsonlMarketDataSource",
    "compose_reference_documents",
    "build_reference_data",
    "generate_feed",
    "load_feed",
    "stream_versions",
]

EVENTS_JSONL = "events.jsonl"
EVENTS_IAP1 = "events.iap1"
FEED_MANIFEST = "feed.json"
_FEED_VERSION = 1


def compose_reference_documents(cfg: MvpConfig) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(instruments_cfg, venues_cfg)`` for :class:`ReferenceData`.

    The instruments document is the MVP instruments file with the session
    block injected from ``mvp.json`` (one source of truth for the session
    window); the venues document is the MVP venues file restricted to the
    configured ``venues`` (order and ids as listed there).
    """
    with open(cfg.reference_path("instruments"), encoding="utf-8") as fh:
        instruments_doc = json.load(fh)
    with open(cfg.reference_path("venues"), encoding="utf-8") as fh:
        venues_doc = json.load(fh)
    where = cfg.reference["instruments"]
    rows = instruments_doc.get("instruments")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{where}: missing 'instruments' array")
    symbols = [row.get("symbol") for row in rows]
    if cfg.instrument not in symbols:
        raise ValueError(f"{where}: instrument {cfg.instrument!r} is not defined "
                         f"(have {symbols})")
    calendar = instruments_doc.get("calendar")
    if not isinstance(calendar, dict) or "trading_days" not in calendar:
        raise ValueError(f"{where}: missing calendar.trading_days")
    if cfg.session.trading_day not in calendar["trading_days"]:
        raise ValueError(f"{where}: session trading_day {cfg.session.trading_day!r} "
                         "is not in calendar.trading_days")
    session = {
        "timezone": cfg.session.timezone,
        "open": cfg.session.open,
        "close": cfg.session.close,
    }
    # The generator resolves the bounds of every asset class it knows before
    # looking at the universe, so an FX session block is required even though
    # the MVP universe has no FX instrument (its FX file is simply empty).
    instruments_cfg = {
        "calendar": calendar,
        "sessions": {"EQUITY": dict(session), "ETF": dict(session), "FX": dict(session)},
        "instruments": rows,
    }
    venue_rows = venues_doc.get("venues")
    if not isinstance(venue_rows, list) or not venue_rows:
        raise ValueError(f"{cfg.reference['venues']}: missing 'venues' array")
    by_name = {row.get("venue"): row for row in venue_rows}
    missing = [v for v in cfg.venues if v not in by_name]
    if missing:
        raise ValueError(f"{cfg.reference['venues']}: venues {missing} not defined")
    venues_cfg = {"venues": [by_name[v] for v in cfg.venues]}
    return instruments_cfg, venues_cfg


def build_reference_data(cfg: MvpConfig) -> ReferenceData:
    """The MVP :class:`ReferenceData` (validated by its constructor)."""
    instruments_cfg, venues_cfg = compose_reference_documents(cfg)
    ref = ReferenceData(instruments_cfg, venues_cfg)
    inst = ref.instrument(cfg.instrument)
    if inst.asset_class not in ("EQUITY", "ETF"):
        raise ValueError(f"MVP instrument {cfg.instrument!r} must be an equity, "
                         f"got {inst.asset_class}")
    listed = set(inst.venues)
    for v in cfg.venues:
        if v not in listed:
            raise ValueError(f"MVP instrument {cfg.instrument!r} is not listed on {v}")
    return ref


@dataclass(frozen=True)
class FeedResult:
    """The captured stream and its identity."""

    events: Tuple[MarketEvent, ...]
    instrument_id: int
    data_version: str        #: sha256 of the IAP1 encoding of the stream
    events_sha256: str       #: sha256 of ``events.jsonl`` bytes
    iap1_sha256: str         #: sha256 of ``events.iap1`` bytes (== data_version)
    n_events_generated: int  #: normalized events before the instrument filter
    generator_stats: Dict[str, Any]
    qc_totals: Dict[str, int]

    @property
    def first_ts(self) -> int:
        return self.events[0].exchange_ts

    @property
    def last_ts(self) -> int:
        return self.events[-1].exchange_ts


def stream_versions(events: List[MarketEvent], jsonl_path: Path,
                    iap1_path: Path) -> Tuple[str, str, str]:
    """``(data_version, events_sha256, iap1_sha256)`` of a captured stream."""
    return sha256_events_iap1(events), sha256_file(jsonl_path), sha256_file(iap1_path)


def generate_feed(cfg: MvpConfig, run_dir: Union[str, Path]) -> FeedResult:
    """Generate, normalise and capture the MVP session into ``run_dir``."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    ref = build_reference_data(cfg)
    gen_cfg = load_generator_config(cfg.reference_path("generator"))
    gen_cfg["seed"] = cfg.seed
    gen_cfg["sessions"] = 1
    generator = MarketDataGenerator(ref, gen_cfg)
    raw_dir = run_dir / "raw"
    normalized_dir = run_dir / "normalized"
    gen_stats = generator.generate_run(raw_dir)
    qc = normalize_run(raw_dir, normalized_dir)

    instrument_id = ref.instrument(cfg.instrument).instrument_id
    events: List[MarketEvent] = []
    n_generated = 0
    for path in sorted(normalized_dir.glob("*.normalized.jsonl")):
        for ev in iter_jsonl(path):
            n_generated += 1
            if ev.instrument_id == instrument_id:
                events.append(ev)
    if not events:
        raise RuntimeError(f"generator produced no events for {cfg.instrument!r}")
    for i, ev in enumerate(events):
        ev.event_id = i + 1
    jsonl_path = run_dir / EVENTS_JSONL
    iap1_path = run_dir / EVENTS_IAP1
    write_jsonl(jsonl_path, events)
    write_iap1(iap1_path, events)
    data_version, events_sha, iap1_sha = stream_versions(events, jsonl_path, iap1_path)
    manifest = {
        "x-version": _FEED_VERSION,
        "instrument": cfg.instrument,
        "instrument_id": instrument_id,
        "seed": cfg.seed,
        "n_events": len(events),
        "n_events_generated": n_generated,
        "data_version": data_version,
        "events_jsonl_sha256": events_sha,
        "events_iap1_sha256": iap1_sha,
        "first_ts": events[0].exchange_ts,
        "last_ts": events[-1].exchange_ts,
        "generator": gen_stats,
        "qc_totals": qc["totals"],
    }
    with open(run_dir / FEED_MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return FeedResult(
        events=tuple(events), instrument_id=instrument_id, data_version=data_version,
        events_sha256=events_sha, iap1_sha256=iap1_sha, n_events_generated=n_generated,
        generator_stats=gen_stats, qc_totals=dict(qc["totals"]),
    )


def load_feed(run_dir: Union[str, Path]) -> FeedResult:
    """Load a captured stream (the incident-replay input) and re-derive its
    identity from the bytes on disk — never from the manifest."""
    run_dir = Path(run_dir)
    jsonl_path = run_dir / EVENTS_JSONL
    iap1_path = run_dir / EVENTS_IAP1
    manifest_path = run_dir / FEED_MANIFEST
    for p in (jsonl_path, iap1_path, manifest_path):
        if not p.is_file():
            raise ValueError(f"captured feed incomplete: missing {p}")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    events = read_jsonl(jsonl_path)
    if not events:
        raise ValueError(f"{jsonl_path}: empty event stream")
    instrument_id = int(manifest["instrument_id"])
    if any(ev.instrument_id != instrument_id for ev in events):
        raise ValueError(f"{jsonl_path}: contains events of another instrument")
    data_version, events_sha, iap1_sha = stream_versions(events, jsonl_path, iap1_path)
    if data_version != iap1_sha:
        raise ValueError(f"{iap1_path}: bytes do not match {jsonl_path} (data_version "
                         f"{data_version} != iap1 sha256 {iap1_sha})")
    return FeedResult(
        events=tuple(events), instrument_id=instrument_id, data_version=data_version,
        events_sha256=events_sha, iap1_sha256=iap1_sha,
        n_events_generated=int(manifest["n_events_generated"]),
        generator_stats=dict(manifest["generator"]), qc_totals=dict(manifest["qc_totals"]),
    )


class JsonlMarketDataSource:
    """:class:`~iap.contracts.protocols.MarketDataSource` over a captured
    ``events.jsonl`` (event-time ordered)."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise ValueError(f"market data file not found: {self.path}")

    def events(self, start_ns: int, end_ns: int) -> Iterator[MarketEvent]:
        """Events with ``start_ns <= exchange_ts < end_ns``, in file order."""
        if end_ns <= start_ns:
            raise ValueError("events(): end_ns must exceed start_ns")
        for ev in iter_jsonl(self.path):
            if ev.exchange_ts >= end_ns:
                break
            if ev.exchange_ts >= start_ns:
                yield ev

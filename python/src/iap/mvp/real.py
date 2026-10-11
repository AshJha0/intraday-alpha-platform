"""The MVP loop on an already-ingested REAL dataset (v1.15 X-P1-2).

``python -m iap.mvp.real --dataset DATASET_DIR --date YYYY-MM-DD
--symbols AAPL[,MSFT] --config configs/prereg/v2.json --out OUT
[--limit-events N] [--allow-legacy-dataset]``

Feeds the normalized event stream of a dataset directory written by
``iap.marketdata.ingest`` (``dataset.json`` + ``normalized/`` +
``configs/{instruments,venues}/``, docs/REAL_DATA.md) through the SAME
:class:`~iap.mvp.engine.MvpEngine` and :func:`~iap.mvp.session.run_session`
the synthetic MVP uses -- nothing in the loop changes; only the feed and the
reference data are different.  One engine per symbol (the MVP is a
single-instrument loop), each into ``OUT/<SYMBOL>/``.

Per symbol:

* **Reference root.** ``OUT/<SYMBOL>/reference_root/`` holds the
  instruments / venues documents composed from the dataset (the one symbol,
  its listed venues, the dataset calendar) plus verbatim copies of the risk,
  execution, alpha-parameter, alpha-registry and generator documents the
  base ``configs/mvp/mvp.json`` references, at the SAME repository-relative
  paths -- so ``config_version`` and ``run_id`` never depend on where
  ``OUT`` is.
* **Session.** ``trading_day`` = ``--date``; timezone / open / close from
  the dataset's ``sessions`` block for the instrument's asset class.
* **Streaming.** The stream is read with :func:`~iap.core.codec.iter_iap1`
  (chunked) or :func:`~iap.core.codec.iter_jsonl`; it is never held as a
  list.  Pass 1 counts the symbol's events (stopping at ``--limit-events``),
  pass 2 hashes them (``data_version`` = sha256 of their IAP1 encoding, the
  platform's dataset definition; ``events_sha256`` = sha256 of their JSONL
  lines), pass 3 drives the engine.  Event ids are renumbered 1..N after
  the symbol filter, exactly as :func:`~iap.mvp.feed.generate_feed` does.
* **Manifest.** ``OUT/<SYMBOL>/run_manifest.json``: dataset identity
  (``dataset_version``, the verified sha256 of the normalized file, the
  dataset ``config_version``), the pre-registration config and its hash,
  the MVP ``config_version`` / ``run_id``, event count, parents / children
  / fills / P&L and the trace digest.  No wall clock, no absolute path:
  two runs of the same inputs write identical manifests.

The pre-registration config (``--config``) is hashed into the manifest.
The MVP engine does not (yet) consume its opt-ins (regulatory fees,
execution realism); the manifest lists them under ``not_applied`` rather
than claiming them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iap.contracts.versions import content_hash
from iap.core.codec import (
    IAP1_MAGIC,
    IAP1_VERSION,
    encode_iap1,
    encode_jsonl_line,
    iter_iap1,
    iter_jsonl,
    sha256_file,
)
from iap.core.events import MarketEvent
from iap.marketdata.ingest import MANIFEST_NAME, MANIFEST_VERSION, config_version
from iap.mvp.config import DEFAULT_CONFIG_PATH, REPO_ROOT, MvpConfig
from iap.mvp.feed import FeedResult
from iap.mvp.session import RunResult, run_session

__all__ = ["RUN_MANIFEST", "RealRun", "run_real", "run_symbol", "main"]

RUN_MANIFEST = "run_manifest.json"
_MANIFEST_VERSION = 1
_INSTRUMENTS_REL = "configs/real/instruments.json"
_VENUES_REL = "configs/real/venues.json"
_COPIED_KEYS = ("generator", "risk", "execution", "alpha_params", "alpha_registry")
# IAP1 header / trailer layout (iap.core.codec): magic, version, count / crc, reserved, count.
_HEADER = struct.Struct("<IIQ")
_TRAILER = struct.Struct("<IIQ")


@dataclass(frozen=True)
class RealRun:
    """One symbol's run: the session result and its manifest."""

    symbol: str
    result: RunResult
    manifest: dict[str, Any]


class _StreamedEvents:
    """Re-iterable view of one symbol's events (never materialised).

    ``run_session`` only iterates ``feed.events`` and takes ``len``;
    ``FeedResult.first_ts`` / ``last_ts`` index ``[0]`` / ``[-1]``, which
    are served from the events seen by the counting pass."""

    def __init__(
        self,
        factory: Callable[[], Iterator[MarketEvent]],
        n: int,
        first: MarketEvent,
        last: MarketEvent,
    ) -> None:
        self._factory = factory
        self._n = n
        self._first = first
        self._last = last

    def __iter__(self) -> Iterator[MarketEvent]:
        return self._factory()

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i: int) -> MarketEvent:
        if i == 0:
            return self._first
        if i == -1:
            return self._last
        raise IndexError("streamed events support [0] and [-1] only")


def _read_dataset_manifest(dataset: Path, allow_legacy: bool) -> tuple[dict[str, Any], bool]:
    path = dataset / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"{dataset} is not an ingested dataset: no {MANIFEST_NAME}")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("kind") != "real":
        raise ValueError(f"{path}: not a real-data manifest")
    version = doc.get("x-version")
    legacy = isinstance(version, int) and version < MANIFEST_VERSION
    if legacy and not allow_legacy:
        raise ValueError(
            f"{path}: manifest x-version {version} < {MANIFEST_VERSION} (ingested under "
            "pre-v1.14 rules); re-ingest it, or pass --allow-legacy-dataset for a smoke run "
            "(recorded as legacy in the run manifest)"
        )
    if not legacy and version != MANIFEST_VERSION:
        raise ValueError(f"{path}: unsupported manifest x-version {version!r}")
    return doc, legacy


def _normalized_source(dataset: Path, manifest: dict[str, Any]) -> tuple[Path, str]:
    """The normalized stream file (IAP1 preferred) and its VERIFIED sha256."""
    files = (manifest.get("normalized") or {}).get("files") or {}
    for suffix in (".normalized.iap1", ".normalized.jsonl"):
        names = sorted(n for n in files if n.endswith(suffix))
        if len(names) > 1:
            raise ValueError(f"{dataset}: several {suffix} files; one session per run")
        if names:
            path = dataset / "normalized" / names[0]
            if not path.is_file():
                raise ValueError(f"{path}: listed in {MANIFEST_NAME} but missing")
            actual = sha256_file(path)
            if actual != files[names[0]]:
                raise ValueError(
                    f"{path}: sha256 {actual} != {files[names[0]]} recorded in {MANIFEST_NAME}"
                )
            return path, actual
    raise ValueError(f"{dataset}/{MANIFEST_NAME}: no normalized stream listed")


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def _symbol_config(
    base: MvpConfig, dataset: Path, date: str, symbol: str, stage: Path
) -> tuple[MvpConfig, int]:
    """The MVP config of one real symbol over a staged reference root."""
    inst_doc = _load_json(dataset / "configs" / "instruments" / "instruments.json")
    venues_doc = _load_json(dataset / "configs" / "venues" / "venues.json")
    rows = [r for r in inst_doc.get("instruments", []) if r.get("symbol") == symbol]
    if len(rows) != 1:
        have = sorted(r.get("symbol") for r in inst_doc.get("instruments", []))
        raise ValueError(f"{dataset}: symbol {symbol!r} not in the dataset universe {have}")
    row = rows[0]
    calendar = inst_doc.get("calendar") or {}
    if date not in calendar.get("trading_days", []):
        raise ValueError(f"{dataset}: {date} is not a trading day of the dataset")
    session = (inst_doc.get("sessions") or {}).get(row["asset_class"])
    if not isinstance(session, dict):
        raise ValueError(f"{dataset}: no session block for asset class {row['asset_class']}")
    venue_names = list(row["venues"])
    by_name = {v["venue"]: v for v in venues_doc.get("venues", [])}
    missing = [v for v in venue_names if v not in by_name]
    if missing:
        raise ValueError(f"{dataset}: venues {missing} of {symbol} not in venues.json")

    if stage.exists():
        shutil.rmtree(stage)
    _write_json(
        stage / _INSTRUMENTS_REL,
        {"x-version": 1, "calendar": {"trading_days": [date]}, "instruments": [row]},
    )
    _write_json(stage / _VENUES_REL, {"x-version": 1, "venues": [by_name[v] for v in venue_names]})
    for key in _COPIED_KEYS:
        rel = base.reference[key]
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base.reference_path(key), dst)

    doc = json.loads(json.dumps(base.document))
    doc["instrument"] = symbol
    doc["venues"] = venue_names
    doc["session"] = {
        "trading_day": date,
        "timezone": session["timezone"],
        "open": session["open"],
        "close": session["close"],
    }
    doc["reference"] = dict(doc["reference"], instruments=_INSTRUMENTS_REL, venues=_VENUES_REL)
    cfg = MvpConfig.from_document(doc, where=f"mvp.json (real {symbol})", repo_root=stage)
    return cfg, int(row["instrument_id"])


def _make_factory(
    source: Path, instrument_id: int, limit: int | None
) -> Callable[[], Iterator[MarketEvent]]:
    reader = iter_iap1 if source.suffix == ".iap1" else iter_jsonl

    def gen() -> Iterator[MarketEvent]:
        n = 0
        for ev in reader(source):
            if ev.instrument_id != instrument_id:
                continue
            n += 1
            ev.event_id = n
            yield ev
            if limit is not None and n >= limit:
                return

    return gen


def _stream_identity(factory: Callable[[], Iterator[MarketEvent]], n: int) -> tuple[str, str]:
    """(sha256 of the IAP1 encoding, sha256 of the JSONL lines) of the
    stream, computed incrementally -- byte-identical to
    ``sha256_events_iap1`` / ``sha256_file(write_jsonl(...))``."""
    iap1 = hashlib.sha256()
    jsonl = hashlib.sha256()
    header = _HEADER.pack(IAP1_MAGIC, IAP1_VERSION, n)
    iap1.update(header)
    crc = zlib.crc32(header)
    seen = 0
    batch: list[MarketEvent] = []

    def flush() -> None:
        nonlocal crc
        if not batch:
            return
        # encode_iap1 frames a batch with its own header + trailer; the
        # records sit between them.
        rec = encode_iap1(batch)[_HEADER.size : -_TRAILER.size]
        iap1.update(rec)
        crc = zlib.crc32(rec, crc)
        batch.clear()

    for ev in factory():
        seen += 1
        jsonl.update(encode_jsonl_line(ev).encode("utf-8") + b"\n")
        batch.append(ev)
        if len(batch) >= 65536:
            flush()
    flush()
    if seen != n:
        raise RuntimeError(f"stream changed between passes ({n} != {seen} events)")
    iap1.update(_TRAILER.pack(crc & 0xFFFFFFFF, 0, n))
    return iap1.hexdigest(), jsonl.hexdigest()


def _prereg_block(path: Path) -> dict[str, Any]:
    doc = _load_json(path)
    try:
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = path.name
    opt_ins = sorted(k for k in doc if k not in ("x-version", "description", "methods"))
    return {
        "path": rel,
        "content_hash": content_hash(doc),
        "methods": doc.get("methods"),
        "applied": [],
        "not_applied": opt_ins,
        "note": "the MVP engine does not consume the pre-registration opt-ins yet; "
        "the config is pinned by hash, not applied",
    }


def run_symbol(
    dataset: str | Path,
    date: str,
    symbol: str,
    out_dir: str | Path,
    *,
    prereg: str | Path,
    mvp_config: str | Path | None = None,
    limit_events: int | None = None,
    allow_legacy: bool = False,
) -> RealRun:
    """Run the MVP loop on one symbol of a real dataset (module docstring)."""
    dataset = Path(dataset)
    out = Path(out_dir)
    if limit_events is not None and limit_events < 1:
        raise ValueError("--limit-events must be >= 1")
    ds_manifest, legacy = _read_dataset_manifest(dataset, allow_legacy)
    if date not in (ds_manifest.get("sessions") or {}):
        raise ValueError(f"{dataset}: no session {date} in {MANIFEST_NAME}")
    source, source_sha = _normalized_source(dataset, ds_manifest)
    base = MvpConfig.load(mvp_config or DEFAULT_CONFIG_PATH)
    cfg, iid = _symbol_config(base, dataset, date, symbol, out / "reference_root")

    factory = _make_factory(source, iid, limit_events)
    n = 0
    first = last = None
    for ev in factory():  # pass 1: count + bounds
        if first is None:
            first = ev
        last = ev
        n += 1
    if first is None or last is None:
        raise ValueError(f"{dataset}: no events for {symbol} (instrument_id {iid})")
    data_version, events_sha = _stream_identity(factory, n)  # pass 2
    feed = FeedResult(
        events=_StreamedEvents(factory, n, first, last),  # type: ignore[arg-type]
        instrument_id=iid,
        data_version=data_version,
        events_sha256=events_sha,
        iap1_sha256=data_version,
        n_events_generated=n,
        generator_stats={},
        qc_totals=dict(sorted((ds_manifest.get("normalized") or {}).get("qc_totals", {}).items())),
    )
    result = run_session(cfg, feed, out)  # pass 3
    rep = result.report
    counters = rep["counts"]["counters"]
    manifest = {
        "x-version": _MANIFEST_VERSION,
        "kind": "mvp-real",
        "dataset": {
            "name": dataset.name,
            "dataset_version": ds_manifest.get("dataset_version"),
            "manifest_x_version": ds_manifest.get("x-version"),
            "legacy": legacy,
            "config_version": config_version(dataset),
            "normalized_file": source.name,
            "normalized_sha256": source_sha,
        },
        "date": date,
        "symbol": symbol,
        "instrument_id": iid,
        "prereg": _prereg_block(Path(prereg)),
        "mvp": {
            "run_id": cfg.run_id,
            "session_id": cfg.session_id,
            "config_version": result.engine.config_version,
            "session": {
                "timezone": cfg.session.timezone,
                "open": cfg.session.open,
                "close": cfg.session.close,
            },
        },
        "limit_events": limit_events,
        "stream": {
            "n_events": n,
            "data_version": data_version,
            "events_sha256": events_sha,
            "first_ts": first.exchange_ts,
            "last_ts": last.exchange_ts,
        },
        "results": {
            "decisions": counters.get("decisions"),
            "parents": result.engine._next_parent_id - 1,
            "parents_finalised": counters.get("parents_finalised"),
            "children_submitted": counters.get("child_orders_submitted"),
            "children_generated": counters.get("child_orders_generated"),
            "fills": counters.get("fills"),
            "final_position": rep["pnl"].get("final_position"),
            "pnl_gross": rep["pnl"].get("gross"),
            "pnl_equity": rep["pnl"].get("equity"),
            "fees": rep["pnl"].get("fees"),
            "execution_cost": rep["pnl"].get("execution_cost"),
        },
        "trace_digest": result.trace_digest,
    }
    _write_json(out / RUN_MANIFEST, manifest)
    return RealRun(symbol=symbol, result=result, manifest=manifest)


def run_real(
    dataset: str | Path,
    date: str,
    symbols: list[str],
    out_dir: str | Path,
    **kwargs: Any,
) -> list[RealRun]:
    """:func:`run_symbol` for every symbol, into ``out_dir/<SYMBOL>/``."""
    return [run_symbol(dataset, date, s, Path(out_dir) / s, **kwargs) for s in symbols]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m iap.mvp.real",
        description="Run the MVP loop on an ingested real dataset (docs/MVP.md section 9).",
    )
    p.add_argument("--dataset", required=True, help="ingested dataset directory")
    p.add_argument("--date", required=True, help="session date YYYY-MM-DD")
    p.add_argument("--symbols", required=True, help="comma-separated symbols")
    p.add_argument(
        "--config", required=True, help="pre-registration config (configs/prereg/v2.json)"
    )
    p.add_argument("--mvp-config", default=None, help="base MVP config (configs/mvp/mvp.json)")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--limit-events", type=int, default=None, help="first N events per symbol")
    p.add_argument(
        "--allow-legacy-dataset",
        action="store_true",
        help="accept a pre-v1.14 dataset manifest (smoke runs only; recorded as legacy)",
    )
    a = p.parse_args(argv)
    try:
        runs = run_real(
            a.dataset,
            a.date,
            [s.strip() for s in a.symbols.split(",") if s.strip()],
            a.out,
            prereg=a.config,
            mvp_config=a.mvp_config,
            limit_events=a.limit_events,
            allow_legacy=a.allow_legacy_dataset,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for run in runs:
        print(
            json.dumps(
                {
                    "symbol": run.symbol,
                    **run.manifest["results"],
                    "n_events": run.manifest["stream"]["n_events"],
                    "trace_digest": run.manifest["trace_digest"],
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

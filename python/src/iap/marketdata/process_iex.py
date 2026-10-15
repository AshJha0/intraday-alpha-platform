"""Process-and-delete for one IEX HIST day (v1.17, A4): the overlap set.

The IEX overlap set pairs the nine Nasdaq 2025/26 sessions on disk
(2025-11-28, 2025-12-08..12, 2026-05-15, 2026-05-18, 2026-06-12) with the
same day's IEX book, so the two-venue work (plan items B7 / B8) runs on
real books of both venues.  An IEX day is two HIST captures of 4-16 GB
each: the **book feed** (DEEP+ ``DPLS``, displayed orders one by one, the
default; or DEEP, price levels) and **TOPS**, the best bid / offer that is
the book's oracle (``book_verification``, :mod:`iap.marketdata.iex`).  Only
one vendor file is ever on disk (the owner's rule), so the chain is::

    disk guard (the larger capture + dataset + partition temp + features + margin)
      -> download_tops   (tools/iex_download.py, only with --download --i-have-approval)
      -> tops_extract    (the universe's TOPS quotes -> <markers>/<date>.tops.json.gz)
      -> delete_tops     (the TOPS capture is gone before the book feed arrives)
      -> download_book   (the DEEP+ / DEEP capture, same approval)
      -> ingest          (iap.marketdata.ingest --format iex-deep-plus --tops <extract>,
                          --partition-instruments N, slim; a divergent book refuses
                          the ingest unless --allow-book-divergence)
      -> delete_raw -> features -> keep (KEEP.json) -> delete (normalized + the
         book capture + the extract) -> _DONE marker

Every step has a marker beside the dataset directory and is skipped when
present (a crashed day resumes); ``process_report.json`` records the sizes,
sha256 of both captures, timings, the oracle counts and the disk peak.  The
manifest is written by ``tools/iex_hist.py index`` from the HIST index and
carries the owner's approval sentence; nothing here contacts the network
(``iap.marketdata`` import policy): the download is a subprocess.

    python -m iap.marketdata process-iex-day --manifest research/v117/iex_overlap_manifest.json \\
        --date 2025-11-28 --vendor-dir data/vendor --out-root data/real/iex \\
        --symbols AAPL,MSFT,QQQ [--feed deep-plus|deep] [--download --i-have-approval] \\
        [--partition-instruments 10] [--engine rust] [--workers 1] [--min-free-gb 30] \\
        [--peak-gb N] [--allow-book-divergence] [--keep-gz] [--dry-run]
    python -m iap.marketdata process-iex-day ... --render-cmd data/real/run_iex_20251128.cmd \\
        [--jobs-registry data/real/jobs.json]

Runbook: docs/runbooks/RUNBOOK_process_and_delete.md (IEX section).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from iap.marketdata.feederrors import BookDivergenceError, IngestError
from iap.marketdata.iex import TOPS_EXTRACT_SUFFIX, TopsQuotes
from iap.marketdata.itch50 import session_midnight_ns
from iap.marketdata.process_delete import (
    GB,
    KEEP_NAME,
    _add_job,
    _sha256,
    disk_guard,
    estimate_peak_gb,
    keep_manifest,
    verify_keep,
)
from iap.marketdata.r7plan import _check_url, verify_file

__all__ = [
    "BOOK_FEEDS",
    "MANIFEST_SCHEMA",
    "load_iex_manifest",
    "process_iex_day",
    "render_iex_jobs_entry",
    "render_iex_wrapper",
]

MANIFEST_SCHEMA = "iap.iex_hist.manifest/v1"
#: ``--feed`` -> (HIST feed name in the manifest, ingest format).
BOOK_FEEDS = {"deep-plus": ("DEEP+", "iex-deep-plus"), "deep": ("DEEP", "iex-deep")}
TOPS_FEED = "TOPS"
_REPO = Path(__file__).resolve().parents[4]


def load_iex_manifest(path: str | Path) -> dict:
    """Read and validate an IEX HIST manifest (``tools/iex_hist.py``; fail closed).

    ``days``: ``[{"date", "feeds": {"TOPS": {file, url, bytes}, "DEEP+": ..., "DEEP": ...}}]``.
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"{path}: schema is {doc.get('schema')!r}, not {MANIFEST_SCHEMA}")
    days = doc.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError(f"{path}: manifest has no 'days'")
    seen: set[str] = set()
    for d in days:
        iso = _dt.date.fromisoformat(d["date"]).isoformat()
        if iso in seen:
            raise ValueError(f"duplicate date {iso}")
        seen.add(iso)
        feeds = d.get("feeds")
        if not isinstance(feeds, dict):
            raise ValueError(f"{iso}: 'feeds' must be a dict (an empty one for a missing day)")
        for feed, e in feeds.items():
            name = e.get("file")
            if not name or "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError(f"{iso} {feed}: 'file' must be a bare file name")
            if not isinstance(e.get("bytes"), int) or e["bytes"] <= 0:
                raise ValueError(f"{iso} {feed}: 'bytes' must be a positive integer")
            if e.get("url"):
                _check_url(e["url"])
    doc["days"] = sorted(days, key=lambda d: d["date"])
    return doc


def _marker(ds: Path, step: str) -> Path:
    d = ds.parent / f"{ds.name}.markers"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"_DONE_{step}"


def _unlink(p: Path) -> int:
    n = p.stat().st_size if p.is_file() else 0
    if p.is_file():
        p.unlink()
    return n


def process_iex_day(
    manifest_path: Path,
    date: str,
    vendor_dir: Path,
    out_root: Path,
    symbols: list[str],
    *,
    feed: str = "deep-plus",
    configs_dir: Path | None = None,
    download: bool = False,
    approval: bool = False,
    partition_instruments: int | None = 10,
    engine: str = "rust",
    workers: int = 1,
    keep_gz: bool = False,
    min_free_gb: float = 30.0,
    peak_gb: float | None = None,
    allow_book_divergence: bool = False,
    round_lots: dict[str, int] | None = None,
    slim: bool = True,
    dry_run: bool = False,
    log: Callable[[str], None] | None = None,
    python: str = sys.executable,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Run the chain for one IEX HIST day (module docs); returns the report.

    ``run`` / ``python`` are the subprocess seam (tests inject a fake)."""
    say = log or (lambda line: print(line, flush=True))
    if feed not in BOOK_FEEDS:
        raise IngestError(f"--feed must be one of {sorted(BOOK_FEEDS)}")
    book_feed, fmt = BOOK_FEEDS[feed]
    doc = load_iex_manifest(manifest_path)
    day = next((d for d in doc["days"] if d["date"] == date), None)
    if day is None:
        raise IngestError(f"{date} is not in {manifest_path}")
    if not symbols:
        raise IngestError("the universe (--symbols / --symbols-file) is empty")
    missing = [f for f in (TOPS_FEED, book_feed) if f not in day["feeds"]]
    if missing:
        raise IngestError(f"{date}: IEX HIST lists no {missing} file for this day")
    tops_entry = day["feeds"][TOPS_FEED]
    book_entry = day["feeds"][book_feed]
    configs_dir = Path(configs_dir) if configs_dir is not None else _REPO / "configs"
    vendor_dir = Path(vendor_dir)
    tops_file = vendor_dir / tops_entry["file"]
    book_file = vendor_dir / book_entry["file"]
    ds = Path(out_root) / f"iex_{date.replace('-', '')}"
    extract = _marker(ds, "").parent / f"{date.replace('-', '')}{TOPS_EXTRACT_SUFFIX}"
    report: dict = {
        "date": date,
        "venue": "IEXG",
        "format": fmt,
        "book_feed": book_feed,
        "dataset_dir": str(ds),
        "tops_file": str(tops_file),
        "book_file": str(book_file),
        "tops_bytes": int(tops_entry["bytes"]),
        "book_bytes": int(book_entry["bytes"]),
        "n_symbols": len(symbols),
        "symbols": list(symbols),
        "approval": doc.get("approval"),
        "round_lots": dict(round_lots or {}),
        "round_lots_source": "nasdaq_dataset" if round_lots else None,
        "steps": [],
        "dry_run": dry_run,
    }
    t0 = time.perf_counter()
    peak_seen = {"free_min_gb": None}

    def free_gb() -> float:
        return shutil.disk_usage(Path(out_root)).free / GB

    def step(name: str, fn: Callable[[], dict | None]) -> None:
        if _marker(ds, name).is_file():
            report["steps"].append({"step": name, "status": "skipped (marker)"})
            say(f"[{date}] {name}: done earlier, skipped")
            return
        say(f"[{date}] {name} ...")
        clock = time.perf_counter()
        detail = fn() if not dry_run else {"dry_run": True}
        f = free_gb()
        peak_seen["free_min_gb"] = (
            f if peak_seen["free_min_gb"] is None else min(peak_seen["free_min_gb"], f)
        )
        report["steps"].append(
            {
                "step": name,
                "status": "ok",
                "seconds": round(time.perf_counter() - clock, 1),
                "free_gb_after": round(f, 2),
                **(detail or {}),
            }
        )
        if not dry_run:
            _marker(ds, name).write_text(_dt.datetime.now(_dt.UTC).isoformat(), encoding="ascii")

    # -- disk guard: the larger capture is the vendor term (never both) ---
    largest = max(int(tops_entry["bytes"]), int(book_entry["bytes"]))
    present = tops_file.is_file() or book_file.is_file()
    est = estimate_peak_gb(largest, len(symbols), vendor_present=present)
    if peak_gb is not None:
        est = {
            **est,
            "peak_gb_estimated": est["peak_gb"],
            "peak_gb": float(peak_gb),
            "override": True,
        }
    guard = disk_guard(Path(out_root), est["peak_gb"], min_free_gb)
    report["estimate"] = est
    report["disk"] = guard
    if not guard["ok"]:
        raise IngestError(
            f"{date}: disk guard refused: {guard['free_gb']} GB free, "
            f"{guard['required_gb']} GB required (peak {est['peak_gb']} + margin {min_free_gb}); "
            "nothing was deleted to make room"
        )
    say(f"[{date}] disk: {guard['free_gb']} GB free, peak estimate {est['peak_gb']} GB: ok")

    # -- downloads --------------------------------------------------------
    def fetch(feed_name: str, entry: dict, dest: Path) -> dict:
        if dest.is_file():
            res = verify_file(dest, int(entry["bytes"]), None, hash_file=False)
            if res["ok"]:
                return {"download": "present", "bytes": res["bytes"]}
            raise IngestError(f"{dest}: {res['reason']}")
        other = book_file if dest == tops_file else tops_file
        if other.is_file() and not keep_gz:
            raise IngestError(
                f"{other.name} is still on disk: one vendor file at a time (delete it first)"
            )
        if not download:
            raise IngestError(f"{dest} is missing and --download was not given")
        if not approval:
            raise IngestError("--download needs --i-have-approval (the owner approved this file)")
        cmd = [
            python,
            str(_REPO / "tools" / "iex_download.py"),
            "--manifest",
            str(manifest_path),
            "--vendor-dir",
            str(vendor_dir),
            "--date",
            date,
            "--feed",
            feed_name,
            "--i-have-approval",
        ]
        clock = time.perf_counter()
        proc = run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise IngestError(
                f"download of {entry['file']} failed ({proc.returncode}): "
                f"{(proc.stdout or '')[-2000:]}{(proc.stderr or '')[-2000:]}"
            )
        secs = round(time.perf_counter() - clock, 1)
        res = verify_file(dest, int(entry["bytes"]), None, hash_file=True)
        if not res["ok"]:
            raise IngestError(f"{dest}: {res['reason']} after download")
        return {
            "download": "downloaded",
            "bytes": res["bytes"],
            "sha256": res["sha256"],
            "seconds_download": secs,
            "mb_per_s": round(res["bytes"] / 1e6 / max(secs, 0.1), 1),
        }

    step("download_tops", lambda: fetch(TOPS_FEED, tops_entry, tops_file))

    def do_extract() -> dict:
        quotes = TopsQuotes.load(tops_file, symbols, session_midnight_ns(date))
        empty = [s for s in symbols if s not in quotes.by_symbol]
        if empty:
            raise IngestError(f"{tops_file.name}: no TOPS quote on {date} for {empty}")
        sha = _sha256(tops_file)
        quotes.save(
            extract,
            source={"file": tops_file.name, "bytes": tops_file.stat().st_size, "sha256": sha},
        )
        return {
            "extract": str(extract),
            "tops_quotes": quotes.quotes,
            "per_symbol": {s: len(quotes.by_symbol[s]) for s in symbols},
            "tops_sha256": sha,
        }

    step("tops_extract", do_extract)

    def do_delete_tops() -> dict:
        if keep_gz:
            return {"kept_gz": True}
        return {"freed_gb": round(_unlink(tops_file) / GB, 2)}

    step("delete_tops", do_delete_tops)
    step("download_book", lambda: fetch(book_feed, book_entry, book_file))

    # -- ingest -----------------------------------------------------------
    def do_ingest() -> dict:
        from iap.marketdata.ingest import ingest

        clock = time.perf_counter()
        lots = round_lots
        lots_source = "nasdaq_dataset"
        if lots is None:
            # no paired Nasdaq day given: the smallest quoted TOPS size per
            # symbol is the round lot (TOPS quotes round lots only; measured
            # 2025-11-28: AAPL 100, MSFT / QQQ 40); recorded as inferred
            lots = infer_round_lots(extract, symbols)
            lots_source = "tops_min_quoted_size"
        report["round_lots"] = dict(lots)
        report["round_lots_source"] = lots_source
        try:
            manifest = ingest(
                fmt,
                [book_file],
                date,
                symbols,
                ds,
                configs_dir=configs_dir,
                partition_instruments=partition_instruments,
                tops=extract,
                allow_book_divergence=allow_book_divergence,
                progress=say,
                slim=slim,
                round_lots=lots,
            )
        except BookDivergenceError as exc:
            (ds.parent / f"{ds.name}.markers" / "book_divergence.json").write_text(
                json.dumps(exc.report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            raise
        session = manifest["sessions"][date]
        ver = session.get("book_verification") or {}
        msgs = session.get("messages") or {}
        return {
            "dataset_version": manifest.get("dataset_version"),
            "events": session["raw"].get("events"),
            "messages": msgs.get("total"),
            "iex": msgs.get("iex"),
            "book_verification": {
                s: {k: r.get(k) for k in ("status", "compared", "mismatched")}
                for s, r in ver.items()
            },
            "book_sha256": _sha256(book_file),
            "seconds_ingest": round(time.perf_counter() - clock, 1),
        }

    step("ingest", do_ingest)

    def do_delete_raw() -> dict:
        freed = 0
        base = ds / "raw"
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    freed += _unlink(p)
            shutil.rmtree(base, ignore_errors=True)
        return {"freed_gb": round(freed / GB, 2)}

    step("delete_raw", do_delete_raw)

    def do_features() -> dict:
        cmd = [
            python,
            "-m",
            "iap.features",
            "--data-dir",
            str(ds / "normalized"),
            "--out-dir",
            str(ds / "features"),
            "--configs",
            str(ds / "configs"),
            "--registry-out",
            str(ds / "features" / "feature_registry.json"),
            "--workers",
            str(workers),
            "--engine",
            engine,
        ]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(_REPO / "python" / "src"))
        proc = run(
            cmd, capture_output=True, text=True, check=False, env=env, cwd=str(_REPO / "python")
        )
        (ds / "features.log").write_text(
            (proc.stdout or "") + (proc.stderr or ""), encoding="utf-8"
        )
        if proc.returncode != 0:
            raise IngestError(f"features failed ({proc.returncode}); see {ds / 'features.log'}")
        n = len(list((ds / "features").glob("features_*.parquet")))
        if n == 0:
            raise IngestError(f"features wrote no features_*.parquet under {ds / 'features'}")
        return {"feature_files": n, "engine": engine}

    step("features", do_features)

    def do_keep() -> dict:
        doc = keep_manifest(ds)
        (ds / KEEP_NAME).write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        bad = verify_keep(ds)
        if bad:
            raise IngestError(f"{KEEP_NAME} verification failed for {bad}")
        return {"kept_files": len(doc["files"])}

    step("keep", do_keep)

    def do_delete() -> dict:
        bad = verify_keep(ds)
        if bad:
            raise IngestError(f"refusing to delete: {KEEP_NAME} no longer matches {bad}")
        freed = 0
        deleted: list[str] = []
        for d in ("raw", "normalized"):
            base = ds / d
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.is_file() and p.name != "qc_report.json":
                    freed += _unlink(p)
                    deleted.append(p.relative_to(ds).as_posix())
            if d == "raw":
                shutil.rmtree(base, ignore_errors=True)
        if not keep_gz:
            for p in (book_file, tops_file):
                if p.is_file():
                    freed += _unlink(p)
                    deleted.append(p.name)
        if extract.is_file():
            freed += _unlink(extract)
            deleted.append(extract.name)
        return {
            "deleted": deleted if len(deleted) <= 50 else deleted[:50] + ["..."],
            "freed_gb": round(freed / GB, 2),
            "kept_gz": keep_gz,
        }

    step("delete", do_delete)

    report["seconds"] = round(time.perf_counter() - t0, 1)
    report["free_gb_min_seen"] = (
        None if peak_seen["free_min_gb"] is None else round(peak_seen["free_min_gb"], 2)
    )
    report["free_gb_end"] = round(free_gb(), 2)
    if not dry_run:
        (ds / "process_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _marker(ds, "ALL").write_text("ok", encoding="ascii")
    say(f"[{date}] done in {report['seconds']} s")
    return report


# ------------------------------------------------------- wrapper / jobs


def render_iex_wrapper(
    *,
    python_dir: str,
    manifest: str,
    dates: list[str],
    vendor_dir: str,
    out_root: str,
    symbols: str,
    log: str,
    extra_args: str = "",
) -> str:
    """A detached-run ``.cmd`` (RUNBOOK_job_watchdog.md): one ``process-iex-day``
    call per date IN ORDER; a failed day is logged (``EXIT_<date> n``) and the
    chain goes on to the next one, so one missing HIST day does not stop the
    set.  The last line is ``EXIT <number of failed days>``."""
    L = [
        "@echo off",
        "REM generated by: python -m iap.marketdata process-iex-day --render-cmd (v1.17, A4).",
        "REM One IEX HIST day at a time, in order; each day: TOPS -> extract -> delete,",
        "REM book feed -> ingest -> features -> KEEP.json -> delete. Resumable (markers).",
        f'cd /d "{python_dir}"',
        "set PYTHONUTF8=1",
        "set PYTHONPATH=src",
        f"set L={log}",
        "set FAILED=0",
        "echo START %DATE% %TIME% >> %L%",
    ]
    for date in dates:
        L += [
            f"echo DAY {date} %DATE% %TIME% >> %L%",
            f'python -m iap.marketdata process-iex-day --manifest "{manifest}" --date {date} '
            f'--vendor-dir "{vendor_dir}" --out-root "{out_root}" --symbols {symbols} '
            f"{extra_args} >> %L% 2>&1",
            f"if errorlevel 1 (set /a FAILED+=1 & echo EXIT_{date} FAILED >> %L%) "
            f"else (echo EXIT_{date} 0 >> %L%)",
        ]
    L += ["echo EXIT %FAILED% >> %L%", "exit /b %FAILED%"]
    return "\r\n".join(L) + "\r\n"


def render_iex_jobs_entry(name: str, wrapper: str, log: str) -> dict:
    """The ``tools/jobs/watchdog.py`` registry entry for the chain."""
    return {
        "name": name,
        "wrapper": wrapper,
        "log": log,
        "process_pattern": "process-iex-da[y] ",
        "done_regex": "^EXIT \\d+\\s*$",
        "restart_safe": True,
        "max_restarts": 2,
        "stall_minutes": 180,
    }


# ------------------------------------------------------------------- CLI


def infer_round_lots(extract: Path, symbols: list[str]) -> dict[str, int]:
    """symbol -> smallest non-zero quoted size in the day's TOPS extract (100
    when a symbol has no quote): TOPS quotes the best level holding at least
    a round lot, so that minimum is the round lot in practice."""
    quotes = TopsQuotes.load_extract(extract, [])
    out = {}
    for s in symbols:
        sizes = [q[i] for q in quotes.by_symbol.get(s, {}).values() for i in (1, 3) if q[i] > 0]
        out[s] = min(sizes) if sizes else 100
    return out


def round_lots_from_dataset(path: str | Path) -> dict[str, int]:
    """``symbol -> lot_size`` of a dataset.json's universe (the paired Nasdaq
    day: its ITCH directory carries the round lot IEX's directory lacks for
    non-IEX-listed names)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return {e["symbol"]: int(e["lot_size"]) for e in doc["universe"]}


def _symbols(a) -> list[str]:
    if a.symbols_file:
        doc = json.loads(Path(a.symbols_file).read_text(encoding="utf-8"))
        syms = doc["symbols"] if isinstance(doc, dict) else doc
        return [str(s) for s in syms]
    return [s.strip() for s in (a.symbols or "").split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m iap.marketdata process-iex-day", description=__doc__.splitlines()[0]
    )
    p.add_argument("--manifest", required=True, help="tools/iex_hist.py index output")
    p.add_argument("--date", action="append", required=True, help="repeatable (--render-cmd)")
    p.add_argument("--vendor-dir", required=True)
    p.add_argument("--out-root", required=True)
    p.add_argument("--symbols", default=None, help="comma-separated universe")
    p.add_argument("--symbols-file", default=None, help='JSON: ["AAPL", ...] or {"symbols": [...]}')
    p.add_argument("--feed", choices=sorted(BOOK_FEEDS), default="deep-plus")
    p.add_argument("--configs-dir", default=None)
    p.add_argument("--download", action="store_true", help="fetch the captures when absent")
    p.add_argument(
        "--i-have-approval", action="store_true", help="the owner approved these downloads"
    )
    p.add_argument("--partition-instruments", type=int, default=10)
    p.add_argument("--engine", choices=("python", "rust"), default="rust")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--keep-gz", action="store_true")
    p.add_argument("--min-free-gb", type=float, default=30.0)
    p.add_argument("--peak-gb", type=float, default=None, help="override the peak estimate (GB)")
    p.add_argument("--allow-book-divergence", action="store_true")
    p.add_argument(
        "--round-lots-from",
        default=None,
        help="dataset.json of the paired Nasdaq day: its universe lot_size per symbol",
    )
    p.add_argument("--no-slim", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="disk guard and the plan only")
    p.add_argument("--render-cmd", default=None, help="write the detached .cmd chain and exit")
    p.add_argument("--jobs-registry", default=None, help="with --render-cmd: watchdog entry")
    p.add_argument("--job-name", default="iex_overlap")
    p.add_argument("--python-dir", default=str(_REPO / "python"))
    a = p.parse_args(argv)
    try:
        if a.render_cmd:
            if not a.symbols:
                p.error("--render-cmd needs --symbols")
            cmd_path = Path(a.render_cmd)
            log = str(cmd_path.with_suffix(".log"))
            extra = [f"--feed {a.feed}"]
            if a.download:
                extra.append("--download")
            if a.i_have_approval:
                extra.append("--i-have-approval")
            if a.keep_gz:
                extra.append("--keep-gz")
            if a.allow_book_divergence:
                extra.append("--allow-book-divergence")
            if a.round_lots_from:
                extra.append(f'--round-lots-from "{Path(a.round_lots_from).resolve()}"')
            extra.append(
                f"--partition-instruments {a.partition_instruments} --engine {a.engine} "
                f"--workers {a.workers} --min-free-gb {a.min_free_gb:g}"
            )
            if a.peak_gb is not None:
                extra.append(f"--peak-gb {a.peak_gb:g}")
            if a.no_slim:
                extra.append("--no-slim")
            text = render_iex_wrapper(
                python_dir=a.python_dir,
                manifest=str(Path(a.manifest).resolve()),
                dates=a.date,
                vendor_dir=str(Path(a.vendor_dir).resolve()),
                out_root=str(Path(a.out_root).resolve()),
                symbols=a.symbols,
                log=log,
                extra_args=" ".join(extra),
            )
            cmd_path.write_bytes(text.encode("ascii"))
            print(f"wrote {cmd_path}")
            if a.jobs_registry:
                _add_job(
                    Path(a.jobs_registry), render_iex_jobs_entry(a.job_name, str(cmd_path), log)
                )
                print(f"registered {a.job_name} in {a.jobs_registry}")
            return 0
        if len(a.date) != 1:
            p.error("one --date per run (several only with --render-cmd)")
        report = process_iex_day(
            Path(a.manifest),
            a.date[0],
            Path(a.vendor_dir),
            Path(a.out_root),
            _symbols(a),
            feed=a.feed,
            configs_dir=Path(a.configs_dir) if a.configs_dir else None,
            download=a.download,
            approval=a.i_have_approval,
            partition_instruments=a.partition_instruments,
            engine=a.engine,
            workers=a.workers,
            keep_gz=a.keep_gz,
            min_free_gb=a.min_free_gb,
            peak_gb=a.peak_gb,
            allow_book_divergence=a.allow_book_divergence,
            round_lots=round_lots_from_dataset(a.round_lots_from) if a.round_lots_from else None,
            slim=not a.no_slim,
            dry_run=a.dry_run,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (IngestError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

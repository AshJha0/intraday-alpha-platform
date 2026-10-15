"""Process-and-delete: one free full-market day at a time (v1.17, A2).

A full-market day (every Nasdaq- or IEX-listed symbol) is 4-18 GB of gzip,
its raw + normalized files are 1-2x that again, and this machine has 16 GB
of memory and little free disk.  The breadth universe (50-100 names, plan
A3) therefore cannot be ingested the R7 way (every day's raw and normalized
files kept).  This module runs the chain for ONE day and keeps only what
research reads::

    download (only with --download --i-have-approval; size / sha256 checked)
      -> ingest (iap.marketdata.ingest, --partition-instruments N: memory-bounded)
      -> features (python -m iap.features, --engine rust by default)
      -> KEEP.json: sha256 of every kept file (features, dataset.json,
         configs, reference, qc_report.json)
      -> delete raw/, normalized/ and the vendor file (after KEEP.json is
         written and verified; --keep-raw / --keep-gz keep them)
      -> _DONE marker

Every step has a marker in the day's dataset directory and is skipped when
present, so a crashed day resumes (``restart_safe`` in the watchdog
registry).  A **disk guard** refuses to start a day whose estimated peak
footprint (vendor file + dataset + partition temp + features + margin)
exceeds the free space (:func:`disk_guard`); nothing is ever deleted to
make room.  The wrapper for a detached run and the ``jobs.json`` entry for
``tools/jobs/watchdog.py`` are rendered by :func:`render_wrapper`.

    python -m iap.marketdata process-day --manifest M --date D --vendor-dir V \\
        --out-root R --symbols-file research/breadth/universe.json \\
        [--download --i-have-approval] [--partition-instruments 10] \\
        [--engine rust] [--workers 1] [--keep-raw] [--keep-gz] [--min-free-gb 10] [--dry-run]
    python -m iap.marketdata process-day ... --render-cmd R\\run_process_20190130.cmd \\
        [--jobs-registry data/real/jobs.json]

Nothing here contacts the network (``iap.marketdata`` import policy): the
download is a subprocess call to ``tools/r7_download.py`` with the owner's
approval flag passed through, and it refuses without it.  Runbook:
docs/runbooks/RUNBOOK_process_and_delete.md.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from iap.marketdata.feederrors import IngestError
from iap.marketdata.r7plan import (
    DATASET_GB_PER_GZ_GB,
    FEATURES_GB_PER_DAY,
    load_candidates,
    verify_file,
)

__all__ = [
    "KEEP_NAME",
    "disk_guard",
    "estimate_peak_gb",
    "keep_manifest",
    "process_day",
    "render_jobs_entry",
    "render_wrapper",
    "verify_keep",
]

KEEP_NAME = "KEEP.json"
_REPO = Path(__file__).resolve().parents[4]
#: Kept after the day is done (relative to the dataset directory).
KEEP_DIRS = ("features", "configs", "reference")
KEEP_FILES = ("dataset.json", "normalized/qc_report.json")
#: Deleted after KEEP.json is verified (relative to the dataset directory).
DELETE_DIRS = ("raw", "normalized")
#: The partitioned normaliser's on-disk partitions plus sorted runs, as a
#: multiple of the dataset's normalized size (two copies at the peak).
PARTITION_TEMP_FACTOR = 2.0
GB = 1e9


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ disk guard


def estimate_peak_gb(gz_bytes: int, n_symbols: int, *, vendor_present: bool) -> dict[str, float]:
    """Peak disk the day needs, by component (the measured R7 rates of
    :mod:`iap.marketdata.r7plan`, upper ends, scaled by the universe)."""
    gz = gz_bytes / GB
    scale = max(1.0, n_symbols / 3)
    dataset = gz * DATASET_GB_PER_GZ_GB * scale
    temp = dataset * PARTITION_TEMP_FACTOR
    features = FEATURES_GB_PER_DAY * scale
    vendor = 0.0 if vendor_present else gz
    return {
        "vendor_gb": round(vendor, 2),
        "dataset_gb": round(dataset, 2),
        "partition_temp_gb": round(temp, 2),
        "features_gb": round(features, 2),
        "peak_gb": round(vendor + dataset + temp + features, 2),
    }


def disk_guard(path: Path, needed_gb: float, min_free_gb: float) -> dict[str, float | bool]:
    """Refuse (``ok`` False) unless the volume holding ``path`` has
    ``needed_gb + min_free_gb`` free.  Never frees anything."""
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free / GB
    required = needed_gb + min_free_gb
    return {
        "free_gb": round(free, 2),
        "needed_gb": round(needed_gb, 2),
        "margin_gb": float(min_free_gb),
        "required_gb": round(required, 2),
        "ok": free >= required,
    }


# ------------------------------------------------------------- KEEP.json


def keep_manifest(ds: Path) -> dict:
    """sha256 of every kept file of a finished dataset directory."""
    files: dict[str, str] = {}
    for rel in KEEP_FILES:
        p = ds / rel
        if p.is_file():
            files[rel.replace("\\", "/")] = _sha256(p)
    for d in KEEP_DIRS:
        base = ds / d
        if base.is_dir():
            for p in sorted(x for x in base.rglob("*") if x.is_file()):
                files[p.relative_to(ds).as_posix()] = _sha256(p)
    if not files:
        raise IngestError(f"{ds}: nothing to keep (no features / dataset.json)")
    return {
        "schema": "iap.process_delete.keep/v1",
        "written_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "files": files,
        "deleted": list(DELETE_DIRS),
    }


def verify_keep(ds: Path) -> list[str]:
    """Paths of ``KEEP.json`` whose bytes no longer match (empty = intact)."""
    doc = json.loads((ds / KEEP_NAME).read_text(encoding="utf-8"))
    bad = []
    for rel, digest in doc["files"].items():
        p = ds / rel
        if not p.is_file() or _sha256(p) != digest:
            bad.append(rel)
    return bad


# ------------------------------------------------------------- the chain


def _marker(ds: Path, step: str) -> Path:
    """Markers live BESIDE the dataset directory (``<ds>.markers/``): the
    ingest refuses a non-empty, non-dataset directory, and the download
    step runs before it."""
    d = ds.parent / f"{ds.name}.markers"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"_DONE_{step}"


def process_day(
    manifest_path: Path,
    date: str,
    vendor_dir: Path,
    out_root: Path,
    symbols: list[str],
    *,
    configs_dir: Path | None = None,
    download: bool = False,
    approval: bool = False,
    partition_instruments: int | None = 10,
    engine: str = "rust",
    workers: int = 1,
    keep_raw: bool = False,
    keep_gz: bool = False,
    min_free_gb: float = 10.0,
    dry_run: bool = False,
    tops: Path | None = None,
    log: Callable[[str], None] | None = None,
    python: str = sys.executable,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    """Run the chain for one manifest day (module docs); returns a report.

    ``run`` / ``python`` are the subprocess seam (tests inject a fake)."""
    say = log or (lambda line: print(line, flush=True))
    doc = load_candidates(manifest_path)
    day = next((d for d in doc["days"] if d["date"] == date), None)
    if day is None:
        raise IngestError(f"{date} is not in {manifest_path}")
    if not symbols:
        raise IngestError("the universe (--symbols / --symbols-file) is empty")
    configs_dir = Path(configs_dir) if configs_dir is not None else _REPO / "configs"
    vendor_file = Path(vendor_dir) / day["file"]
    ds = Path(out_root) / f"ds_{date.replace('-', '')}"
    fmt = day.get("format", "itch50")
    report: dict = {
        "date": date,
        "format": fmt,
        "dataset_dir": str(ds),
        "vendor_file": str(vendor_file),
        "n_symbols": len(symbols),
        "steps": [],
        "dry_run": dry_run,
    }
    t0 = time.perf_counter()

    def step(name: str, fn: Callable[[], dict | None]) -> None:
        if _marker(ds, name).is_file():
            report["steps"].append({"step": name, "status": "skipped (marker)"})
            say(f"[{date}] {name}: done earlier, skipped")
            return
        say(f"[{date}] {name} ...")
        clock = time.perf_counter()
        detail = fn() if not dry_run else {"dry_run": True}
        report["steps"].append(
            {
                "step": name,
                "status": "ok",
                "seconds": round(time.perf_counter() - clock, 1),
                **(detail or {}),
            }
        )
        if not dry_run:
            _marker(ds, name).write_text(_dt.datetime.now(_dt.UTC).isoformat(), encoding="ascii")

    # -- disk guard (before anything is written) ----------------------
    est = estimate_peak_gb(int(day["bytes"]), len(symbols), vendor_present=vendor_file.is_file())
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

    # -- download -----------------------------------------------------
    def do_download() -> dict:
        if vendor_file.is_file():
            res = verify_file(vendor_file, int(day["bytes"]), day.get("sha256"), hash_file=False)
            if res["ok"]:
                return {"download": "present"}
            raise IngestError(f"{vendor_file}: {res['reason']}")
        if not download:
            raise IngestError(f"{vendor_file} is missing and --download was not given")
        if not approval:
            raise IngestError("--download needs --i-have-approval (the owner approved this file)")
        cmd = [
            python,
            str(_REPO / "tools" / "r7_download.py"),
            "--manifest",
            str(manifest_path),
            "--vendor-dir",
            str(vendor_dir),
            "--date",
            date,
            "--i-have-approval",
        ]
        proc = run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise IngestError(
                f"download failed ({proc.returncode}): {proc.stdout[-2000:]}{proc.stderr[-2000:]}"
            )
        res = verify_file(vendor_file, int(day["bytes"]), day.get("sha256"), hash_file=True)
        if not res["ok"]:
            raise IngestError(f"{vendor_file}: {res['reason']} after download")
        return {"download": "downloaded", "sha256": res.get("sha256")}

    step("download", do_download)

    # -- ingest ---------------------------------------------------------
    def do_ingest() -> dict:
        from iap.marketdata.ingest import ingest

        clock = time.perf_counter()
        manifest = ingest(
            fmt,
            [vendor_file],
            date,
            symbols,
            ds,
            configs_dir=configs_dir,
            partition_instruments=partition_instruments,
            tops=tops,
            progress=say,
        )
        return {
            "dataset_version": manifest.get("dataset_version"),
            "events": manifest["sessions"][date]["raw"].get("events"),
            "seconds_ingest": round(time.perf_counter() - clock, 1),
        }

    step("ingest", do_ingest)

    # -- features -------------------------------------------------------
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

    # -- keep + delete --------------------------------------------------
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
        for d in DELETE_DIRS:
            base = ds / d
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.is_file() and p.name != "qc_report.json":
                    freed += p.stat().st_size
                    if not keep_raw:
                        p.unlink()
                        deleted.append(p.relative_to(ds).as_posix())
            if not keep_raw and d == "raw":
                shutil.rmtree(base, ignore_errors=True)
        if not keep_gz and vendor_file.is_file():
            freed += vendor_file.stat().st_size
            vendor_file.unlink()
            deleted.append(vendor_file.name)
        return {
            "deleted": deleted if len(deleted) <= 50 else deleted[:50] + ["..."],
            "freed_gb": round(freed / GB, 2),
            "kept_raw": keep_raw,
            "kept_gz": keep_gz,
        }

    step("delete", do_delete)

    report["seconds"] = round(time.perf_counter() - t0, 1)
    if not dry_run:
        (ds / "process_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _marker(ds, "ALL").write_text("ok", encoding="ascii")
    say(f"[{date}] done in {report['seconds']} s")
    return report


# ------------------------------------------------------- wrapper / jobs


def render_wrapper(
    *,
    python_dir: str,
    manifest: str,
    date: str,
    vendor_dir: str,
    out_root: str,
    symbols_file: str,
    log: str,
    extra_args: str = "",
) -> str:
    """A detached-run ``.cmd`` (WMI launch, docs/runbooks/RUNBOOK_job_watchdog.md):
    ``START``, the process-day call, ``EXIT n``."""
    L = [
        "@echo off",
        "REM generated by: python -m iap.marketdata process-day --render-cmd (v1.17, A2).",
        "REM Resumable: steps with a _DONE marker are skipped. Deletes raw/normalized/gz of",
        "REM THIS day only, after KEEP.json is verified.",
        f'cd /d "{python_dir}"',
        "set PYTHONUTF8=1",
        "set PYTHONPATH=src",
        f"set L={log}",
        "echo START %DATE% %TIME% >> %L%",
        f'python -m iap.marketdata process-day --manifest "{manifest}" --date {date} '
        f'--vendor-dir "{vendor_dir}" --out-root "{out_root}" --symbols-file "{symbols_file}" '
        f"{extra_args} >> %L% 2>&1",
        "set RC=%ERRORLEVEL%",
        "echo EXIT %RC% >> %L%",
        "exit /b %RC%",
    ]
    return "\r\n".join(L) + "\r\n"


def render_jobs_entry(date: str, wrapper: str, log: str) -> dict:
    """The ``tools/jobs/watchdog.py`` registry entry for one day's run."""
    tag = date.replace("-", "")
    return {
        "name": f"process_{tag}",
        "wrapper": wrapper,
        "log": log,
        # the bracket trick: matches the python command line, never a grep of it
        "process_pattern": f"process-da[y] .*--date {date}",
        "done_regex": "^EXIT 0\\s*$",
        "restart_safe": True,
        "max_restarts": 2,
        "stall_minutes": 120,
    }


def _add_job(registry: Path, entry: dict) -> None:
    doc = json.loads(registry.read_text(encoding="utf-8")) if registry.is_file() else {"jobs": []}
    doc.setdefault("jobs", [])
    doc["jobs"] = [j for j in doc["jobs"] if j.get("name") != entry["name"]] + [entry]
    registry.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------- CLI


def _symbols(a) -> list[str]:
    if a.symbols_file:
        doc = json.loads(Path(a.symbols_file).read_text(encoding="utf-8"))
        syms = doc["symbols"] if isinstance(doc, dict) else doc
        return [str(s) for s in syms]
    return [s.strip() for s in (a.symbols or "").split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m iap.marketdata process-day", description=__doc__.splitlines()[0]
    )
    p.add_argument("--manifest", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--vendor-dir", required=True)
    p.add_argument("--out-root", required=True)
    p.add_argument("--symbols", default=None, help="comma-separated universe")
    p.add_argument("--symbols-file", default=None, help='JSON: ["AAPL", ...] or {"symbols": [...]}')
    p.add_argument("--configs-dir", default=None)
    p.add_argument("--download", action="store_true", help="fetch the vendor file when absent")
    p.add_argument(
        "--i-have-approval", action="store_true", help="the owner approved this download"
    )
    p.add_argument("--partition-instruments", type=int, default=10)
    p.add_argument("--engine", choices=("python", "rust"), default="rust")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--keep-raw", action="store_true")
    p.add_argument("--keep-gz", action="store_true")
    p.add_argument("--min-free-gb", type=float, default=10.0)
    p.add_argument("--tops", default=None, help="IEX formats: the day's TOPS capture (oracle)")
    p.add_argument("--dry-run", action="store_true", help="disk guard and the plan only")
    p.add_argument(
        "--render-cmd", default=None, help="write the detached .cmd wrapper here and exit"
    )
    p.add_argument(
        "--jobs-registry", default=None, help="with --render-cmd: add the watchdog entry"
    )
    p.add_argument("--python-dir", default=str(_REPO / "python"))
    a = p.parse_args(argv)
    try:
        if a.render_cmd:
            cmd_path = Path(a.render_cmd)
            log = str(cmd_path.with_suffix(".log"))
            extra = []
            if a.download:
                extra.append("--download")
            if a.i_have_approval:
                extra.append("--i-have-approval")
            if a.keep_raw:
                extra.append("--keep-raw")
            if a.keep_gz:
                extra.append("--keep-gz")
            if a.tops:
                extra.append(f'--tops "{a.tops}"')
            extra.append(
                f"--partition-instruments {a.partition_instruments} --engine {a.engine} --workers {a.workers} --min-free-gb {a.min_free_gb:g}"
            )
            text = render_wrapper(
                python_dir=a.python_dir,
                manifest=str(Path(a.manifest).resolve()),
                date=a.date,
                vendor_dir=str(Path(a.vendor_dir).resolve()),
                out_root=str(Path(a.out_root).resolve()),
                symbols_file=str(Path(a.symbols_file).resolve()) if a.symbols_file else "",
                log=log,
                extra_args=" ".join(extra),
            )
            if not a.symbols_file:
                p.error("--render-cmd needs --symbols-file")
            cmd_path.write_bytes(text.encode("ascii"))
            print(f"wrote {cmd_path}")
            if a.jobs_registry:
                _add_job(Path(a.jobs_registry), render_jobs_entry(a.date, str(cmd_path), log))
                print(f"registered process_{a.date.replace('-', '')} in {a.jobs_registry}")
            return 0
        report = process_day(
            Path(a.manifest),
            a.date,
            Path(a.vendor_dir),
            Path(a.out_root),
            _symbols(a),
            configs_dir=Path(a.configs_dir) if a.configs_dir else None,
            download=a.download,
            approval=a.i_have_approval,
            partition_instruments=a.partition_instruments,
            engine=a.engine,
            workers=a.workers,
            keep_raw=a.keep_raw,
            keep_gz=a.keep_gz,
            min_free_gb=a.min_free_gb,
            dry_run=a.dry_run,
            tops=Path(a.tops) if a.tops else None,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (IngestError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

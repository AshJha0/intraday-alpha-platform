"""R7 data preparation: candidate-day manifests, a resumable batch-ingest
wrapper, download verification and recorded day sampling (v1.13, opt-in).

Nothing here touches the network (``python/tests/test_import_policy.py``
guards ``iap.marketdata``).  The only download code is
``tools/r7_download.py``, which refuses to run without ``--i-have-approval``
and only fetches URLs on :data:`ALLOWED_HOSTS`.  docs/R7_DATA_PLAN.md.

    python -m iap.marketdata plan-ingest --manifest M --vendor-dir V \\
        --out-root ..\\data\\real\\r7 --cmd ..\\data\\real\\run_r7_ingest.cmd [--dry-run]
    python -m iap.marketdata verify-download --manifest M --vendor-dir V [--date D] [--no-hash]
    python -m iap.marketdata record-sampling --dataset-dir D --n N --seed S [--strata month]

A candidate manifest (``research/r7/r7_candidates.json``) lists days as
``{date, file, url, bytes, sha256|null, role: insample|holdout}``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path, PureWindowsPath

#: Hosts a manifest URL may name (and the only hosts tools/r7_download.py
#: fetches): Nasdaq's sample directory and, since v1.17 (A1/A2), IEX HIST's
#: index and the Google Cloud Storage links it hands out.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {"emi.nasdaq.com", "iextrading.com", "www.googleapis.com", "storage.googleapis.com"}
)
ROLES = ("insample", "holdout")
#: Ingest formats a candidate day may name (``format``, default ``itch50``):
#: the Nasdaq public files are ITCH 5.0, the Databento tier 2 is DBN MBO.
DAY_FORMATS = ("itch50", "dbn-mbo", "iex-deep-plus", "iex-deep")
#: Sessions already used by a study (in-sample 2019-20 and the v1.8 holdout).
#: They are SPENT: none may be a holdout again.
SPENT_DAYS: frozenset[str] = frozenset(
    {
        "2019-01-30",
        "2019-03-27",
        "2019-07-30",
        "2019-08-30",
        "2019-10-30",
        "2019-12-30",
        "2020-01-30",
        "2026-05-15",
        "2026-05-18",
    }
)
#: Measured: 59 min for the 16.15 GB 2026-05-18 file after the v1.8 fix.
INGEST_MIN_PER_GZ_GB = 59.0 / 16.15
#: Measured: a 3-symbol single-day dataset (raw JSONL + IAP1 + JSONL +
#: Parquet) is 0.8x (2019) to 1.7x (2026) the gzip size; the upper end.
DATASET_GB_PER_GZ_GB = 1.7
#: Measured: features for 2 days of 2026 = 1.1 GB, 7 days of 2019 = 2.3 GB.
FEATURES_GB_PER_DAY = 0.6
FEATURES_MIN_PER_DAY = 20.0


def _check_url(url: str) -> None:
    # string parsing only: iap.marketdata may not import urllib (import policy)
    scheme, sep, rest = url.partition("://")
    host = rest.split("/", 1)[0].split("@")[-1].split(":")[0].lower() if sep else ""
    if scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"URL not on the allowlist {sorted(ALLOWED_HOSTS)} (https only): {url}")


def load_candidates(path: str | Path) -> dict:
    """Read and validate a candidate manifest (fail closed)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    days = doc.get("days")
    if not isinstance(days, list) or not days:
        raise ValueError("manifest has no 'days'")
    seen: set[str] = set()
    for d in days:
        date = _dt.date.fromisoformat(d["date"])
        iso = date.isoformat()
        if date.weekday() >= 5:
            raise ValueError(f"{iso} is a weekend")
        if iso in seen:
            raise ValueError(f"duplicate date {iso}")
        seen.add(iso)
        if d.get("role") not in ROLES:
            raise ValueError(f"{iso}: role must be one of {ROLES}")
        if d["role"] == "holdout" and iso in SPENT_DAYS:
            raise ValueError(f"{iso} is SPENT and cannot be a holdout")
        name = d.get("file")
        if not name or Path(name).name != name or PureWindowsPath(name).name != name:
            raise ValueError(f"{iso}: 'file' must be a bare file name")
        if not isinstance(d.get("bytes"), int) or d["bytes"] <= 0:
            raise ValueError(f"{iso}: 'bytes' must be a positive integer")
        if d.get("format", "itch50") not in DAY_FORMATS:
            raise ValueError(f"{iso}: format must be one of {DAY_FORMATS}")
        if d.get("url"):
            _check_url(d["url"])
    doc["days"] = sorted(days, key=lambda d: d["date"])
    return doc


def verify_file(
    path: str | Path, expected_bytes: int, sha256: str | None = None, *, hash_file: bool = True
) -> dict:
    """Size (and sha256) check of a downloaded file."""
    p = Path(path)
    out = {"file": p.name, "exists": p.is_file(), "expected_bytes": expected_bytes}
    if not out["exists"]:
        return {**out, "ok": False, "reason": "missing"}
    size = p.stat().st_size
    out["bytes"] = size
    if size != expected_bytes:
        return {**out, "ok": False, "reason": "size mismatch"}
    if hash_file:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        out["sha256"] = h.hexdigest()
        if sha256 and out["sha256"] != sha256.lower():
            return {**out, "ok": False, "reason": "sha256 mismatch"}
    return {**out, "ok": True}


def estimate(days: list[dict], n_symbols: int = 3) -> dict:
    """Disk/time estimate from the measured v1.8 rates (upper ends).  The
    dataset size scales with the symbols' events; ``n_symbols / 3`` is a
    crude upper bound (QQQ is the heaviest of the three measured)."""
    gz = sum(d["bytes"] for d in days) / 1e9
    scale = max(1.0, n_symbols / 3)
    return {
        "days": len(days),
        "gz_gb": round(gz, 2),
        "dataset_gb": round(gz * DATASET_GB_PER_GZ_GB * scale, 1),
        "features_gb": round(len(days) * FEATURES_GB_PER_DAY * scale, 1),
        "ingest_hours": round(gz * INGEST_MIN_PER_GZ_GB / 60, 1),
        "features_hours": round(len(days) * FEATURES_MIN_PER_DAY * scale / 60, 1),
    }


def _tag(date: str) -> str:
    return date.replace("-", "")


def render_batch(
    doc: dict,
    *,
    manifest: str,
    python_dir: str,
    vendor_dir: str,
    out_root: str,
    workers: int = 2,
    symbols: str | None = None,
) -> str:
    """A resumable Windows batch file (for a WMI-detached launch, see
    docs/R7_DATA_PLAN.md §5).  Per day: verify-download, ingest into its own
    dataset dir, write ``_DONE_<yyyymmdd>``; a day with a marker is skipped.
    Then per role: merge, record the sampling, build features (each with its
    own marker).  Nothing is downloaded or deleted."""
    syms = symbols or doc.get("symbols") or "AAPL,MSFT,QQQ"
    L = [
        "@echo off",
        "REM generated by: python -m iap.marketdata plan-ingest (R7, v1.13). Resumable:",
        "REM a step whose _DONE marker exists is skipped. Downloads nothing, deletes nothing.",
        f'cd /d "{python_dir}"',
        "set PYTHONUTF8=1",
        "set PYTHONPATH=src",
        f"set OUT={out_root}",
        f"set V={vendor_dir}",
        f"set M={manifest}",
        'if not exist "%OUT%" mkdir "%OUT%"',
        "set L=%OUT%\\r7_batch.log",
        "echo START %DATE% %TIME% >> %L%",
    ]
    by_role: dict[str, list[str]] = {r: [] for r in ROLES}
    for d in doc["days"]:
        t = _tag(d["date"])
        ds = f"%OUT%\\ds_{t}"
        by_role[d["role"]].append(ds)
        L += [
            f'if exist "%OUT%\\_DONE_{t}" goto skip_{t}',
            f"echo ==== {d['date']} {d['role']} %TIME% >> %L%",
            f'python -m iap.marketdata verify-download --manifest "%M%" --vendor-dir "%V%" '
            f"--date {d['date']} >> %L% 2>&1",
            "if errorlevel 1 goto fail",
            f'python -m iap.marketdata ingest --format {d.get("format", "itch50")} --input "%V%\\{d["file"]}" '
            f'--date {d["date"]} --symbols {syms} --out "{ds}" > "%OUT%\\ingest_{t}.log" 2>&1',
            "if errorlevel 1 goto fail",
            f'echo ok > "%OUT%\\_DONE_{t}"',
            f":skip_{t}",
        ]
    for role, dirs in by_role.items():
        if not dirs:
            continue
        ds = f"%OUT%\\r7_{role}"
        merge = (
            f"python -m iap.marketdata merge {' '.join(chr(34) + x + chr(34) for x in dirs)} "
            f'--out "{ds}" >> %L% 2>&1'
            if len(dirs) > 1
            else f'xcopy /e /i /q "{dirs[0]}" "{ds}" >> %L% 2>&1'
        )
        L += [
            f'if exist "%OUT%\\_DONE_merge_{role}" goto skip_merge_{role}',
            merge,
            "if errorlevel 1 goto fail",
            f'python -m iap.marketdata record-sampling --dataset-dir "{ds}" --n {len(dirs)} '
            f"--seed 20261011 --strata month >> %L% 2>&1",
            "if errorlevel 1 goto fail",
            f'echo ok > "%OUT%\\_DONE_merge_{role}"',
            f":skip_merge_{role}",
            f'if exist "%OUT%\\_DONE_features_{role}" goto skip_features_{role}',
            f'python -m iap.features --data-dir "{ds}\\normalized" --out-dir "{ds}\\features" '
            f'--configs "{ds}\\configs" --registry-out "{ds}\\features\\feature_registry.json" '
            f'--workers {workers} > "%OUT%\\features_{role}.log" 2>&1',
            "if errorlevel 1 goto fail",
            f'echo ok > "%OUT%\\_DONE_features_{role}"',
            f":skip_features_{role}",
        ]
    L += [
        "echo DONE %DATE% %TIME% >> %L%",
        "exit /b 0",
        ":fail",
        "echo FAILED %ERRORLEVEL% %DATE% %TIME% >> %L%",
        "exit /b 1",
    ]
    return "\r\n".join(L) + "\r\n"


def _plan_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m iap.marketdata plan-ingest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--vendor-dir", required=True)
    p.add_argument("--out-root", required=True)
    p.add_argument("--cmd", help="where to write the .cmd (required unless --dry-run)")
    p.add_argument("--python-dir", default=str(Path(__file__).resolve().parents[3]))
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--symbols", default=None)
    p.add_argument("--dry-run", action="store_true", help="print the plan and the .cmd only")
    a = p.parse_args(argv)
    doc = load_candidates(a.manifest)
    vendor = Path(a.vendor_dir)
    n_sym = len((a.symbols or doc.get("symbols") or "AAPL,MSFT,QQQ").split(","))
    summary = {
        "estimate": estimate(doc["days"], n_sym),
        "days": [
            {
                "date": d["date"],
                "role": d["role"],
                "file": d["file"],
                "gb": round(d["bytes"] / 1e9, 2),
                "present": (vendor / d["file"]).is_file(),
            }
            for d in doc["days"]
        ],
    }
    text = render_batch(
        doc,
        manifest=str(Path(a.manifest).resolve()),
        python_dir=a.python_dir,
        vendor_dir=a.vendor_dir,
        out_root=a.out_root,
        workers=a.workers,
        symbols=a.symbols,
    )
    print(json.dumps(summary, indent=2))
    if a.dry_run:
        print(text)
        return 0
    if not a.cmd:
        p.error("--cmd is required without --dry-run")
    Path(a.cmd).write_bytes(text.encode("ascii"))
    print(f"wrote {a.cmd}")
    return 0


def _verify_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m iap.marketdata verify-download")
    p.add_argument("--manifest", required=True)
    p.add_argument("--vendor-dir", required=True)
    p.add_argument("--date", default=None, help="only this day")
    p.add_argument("--no-hash", action="store_true", help="size check only")
    a = p.parse_args(argv)
    doc = load_candidates(a.manifest)
    days = [d for d in doc["days"] if a.date in (None, d["date"])]
    if not days:
        p.error(f"no day {a.date} in the manifest")
    results = [
        verify_file(
            Path(a.vendor_dir) / d["file"], d["bytes"], d.get("sha256"), hash_file=not a.no_hash
        )
        | {"date": d["date"]}
        for d in days
    ]
    print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


def _sampling_main(argv: list[str]) -> int:
    from iap.marketdata.ingest import load_manifest
    from iap.validation.sessions import STRATA, record_day_sampling, stratified_day_sample

    p = argparse.ArgumentParser(prog="python -m iap.marketdata record-sampling")
    p.add_argument("--dataset-dir", required=True)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--strata", default="month", choices=STRATA)
    p.add_argument("--exclude-tags", default="", help="comma-separated: fomc,holiday_thin")
    a = p.parse_args(argv)
    sessions = sorted(load_manifest(a.dataset_dir).get("sessions", {}))
    tags = [t for t in a.exclude_tags.split(",") if t]
    record = stratified_day_sample(sessions, a.n, a.seed, a.strata, tags)
    record_day_sampling(a.dataset_dir, record)
    print(json.dumps(record, indent=2))
    return 0


COMMANDS = {
    "plan-ingest": _plan_main,
    "verify-download": _verify_main,
    "record-sampling": _sampling_main,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    return COMMANDS[args[0]](args[1:])

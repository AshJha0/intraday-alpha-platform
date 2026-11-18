#!/usr/bin/env python3
"""Download -> itchbars -> delete, one in-sample full-market Nasdaq ITCH day at a time.

    python tools/itch_bars_chain.py --i-have-approval [--date 2019-03-27 ...]

For each ``insample`` day of research/breadth/candidates.json without bars:
download the file (tools/r7_download.download_one: allowed hosts, size check,
sha256 while streaming), run the ``itchbars`` binary on the decompressed stream
inside WSL, check the message count is positive and the bars file is not
empty, append one record to ``<bars-dir>/chain_log.jsonl`` and delete the raw
file the chain downloaded (a file that was already on disk is kept).

Only days with role ``insample`` are ever touched.  The 2025/26 sessions are
the breadth holdout (research/holdout_registry.json) and are not in the
manifest; a day whose date is in 2025 or later is refused outright as a
second guard.  One raw file is on disk at a time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from iap.marketdata.r7plan import load_candidates  # noqa: E402
from r7_download import download_one  # noqa: E402

MANIFEST = ROOT / "research" / "breadth" / "candidates.json"
#: built once with: CARGO_TARGET_DIR=$HOME/iap-itchbars-target cargo build -p itchbars --release
WSL_BINARY = "$HOME/iap-itchbars-target/release/itchbars"
SUMMARY = re.compile(r"itchbars: messages (\d+) symbols (\d+) unknown_order_refs (\d+)")


def wsl_path(p: Path) -> str:
    p = p.resolve()
    drive = p.drive.rstrip(":").lower()
    return f"/mnt/{drive}" + p.as_posix()[len(p.drive) :]


def bars_path(bars_dir: Path, date: str) -> Path:
    return bars_dir / f"bars_{date.replace('-', '')}.csv"


def eligible(day: dict) -> bool:
    """In-sample days before 2025 only (the holdout guard)."""
    return day.get("role") == "insample" and day["date"] < "2025-01-01"


def run_itchbars(raw: Path, out: Path) -> dict:
    tmp = out.with_name(out.name + ".part")
    cmd = f"set -o pipefail; gzip -dc '{wsl_path(raw)}' | {WSL_BINARY} > '{wsl_path(tmp)}'"
    proc = subprocess.run(  # noqa: S603 - fixed command, paths from the manifest
        ["wsl", "-e", "bash", "-lc", cmd], capture_output=True, text=True, check=False
    )
    m = SUMMARY.search(proc.stderr or "")
    if proc.returncode != 0 or not m or int(m.group(1)) == 0 or tmp.stat().st_size < 1_000_000:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "returncode": proc.returncode, "stderr": (proc.stderr or "")[-500:]}
    tmp.replace(out)
    return {
        "ok": True,
        "messages": int(m.group(1)),
        "symbols": int(m.group(2)),
        "unknown_order_refs": int(m.group(3)),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vendor-dir", type=Path, default=ROOT / "data" / "vendor")
    ap.add_argument("--bars-dir", type=Path, default=ROOT / "data" / "real" / "bars")
    ap.add_argument("--date", action="append", help="only these days (repeatable)")
    ap.add_argument("--i-have-approval", action="store_true")
    a = ap.parse_args(argv)
    if not a.i_have_approval:
        print("refusing: pass --i-have-approval once the owner approved the downloads")
        return 2
    days = [d for d in load_candidates(MANIFEST)["days"] if not a.date or d["date"] in a.date]
    refused = [d["date"] for d in days if not eligible(d)]
    if refused or not days:
        print(f"refusing: not in-sample days before 2025: {refused or 'no matching day'}")
        return 2
    a.bars_dir.mkdir(parents=True, exist_ok=True)
    log = a.bars_dir / "chain_log.jsonl"
    failed = 0
    for day in days:
        out = bars_path(a.bars_dir, day["date"])
        if out.is_file():
            print(f"{day['date']}: bars present, skipped", flush=True)
            continue
        raw = a.vendor_dir / day["file"]
        was_present = raw.is_file()
        t0 = time.time()
        rec: dict = {"date": day["date"], "file": day["file"]}
        try:
            dl = download_one(day, a.vendor_dir, verify_sha=False)
            rec["download"] = dl
            if dl["status"] == "failed":
                raise RuntimeError(f"download failed: {dl.get('reason')}")
            t1 = time.time()
            res = run_itchbars(raw, out)
            rec["itchbars"] = res
            if not res["ok"]:
                raise RuntimeError("itchbars failed")
            rec["bars_sha256"] = sha256(out)
            rec["bars_bytes"] = out.stat().st_size
            rec["seconds"] = {"download": round(t1 - t0), "bars": round(time.time() - t1)}
            if not was_present:
                raw.unlink()
                rec["raw_deleted"] = True
            rec["ok"] = True
        except Exception as exc:  # one day's failure must not stop the others
            rec["ok"] = False
            rec["error"] = f"{type(exc).__name__}: {exc}"
            failed += 1
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        print(json.dumps(rec, sort_keys=True), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

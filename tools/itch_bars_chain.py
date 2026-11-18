#!/usr/bin/env python3
"""Download -> itchbars -> delete, one in-sample full-market Nasdaq ITCH day at a time.

    python tools/itch_bars_chain.py --i-have-approval [--date 2019-03-27 ...]

For each ``insample`` day of research/breadth/candidates.json without bars:
download the file (tools/r7_download.download_one: allowed hosts, size check,
sha256 while streaming), run the ``itchbars`` binary on the decompressed stream
inside WSL, check the message count is positive and the bars file is not
empty, write the closing-auction rows beside the bars (``auction_<date>.csv``),
append one record to ``<bars-dir>/chain_log.jsonl`` and delete the raw
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
import socket
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
AUCTION_SUMMARY = re.compile(r"itchbars: auction rows (\d+) short_auction_messages (\d+)")


def wsl_path(p: Path) -> str:
    p = p.resolve()
    drive = p.drive.rstrip(":").lower()
    return f"/mnt/{drive}" + p.as_posix()[len(p.drive) :]


def bars_path(bars_dir: Path, date: str) -> Path:
    return bars_dir / f"bars_{date.replace('-', '')}.csv"


def auction_path(bars_dir: Path, date: str) -> Path:
    return bars_dir / f"auction_{date.replace('-', '')}.csv"


#: a stalled connection is dropped after this many seconds without data, and a
#: failed download is tried again this many times (the first chain hung for
#: half an hour on a connection that never timed out)
SOCKET_TIMEOUT_S = 120
DOWNLOAD_ATTEMPTS = 4


def download_with_retry(day: dict, vendor_dir: Path) -> dict:
    last: dict = {}
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            last = download_one(day, vendor_dir, verify_sha=False)
        except OSError as exc:  # timeouts and resets
            last = {
                "date": day["date"],
                "status": "failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        last["attempts"] = attempt
        if last["status"] != "failed":
            return last
        (vendor_dir / (day["file"] + ".part")).unlink(missing_ok=True)
        time.sleep(30)
    return last


def eligible(day: dict) -> bool:
    """In-sample days before 2025 only (the holdout guard)."""
    return day.get("role") == "insample" and day["date"] < "2025-01-01"


def run_itchbars(raw: Path, out: Path, auction: Path) -> dict:
    """Bars to ``out`` and the closing-auction rows to ``auction`` in one pass.
    When ``out`` already exists it is not replaced: the new bars are written
    beside it and must be byte-identical, which the caller checks."""
    tmp = out.with_name(out.name + ".part")
    atmp = auction.with_name(auction.name + ".part")
    cmd = (
        f"set -o pipefail; gzip -dc '{wsl_path(raw)}' | {WSL_BINARY} "
        f"--auction '{wsl_path(atmp)}' > '{wsl_path(tmp)}'"
    )
    proc = subprocess.run(  # noqa: S603 - fixed command, paths from the manifest
        ["wsl", "-e", "bash", "-lc", cmd], capture_output=True, text=True, check=False
    )
    m = SUMMARY.search(proc.stderr or "")
    a = AUCTION_SUMMARY.search(proc.stderr or "")
    ok = (
        proc.returncode == 0
        and m is not None
        and a is not None
        and int(m.group(1)) > 0
        and tmp.stat().st_size >= 1_000_000
        and int(a.group(1)) > 0
    )
    if not ok:
        tmp.unlink(missing_ok=True)
        atmp.unlink(missing_ok=True)
        return {"ok": False, "returncode": proc.returncode, "stderr": (proc.stderr or "")[-500:]}
    res = {
        "ok": True,
        "messages": int(m.group(1)),
        "symbols": int(m.group(2)),
        "unknown_order_refs": int(m.group(3)),
        "auction_rows": int(a.group(1)),
        "short_auction_messages": int(a.group(2)),
    }
    if out.is_file():
        res["bars_identical_to_existing"] = sha256(tmp) == sha256(out)
        tmp.unlink()
        if not res["bars_identical_to_existing"]:
            atmp.unlink(missing_ok=True)
            res["ok"] = False
            return res
    else:
        tmp.replace(out)
    atmp.replace(auction)
    return res


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
    socket.setdefaulttimeout(SOCKET_TIMEOUT_S)
    a.bars_dir.mkdir(parents=True, exist_ok=True)
    log = a.bars_dir / "chain_log.jsonl"
    failed = 0
    for day in days:
        out = bars_path(a.bars_dir, day["date"])
        auc = auction_path(a.bars_dir, day["date"])
        if out.is_file() and auc.is_file():
            print(f"{day['date']}: bars and auction present, skipped", flush=True)
            continue
        raw = a.vendor_dir / day["file"]
        was_present = raw.is_file()
        t0 = time.time()
        rec: dict = {"date": day["date"], "file": day["file"]}
        try:
            dl = download_with_retry(day, a.vendor_dir)
            rec["download"] = dl
            if dl["status"] == "failed":
                raise RuntimeError(f"download failed: {dl.get('reason')}")
            t1 = time.time()
            res = run_itchbars(raw, out, auc)
            rec["itchbars"] = res
            if not res["ok"]:
                raise RuntimeError("itchbars failed")
            rec["bars_sha256"] = sha256(out)
            rec["bars_bytes"] = out.stat().st_size
            rec["auction_sha256"] = sha256(auc)
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

#!/usr/bin/env python3
"""Download R7 candidate ITCH files — ONLY with the owner's explicit approval.

Refuses to run without ``--i-have-approval`` and fetches only https URLs on
``iap.marketdata.r7plan.ALLOWED_HOSTS``, named in a validated candidate
manifest (research/r7/r7_candidates.json).  Each file is streamed to
``<vendor-dir>/<file>.part``, size-checked against the manifest and renamed;
a file already present with the right size is skipped.  Read the provider's
terms first (docs/R7_DATA_PLAN.md §1).

    PYTHONPATH=python/src python tools/r7_download.py --manifest research/r7/r7_candidates.json \\
        --vendor-dir data/vendor --date 2025-12-08 --i-have-approval
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.marketdata.r7plan import _check_url, load_candidates, verify_file  # noqa: E402

CHUNK = 1 << 22


def download_one(day: dict, vendor_dir: Path, opener=urllib.request.urlopen) -> dict:
    _check_url(day["url"])
    dest = vendor_dir / day["file"]
    if dest.is_file() and dest.stat().st_size == day["bytes"]:
        return {"date": day["date"], "status": "present"}
    part = dest.with_name(dest.name + ".part")
    with opener(day["url"]) as resp, open(part, "wb") as out:
        while chunk := resp.read(CHUNK):
            out.write(chunk)
    res = verify_file(part, day["bytes"], day.get("sha256"), hash_file=False)
    if not res["ok"]:
        return {"date": day["date"], "status": "failed", "reason": res["reason"]}
    part.replace(dest)
    return {"date": day["date"], "status": "downloaded"}


def main(argv: list[str] | None = None, opener=urllib.request.urlopen) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--manifest", required=True)
    p.add_argument("--vendor-dir", required=True)
    p.add_argument("--date", action="append", help="only these days (repeatable)")
    p.add_argument(
        "--i-have-approval",
        action="store_true",
        help="the data owner approved downloading exactly these files",
    )
    a = p.parse_args(argv)
    if not a.i_have_approval:
        print(
            "refusing: pass --i-have-approval once the owner approved the download", file=sys.stderr
        )
        return 2
    doc = load_candidates(a.manifest)
    days = [d for d in doc["days"] if not a.date or d["date"] in a.date]
    if not days or any(not d.get("url") for d in days):
        print("refusing: no matching days, or a day without a url", file=sys.stderr)
        return 2
    vendor = Path(a.vendor_dir)
    vendor.mkdir(parents=True, exist_ok=True)
    ok = True
    for d in days:
        r = download_one(d, vendor, opener)
        print(r, flush=True)
        ok &= r["status"] != "failed"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

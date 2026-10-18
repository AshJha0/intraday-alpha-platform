#!/usr/bin/env python3
"""Download R7 candidate ITCH files — ONLY with the owner's explicit approval.

Refuses to run without ``--i-have-approval`` and fetches only https URLs on
``iap.marketdata.r7plan.ALLOWED_HOSTS``, named in a validated candidate
manifest (research/r7/r7_candidates.json).  Each file is streamed to
``<vendor-dir>/<file>.part``, size- and sha256-checked against the manifest
and renamed; a file already present is skipped once its size (and sha256)
match.  The sha256 is computed while streaming (no second pass) and checked
whenever the manifest carries one; that is the default since v1.18 (REL-12),
``--no-verify-sha`` restores the size-only check.  The result records the
computed sha256 and whether it was verified, so a manifest without one gets
the hash to pin before the ingest.  Read the provider's
terms first (docs/R7_DATA_PLAN.md §1).

    PYTHONPATH=python/src python tools/r7_download.py --manifest research/r7/r7_candidates.json \\
        --vendor-dir data/vendor --date 2025-12-08 --i-have-approval
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.marketdata.r7plan import _check_url, load_candidates, verify_file  # noqa: E402

CHUNK = 1 << 22


def download_one(
    day: dict, vendor_dir: Path, opener=urllib.request.urlopen, *, verify_sha: bool = True
) -> dict:
    """Fetch one manifest file; ``verify_sha`` (default) checks the manifest's
    sha256 when it has one, on the streamed bytes and on a present file."""
    _check_url(day["url"])
    dest = vendor_dir / day["file"]
    expected = (day.get("sha256") or "").lower() or None
    if dest.is_file() and dest.stat().st_size == day["bytes"]:
        if not (verify_sha and expected):
            return {"date": day["date"], "status": "present", "sha256_verified": False}
        res = verify_file(dest, day["bytes"], expected)
        if res["ok"]:
            return {"date": day["date"], "status": "present", "sha256_verified": True}
        # a present file of the right size but the wrong bytes is re-fetched
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    with opener(day["url"]) as resp, open(part, "wb") as out:
        while chunk := resp.read(CHUNK):
            h.update(chunk)
            out.write(chunk)
    res = verify_file(part, day["bytes"], None, hash_file=False)
    if not res["ok"]:
        return {"date": day["date"], "status": "failed", "reason": res["reason"]}
    digest = h.hexdigest()
    verified = bool(verify_sha and expected)
    if verified and digest != expected:
        return {"date": day["date"], "status": "failed", "reason": "sha256 mismatch",
                "sha256": digest}  # fmt: skip
    part.replace(dest)
    return {"date": day["date"], "status": "downloaded", "sha256": digest,
            "sha256_verified": verified}  # fmt: skip


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
    p.add_argument(
        "--no-verify-sha",
        action="store_true",
        help="size check only (the pre-v1.18 behaviour); default: verify the manifest sha256",
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
        r = download_one(d, vendor, opener, verify_sha=not a.no_verify_sha)
        print(r, flush=True)
        ok &= r["status"] != "failed"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

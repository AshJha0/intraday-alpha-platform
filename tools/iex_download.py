#!/usr/bin/env python3
"""Download ONE IEX HIST capture named in an iex_hist.py manifest — only with
the owner's explicit approval (v1.17, A4).

Refuses without ``--i-have-approval`` AND without an ``approval`` sentence
in the manifest; fetches only https URLs on ``r7plan.ALLOWED_HOSTS`` (the
Google Cloud Storage links HIST hands out).  Streams to ``<file>.part``,
size-checks against the manifest and renames; a present file of the right
size is skipped.  One feed per call (``--feed TOPS`` / ``DEEP+`` / ``DEEP``):
the process-iex-day chain keeps one vendor file on disk at a time.

    PYTHONPATH=python/src python tools/iex_download.py --manifest research/v117/iex_overlap_manifest.json \\
        --vendor-dir data/vendor --date 2025-11-28 --feed TOPS --i-have-approval
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from iap.marketdata.process_iex import load_iex_manifest  # noqa: E402
from r7_download import download_one  # noqa: E402


def main(argv: list[str] | None = None, opener=urllib.request.urlopen) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--manifest", required=True)
    p.add_argument("--vendor-dir", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--feed", required=True, help="TOPS, DEEP+ or DEEP (manifest feed name)")
    p.add_argument(
        "--i-have-approval",
        action="store_true",
        help="the data owner approved downloading exactly this manifest's files",
    )
    a = p.parse_args(argv)
    if not a.i_have_approval:
        print(
            "refusing: pass --i-have-approval once the owner approved the download", file=sys.stderr
        )
        return 2
    doc = load_iex_manifest(a.manifest)
    if not doc.get("approval"):
        print("refusing: the manifest carries no owner approval sentence", file=sys.stderr)
        return 2
    day = next((d for d in doc["days"] if d["date"] == a.date), None)
    entry = (day or {}).get("feeds", {}).get(a.feed)
    if entry is None or not entry.get("url"):
        print(f"refusing: no {a.feed} file for {a.date} in the manifest", file=sys.stderr)
        return 2
    vendor = Path(a.vendor_dir)
    vendor.mkdir(parents=True, exist_ok=True)
    r = download_one({**entry, "date": a.date}, vendor, opener)
    print(r, flush=True)
    return 0 if r["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Markdown rows for docs/research/IEX_OVERLAP_SET.md from the finished days'
``process_report.json`` (v1.17, A4).  Read-only.

    python tools/iex_overlap_rows.py --out-root data/real/iex [--date 2025-11-28]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _hms(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def row(report: dict) -> str:
    steps = {s["step"]: s for s in report["steps"]}
    dl = (steps.get("download_tops", {}).get("seconds_download") or 0) + (
        steps.get("download_book", {}).get("seconds_download") or 0
    )
    ing = steps.get("ingest", {})
    ver = ing.get("book_verification") or {}
    oracle = " / ".join(
        f"{ver[s]['compared']}/{ver[s]['mismatched']}" if s in ver else "-"
        for s in report.get("symbols", [])
    )
    status = {v["status"] for v in ver.values()}
    if status and status != {"match"}:
        oracle += f" ({', '.join(sorted(status))})"
    return (
        f"| {report['date']} | {report['tops_bytes'] / 1e9:.2f} / {report['book_bytes'] / 1e9:.2f} "
        f"| {_hms(dl) if dl else 'present'} | {_hms(steps.get('tops_extract', {}).get('seconds'))} "
        f"| {_hms(ing.get('seconds_ingest'))} | {ing.get('messages') or '-':,} "
        f"| {ing.get('events') or '-':,} | {oracle} "
        f"| {report.get('free_gb_min_seen', '-')} / {report.get('free_gb_end', '-')} "
        f"| `{(ing.get('dataset_version') or '-')[:12]}` |"
    ).replace(",-", "-")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out-root", required=True)
    p.add_argument("--date", action="append", default=None)
    a = p.parse_args(argv)
    for rep in sorted(Path(a.out_root).glob("iex_*/process_report.json")):
        doc = json.loads(rep.read_text(encoding="utf-8"))
        if a.date and doc["date"] not in a.date:
            continue
        print(row(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

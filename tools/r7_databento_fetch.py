#!/usr/bin/env python3
"""R7 tier 2: Databento XNAS.ITCH MBO cost estimate and (approved) download.

Reads the tier-2 date list (research/r7/r7_tier2_dates.json), asks Databento
for the cost of each session (``metadata.get_cost``; costs nothing) and
prints the per-day and total estimate.  It downloads ONLY when both
``--i-have-approval`` and ``--max-cost-usd`` are given and the estimated
total is within that ceiling; otherwise it stops after the estimate.

The API key is read from the environment variable ``DATABENTO_API_KEY`` and
is never printed, logged or written (error messages are scrubbed of it).
Each session is the New York calendar day (the window the ``dbn-mbo``
reader keeps), saved as ``<vendor-dir>/xnas-itch-<yyyymmdd>.mbo.dbn.zst``
via a ``.part`` file; ``--manifest-out`` writes a candidate manifest
(format ``dbn-mbo``, bytes, sha256) for ``plan-ingest`` / ``verify-download``.
Needs the optional extra: ``pip install 'iap[databento]'``.
docs/R7_DATA_PLAN.md §7.

    set DATABENTO_API_KEY=...            (never on the command line)
    python tools/r7_databento_fetch.py                       # estimate only
    python tools/r7_databento_fetch.py --vendor-dir data/vendor/dbn \\
        --i-have-approval --max-cost-usd 150 --manifest-out research/r7/r7_tier2_candidates.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.marketdata.itch50 import session_midnight_ns  # noqa: E402
from iap.marketdata.r7plan import SPENT_DAYS  # noqa: E402

DEFAULT_DATES = ROOT / "research" / "r7" / "r7_tier2_dates.json"
KEY_ENV = "DATABENTO_API_KEY"
DATASET = "XNAS.ITCH"
SCHEMA = "mbo"
_DAY_NS = 86_400 * 10**9


def load_dates(path: str | Path) -> dict:
    """The tier-2 date list, validated (weekdays, unique, none spent)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    dates = doc.get("dates")
    if not isinstance(dates, list) or not dates:
        raise ValueError("date list has no 'dates'")
    seen: set[str] = set()
    for d in dates:
        day = _dt.date.fromisoformat(d)
        if day.weekday() >= 5:
            raise ValueError(f"{d} is a weekend")
        if d in seen:
            raise ValueError(f"duplicate date {d}")
        if d in SPENT_DAYS:
            raise ValueError(f"{d} is SPENT")
        seen.add(d)
    if doc.get("dataset", DATASET) != DATASET:
        raise ValueError(f"dataset must be {DATASET}")
    doc["dates"] = sorted(dates)
    return doc


def window(date: str) -> tuple[str, str]:
    """``(start, end)`` ISO UTC of the session's New York calendar day."""
    start = session_midnight_ns(date)
    iso = lambda ns: _dt.datetime.fromtimestamp(ns // 10**9, _dt.UTC).isoformat()  # noqa: E731
    return iso(start), iso(start + _DAY_NS)


def file_name(date: str) -> str:
    return f"xnas-itch-{date.replace('-', '')}.mbo.dbn.zst"


class _Scrub:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def __call__(self, text: object) -> str:
        s = str(text)
        return s.replace(self.secret, "***") if self.secret else s


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _default_client(key: str) -> Any:
    try:
        import databento  # the network client: tools only, never imported by iap
    except ImportError as exc:
        raise SystemExit("needs the databento client: pip install 'iap[databento]'") from exc
    return databento.Historical(key=key)


def estimate(client: Any, dates: list[str], symbols: list[str]) -> list[dict]:
    rows = []
    for d in dates:
        start, end = window(d)
        cost = client.metadata.get_cost(
            dataset=DATASET, schema=SCHEMA, symbols=symbols, stype_in="raw_symbol",
            start=start, end=end,
        )  # fmt: skip
        rows.append({"date": d, "start": start, "end": end, "cost_usd": float(cost)})
    return rows


def download(client: Any, row: dict, symbols: list[str], vendor: Path) -> dict:
    dest = vendor / file_name(row["date"])
    if not dest.is_file():
        part = dest.with_name(dest.name + ".part")
        client.timeseries.get_range(
            dataset=DATASET, schema=SCHEMA, symbols=symbols, stype_in="raw_symbol",
            start=row["start"], end=row["end"], path=str(part),
        )  # fmt: skip
        part.replace(dest)
        status = "downloaded"
    else:
        status = "present"
    return {
        "date": row["date"],
        "status": status,
        "file": dest.name,
        "bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
    }


def main(argv: list[str] | None = None, client_factory: Callable[[str], Any] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dates-file", default=str(DEFAULT_DATES))
    p.add_argument("--date", action="append", help="only these days (repeatable)")
    p.add_argument("--symbols", default=None, help="default: the date list's symbols")
    p.add_argument("--vendor-dir", default=None, help="download target (download only)")
    p.add_argument("--max-cost-usd", type=float, default=None, help="estimated-cost ceiling")
    p.add_argument(
        "--i-have-approval",
        action="store_true",
        help="the owner approved buying exactly these sessions within --max-cost-usd",
    )
    p.add_argument("--manifest-out", default=None, help="write a dbn-mbo candidate manifest")
    a = p.parse_args(argv)

    doc = load_dates(a.dates_file)
    dates = [d for d in doc["dates"] if not a.date or d in a.date]
    if not dates:
        print("refusing: no matching dates", file=sys.stderr)
        return 2
    symbols = [s.strip() for s in (a.symbols or doc["symbols"]).split(",") if s.strip()]
    key = os.environ.get(KEY_ENV, "")
    if not key:
        print(f"refusing: set the API key in the environment variable {KEY_ENV}", file=sys.stderr)
        return 2
    scrub = _Scrub(key)
    try:
        client = (client_factory or _default_client)(key)
        rows = estimate(client, dates, symbols)
    except Exception as exc:  # noqa: BLE001 - report without the key
        print(f"error: cost estimate failed: {scrub(exc)}", file=sys.stderr)
        return 1
    total = round(sum(r["cost_usd"] for r in rows), 2)
    report = {
        "dataset": DATASET,
        "schema": SCHEMA,
        "symbols": symbols,
        "days": len(rows),
        "per_day_usd": {r["date"]: round(r["cost_usd"], 4) for r in rows},
        "estimated_total_usd": total,
        "max_cost_usd": a.max_cost_usd,
    }
    print(json.dumps(report, indent=2), flush=True)

    if not a.i_have_approval or a.max_cost_usd is None:
        print(
            "estimate only: nothing downloaded (a download needs --i-have-approval and "
            "--max-cost-usd)",
            file=sys.stderr,
        )
        return 0
    if total > a.max_cost_usd:
        print(
            f"refusing: estimated total {total} USD exceeds --max-cost-usd {a.max_cost_usd}",
            file=sys.stderr,
        )
        return 3
    if not a.vendor_dir:
        print("refusing: --vendor-dir is required to download", file=sys.stderr)
        return 2
    vendor = Path(a.vendor_dir)
    vendor.mkdir(parents=True, exist_ok=True)
    days = []
    for row in rows:
        try:
            r = download(client, row, symbols, vendor)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {row['date']}: {scrub(exc)}", file=sys.stderr)
            return 1
        print(json.dumps(r), flush=True)
        days.append(
            {"date": r["date"], "file": r["file"], "bytes": r["bytes"], "sha256": r["sha256"],
             "role": doc.get("role", "holdout"), "format": "dbn-mbo"}
        )  # fmt: skip
    if a.manifest_out:
        manifest = {
            "schema": "iap.r7_candidates/v1",
            "status": "downloaded under owner approval (tier 2, Databento)",
            "source": f"Databento {DATASET} {SCHEMA}",
            "symbols": ",".join(symbols),
            "days": days,
        }
        Path(a.manifest_out).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

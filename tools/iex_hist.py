#!/usr/bin/env python3
"""IEX HIST index -> a download manifest (v1.17, A4): the overlap set and the
rolling "recent IEX days" list.

    python tools/iex_hist.py index --date 2025-11-28 --date 2025-12-08 ... \\
        --out research/v117/iex_overlap_manifest.json --approval "<the owner's sentence>"
    python tools/iex_hist.py recent --n 5 [--lookback 21] [--today 2026-10-15] \\
        --out research/v117/iex_recent_manifest.json

``index`` makes one small JSON GET of ``https://iextrading.com/api/1.0/hist?date=YYYYMMDD``
per day (never a capture) and records every feed file the index lists: name,
size and the Google Cloud Storage link, under the feed names ``TOPS``,
``DEEP``, ``DEEP+`` (HIST ``DPLS``) and ``DEEP+C`` (``DPLC``).  A day the
index does not have is recorded with empty ``feeds`` and a note.  The
manifest carries the owner's approval sentence for its downloads (none
without one: ``tools/iex_download.py`` refuses a manifest without
``approval``).

``recent`` is the selection rule of the rolling set: **the latest N calendar
days, walking back from UTC today - 1 over at most ``--lookback`` days, whose
HIST index lists both a TOPS and a DEEP+ file** (weekends and holidays have
no index entry and fall out by themselves; the rule is a pure function,
:func:`select_recent`, tested).  It writes the manifest WITHOUT an approval
sentence: those days are a list, not a download, until the owner approves
them.  Nothing in this tool downloads a capture.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.marketdata.process_iex import MANIFEST_SCHEMA  # noqa: E402
from iap.marketdata.r7plan import _check_url  # noqa: E402

IEX_HIST_URL = "https://iextrading.com/api/1.0/hist?date={yyyymmdd}"
USER_AGENT = "intraday-alpha-platform/1.17 (iex_hist; one index GET per day)"
#: HIST ``feed`` field -> manifest feed name.
FEED_NAMES = {"TOPS": "TOPS", "DEEP": "DEEP", "DPLS": "DEEP+", "DPLC": "DEEP+C"}
#: A usable day of the rolling set has both of these.
RECENT_REQUIRED = ("TOPS", "DEEP+")


def fetch_index(yyyymmdd: str, timeout: float = 30.0) -> str:
    """One polite GET of the HIST index for one day (JSON text; never a file)."""
    url = IEX_HIST_URL.format(yyyymmdd=yyyymmdd)
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URL
        return r.read().decode("utf-8", errors="replace")


def parse_index(text: str, yyyymmdd: str) -> dict[str, dict]:
    """``{feed name: {file, url, bytes, version, protocol}}`` of one day's index.

    The index is a JSON list of ``{link, date, feed, version, protocol, size}``
    (or a dict keyed by date); an empty day is ``{}``.  Links must be on the
    allow-list (``r7plan.ALLOWED_HOSTS``).
    """
    doc = json.loads(text) if text.strip() else []
    if isinstance(doc, dict):
        doc = doc.get(yyyymmdd, [])
    if not isinstance(doc, list):
        raise ValueError("IEX HIST index is neither a list nor a dict keyed by date")
    out: dict[str, dict] = {}
    for e in doc:
        if not isinstance(e, dict):
            continue
        feed = FEED_NAMES.get(str(e.get("feed", "")).upper())
        if feed is None:
            continue
        url = str(e.get("link", ""))
        _check_url(url)
        name = url.split("?", 1)[0].rsplit("%2F", 1)[-1].rsplit("/", 1)[-1]
        out[feed] = {
            "file": name,
            "url": url,
            "bytes": int(e.get("size")),
            "version": str(e.get("version", "")),
            "protocol": str(e.get("protocol", "")),
        }
    return out


def iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def select_recent(available: dict[str, dict[str, dict]], n: int) -> list[str]:
    """The rolling-set rule: the latest ``n`` ISO dates of ``available``
    (date -> feeds, as :func:`parse_index` gives them) that list every feed in
    :data:`RECENT_REQUIRED`, newest first."""
    ok = [d for d, feeds in available.items() if all(f in feeds for f in RECENT_REQUIRED)]
    return sorted(ok, reverse=True)[:n]


def build_manifest(days: list[dict], *, source: str, approval: str | None, note: str) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "source": source,
        "fetched_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "approval": approval,
        "note": note,
        "days": sorted(days, key=lambda d: d["date"]),
    }


def _index_days(dates: list[str], fetch=fetch_index) -> list[dict]:
    days = []
    for d in dates:
        key = d.replace("-", "")
        rec: dict = {"date": iso(key), "feeds": {}}
        try:
            rec["feeds"] = parse_index(fetch(key), key)
        except Exception as exc:  # noqa: BLE001 - recorded per day, the run goes on
            rec["error"] = f"{type(exc).__name__}: {exc}"
        if not rec["feeds"] and "error" not in rec:
            rec["note"] = "not on IEX HIST (no index entry)"
        days.append(rec)
    return days


def main(argv: list[str] | None = None, fetch=fetch_index) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("index", help="the named days' feed files")
    a.add_argument("--date", action="append", required=True, help="YYYY-MM-DD, repeatable")
    a.add_argument("--out", required=True)
    a.add_argument("--approval", default=None, help="the owner's approval sentence, verbatim")
    r = sub.add_parser("recent", help="the rolling set: latest N days on HIST (no approval)")
    r.add_argument("--n", type=int, default=5)
    r.add_argument("--lookback", type=int, default=21, help="calendar days to walk back")
    r.add_argument("--today", default=None, help="UTC date to walk back from (default: now)")
    r.add_argument("--out", required=True)
    args = p.parse_args(argv)
    if args.cmd == "index":
        days = _index_days(args.date, fetch)
        doc = build_manifest(
            days,
            source=IEX_HIST_URL,
            approval=args.approval,
            note="overlap set: every file is listed; download only with the approval sentence",
        )
    else:
        today = (
            _dt.date.fromisoformat(args.today) if args.today else _dt.datetime.now(_dt.UTC).date()
        )
        dates = [(today - _dt.timedelta(days=i)).isoformat() for i in range(1, args.lookback + 1)]
        days = _index_days(dates, fetch)
        available = {d["date"]: d["feeds"] for d in days if d["feeds"]}
        chosen = select_recent(available, args.n)
        doc = build_manifest(
            [d for d in days if d["date"] in chosen],
            source=IEX_HIST_URL,
            approval=None,
            note=(
                f"rolling set: the latest {args.n} HIST days with TOPS and DEEP+ among the "
                f"{args.lookback} calendar days before {today.isoformat()} (UTC); NOT approved "
                "for download; the owner approves before tools/iex_download.py runs"
            ),
        )
        doc["rule"] = {
            "n": args.n,
            "lookback_days": args.lookback,
            "today_utc": today.isoformat(),
            "required_feeds": list(RECENT_REQUIRED),
            "candidates_with_data": sorted(available),
        }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    for d in doc["days"]:
        sizes = ", ".join(f"{f} {e['bytes'] / 1e9:.2f} GB" for f, e in sorted(d["feeds"].items()))
        print(f"{d['date']}: {sizes or d.get('note') or d.get('error')}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

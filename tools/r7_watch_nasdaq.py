"""Watch Nasdaq's free ITCH sample directory for new full-day files (R7).

Read-only: one GET of the directory listing, never a download (downloads stay
manual via ``tools/r7_download.py --i-have-approval``). The listing is compared
to the committed known-list ``research/r7/nasdaq_listing_known.json``.

v1.17 WS0: the watcher also reads the IEX HIST index
(``https://iextrading.com/api/1.0/hist?date=YYYYMMDD``, one small JSON GET per
day for the last ``--iex-days`` calendar days, never following a ``link``) and
reports which days have DEEP / TOPS files and their sizes. That is
informational: it goes to the JSON report (``iex``) and the toast text, and a
separate ``iex_new`` flag; the exit code is decided by Nasdaq alone.

Exit codes: 0 = nothing new, 10 = new usable ITCH 5.0 day(s), 1 = error.
Runbook: docs/runbooks/RUNBOOK_r7_nasdaq_watch.md.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sys
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.marketdata.r7plan import SPENT_DAYS  # noqa: E402

URL = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/"
USER_AGENT = (
    "intraday-alpha-platform r7_watch_nasdaq/1.0 "
    "(+https://github.com/AshJha0/intraday-alpha-platform; read-only listing check)"
)
IEX_HIST_URL = "https://iextrading.com/api/1.0/hist?date={yyyymmdd}"
IEX_FEEDS = ("DEEP", "TOPS")  # "DEEP+" is reported under DEEP
IEX_DAYS = 7
KNOWN = REPO / "research" / "r7" / "nasdaq_listing_known.json"
CANDIDATES = REPO / "research" / "r7" / "r7_candidates.json"
EXIT_NOTHING, EXIT_NEW, EXIT_ERROR = 0, 10, 1

# IIS listing row: ' 6/23/2026  3:17 PM  17894268560 <A HREF="...">name</A>'
_ROW = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s+([AP]M)\s+(\d+|&lt;dir&gt;|<dir>)\s+"
    r'<A HREF="[^"]*">([^<]+)</A>',
    re.IGNORECASE,
)
_S_V50 = re.compile(r"^S(\d{2})(\d{2})(\d{2})-v50\.txt\.gz$")
_NQ50 = re.compile(r"^(\d{2})(\d{2})(\d{4})\.NASDAQ_ITCH50\.gz$")
_ITCH50_MD = re.compile(r"^itch50_(\d{2})_(\d{2})\.gz$")


def fetch_listing(url: str = URL, timeout: float = 30.0) -> str:
    """One polite read-only GET of the directory listing."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URL
        return r.read().decode("utf-8", errors="replace")


def fetch_iex_index(yyyymmdd: str, timeout: float = 30.0) -> str:
    """One polite GET of the IEX HIST index for one day (JSON text; never a file)."""
    url = IEX_HIST_URL.format(yyyymmdd=yyyymmdd)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URL
        return r.read().decode("utf-8", errors="replace")


def iex_dates(today: date, days: int = IEX_DAYS) -> list[str]:
    """UTC today minus 1..days as YYYYMMDD, most recent first."""
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(1, days + 1)]


def parse_iex_index(text: str, yyyymmdd: str) -> list[dict]:
    """DEEP / TOPS entries of one day's index: {feed, version, protocol, size, link}.

    The index is a JSON list of objects (``link``, ``date``, ``feed``, ``version``,
    ``protocol``, ``size``); the undated form is a dict keyed by date, which is
    accepted too. ``DEEP+`` is normalised to ``DEEP``. Other feeds are dropped.
    """
    doc = json.loads(text) if text.strip() else []
    if isinstance(doc, dict):
        doc = doc.get(yyyymmdd, [])
    if not isinstance(doc, list):
        raise ValueError("IEX HIST index is neither a list nor a dict keyed by date")
    out = []
    for e in doc:
        if not isinstance(e, dict):
            continue
        feed = str(e.get("feed", "")).upper().rstrip("+")
        if feed not in IEX_FEEDS:
            continue
        size = e.get("size")
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        out.append(
            {
                "feed": feed,
                "version": str(e.get("version", "")),
                "protocol": str(e.get("protocol", "")),
                "size": size,
                "link": str(e.get("link", "")),
            }
        )
    return sorted(out, key=lambda r: (r["feed"], r["version"], r["size"] or 0))


def iex_report(
    dates: list[str], known_days: set[str], fetch_iex=fetch_iex_index, timeout: float = 30.0
) -> dict:
    """Per-day IEX HIST availability over ``dates``; one GET each, errors recorded.

    ``days_with_data`` are ISO dates with at least one DEEP / TOPS entry;
    ``new_days`` are those not in ``known_days`` (the known-list's ``iex_days``).
    """
    days = []
    for d in dates:
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        rec = {"date": iso, "feeds": [], "error": None}
        try:
            rec["feeds"] = parse_iex_index(fetch_iex(d, timeout), d)
        except Exception as exc:  # noqa: BLE001 - informational, never exit 1
            rec["error"] = f"{type(exc).__name__}: {exc}"
        days.append(rec)
    with_data = [r["date"] for r in days if r["feeds"]]
    return {
        "source": IEX_HIST_URL,
        "days": days,
        "days_with_data": with_data,
        "new_days": [d for d in with_data if d not in known_days],
    }


def parse_listing(text: str) -> list[dict]:
    """Entries as {name, bytes (None for dirs), listed (ISO minute), is_dir}."""
    out = []
    for mo, dd, yy, hh, mi, ap, size, name in _ROW.findall(text):
        h = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
        is_dir = not size.isdigit()
        out.append(
            {
                "name": html.unescape(name).strip(),
                "bytes": None if is_dir else int(size),
                "listed": f"{int(yy):04d}-{int(mo):02d}-{int(dd):02d}T{h:02d}:{mi}",
                "is_dir": is_dir,
            }
        )
    return out


def session_date(name: str, listed: str | None = None) -> str | None:
    """Session date of an ITCH 5.0 full-day file name, else None.

    ``itch50_MM_DD.gz`` carries no year: the listing date's year is used.
    """
    try:
        if m := _S_V50.match(name):
            return date(2000 + int(m[3]), int(m[1]), int(m[2])).isoformat()
        if m := _NQ50.match(name):
            return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
        if (m := _ITCH50_MD.match(name)) and listed:
            return date(int(listed[:4]), int(m[1]), int(m[2])).isoformat()
    except ValueError:
        return None
    return None


def load_known(path: Path = KNOWN) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def candidate_dates(path: Path = CANDIDATES) -> set[str]:
    if not path.exists():
        return set()
    return {d["date"] for d in json.loads(path.read_text(encoding="utf-8")).get("days", [])}


def compare(entries: list[dict], known: dict, candidates: set[str]) -> dict:
    """New entries (classified), size changes and entries no longer listed."""
    known_by = {e["name"]: e for e in known.get("entries", [])}
    new, changed, usable = [], [], []
    for e in entries:
        k = known_by.get(e["name"])
        if k is None:
            sd = None if e["is_dir"] else session_date(e["name"], e["listed"])
            rec = dict(e, itch50_full_day=sd is not None)
            if sd:
                d = date.fromisoformat(sd)
                rec.update(
                    session=sd,
                    weekday=d.strftime("%a"),
                    is_weekday=d.weekday() < 5,
                    spent=sd in SPENT_DAYS,
                    candidate=sd in candidates,
                )
                if rec["is_weekday"] and not rec["spent"] and not rec["candidate"]:
                    usable.append(rec)
            new.append(rec)
        elif k.get("bytes") != e["bytes"]:
            changed.append({"name": e["name"], "known_bytes": k.get("bytes"), "bytes": e["bytes"]})
    names = {e["name"] for e in entries}
    removed = sorted(n for n in known_by if n not in names)
    return {"new": new, "usable_new_days": usable, "changed_size": changed, "removed": removed}


def summarize(report: dict) -> str:
    lines = [
        f"Nasdaq ITCH listing: {report['listed_entries']} entries, checked {report['checked']}"
    ]
    if not (report["new"] or report["changed_size"] or report["removed"]):
        lines.append("nothing new vs the known-list")
    for r in report["new"]:
        if r.get("session"):
            flags = [r["weekday"]] + [k for k in ("spent", "candidate") if r.get(k)]
            lines.append(
                f"NEW ITCH50 day {r['session']} ({', '.join(flags)}): {r['name']} {r['bytes']:,} B"
            )
        else:
            lines.append(f"new entry (not a full-day ITCH 5.0 file): {r['name']}")
    for c in report["changed_size"]:
        lines.append(f"SIZE CHANGED {c['name']}: {c['known_bytes']} -> {c['bytes']}")
    for n in report["removed"]:
        lines.append(f"no longer listed: {n}")
    if report["usable_new_days"]:
        lines.append(
            "Usable new day(s). Review, add to research/r7/r7_candidates.json, then download "
            "manually with tools/r7_download.py --i-have-approval."
        )
    lines.extend(summarize_iex(report.get("iex")))
    return "\n".join(lines)


def summarize_iex(iex: dict | None) -> list[str]:
    """Text lines for the IEX HIST block (empty list when IEX was not checked)."""
    if not iex:
        return []
    n = len(iex["days"])
    lines = [f"IEX HIST: {len(iex['days_with_data'])} of the last {n} days have DEEP/TOPS files"]
    for r in iex["days"]:
        if r["feeds"]:
            parts = [
                f"{f['feed']} {f['version']} {f['size']:,} B"
                if f["size"] is not None
                else f"{f['feed']} {f['version']} (size unknown)"
                for f in r["feeds"]
            ]
            tag = " NEW" if r["date"] in iex["new_days"] else ""
            lines.append(f"  {r['date']}{tag}: {'; '.join(parts)}")
        elif r["error"]:
            lines.append(f"  {r['date']}: error {r['error']}")
    if iex["new_days"]:
        lines.append(
            "New IEX day(s) (informational; not downloaded): " + ", ".join(iex["new_days"])
        )
    return lines


def known_doc(
    entries: list[dict], checked: str, prior: dict | None = None, iex_days: list[str] | None = None
) -> dict:
    """Known-list document; with ``prior`` it only appends entries not yet known.

    ``iex_days`` (ISO dates with IEX HIST data) are merged into the optional
    ``iex_days`` key, which marks IEX days already seen.
    """
    have = list((prior or {}).get("entries", []))
    seen = {e["name"] for e in have}
    for e in entries:
        if e["name"] not in seen:
            keep = {k: e[k] for k in ("name", "bytes", "listed", "is_dir")}
            have.append(keep | {"first_seen": checked})
    doc = {
        "schema": "iap.r7_nasdaq_listing_known/v1",
        "source": URL,
        "updated": checked,
        "note": "Every entry seen in the listing. Append with --update-known after owner review.",
        "entries": sorted(have, key=lambda e: e["name"]),
    }
    seen_iex = sorted(set((prior or {}).get("iex_days", [])) | set(iex_days or []))
    if seen_iex:
        doc["iex_days"] = seen_iex
    return doc


def _toast(title: str, message: str) -> str:
    """Reuse tools/jobs/watchdog.py's notifier."""
    spec = importlib.util.spec_from_file_location(
        "iap_watchdog", REPO / "tools" / "jobs" / "watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iap_watchdog"] = mod
    spec.loader.exec_module(mod)
    return mod.notify(title, message)


def main(
    argv: list[str] | None = None,
    fetch=fetch_listing,
    notifier=None,
    fetch_iex=fetch_iex_index,
    today: date | None = None,
) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--known", type=Path, default=KNOWN)
    ap.add_argument("--candidates", type=Path, default=CANDIDATES)
    ap.add_argument("--json-out", type=Path, help="write the JSON report here")
    ap.add_argument("--listing-file", type=Path, help="parse a saved listing instead of fetching")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--notify", action="store_true", help="toast when usable new days appear")
    ap.add_argument(
        "--update-known", action="store_true", help="append newly seen entries (after review)"
    )
    ap.add_argument(
        "--iex-days",
        type=int,
        default=IEX_DAYS,
        help="read the IEX HIST index for UTC today minus 1..N (one GET per day; 0 = skip)",
    )
    ap.add_argument("--no-iex", action="store_true", help="do not read the IEX HIST index")
    a = ap.parse_args(argv)
    checked = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if a.listing_file:
            text = a.listing_file.read_text(encoding="utf-8-sig")
        else:
            text = fetch(URL, a.timeout)
        entries = parse_listing(text)
        if not entries:
            raise ValueError("listing parsed to zero entries (format changed?)")
        known = load_known(a.known) if a.known.exists() else {"entries": []}
        report = {"checked": checked, "source": URL, "listed_entries": len(entries)}
        report |= compare(entries, known, candidate_dates(a.candidates))
    except Exception as exc:  # noqa: BLE001 - any failure is exit 1
        print(f"r7_watch_nasdaq: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    report["iex"] = None
    report["iex_new"] = False
    if not a.no_iex and a.iex_days > 0:
        dates = iex_dates(today or datetime.now(UTC).date(), a.iex_days)
        report["iex"] = iex_report(dates, set(known.get("iex_days", [])), fetch_iex, a.timeout)
        report["iex_new"] = bool(report["iex"]["new_days"])
    if a.json_out:
        a.json_out.parent.mkdir(parents=True, exist_ok=True)
        a.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(summarize(report))
    if a.update_known:
        doc = known_doc(
            entries,
            checked[:10],
            known if known.get("entries") else None,
            iex_days=(report["iex"] or {}).get("days_with_data"),
        )
        a.known.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print(f"known-list updated: {a.known} ({len(doc['entries'])} entries)")
    if a.notify and (report["usable_new_days"] or report["iex_new"]):
        parts, title = [], []
        if report["usable_new_days"]:
            title.append(f"{len(report['usable_new_days'])} new Nasdaq ITCH day(s)")
            parts.append("Nasdaq: " + ", ".join(r["session"] for r in report["usable_new_days"]))
        if report["iex_new"]:
            title.append(f"{len(report['iex']['new_days'])} new IEX HIST day(s)")
            parts.append("IEX: " + ", ".join(report["iex"]["new_days"]))
        (notifier or _toast)("; ".join(title), "; ".join(parts))
    # The exit code is Nasdaq's alone: IEX days are informational (``iex_new``).
    return EXIT_NEW if report["usable_new_days"] else EXIT_NOTHING


if __name__ == "__main__":
    sys.exit(main())

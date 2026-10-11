"""Watch Nasdaq's free ITCH sample directory for new full-day files (R7).

Read-only: one GET of the directory listing, never a download (downloads stay
manual via ``tools/r7_download.py --i-have-approval``). The listing is compared
to the committed known-list ``research/r7/nasdaq_listing_known.json``.

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
from datetime import UTC, date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.marketdata.r7plan import SPENT_DAYS  # noqa: E402

URL = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/"
USER_AGENT = (
    "intraday-alpha-platform r7_watch_nasdaq/1.0 "
    "(+https://github.com/AshJha0/intraday-alpha-platform; read-only listing check)"
)
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
    return "\n".join(lines)


def known_doc(entries: list[dict], checked: str, prior: dict | None = None) -> dict:
    """Known-list document; with ``prior`` it only appends entries not yet known."""
    have = list((prior or {}).get("entries", []))
    seen = {e["name"] for e in have}
    for e in entries:
        if e["name"] not in seen:
            keep = {k: e[k] for k in ("name", "bytes", "listed", "is_dir")}
            have.append(keep | {"first_seen": checked})
    return {
        "schema": "iap.r7_nasdaq_listing_known/v1",
        "source": URL,
        "updated": checked,
        "note": "Every entry seen in the listing. Append with --update-known after owner review.",
        "entries": sorted(have, key=lambda e: e["name"]),
    }


def _toast(title: str, message: str) -> str:
    """Reuse tools/jobs/watchdog.py's notifier."""
    spec = importlib.util.spec_from_file_location(
        "iap_watchdog", REPO / "tools" / "jobs" / "watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["iap_watchdog"] = mod
    spec.loader.exec_module(mod)
    return mod.notify(title, message)


def main(argv: list[str] | None = None, fetch=fetch_listing, notifier=None) -> int:
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
    if a.json_out:
        a.json_out.parent.mkdir(parents=True, exist_ok=True)
        a.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(summarize(report))
    if a.update_known:
        doc = known_doc(entries, checked[:10], known if known.get("entries") else None)
        a.known.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print(f"known-list updated: {a.known} ({len(doc['entries'])} entries)")
    if report["usable_new_days"]:
        if a.notify:
            days = ", ".join(r["session"] for r in report["usable_new_days"])
            (notifier or _toast)(f"{len(report['usable_new_days'])} new Nasdaq ITCH day(s)", days)
        return EXIT_NEW
    return EXIT_NOTHING


if __name__ == "__main__":
    sys.exit(main())

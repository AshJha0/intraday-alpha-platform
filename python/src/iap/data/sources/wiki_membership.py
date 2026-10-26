"""Point-in-time index membership from Wikipedia revision history (U0 DR-6; a **labelled proxy**).

The English Wikipedia pages ``List of S&P 500 companies`` and ``Nasdaq-100`` carry
a constituent table that editors update around each index change.  The
MediaWiki revision API gives every past revision of those pages with its
timestamp, so the table *as it stood on date D* is recoverable, which is what a
point-in-time universe needs (``iap.data.pit``).  It is a proxy: an editor may
lag or lead the index provider's effective date by days, early revisions are
incomplete, and vandalised revisions exist.  Every interval this module
produces therefore carries ``label = "wikipedia-proxy"`` and the revision id it
came from, and DD1's membership-quality test (coverage report, section 5)
decides whether the proxy is good enough per family; failing it downgrades the
family, it never relaxes the test (DR-6 stop condition).

API calls (``https://en.wikipedia.org/w/api.php``, ``format=json&formatversion=2``):

- ``action=query&prop=revisions&titles=<page>&rvprop=ids|timestamp|size&rvlimit=max&rvdir=newer``
  with ``rvcontinue`` paging: the revision list (ids and timestamps only).
- ``action=query&prop=revisions&revids=<id>&rvprop=ids|timestamp|content&rvslots=main``:
  one revision's wikitext, parsed for ticker symbols.

``coverage()`` lists the revisions (counts, first and last timestamps) and then
samples one revision per calendar month, parses each, and reports how many
parse to a plausible constituent count, the earliest month that does (the
"earliest reliable revision"), and the number of membership intervals those
snapshots imply.  It returns counts only: no symbol list leaves the call.
Wikimedia asks for a descriptive User-Agent with contact and serial requests;
the limiter holds one request per second.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from iap.data.contracts import AvailabilityRule, DatasetContract, dataset
from iap.data.sources.base import Coverage, SourceAdapter, SourceDeclaration

__all__ = [
    "DECLARATION",
    "EXTRA_TITLES",
    "PAGES",
    "PROXY_LABEL",
    "MembershipInterval",
    "WikiMembershipSource",
    "intervals_from_snapshots",
    "parse_constituents",
]

API = "https://en.wikipedia.org/w/api.php"
PROXY_LABEL = "wikipedia-proxy"
#: index id -> (page title, plausible constituent-count range for a "reliable" revision)
PAGES: dict[str, tuple[str, tuple[int, int]]] = {
    "sp500": ("List of S&P 500 companies", (480, 520)),
    "ndx100": ("Nasdaq-100", (95, 110)),
}
#: Further pages that carry an index's constituent table, preferred over the main page
#: on a day both were edited.  The Nasdaq-100 table moved to its own list page; the
#: main page's recent revisions have no table (probe run 40320099644: 0 symbols, D-105).
EXTRA_TITLES: dict[str, tuple[str, ...]] = {
    "ndx100": ("List of NASDAQ-100 companies",),
}

DECLARATION = SourceDeclaration(
    contract=dataset(
        DatasetContract(
            dataset_id="wikipedia.index_membership",
            source="wikipedia",
            # a revision saved on D describes membership known on D; usable from the session
            # after D plus one (D-28 default: two sessions when D is a session)
            availability=AvailabilityRule("publication_date", 2),
            adjustment="none",
            missing_policy="ineligible",
            route="daily_pit",
            leakage_test_id="LK-CAUSALITY",
            fields=("index", "symbol", "start", "end", "revision_id", "label"),
        )
    ),
    licence_id="wikimedia",
    access="public MediaWiki API; CC BY-SA 4.0 text; descriptive User-Agent, serial requests",
    user_agent=(
        "intraday-alpha-platform/2.0 (https://github.com/AshJha0/intraday-alpha-platform; "
        "point-in-time index membership research; serial requests)"
    ),
    requests_per_second=1.0,
)


@dataclass(frozen=True, slots=True)
class MembershipInterval:
    index: str
    symbol: str
    start: _dt.date
    end: _dt.date | None
    revision_id: int
    label: str = PROXY_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "revision_id": self.revision_id,
            "label": self.label,
        }


# ------------------------------------------------------------------ wikitext parsing
_TEMPLATE_TICKER = re.compile(
    # {{NYSE|MMM}}, {{NyseSymbol|MMM}}, {{NasdaqSymbol|AAPL}}, {{Nasdaq link|AAPL}}, {{Cboe|X}} ...
    r"\{\{\s*[A-Za-z ]*?(?:nyse|nasdaq|cboe|bats|amex)[A-Za-z ]*?\s*\|\s*"
    r"([A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?)\s*(?:\||\}\})",
    re.I,
)
_LINK_TICKER = re.compile(
    r"\[\[(?:NYSE|NASDAQ|Nasdaq|NYSE Arca|NYSE American|Cboe)\s*:\s*([A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?)"
)
_EXT_TICKER = re.compile(
    r"\[https?://[^\s\]]*(?:nyse|nasdaq|cboe)[^\s\]]*\s+([A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?)\]"
)
_PLAIN_TICKER = re.compile(r"^\s*'*\s*([A-Z][A-Z0-9]{0,4}(?:[.\-][A-Z])?)\s*'*\s*$")
_TABLE = re.compile(r"\{\|(.*?)\n\|\}", re.S)


def _quote(value: str) -> str:
    """Percent-encode a query value (``|`` and ``:`` kept, as the API accepts them);
    written out because ``iap.data`` may not import ``urllib``."""
    keep = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~|:")
    return "".join(
        c if c in keep else "".join(f"%{b:02X}" for b in c.encode("utf-8")) for c in value
    )


def _strip(cell: str) -> str:
    cell = re.sub(r"<[^>]+>", "", cell)
    cell = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", cell)
    cell = re.sub(r"\{\{[^}]*\}\}", "", cell)
    return html.unescape(cell).strip()


def _row_cells(row: str) -> list[str]:
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    return [c for c in re.split(r"\n\s*\|\s*|\s*\|\|\s*", body) if c is not None]


def parse_constituents(wikitext: str) -> frozenset[str]:
    """The ticker symbols of the largest constituent table in ``wikitext``.

    Four spellings are recognised, newest first: exchange templates
    (``{{NYSE|MMM}}``), exchange links (``[[NYSE:MMM]]``), external links whose
    text is the ticker, and a bare upper-case first cell.  The largest table
    wins (the page also has a "recent changes" table with a few rows).
    """
    best: frozenset[str] = frozenset()
    for table in _TABLE.findall(wikitext):
        found: set[str] = set()
        for row in re.split(r"\n\|-[^\n]*", table)[1:]:
            if row.lstrip().startswith("!"):
                continue
            cells = _row_cells(row)
            if not cells:
                continue
            sym = None
            head = "\n".join(cells[:2])
            for rx in (_TEMPLATE_TICKER, _LINK_TICKER, _EXT_TICKER):
                m = rx.search(head)
                if m and m.group(1).upper() == m.group(1):
                    sym = m.group(1)
                    break
            if sym is None:
                m = _PLAIN_TICKER.match(_strip(cells[0]))
                if m:
                    sym = m.group(1)
            if sym:
                found.add(sym.replace("-", "."))
        if len(found) > len(best):
            best = frozenset(found)
    return best


def intervals_from_snapshots(
    index: str, snapshots: Iterable[tuple[_dt.date, int, frozenset[str]]]
) -> list[MembershipInterval]:
    """Dated snapshots ``(date, revision_id, symbols)`` -> membership intervals.

    A symbol present in consecutive snapshots is one interval from its first
    snapshot date; the interval ends on the first later snapshot date that
    lacks it (``end`` is exclusive) or stays open.  Snapshot dates are the
    revision dates: the availability lag of the contract applies on top.
    """
    open_: dict[str, tuple[_dt.date, int]] = {}
    out: list[MembershipInterval] = []
    for date, rev, symbols in sorted(snapshots, key=lambda s: s[0]):
        for sym in symbols:
            if sym not in open_:
                open_[sym] = (date, rev)
        for sym in list(open_):
            if sym not in symbols:
                start, r0 = open_.pop(sym)
                out.append(MembershipInterval(index, sym, start, date, r0))
    for sym, (start, r0) in open_.items():
        out.append(MembershipInterval(index, sym, start, None, r0))
    return sorted(out, key=lambda i: (i.start, i.symbol))


# ------------------------------------------------------------------ the adapter
class WikiMembershipSource(SourceAdapter):
    declaration = DECLARATION

    def _api(self, **params: str) -> dict[str, Any]:
        q = "&".join(f"{k}={_quote(v)}" for k, v in params.items())
        url = f"{API}?format=json&formatversion=2&{q}"
        return json.loads(self._get_text(url))

    def list_revisions(self, title: str, *, max_pages: int = 200) -> list[tuple[int, str]]:
        """Every revision ``(id, ISO timestamp)`` of ``title``, oldest first."""
        out: list[tuple[int, str]] = []
        cont: dict[str, str] = {}
        for _ in range(max_pages):
            doc = self._api(
                action="query",
                prop="revisions",
                titles=title,
                rvprop="ids|timestamp",
                rvlimit="max",
                rvdir="newer",
                **cont,
            )
            pages = doc.get("query", {}).get("pages", [])
            for page in pages:
                for rev in page.get("revisions", []):
                    out.append((int(rev["revid"]), str(rev["timestamp"])))
            if "continue" in doc and "rvcontinue" in doc["continue"]:
                cont = {"rvcontinue": doc["continue"]["rvcontinue"]}
            else:
                break
        return out

    def revision_symbols(self, revid: int) -> frozenset[str]:
        doc = self._api(
            action="query",
            prop="revisions",
            revids=str(revid),
            rvprop="ids|timestamp|content",
            rvslots="main",
        )
        for page in doc.get("query", {}).get("pages", []):
            for rev in page.get("revisions", []):
                slot = rev.get("slots", {}).get("main", {})
                text = slot.get("content") or rev.get("content") or ""
                return parse_constituents(text)
        return frozenset()

    @staticmethod
    def monthly_sample(revisions: list[tuple[int, str]]) -> list[tuple[int, str]]:
        """The first revision of each calendar month (oldest first)."""
        seen: set[str] = set()
        out: list[tuple[int, str]] = []
        for rid, ts in revisions:
            key = ts[:7]
            if key not in seen:
                seen.add(key)
                out.append((rid, ts))
        return out

    def coverage(
        self, *, indices: Iterable[str] = ("sp500", "ndx100"), sample: bool = True
    ) -> Coverage:
        cov = self._coverage()
        for index in indices:
            title, (lo, hi) = PAGES[index]
            revs = self.list_revisions(title)
            cov.counts[f"{index}_revisions"] = len(revs)
            if revs:
                cov.ranges[f"{index}_revision_dates"] = (revs[0][1][:10], revs[-1][1][:10])
            if not sample or not revs:
                continue
            months = self.monthly_sample(revs)
            cov.counts[f"{index}_months_sampled"] = len(months)
            snapshots: list[tuple[_dt.date, int, frozenset[str]]] = []
            reliable_first: str | None = None
            parsed_sizes: list[int] = []
            for rid, ts in months:
                syms = self.revision_symbols(rid)
                parsed_sizes.append(len(syms))
                if lo <= len(syms) <= hi:
                    if reliable_first is None:
                        reliable_first = ts[:10]
                    snapshots.append((_dt.date.fromisoformat(ts[:10]), rid, syms))
            cov.counts[f"{index}_months_reliable"] = len(snapshots)
            cov.counts[f"{index}_months_unparsed_or_implausible"] = len(months) - len(snapshots)
            if reliable_first:
                cov.ranges[f"{index}_reliable_revisions"] = (reliable_first, months[-1][1][:10])
            intervals = intervals_from_snapshots(index, snapshots)
            cov.counts[f"{index}_membership_intervals"] = len(intervals)
            cov.counts[f"{index}_distinct_symbols"] = len({i.symbol for i in intervals})
            cov.counts[f"{index}_closed_intervals"] = sum(1 for i in intervals if i.end)
            if parsed_sizes:
                cov.counts[f"{index}_max_parsed_rows"] = max(parsed_sizes)
        cov.notes.append(
            f"label={PROXY_LABEL}: editor timing is not the index provider's effective date; "
            "reliable = parsed constituent count within the plausible range per index; monthly "
            "sampling, so interval starts are month-granular here (ingestion parses every revision)"
        )
        cov.requests_made = self.requests_made
        return cov

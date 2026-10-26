"""SEC EDGAR earnings-announcement calendar: 8-K item 2.02 with acceptance timestamps (D4 input).

D4 on the free route has only filing dates (``edgar.filings``), so it cannot
tell an announcement made at 07:00 from one made at 16:05.  This adapter builds
the missing input from EDGAR alone, still free and point in time:

1. the quarterly **full index** (``form.idx``) lists every ``8-K`` and
   ``8-K/A`` with its CIK and path; the index carries no item numbers, so
2. each candidate filing's **header** (``<accession>-index-headers.html``, a few
   kilobytes; the filing body is never requested) is read for
   ``CONFORMED SUBMISSION TYPE``, the ``ITEM INFORMATION`` lines and
   ``<ACCEPTANCE-DATETIME>``;
3. a filing is an **announcement** when one of its items is 2.02 "Results of
   Operations and Financial Condition" (item 12 under the pre-August-2004
   numbering, same wording, also accepted).

Timing.  ``ACCEPTANCE-DATETIME`` is ``YYYYMMDDHHMMSS`` in **US Eastern wall
time** (EDT or EST as in force that day), not UTC.  (The submissions API writes
the same instant with a ``Z`` suffix that is not UTC either; this adapter does
not use it.)  The timing class is decided on Eastern wall time, so it needs no
DST rule: ``before_open`` < 09:30 <= ``during`` < 16:00 <= ``after_close``.  The
UTC instant is recorded beside it with the US DST rule written out below
(:func:`eastern_to_utc`, no tz database needed).  Acceptance on a non-session
day (weekend, holiday) is ``non_session``.

Point in time.  ``as_of_session`` is the last session whose information set
the announcement belongs to, and the contract's lag of one session makes the
first session the announcement may be traded on:

=================  ======================================  ==========================
timing             ``as_of_session``                       first usable session
=================  ======================================  ==========================
before_open        the previous session                    the acceptance day
during             the acceptance day                      the next session
after_close        the acceptance day                      the next session
non_session        the previous session                    the next session
=================  ======================================  ==========================

A ``during`` filing is treated as known only after that session (conservative:
the intraday reaction is not tradable on daily bars).  EDGAR's own ``Date
Filed`` can differ from the acceptance date (filings accepted after 17:30 are
dated the next business day); the acceptance timestamp decides, never the date
filed.

Amendments.  An ``8-K/A`` repeats or corrects an announcement already made.  It
never creates a second event when an original item-2.02 ``8-K`` of the same CIK
was accepted within :data:`AMENDMENT_WINDOW_DAYS` before it; it is kept (flagged
``amendment_only``) only when no such original exists in the data, because then
the amendment is the first public record this route has.

The access rules are those of :mod:`iap.data.sources.edgar` (declarative
User-Agent, at most 10 requests per second; the limiter runs at 8).
``coverage()`` returns counts only; ``fetch()`` is the runner-only, sealed-target
call of :class:`~iap.data.sources.base.SourceAdapter`; the ingestion job is
``tools/ingest/dd1_events.py``.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from iap.data.contracts import AvailabilityRule, DatasetContract, dataset
from iap.data.sources import edgar
from iap.data.sources.base import Coverage, SourceAdapter, SourceDeclaration, Transport

__all__ = [
    "AMENDMENT_WINDOW_DAYS",
    "DECLARATION",
    "EVENT_FIELDS",
    "Announcement",
    "EdgarEventsSource",
    "FilingHeader",
    "IndexRow",
    "classify",
    "dedupe_amendments",
    "eastern_to_utc",
    "header_url",
    "parse_form_index_8k",
    "parse_header",
]

FULL_INDEX_URL = edgar.FULL_INDEX_URL
HEADER_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index-headers.html"
FORMS_8K = ("8-K", "8-K/A")
#: Item 2.02 since 23 August 2004; item 12 before, same title.
EARNINGS_ITEMS = ("2.02", "12")
EARNINGS_TITLE = "results of operations and financial condition"
AMENDMENT_WINDOW_DAYS = 30
OPEN = _dt.time(9, 30)
CLOSE = _dt.time(16, 0)
EVENT_FIELDS = (
    "cik",
    "accession",
    "form",
    "items",
    "acceptance_et",
    "acceptance_utc",
    "date_filed",
    "timing",
    "as_of_session",
    "usable_session",
    "amendment_only",
)

DECLARATION = SourceDeclaration(
    contract=dataset(
        DatasetContract(
            dataset_id="edgar.earnings_8k",
            source="sec_edgar",
            # the record's period is ``as_of_session``; usable one session later
            # (table in the module docstring)
            availability=AvailabilityRule("publication_date", 1),
            adjustment="none",
            missing_policy="ineligible",
            route="daily_pit",
            leakage_test_id="LK-CAUSALITY",
            fields=EVENT_FIELDS,
        )
    ),
    licence_id="sec_edgar",
    access="public; User-Agent 'name contact'; <= 10 requests/second (SEC access rules)",
    user_agent=edgar.DECLARATION.user_agent,
    requests_per_second=8.0,
)


# ------------------------------------------------------------------ the index --
@dataclass(frozen=True, slots=True)
class IndexRow:
    form: str
    cik: int
    date_filed: str
    path: str

    @property
    def accession(self) -> str:
        """``0000320193-24-000006`` from ``edgar/data/320193/0000320193-24-000006.txt``."""
        return self.path.rsplit("/", 1)[-1].removesuffix(".txt")


_IDX_LINE = re.compile(r"^(.{12})(.{62})(.{12})(.{12})(.*)$")


def parse_form_index_8k(text: str, ciks: Iterable[int] | None = None) -> list[IndexRow]:
    """The ``8-K`` and ``8-K/A`` rows of one ``form.idx`` (fixed width, header up to
    the dashed line), optionally restricted to ``ciks``."""
    want = set(ciks) if ciks is not None else None
    rows: list[IndexRow] = []
    started = False
    for line in text.splitlines():
        if not started:
            started = line.startswith("---")
            continue
        m = _IDX_LINE.match(line)
        if not m:
            continue
        form, _company, cik_s, date, path = (g.strip() for g in m.groups())
        if form not in FORMS_8K or not cik_s.isdigit():
            continue
        cik = int(cik_s)
        if want is not None and cik not in want:
            continue
        try:
            filed = _dt.date.fromisoformat(date).isoformat()
        except ValueError:
            continue
        rows.append(IndexRow(form, cik, filed, path))
    return rows


def header_url(row: IndexRow) -> str:
    acc = row.accession
    return HEADER_URL.format(cik=row.cik, acc_nodash=acc.replace("-", ""), acc=acc)


# ------------------------------------------------------------------ the header --
@dataclass(frozen=True, slots=True)
class FilingHeader:
    accession: str
    form: str
    cik: int
    acceptance_et: _dt.datetime  # naive, US Eastern wall time
    items: tuple[str, ...]  # item numbers where known, else the item titles, lower-cased
    date_filed: str

    @property
    def is_earnings(self) -> bool:
        return any(i in EARNINGS_ITEMS or EARNINGS_TITLE in i for i in self.items)


_TAG = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")
_KV = re.compile(r"^\s*([A-Z][A-Z \-/]+?):\s*(.*?)\s*$")
_ITEM_NO = re.compile(r"^(?:item\s+)?(\d{1,2}\.\d{2}|\d{1,2})\b")


def parse_header(text: str) -> FilingHeader:
    """One EDGAR SEC-header (``-index-headers.html``, ``.hdr.sgml`` text or the top
    of the full ``.txt``).  HTML tags other than ``<ACCEPTANCE-DATETIME>`` are
    dropped; the first ``CENTRAL INDEX KEY`` (the filer) is the CIK.  Raises
    ``ValueError`` when the acceptance timestamp or the form type is missing."""
    text = html.unescape(text)  # -index-headers.html shows the SGML tags escaped
    m = _TAG.search(text)
    if not m:
        raise ValueError("no <ACCEPTANCE-DATETIME> in the header")
    acc_dt = _dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    plain = re.sub(r"<[^>]+>", "\n", text)
    fields: dict[str, str] = {}
    items: list[str] = []
    for line in plain.splitlines():
        kv = _KV.match(line)
        if not kv:
            continue
        key, val = kv.group(1).strip(), kv.group(2).strip()
        if key == "ITEM INFORMATION":
            items.append(_item_key(val))
        elif key not in fields:
            fields[key] = val
    form = fields.get("CONFORMED SUBMISSION TYPE", "")
    if not form:
        raise ValueError("no CONFORMED SUBMISSION TYPE in the header")
    filed = fields.get("FILED AS OF DATE", "")
    return FilingHeader(
        accession=fields.get("ACCESSION NUMBER", ""),
        form=form,
        cik=int(fields.get("CENTRAL INDEX KEY", "0") or 0),
        acceptance_et=acc_dt,
        items=tuple(items),
        date_filed=f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}" if len(filed) == 8 else "",
    )


def _item_key(value: str) -> str:
    v = value.strip().lower()
    m = _ITEM_NO.match(v)
    return m.group(1) if m else v


# -------------------------------------------------------------------- timing --
def _nth_sunday(year: int, month: int, n: int) -> _dt.date:
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)
    return d - _dt.timedelta(days=(d.weekday() + 1) % 7)


def _dst_bounds(year: int) -> tuple[_dt.datetime, _dt.datetime]:
    """Local wall-clock start and end of US daylight time (Energy Policy Act 2005
    from 2007: second Sunday of March to first Sunday of November; 1987-2006:
    first Sunday of April to last Sunday of October), both at 02:00."""
    if year >= 2007:
        start, end = _nth_sunday(year, 3, 2), _nth_sunday(year, 11, 1)
    else:
        start, end = _nth_sunday(year, 4, 1), _last_sunday(year, 10)
    two = _dt.time(2, 0)
    return _dt.datetime.combine(start, two), _dt.datetime.combine(end, two)


def eastern_to_utc(local: _dt.datetime) -> _dt.datetime:
    """US Eastern wall time to aware UTC.  The skipped spring hour (02:00-02:59)
    is read as EST; the repeated autumn hour (01:00-01:59) as its first, EDT,
    occurrence.  EDGAR accepts no filings at either hour, so neither choice
    reaches a real record."""
    start, end = _dst_bounds(local.year)
    dst = start + _dt.timedelta(hours=1) <= local < end
    if local.date() == end.date() and _dt.time(1, 0) <= local.time() < _dt.time(2, 0):
        dst = True
    offset = _dt.timedelta(hours=4 if dst else 5)
    return (local + offset).replace(tzinfo=_dt.UTC)


def classify(
    acceptance_et: _dt.datetime, sessions: Sequence[str]
) -> tuple[str, str | None, str | None]:
    """``(timing, as_of_session, usable_session)`` for one acceptance time against
    the sorted ISO session list.  ``None`` where the calendar does not reach."""
    import bisect

    day = acceptance_et.date().isoformat()
    t = acceptance_et.time()
    i = bisect.bisect_left(sessions, day)
    on_session = i < len(sessions) and sessions[i] == day
    prev_s = sessions[i - 1] if i > 0 else None
    if not on_session:
        nxt = sessions[i] if i < len(sessions) else None
        return "non_session", prev_s, nxt
    nxt = sessions[i + 1] if i + 1 < len(sessions) else None
    if t < OPEN:
        return "before_open", prev_s, day
    if t < CLOSE:
        return "during", day, nxt
    return "after_close", day, nxt


# ---------------------------------------------------------------- the events --
@dataclass(frozen=True, slots=True)
class Announcement:
    header: FilingHeader
    timing: str
    as_of_session: str | None
    usable_session: str | None
    amendment_only: bool = False

    def row(self) -> dict[str, Any]:
        h = self.header
        return {
            "cik": h.cik,
            "accession": h.accession,
            "form": h.form,
            "items": ";".join(h.items),
            "acceptance_et": h.acceptance_et.isoformat(),
            "acceptance_utc": eastern_to_utc(h.acceptance_et).isoformat(),
            "date_filed": h.date_filed,
            "timing": self.timing,
            "as_of_session": self.as_of_session or "",
            "usable_session": self.usable_session or "",
            "amendment_only": int(self.amendment_only),
        }


def dedupe_amendments(
    headers: Iterable[FilingHeader], *, window_days: int = AMENDMENT_WINDOW_DAYS
) -> tuple[list[FilingHeader], Counter[str]]:
    """Earnings headers with redundant ``8-K/A`` removed (module docstring).
    Returns the kept headers in acceptance order and counts of what was dropped."""
    earn = sorted(
        (h for h in headers if h.is_earnings), key=lambda h: (h.acceptance_et, h.accession)
    )
    originals: dict[int, list[_dt.datetime]] = {}
    for h in earn:
        if not h.form.endswith("/A"):
            originals.setdefault(h.cik, []).append(h.acceptance_et)
    kept: list[FilingHeader] = []
    seen: set[str] = set()
    stats: Counter[str] = Counter()
    win = _dt.timedelta(days=window_days)
    for h in earn:
        if h.accession and h.accession in seen:
            stats["duplicate_accession"] += 1
            continue
        seen.add(h.accession)
        if h.form.endswith("/A") and any(
            _dt.timedelta(0) <= h.acceptance_et - t <= win for t in originals.get(h.cik, ())
        ):
            stats["amendment_of_original"] += 1
            continue
        kept.append(h)
    return kept, stats


def announcements(
    headers: Iterable[FilingHeader], sessions: Sequence[str]
) -> tuple[list[Announcement], Counter[str]]:
    kept, stats = dedupe_amendments(headers)
    out = []
    for h in kept:
        timing, as_of, usable = classify(h.acceptance_et, sessions)
        out.append(Announcement(h, timing, as_of, usable, amendment_only=h.form.endswith("/A")))
        stats[timing] += 1
    return out, stats


# ----------------------------------------------------------------- the adapter --
class EdgarEventsSource(SourceAdapter):
    declaration = DECLARATION

    def __init__(self, transport: Transport | None = None, **kw: Any) -> None:
        super().__init__(transport, **kw)

    def index_rows(
        self, year: int, quarter: int, ciks: Iterable[int] | None = None
    ) -> list[IndexRow]:
        return parse_form_index_8k(
            self._get_text(FULL_INDEX_URL.format(year=year, quarter=quarter)), ciks
        )

    def header(self, row: IndexRow) -> FilingHeader:
        return parse_header(self._get_text(header_url(row)))

    def coverage(
        self, *, year: int, quarter: int, ciks: Iterable[int] | None = None, max_headers: int = 50
    ) -> Coverage:
        """One quarter's 8-K counts from the index, and the item-2.02 share and timing
        mix over the first ``max_headers`` headers.  Counts only."""
        cov = self._coverage()
        rows = self.index_rows(year, quarter, ciks)
        cov.counts["index_8k"] = sum(r.form == "8-K" for r in rows)
        cov.counts["index_8k_a"] = sum(r.form == "8-K/A" for r in rows)
        heads = [self.header(r) for r in rows[:max_headers]]
        cov.counts["headers_read"] = len(heads)
        cov.counts["headers_item_202"] = sum(h.is_earnings for h in heads)
        timing = Counter(
            "before_open"
            if h.acceptance_et.time() < OPEN
            else "during"
            if h.acceptance_et.time() < CLOSE
            else "after_close"
            for h in heads
            if h.is_earnings
        )
        for k, v in timing.items():
            cov.counts[f"item_202_{k}"] = v
        if rows:
            ds = sorted(r.date_filed for r in rows)
            cov.ranges["date_filed"] = (ds[0], ds[-1])
        cov.notes.append(
            "headers only (-index-headers.html); filing bodies never requested; timing by "
            "Eastern wall clock (weekends and holidays not separated here)"
        )
        cov.requests_made = self.requests_made
        return cov

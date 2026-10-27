"""Session days: the event calendar and seeded, stratified day sampling (R1).

Up to v1.8.0 the real-data sessions were picked by hand: five of the seven
2019 ITCH days are the 30th of a month, two of them (2019-01-30 and
2019-10-30) are FOMC decision days and 2019-12-30 is a holiday-thin
session.  An IC measured on such a sample is partly an IC of the event
afternoons.  This module makes the choice of days an explicit, seeded,
recorded step and lets every report be read with and without the event
days:

- :data:`FOMC_DAYS` / :data:`HOLIDAY_THIN_DAYS` — read (v1.13, R-P2-9) from
  ``configs/reference/event_calendar.json``, which records a ``source`` and
  ``as_of`` per year; :func:`event_calendar_for_year` fails closed on a year
  it does not cover.  Extend the file when a session outside these years is
  ingested.
- :func:`day_tags` — ``{"fomc", "holiday_thin"}`` subset for one date.
- :func:`session_days` — the UTC session date of each row timestamp (US
  equity sessions, 13:30-20:00 UTC, never straddle UTC midnight).
- :func:`stratified_day_sample` — a deterministic draw of ``n`` days from a
  candidate list, stratified by month (default), weekday or event tag, with
  the seed, the strata and the allocation in the returned record.
- :func:`iap.marketdata.manifest_records.record_day_sampling` writes that record into
  a real dataset's ``dataset.json`` (it lived here until v2.1 Track 3; it
  writes a marketdata manifest, so it moved to the marketdata package).
- :func:`iap.marketdata.manifest_records.book_scope_for_dataset` (R4) reads
  the book scope from the manifest; it moved with the sampling record.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np

NS_DAY = 86_400 * 1_000_000_000

#: The event calendar file (v1.13, R-P2-9): per-year FOMC and holiday-thin
#: days with their ``source`` and ``as_of``.  ``IAP_EVENT_CALENDAR``
#: overrides the path.
EVENT_CALENDAR_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "reference" / "event_calendar.json"
)


class EventCalendarError(ValueError):
    """The event calendar is missing, malformed or does not cover a year."""


def _calendar_path() -> Path:
    env = os.environ.get("IAP_EVENT_CALENDAR")
    return Path(env) if env else EVENT_CALENDAR_PATH


def load_event_calendar(path: str | Path | None = None) -> dict:
    """The calendar document; every year must carry ``fomc``,
    ``holiday_thin``, ``source`` and ``as_of`` (fail closed)."""
    p = Path(path) if path is not None else _calendar_path()
    if not p.is_file():
        raise EventCalendarError(f"event calendar not found: {p}")
    doc = json.loads(p.read_text(encoding="utf-8"))
    years = doc.get("years")
    if not isinstance(years, dict) or not years:
        raise EventCalendarError(f"{p}: no 'years'")
    for year, entry in years.items():
        for key in ("fomc", "holiday_thin", "source", "as_of"):
            if key not in entry:
                raise EventCalendarError(f"{p}: year {year} has no {key!r}")
        for key in ("fomc", "holiday_thin"):
            for d in entry[key]:
                if _dt.date.fromisoformat(d).year != int(year):
                    raise EventCalendarError(f"{p}: {d} listed under year {year}")
    return doc


def event_calendar_for_year(year: int, path: str | Path | None = None) -> dict:
    """One year's entry; raises :class:`EventCalendarError` for a year the
    calendar does not cover (an uncovered year is not an event-free one)."""
    years = load_event_calendar(path)["years"]
    entry = years.get(str(int(year)))
    if entry is None:
        raise EventCalendarError(
            f"event calendar does not cover {int(year)} (covered: {sorted(years)}); "
            "add the year with its source to configs/reference/event_calendar.json"
        )
    return entry


def _calendar_days(key: str) -> frozenset[str]:
    try:
        years = load_event_calendar()["years"]
    except EventCalendarError:
        return frozenset()
    return frozenset(d for entry in years.values() for d in entry[key])


#: FOMC statement days (scheduled meetings, decision day), read from the
#: event calendar file.
FOMC_DAYS: frozenset[str] = _calendar_days("fomc")

#: Early closes and holiday-thin sessions, read from the event calendar file.
HOLIDAY_THIN_DAYS: frozenset[str] = _calendar_days("holiday_thin")

#: Tags an event-day split can exclude.
EVENT_TAGS = ("fomc", "holiday_thin")
#: Stratification keys of :func:`stratified_day_sample`.
STRATA = ("month", "weekday", "event", "none")
SAMPLING_METHOD = "stratified_v1"


def _iso(day: str | _dt.date) -> str:
    if isinstance(day, _dt.date):
        return day.isoformat()
    return _dt.date.fromisoformat(str(day)).isoformat()


def day_tags(day: str | _dt.date) -> frozenset[str]:
    """The event tags of one session date."""
    d = _iso(day)
    tags = set()
    if d in FOMC_DAYS:
        tags.add("fomc")
    if d in HOLIDAY_THIN_DAYS:
        tags.add("holiday_thin")
    return frozenset(tags)


def session_days(ts_ns: np.ndarray) -> np.ndarray:
    """Integer UTC day index (days since the epoch) of each timestamp."""
    return np.asarray(ts_ns, dtype=np.int64) // NS_DAY


def day_index_to_iso(day: int) -> str:
    return (_dt.date(1970, 1, 1) + _dt.timedelta(days=int(day))).isoformat()


def event_day_mask(ts_ns: np.ndarray, tags: Iterable[str] = EVENT_TAGS) -> np.ndarray:
    """True for rows whose session date carries any of ``tags``."""
    want = set(tags)
    unknown = want - set(EVENT_TAGS)
    if unknown:
        raise ValueError(f"unknown event tags {sorted(unknown)}; known: {EVENT_TAGS}")
    days = session_days(ts_ns)
    out = np.zeros(days.shape, dtype=bool)
    for d in np.unique(days):
        if day_tags(day_index_to_iso(int(d))) & want:
            out[days == d] = True
    return out


def _stratum(day: str, strata: str) -> str:
    d = _dt.date.fromisoformat(day)
    if strata == "month":
        return f"{d.year:04d}-{d.month:02d}"
    if strata == "weekday":
        return d.strftime("%a")
    if strata == "event":
        return "+".join(sorted(day_tags(d))) or "regular"
    return "all"


def stratified_day_sample(
    candidates: Sequence[str],
    n: int,
    seed: int,
    strata: str = "month",
    exclude_tags: Iterable[str] = (),
) -> dict:
    """Draw ``n`` session days from ``candidates``, stratified and seeded.

    Candidates are de-duplicated and sorted first, so the draw depends only
    on the SET of candidates, ``n``, ``seed`` and ``strata``.  Days carrying
    any of ``exclude_tags`` are removed before the draw (and listed).  Each
    stratum gets ``n x share`` days rounded by largest remainder (ties to
    the earlier stratum), never more than it holds; days inside a stratum
    are drawn without replacement by ``numpy.random.default_rng(seed)``.

    Returns the record :func:`iap.marketdata.manifest_records.record_day_sampling` stores::

        {"method", "seed", "n", "strata", "exclude_tags", "excluded",
         "allocation": {stratum: k}, "selected": [sorted dates],
         "event_days": {date: [tags]} for the selected days}
    """
    if strata not in STRATA:
        raise ValueError(f"unknown strata {strata!r}; known: {STRATA}")
    ex = sorted(set(exclude_tags))
    if set(ex) - set(EVENT_TAGS):
        raise ValueError(f"unknown exclude_tags {ex}; known: {EVENT_TAGS}")
    pool = sorted({_iso(c) for c in candidates})
    excluded = [d for d in pool if day_tags(d) & set(ex)]
    pool = [d for d in pool if d not in set(excluded)]
    if n < 1 or n > len(pool):
        raise ValueError(f"cannot draw {n} days from {len(pool)} eligible candidates")
    groups: dict[str, list[str]] = {}
    for d in pool:
        groups.setdefault(_stratum(d, strata), []).append(d)
    keys = sorted(groups)
    quotas = [n * len(groups[k]) / len(pool) for k in keys]
    alloc = [min(int(q), len(groups[k])) for k, q in zip(keys, quotas, strict=True)]
    order = sorted(range(len(keys)), key=lambda i: (-(quotas[i] - int(quotas[i])), i))
    while sum(alloc) < n:
        for i in order:
            if sum(alloc) < n and alloc[i] < len(groups[keys[i]]):
                alloc[i] += 1
    rng = np.random.default_rng(int(seed))
    selected: list[str] = []
    for k, a in zip(keys, alloc, strict=True):
        if a:
            idx = rng.choice(len(groups[k]), size=a, replace=False)
            selected.extend(groups[k][int(i)] for i in sorted(idx))
    selected.sort()
    return {
        "method": SAMPLING_METHOD,
        "seed": int(seed),
        "n": int(n),
        "strata": strata,
        "exclude_tags": ex,
        "excluded": excluded,
        "allocation": {k: int(a) for k, a in zip(keys, alloc, strict=True)},
        "selected": selected,
        "event_days": {d: sorted(day_tags(d)) for d in selected if day_tags(d)},
    }

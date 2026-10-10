"""Session days: the event calendar and seeded, stratified day sampling (R1).

Up to v1.8.0 the real-data sessions were picked by hand: five of the seven
2019 ITCH days are the 30th of a month, two of them (2019-01-30 and
2019-10-30) are FOMC decision days and 2019-12-30 is a holiday-thin
session.  An IC measured on such a sample is partly an IC of the event
afternoons.  This module makes the choice of days an explicit, seeded,
recorded step and lets every report be read with and without the event
days:

- :data:`FOMC_DAYS` / :data:`HOLIDAY_THIN_DAYS` — a small pinned calendar of
  the sessions the platform has data for (2019 and the 2026 holdout year).
  It is a research convenience, not a reference-data service: extend it
  when a session outside these years is ingested.
- :func:`day_tags` — ``{"fomc", "holiday_thin"}`` subset for one date.
- :func:`session_days` — the UTC session date of each row timestamp (US
  equity sessions, 13:30-20:00 UTC, never straddle UTC midnight).
- :func:`stratified_day_sample` — a deterministic draw of ``n`` days from a
  candidate list, stratified by month (default), weekday or event tag, with
  the seed, the strata and the allocation in the returned record.
- :func:`record_day_sampling` — writes that record into a real dataset's
  ``dataset.json`` under ``"sampling"`` (the manifest's other fields and its
  ``dataset_version`` are untouched: the version hashes the normalized
  files, not the manifest).
- :func:`book_scope_for_dataset` — ``"single_venue"`` for an ingested
  ITCH/LOBSTER dataset (one Nasdaq book, no consolidated NBBO; R4).
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np

NS_DAY = 86_400 * 1_000_000_000

#: FOMC statement days (scheduled meetings, decision day) of the years the
#: platform holds sessions for.
FOMC_DAYS: frozenset[str] = frozenset(
    {
        # 2019
        "2019-01-30",
        "2019-03-20",
        "2019-05-01",
        "2019-06-19",
        "2019-07-31",
        "2019-09-18",
        "2019-10-30",
        "2019-12-11",
        # 2026
        "2026-01-28",
        "2026-03-18",
        "2026-04-29",
        "2026-06-17",
        "2026-07-29",
        "2026-09-16",
        "2026-10-28",
        "2026-12-09",
    }
)

#: Early closes and holiday-thin sessions (between Christmas and New Year,
#: the day after Thanksgiving, the eve of Independence Day).
HOLIDAY_THIN_DAYS: frozenset[str] = frozenset(
    {
        "2019-07-03",
        "2019-11-29",
        "2019-12-24",
        "2019-12-26",
        "2019-12-27",
        "2019-12-30",
        "2019-12-31",
        "2026-07-02",
        "2026-11-27",
        "2026-12-24",
        "2026-12-28",
        "2026-12-29",
        "2026-12-30",
        "2026-12-31",
    }
)

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

    Returns the record :func:`record_day_sampling` stores::

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


def record_day_sampling(dataset_dir: str | Path, record: dict) -> dict:
    """Store ``record`` as ``"sampling"`` in a real dataset's manifest and
    return the updated manifest.  Every selected day must be a session of
    the dataset (fail closed: a record naming a day that was never ingested
    describes another dataset)."""
    from iap.marketdata.ingest import MANIFEST_NAME, load_manifest

    manifest = load_manifest(dataset_dir)
    missing = sorted(set(record.get("selected", ())) - set(manifest.get("sessions", {})))
    if missing:
        raise ValueError(f"sampling record names days the dataset does not hold: {missing}")
    manifest["sampling"] = record
    path = Path(dataset_dir) / MANIFEST_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def book_scope_for_dataset(dataset_dir: str | Path | None) -> str:
    """``"single_venue"`` for an ingested real dataset (one exchange's own
    book: ITCH and LOBSTER are Nasdaq-only, there is no consolidated NBBO
    and a crossed book cannot be a stale other-venue quote), else
    ``"consolidated"`` (the synthetic multi-venue books)."""
    if dataset_dir is None or not (Path(dataset_dir) / "dataset.json").is_file():
        return "consolidated"
    from iap.marketdata.ingest import load_manifest

    manifest = load_manifest(dataset_dir)
    return "single_venue" if manifest.get("source", {}).get("venue") else "consolidated"

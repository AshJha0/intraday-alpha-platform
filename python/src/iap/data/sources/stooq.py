"""Stooq bulk daily files adapter (no account; U0 DR-6 route, daily prices option 2).

Stooq publishes its whole daily database as ASCII archives at
``https://stooq.com/db/h/``: for US listings ``d_us_txt.zip`` (daily), with the
listing page giving each archive's size and last-update date.  Per-symbol daily
CSVs exist at ``https://stooq.com/q/d/l/?s=<sym>.us&i=d`` (``fetch`` only; a
handful of those per day before the site throttles, which is why the bulk file
is the ingestion path).

``coverage()`` reads the listing page only: which archives exist, their sizes
and dates.  **Symbol counts and per-symbol date ranges are inside the bulk
archive, which is price data**; they are therefore not computed here and wait
for the ``iap-runner`` ingestion (``fetch``), which records them as metadata of
the sealed partition.  The archive's layout (one file per symbol under
``data/daily/us/<market>/``) is known from Stooq's documentation and is what
the ingestion will walk.
"""

from __future__ import annotations

import re
from typing import Any

from iap.data.contracts import AvailabilityRule, DatasetContract, dataset
from iap.data.sources.base import Coverage, SourceAdapter, SourceDeclaration

__all__ = ["DECLARATION", "StooqSource", "parse_listing"]

LISTING_URL = "https://stooq.com/db/h/"
BULK_DAILY_US = "d_us_txt.zip"
SYMBOL_CSV_URL = "https://stooq.com/q/d/l/?s={symbol}.us&i=d"

DECLARATION = SourceDeclaration(
    contract=dataset(
        DatasetContract(
            dataset_id="stooq.daily",
            source="stooq",
            availability=AvailabilityRule("period_end", 1),
            adjustment="split_dividend",
            missing_policy="ineligible",
            route="daily_pit",
            leakage_test_id="LK-CAUSALITY",
            fields=("date", "open", "high", "low", "close", "volume", "openint"),
        )
    ),
    licence_id="stooq",
    access="public bulk archives, no account; per-symbol CSV throttled per day",
    user_agent="intraday-alpha-platform/2.0 (free data route coverage; listing page only)",
    requests_per_second=0.5,
)

#: ``<a href="d_us_txt.zip">d_us_txt.zip</a> ... 12 Oct 2026 ... 123,456,789``: the page is a
#: plain table; we take, per archive name, the first date and the largest number on its row.
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_NAME = re.compile(r"([a-z0-9_]+\.(?:zip|txt))", re.I)
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2} [A-Za-z]{3} \d{4}|\d{1,2}/\d{1,2}/\d{4})")
_NUM = re.compile(r"(\d[\d,.]*)\s*(KB|MB|GB|B)?", re.I)
_TAGS = re.compile(r"<[^>]+>")


def _size_bytes(cell: str) -> int | None:
    m = _NUM.search(cell)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "B").upper()
    return int(value * {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30}[unit])


def parse_listing(html: str) -> dict[str, dict[str, Any]]:
    """Archive name -> ``{"date": ..., "size_bytes": ...}`` from the ``/db/h/`` page."""
    out: dict[str, dict[str, Any]] = {}
    for row in _ROW.findall(html):
        text = _TAGS.sub(" ", row)
        names = _NAME.findall(text)
        if not names:
            continue
        name = names[0].lower()
        date = _DATE.search(text)
        size = None
        for cell in re.split(r"\s{2,}|\t", text):
            if name in cell.lower():
                continue
            s = _size_bytes(cell)
            if s and (size is None or s > size):
                size = s
        out[name] = {"date": date.group(1) if date else None, "size_bytes": size}
    return out


class StooqSource(SourceAdapter):
    declaration = DECLARATION

    def coverage(self) -> Coverage:
        cov = self._coverage()
        listing = parse_listing(self._get_text(LISTING_URL))
        us = {k: v for k, v in listing.items() if "_us_" in k}
        cov.counts["archives_listed"] = len(listing)
        cov.counts["archives_us"] = len(us)
        bulk = listing.get(BULK_DAILY_US)
        cov.counts["bulk_daily_us_present"] = int(bulk is not None)
        if bulk:
            if bulk.get("size_bytes"):
                cov.counts["bulk_daily_us_bytes"] = int(bulk["size_bytes"])
            if bulk.get("date"):
                cov.ranges["bulk_daily_us_last_update"] = (bulk["date"], bulk["date"])
        cov.notes.append(
            "symbol counts and date ranges live inside the bulk archive (price data) and are "
            "recorded by the iap-runner ingestion, not here"
        )
        cov.requests_made = self.requests_made
        return cov

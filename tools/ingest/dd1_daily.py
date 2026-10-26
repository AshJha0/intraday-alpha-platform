#!/usr/bin/env python3
"""DD1: ingest the free daily route and seal it (v2.0 stage 2; U0 section 2, section 3 "Stage 2", DR-6, DR-7).

    python tools/ingest/dd1_daily.py run --year 2026 --dev-root C:\\iap\\daily
    python tools/ingest/dd1_daily.py run --dry-run --year 2026 --dev-root <tmp> --sealed-root <tmp>

Runs as ``iap-runner`` only (the same identity rule as the source adapters'
``fetch``: :func:`iap.data.sources.base.require_runner`).  The development
identity cannot run it, and the job never prints a price, a return, a filing or
a symbol: its log is redacted (``tools/runner/redact.py``) and its outputs are
hashes, counts and pass/fail.

What one run does, in order (each step refusing before the next):

1. **identity**: the process is ``iap-runner`` (exit 3 otherwise; mocked only
   in ``--dry-run``, which also answers every request from synthetic fixtures
   and writes only under temp directories);
2. **pull** (``https`` to the allow-listed hosts only): daily prices from
   **Stooq's bulk archive** (since October 2026 Stooq answers scripts with a
   browser check, so the archive is the owner's browser download passed as
   ``--stooq-zip`` with its recorded ``--stooq-sha256``, verified before the
   first request; DR-15) unless ``TIINGO_API_KEY`` is present in the
   environment, in which case **Tiingo** per-ticker CSVs (DR-7; the owner
   creates no account that asks for a card); the S&P 500 and Nasdaq-100
   constituent tables from **every Wikipedia revision** (last revision of each
   day; the proxy label stays on every interval); the **EDGAR** full index per
   quarter filtered to the universe's CIKs; the three **Kenneth French** daily
   factor files, whose dates are the exchange calendar the calendar test uses
   (CRSP trading days);
3. **cut** the four partitions of U0 section 2 by purchase year ``Y`` =
   ``--year`` (the ingestion year): development = earliest date to the end of
   ``Y-4``; validation = year ``Y-3``; Track B test = year ``Y-2``; Track C
   confirmation = ``Y-1`` onward.  Membership intervals are clipped to each
   window, filings and factors by date;
4. **DD1 tests on the development partition only** (:func:`dd1_tests`):
   survivorship (delisted names present through their last membership interval
   plus the retention horizon), corporate actions (split days reconcile: no
   unexplained split-sized jump in the adjusted series; Tiingo ``splitFactor``
   days reconcile ``close``/``adjClose``), calendar (the panel's sessions equal
   the French calendar within the window), open/close convention (documented
   for D3 and measured: the share of opens equal to the previous close),
   membership intervals sane (ordered, non-overlapping per index and symbol,
   every member priced, reliability per year);
5. **write**: development and validation partitions to ``--dev-root``, a
   location the development identity can read; Track B and Track C partitions
   through the sealed services' ``ingest`` endpoint ONLY (``tools/sealed_service.py``:
   the service writes under its own partition, re-hashes, records the dataset
   and answers with hashes and the job's row counts, never content).  Sealed
   writes happen only when every DD1 test passed;
6. **record**: ``<dev-root>/dd1_<Y>/dataset.json`` (kind ``daily_pit``) with
   per-partition ``dataset_hash_id`` (the sealed service's content hash over
   the file set), file hashes, row counts (sealed partitions: as returned by
   the service, which recorded the job's own counts; never recomputed by the
   development identity), vendor metadata, the test results and ``status``
   ``complete`` or ``incomplete``.  ``tools/data_manifest.py generate`` lists it.

The parsed constituents of each Wikipedia revision are cached under
``--cache-root`` (default ``C:\\iap-sealed\\runner\\dd1_wiki_cache``, readable by
``iap-runner`` only), so a re-run lists the revisions again but fetches only
the ones it has not parsed before (D-87: the walk is about 4 h at 1 request/s).
The cache spans the sealed Track B/C windows, so a cache root under
``--dev-root`` is refused (exit 3).  The log carries the Wikipedia request and
cache-hit counts.

Exit codes: 0 complete · 2 a DD1 test failed or a service refused (nothing
sealed; the record says ``incomplete``) · 3 refused before any request.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import importlib.util
import io
import json
import os
import re
import statistics
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
for _p in (str(TOOLS), str(REPO / "python" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from iap.data.sources import edgar, french, stooq, tiingo, wiki_membership  # noqa: E402
from iap.data.sources.base import RUNNER_IDENTITY, current_identity  # noqa: E402
from iap.data.sources.wiki_membership import (  # noqa: E402
    PAGES,
    PROXY_LABEL,
    intervals_from_snapshots,
)
from runner import redact  # noqa: E402

SCHEMA = "iap.dd1_ingestion/v1"
PARTITIONS = ("development", "validation", "b", "c")
SEALED = {"b": "b", "c": "c"}  # partition name -> sealed service partition
STOOQ_BULK_URL = "https://stooq.com/db/d/?b=d_us_txt"
TIINGO_PRICES_URL = (
    "https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=1900-01-01&format=csv"
)
ALLOWED_HOSTS = frozenset(
    {
        "api.tiingo.com",
        "apimedia.tiingo.com",
        "stooq.com",
        "en.wikipedia.org",
        "www.sec.gov",
        "data.sec.gov",
        "mba.tuck.dartmouth.edu",
    }
)
PRICE_FIELDS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_open",
    "adj_close",
    "split_factor",
    "div_cash",
)
#: Default --cache-root of a real run: under the runner's own sealed directory (D-87).
DEFAULT_CACHE_ROOT = Path(r"C:\iap-sealed\runner\dd1_wiki_cache")
WIKI_HOST = "en.wikipedia.org"
MEMBERSHIP_FIELDS = ("index", "symbol", "start", "end", "revision_id", "label")
FILING_FIELDS = ("cik", "symbol", "form", "filing_date", "path")
FACTOR_FIELDS = ("date", "mkt_rf", "smb", "hml", "rmw", "cma", "mom", "rf")
FORMS = ("8-K", "10-Q", "10-K")
#: EDGAR's quarterly full index begins with 1993 Q1; earlier quarters do not exist (D-97).
EDGAR_FIRST_YEAR = 1993

#: Defaults of the DD1 tests (coverage report section 5; defect register D-59 .. D-61).
THRESHOLDS = {
    "survivorship_max_fail_share": 0.02,  # of the delisted population
    "retention_sessions": 60,  # D4's longest horizon
    "split_jump_min": 0.40,  # |close_t / close_{t-1} - 1| above this is a candidate split jump
    "split_ratio_tolerance": 0.02,  # within this of 2, 3, 4, 1.5, 1/2, 1/3, 2/3 ...
    "calendar_session_min_share": 0.5,  # a date is a panel session when this share of active names has a row
    "open_equals_prev_close_max_share": 0.05,
    "membership_reliable_min_share": 0.90,
    "membership_median_gap_max_days": 45,
}
OPEN_CLOSE_CONVENTION = {
    "stooq": "open = first regular-session print as published by the exchange's daily summary; "
    "close = official closing price (closing auction where the exchange has one); both "
    "split- and dividend-adjusted by the vendor (Stooq publishes adjusted series only); "
    "not verified against an exchange source (D-60)",
    "tiingo": "open/close = exchange daily summary (composite); adjOpen/adjClose carry the "
    "vendor's split and dividend adjustment; convention of the opening print not stated by "
    "the vendor (D-60)",
}


class IngestRefused(RuntimeError):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ transport --
#: HTTP answers a source gives while busy or briefly down; anything else is final.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
#: Waits before the 2nd, 3rd, 4th and 5th attempt (seconds); a Retry-After header
#: up to RETRY_AFTER_MAX replaces the scheduled wait (D-96).
RETRY_WAITS = (5.0, 20.0, 60.0, 180.0)
RETRY_AFTER_MAX = 600.0


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    try:
        v = float((exc.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= RETRY_AFTER_MAX else None


def urllib_transport(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout: float = 120.0,
    waits: tuple[float, ...] = RETRY_WAITS,
    sleep: Callable[[float], None] | None = None,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    """GET one allow-listed https URL.  429/5xx answers, timeouts and dropped
    connections are retried ``len(waits)`` times (D-96); 4xx other than 429 is
    final.  ``sleep`` and ``opener`` are for tests."""
    import time

    scheme, sep, rest = url.partition("://")
    host = rest.split("/", 1)[0].split("@")[-1].split(":")[0].lower() if sep else ""
    if scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError(f"URL not on the allow-list (https only): {url}")
    sleep = sleep or time.sleep
    opener = opener or urllib.request.urlopen
    for attempt in range(len(waits) + 1):
        req = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with opener(req, timeout=timeout) as r:  # noqa: S310 - allow-listed https
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt == len(waits):
                raise
            ra = _retry_after(exc)
            sleep(ra if ra is not None else waits[attempt])
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == len(waits):
                raise
            sleep(waits[attempt])
    raise AssertionError("unreachable")


def _sealed_service_module():
    name = "sealed_service"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / "sealed_service.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SealedTransport = Callable[[str, Mapping[str, Any], list[tuple[str, bytes]]], dict[str, Any]]


def pipe_sealed_transport(
    partition: str, request: Mapping[str, Any], files: list[tuple[str, bytes]]
) -> dict[str, Any]:
    ss = _sealed_service_module()
    resp, _ = ss.request_over_pipe(ss.pipe_name(partition), request, files)
    return resp


# ------------------------------------------------------------------- tables --
@dataclass
class Tables:
    prices: list[dict[str, Any]] = field(default_factory=list)
    membership: list[dict[str, Any]] = field(default_factory=list)
    filings: list[dict[str, Any]] = field(default_factory=list)
    factors: list[dict[str, Any]] = field(default_factory=list)
    vendor: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "prices.csv": len(self.prices),
            "membership.csv": len(self.membership),
            "filings.csv": len(self.filings),
            "factors.csv": len(self.factors),
        }


def _iso(d: _dt.date) -> str:
    return d.isoformat()


def _date(text: str) -> _dt.date:
    t = text.strip()
    if len(t) == 8 and t.isdigit():
        return _dt.date(int(t[:4]), int(t[4:6]), int(t[6:8]))
    return _dt.date.fromisoformat(t[:10])


# ---------------------------------------------------------------- the pull --
def _under(path: Path, root: Path) -> bool:
    a = os.path.normcase(str(Path(path).resolve()))
    b = os.path.normcase(str(Path(root).resolve()))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # different drives
        return False


class RevisionCache:
    """Parsed constituents per Wikipedia revision id, one JSON file each under
    ``root``.  A revision never changes, so a hit is final; an unreadable entry
    is a miss and is fetched again."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root) / "wiki_revisions"
        self.hits = 0

    def _path(self, revid: int) -> Path:
        return self.root / f"{int(revid)}.json"

    def get(self, revid: int) -> frozenset[str] | None:
        try:
            doc = json.loads(self._path(revid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("revid") != int(revid):
            return None
        self.hits += 1
        return frozenset(doc.get("symbols", []))

    def put(self, revid: int, symbols: frozenset[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(revid)
        tmp = path.with_suffix(".tmp")
        doc = {"revid": int(revid), "symbols": sorted(symbols)}
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)


def pull_membership(
    transport,
    *,
    indices: Iterable[str] = ("sp500", "ndx100"),
    plausible: Mapping[str, tuple[int, int]] | None = None,
    adapter_kw: Mapping[str, Any] | None = None,
    log=None,
    cache_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every revision of each index page (last revision per day), parsed; intervals
    from the reliable snapshots; per-year reliability counts (metadata).
    ``plausible`` overrides the per-index constituent-count range (dry run only).
    ``cache_root``: reuse the revisions an earlier run parsed (D-87)."""
    wiki_requests = [0]

    def counted(url, headers, *a, **kw):
        if WIKI_HOST in url:
            wiki_requests[0] += 1
        return transport(url, headers, *a, **kw)

    src = wiki_membership.WikiMembershipSource(counted, **(adapter_kw or {}))
    cache = RevisionCache(cache_root) if cache_root is not None else None
    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    for index in indices:
        title, (lo, hi) = PAGES[index]
        if plausible and index in plausible:
            lo, hi = plausible[index]
        revs = src.list_revisions(title)
        by_day: dict[str, tuple[int, str]] = {}
        for rid, ts in revs:
            by_day[ts[:10]] = (rid, ts)  # the last revision of each day wins
        snapshots = []
        per_year: dict[str, dict[str, Any]] = {}
        reliable_dates: dict[str, list[_dt.date]] = {}
        for day in sorted(by_day):
            rid, ts = by_day[day]
            syms = cache.get(rid) if cache else None
            if syms is None:
                syms = src.revision_symbols(rid)
                if cache:
                    cache.put(rid, syms)
            y = day[:4]
            stat = per_year.setdefault(y, {"revisions": 0, "reliable": 0})
            stat["revisions"] += 1
            if lo <= len(syms) <= hi:
                stat["reliable"] += 1
                d = _dt.date.fromisoformat(day)
                snapshots.append((d, rid, syms))
                reliable_dates.setdefault(y, []).append(d)
        for y, stat in per_year.items():
            ds = reliable_dates.get(y, [])
            gaps = [(b - a).days for a, b in zip(ds, ds[1:], strict=False)]
            stat["median_gap_days"] = statistics.median(gaps) if gaps else None
            stat["reliable_share"] = (
                stat["reliable"] / stat["revisions"] if stat["revisions"] else 0.0
            )
        for iv in intervals_from_snapshots(index, snapshots):
            rows.append(iv.to_dict())
        meta[index] = {
            "revisions_listed": len(revs),
            "days_parsed": len(by_day),
            "reliable_snapshots": len(snapshots),
            "per_year": per_year,
            "plausible_range": [lo, hi],
            "label": PROXY_LABEL,
        }
        if log:
            log(
                f"membership {index}: {len(revs)} revisions, {len(by_day)} days, {len(snapshots)} reliable"
            )
    if log:
        log(
            f"membership: {wiki_requests[0]} Wikipedia requests, "
            f"{cache.hits if cache else 0} revision cache hits"
        )
    return rows, meta


def pull_listed_ends(
    transport, *, adapter_kw: Mapping[str, Any] | None = None, log=None
) -> tuple[dict[str, str], dict[str, Any]]:
    """Tiingo's PUBLIC supported-tickers list (no key, no account): the last date
    each US stock is listed with, which is the survivorship test's "listed end"
    whatever the price source (DR-7; coverage report section 2)."""
    src = tiingo.TiingoSource(transport, key="", **(adapter_kw or {}))
    rows = tiingo.parse_supported_tickers(src._get(tiingo.SUPPORTED_TICKERS_URL))
    ends: dict[str, str] = {}
    for r in rows:
        if (r.get("exchange") or "").upper() not in tiingo.US_EXCHANGES:
            continue
        if (r.get("assetType") or "").lower() != "stock" or not r.get("endDate"):
            continue
        sym = str(r.get("ticker") or "").upper().replace("-", ".")
        if sym:
            ends[sym] = max(ends.get(sym, ""), _iso(_date(r["endDate"])))
    meta = {
        "source": "tiingo_supported_tickers",
        "url": tiingo.SUPPORTED_TICKERS_URL,
        "tickers": len(rows),
        "us_stocks_with_end_date": len(ends),
    }
    if log:
        log(f"listed ends: {len(ends)} US stocks with a last date")
    return ends, meta


def _stooq_name(symbol: str) -> str:
    return symbol.lower().replace(".", "-") + ".us"


def read_local_archive(path: Path, expected_sha256: str) -> tuple[bytes, str]:
    """The owner's browser download of the bulk archive (DR-15): Stooq answers
    scripted requests with a JavaScript browser check, which the job does not
    try to pass.  Refuses (exit 3, before any request) unless the file is a zip
    whose sha256 equals the one the owner recorded when downloading it."""
    p = Path(path)
    if not p.is_file():
        raise IngestRefused(3, "the local Stooq archive is missing")
    want = (expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise IngestRefused(3, "the local Stooq archive needs the owner's recorded sha256")
    blob = p.read_bytes()
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise IngestRefused(3, "the local Stooq archive does not match the recorded sha256")
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        raise IngestRefused(3, "the local Stooq archive is not a zip file")
    return blob, got


def pull_prices_stooq(
    transport,
    universe: set[str],
    *,
    adapter_kw: Mapping[str, Any] | None = None,
    log=None,
    local_blob: bytes | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The bulk daily archive, one file per symbol; only universe symbols are kept.
    ``local_blob`` (already verified by :func:`read_local_archive`) replaces both
    requests; the listing page is then not read."""
    if local_blob is None:
        src = stooq.StooqSource(transport, **(adapter_kw or {}))
        listing = stooq.parse_listing(src._get_text(stooq.LISTING_URL))
        blob = src._get(STOOQ_BULK_URL)
        if not zipfile.is_zipfile(io.BytesIO(blob)):
            raise IngestRefused(
                2, "Stooq did not serve a zip (browser check?): use --stooq-zip (DR-15)"
            )
    else:
        listing, blob = {}, local_blob
    wanted = {_stooq_name(s): s for s in universe}
    rows: list[dict[str, Any]] = []
    files_seen = 0
    symbols_found: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if not base.endswith(".txt"):
                continue
            files_seen += 1
            sym = wanted.get(base[:-4])
            if sym is None:
                continue
            symbols_found.add(sym)
            text = zf.read(name).decode("utf-8", "replace")
            for rec in csv.DictReader(io.StringIO(text)):
                try:
                    d = _date(rec["<DATE>"])
                    o, h, l, c = (float(rec[k]) for k in ("<OPEN>", "<HIGH>", "<LOW>", "<CLOSE>"))
                    v = int(float(rec.get("<VOL>") or 0))
                except (KeyError, ValueError):
                    continue
                rows.append(
                    {
                        "symbol": sym,
                        "date": _iso(d),
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                        "adj_open": o,
                        "adj_close": c,
                        "split_factor": "",
                        "div_cash": "",
                    }
                )
    meta = {
        "source": "stooq",
        "archive": stooq.BULK_DAILY_US,
        "archive_bytes": len(blob),
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "archive_origin": "network" if local_blob is None else "owner_browser_download",
        "listing": listing.get(stooq.BULK_DAILY_US),
        "files_in_archive": files_seen,
        "universe_symbols": len(universe),
        "symbols_found": len(symbols_found),
        "symbols_absent": len(universe) - len(symbols_found),
        "adjustment": "split_dividend (vendor-adjusted series; raw not published)",
        "contract": stooq.DECLARATION.to_dict(),
    }
    if log:
        log(f"stooq: {files_seen} files, {len(symbols_found)} of {len(universe)} universe symbols")
    return rows, meta


def pull_prices_tiingo(
    transport,
    universe: set[str],
    key: str,
    *,
    adapter_kw: Mapping[str, Any] | None = None,
    log=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    src = tiingo.TiingoSource(transport, key=key, **(adapter_kw or {}))
    rows: list[dict[str, Any]] = []
    found = 0
    for sym in sorted(universe):
        try:
            text = src._get_text(TIINGO_PRICES_URL.format(ticker=sym.lower()), src._auth())
        except Exception:  # a ticker Tiingo does not hold: counted, not fatal
            continue
        found += 1
        for rec in csv.DictReader(io.StringIO(text)):
            try:
                rows.append(
                    {
                        "symbol": sym,
                        "date": _iso(_date(rec["date"])),
                        "open": float(rec["open"]),
                        "high": float(rec["high"]),
                        "low": float(rec["low"]),
                        "close": float(rec["close"]),
                        "volume": int(float(rec["volume"] or 0)),
                        "adj_open": float(rec["adjOpen"]),
                        "adj_close": float(rec["adjClose"]),
                        "split_factor": float(rec.get("splitFactor") or 1.0),
                        "div_cash": float(rec.get("divCash") or 0.0),
                    }
                )
            except (KeyError, ValueError):
                continue
    meta = {
        "source": "tiingo",
        "universe_symbols": len(universe),
        "symbols_found": found,
        "symbols_absent": len(universe) - found,
        "requests": src.requests_made,
        "adjustment": "raw and vendor-adjusted columns",
        "contract": tiingo.DECLARATION.to_dict(),
    }
    if log:
        log(f"tiingo: {found} of {len(universe)} universe symbols")
    return rows, meta


_IDX_LINE = re.compile(r"^(.{12})(.{62})(.{12})(.{12})(.*)$")


def parse_form_index_rows(text: str) -> Iterable[tuple[str, int, str, str]]:
    """``(form, cik, filing_date, path)`` of every row of a ``form.idx`` (fixed-width)."""
    started = False
    for line in text.splitlines():
        if not started:
            started = line.startswith("---")
            continue
        m = _IDX_LINE.match(line.rstrip("\n"))
        if not m:
            continue
        form, _company, cik, date, path = (g.strip() for g in m.groups())
        try:
            yield form, int(cik), _iso(_date(date)), path
        except ValueError:
            continue


def pull_filings(
    transport,
    universe: set[str],
    years: Iterable[int],
    *,
    today: _dt.date,
    adapter_kw: Mapping[str, Any] | None = None,
    log=None,
    tickers_text: str | None = None,
    cache_root: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Quarter indexes from EDGAR_FIRST_YEAR on.  A quarter that ended before
    ``today`` never changes, so its FORMS rows are cached under ``cache_root``
    (runner-only, D-87) and a re-run reads them instead of the network (D-97)."""
    src = edgar.EdgarSource(transport, **(adapter_kw or {}))
    qcache = Path(cache_root) / "edgar_quarters" if cache_root is not None else None
    cache_hits = 0
    if tickers_text is None:
        tickers_text = src._get_text(edgar.TICKERS_URL)
    doc = json.loads(tickers_text)
    entries = list(doc.values()) if isinstance(doc, dict) else list(doc)
    cik_of: dict[str, int] = {}
    for e in entries:
        t = str(e.get("ticker") or "").upper().replace("-", ".")
        if t in universe and e.get("cik_str"):
            cik_of[t] = int(e["cik_str"])
    by_cik = {c: s for s, c in cik_of.items()}
    rows: list[dict[str, Any]] = []
    quarters = 0
    for y in years:
        if y < EDGAR_FIRST_YEAR:
            continue
        for q in range(1, 5):
            start = _dt.date(y, 3 * (q - 1) + 1, 1)
            if start > today:
                continue
            ended = (_dt.date(y + 1, 1, 1) if q == 4 else _dt.date(y, 3 * q + 1, 1)) <= today
            cpath = qcache / f"{y}Q{q}.json" if qcache is not None else None
            qrows = None
            if cpath is not None and ended:
                try:
                    qrows = [tuple(r) for r in json.loads(cpath.read_text(encoding="utf-8"))]
                    cache_hits += 1
                except (OSError, ValueError):
                    qrows = None
            if qrows is None:
                text = src._get_text(edgar.FULL_INDEX_URL.format(year=y, quarter=q))
                qrows = [r for r in parse_form_index_rows(text) if r[0] in FORMS]
                if cpath is not None and ended:
                    cpath.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cpath.with_suffix(".tmp")
                    tmp.write_text(json.dumps(qrows), encoding="utf-8")
                    os.replace(tmp, cpath)
            quarters += 1
            for form, cik, date, path in qrows:
                if form in FORMS and cik in by_cik:
                    rows.append(
                        {
                            "cik": cik,
                            "symbol": by_cik[cik],
                            "form": form,
                            "filing_date": date,
                            "path": path,
                        }
                    )
    meta = {
        "source": "sec_edgar",
        "quarters_read": quarters,
        "quarters_from_cache": cache_hits,
        "first_year": EDGAR_FIRST_YEAR,
        "universe_symbols": len(universe),
        "symbols_with_cik": len(cik_of),
        "symbols_without_cik": len(universe) - len(cik_of),
        "forms": list(FORMS),
        "cik_map_as_of": _iso(today),
        "note": "company_tickers.json is a current map, not point-in-time: a delisted name "
        "without a current listing has no CIK here (D-61)",
        "contract": edgar.DECLARATION.to_dict(),
    }
    if log:
        log(
            f"edgar: {quarters} quarter indexes ({cache_hits} from cache), "
            f"{len(cik_of)} of {len(universe)} symbols mapped"
        )
    return rows, meta


def _french_rows(blob: bytes) -> tuple[list[str], dict[str, list[str]]]:
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            text = zf.read(name).decode("utf-8", "replace")
    else:
        text = blob.decode("utf-8", "replace")
    columns: list[str] = []
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not columns and line.strip().startswith(","):
            columns = [c.strip() for c in line.split(",")[1:] if c.strip()]
            continue
        m = french._DATE_ROW.match(line)
        if m:
            parts = [p.strip() for p in line.split(",")]
            out[_iso(_date(parts[0]))] = parts[1 : 1 + len(columns)]
    return columns, out


def pull_factors(
    transport, *, adapter_kw: Mapping[str, Any] | None = None, log=None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    src = french.FrenchSource(transport, **(adapter_kw or {}))
    merged: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {"source": "kenneth_french_library", "files": {}}
    col_map = {
        "Mkt-RF": "mkt_rf",
        "SMB": "smb",
        "HML": "hml",
        "RMW": "rmw",
        "CMA": "cma",
        "Mom": "mom",
        "RF": "rf",
    }
    for key, name in french.FILES.items():
        blob = src._get(french.BASE + name)
        columns, rows = _french_rows(blob)
        meta["files"][key] = {
            "name": name,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "rows": len(rows),
            "columns": columns,
        }
        for d, vals in rows.items():
            rec = merged.setdefault(d, {"date": d})
            for c, v in zip(columns, vals, strict=False):
                rec[col_map.get(c, c.lower())] = v
    out = [{f: merged[d].get(f, "") for f in FACTOR_FIELDS} for d in sorted(merged)]
    meta["contract"] = french.DECLARATION.to_dict()
    meta["calendar_note"] = (
        "the union of the files' dates is the exchange calendar used by the calendar test (CRSP trading days)"
    )
    if log:
        log(f"french: {len(out)} calendar days across {len(french.FILES)} files")
    return out, meta


# ------------------------------------------------------------------- the cut --
def windows(year: int) -> dict[str, tuple[str | None, str | None]]:
    """U0 section 2 partitions for purchase year ``year`` (inclusive ISO bounds, None = open)."""
    return {
        "development": (None, f"{year - 4}-12-31"),
        "validation": (f"{year - 3}-01-01", f"{year - 3}-12-31"),
        "b": (f"{year - 2}-01-01", f"{year - 2}-12-31"),
        "c": (f"{year - 1}-01-01", None),
    }


def _in(d: str, lo: str | None, hi: str | None) -> bool:
    return (lo is None or d >= lo) and (hi is None or d <= hi)


def cut(tables: Tables, year: int) -> dict[str, Tables]:
    out: dict[str, Tables] = {}
    for name, (lo, hi) in windows(year).items():
        t = Tables(vendor=tables.vendor)
        t.prices = [r for r in tables.prices if _in(r["date"], lo, hi)]
        t.filings = [r for r in tables.filings if _in(r["filing_date"], lo, hi)]
        t.factors = [r for r in tables.factors if _in(r["date"], lo, hi)]
        for r in tables.membership:
            # an interval is kept when it overlaps the window; its start is clipped
            # to the window, and an interval still open at the window's end stays
            # open (``end`` None): as of that end the name IS a member, which is
            # what the survivorship test's "left the index" population relies on
            start, end = r["start"], r["end"]
            if hi is not None and start > hi:
                continue
            if lo is not None and end is not None and end <= lo:
                continue
            c = dict(r)
            if lo is not None and start < lo:
                c["start"] = lo
            if hi is not None and (end is None or end > hi):
                c["end"] = None
            t.membership.append(c)
        out[name] = t
    return out


# ----------------------------------------------------------------- the tests --
def _sessions_of(prices: list[dict[str, Any]], min_share: float) -> list[str]:
    """Dates on which at least ``min_share`` of the names active that month have a row."""
    by_date: dict[str, set[str]] = {}
    active: dict[str, set[str]] = {}
    for r in prices:
        by_date.setdefault(r["date"], set()).add(r["symbol"])
        active.setdefault(r["date"][:7], set()).add(r["symbol"])
    return sorted(d for d, syms in by_date.items() if len(syms) >= min_share * len(active[d[:7]]))


def survivorship_test(
    membership: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    calendar: list[str],
    th: Mapping[str, Any] = THRESHOLDS,
    listed_end: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Every symbol whose last interval closed inside the window (left the index)
    must carry prices on every calendar session from its first interval start
    through the interval's end, and after it through the retention horizon or
    the end of its listed history (``listed_end``: the vendor's public last date
    per ticker, Tiingo's supported-tickers list), whichever is first.  A name
    without a listed end is held to the retention horizon as far as the panel's
    last session.  Counts only."""
    listed_end = listed_end or {}
    have: dict[str, set[str]] = {}
    for r in prices:
        have.setdefault(r["symbol"], set()).add(r["date"])
    last_end: dict[str, str] = {}
    for r in membership:
        if r["end"] is not None:
            last_end[r["symbol"]] = max(last_end.get(r["symbol"], ""), r["end"])
    open_syms = {r["symbol"] for r in membership if r["end"] is None}
    left = {s: e for s, e in last_end.items() if s not in open_syms}
    idx = {d: i for i, d in enumerate(calendar)}
    failures = 0
    absent = 0
    for sym, end in sorted(left.items()):
        rows = have.get(sym)
        if not rows:
            absent += 1
            failures += 1
            continue
        # rows must exist on every calendar session from the interval start to its
        # end; after the end, through the retention horizon unless the history stops
        starts = [r["start"] for r in membership if r["symbol"] == sym]
        first = min(starts)
        need = [d for d in calendar if first <= d <= end]
        missing = sum(1 for d in need if d not in rows)
        if missing:
            failures += 1
            continue
        j = idx.get(end)
        if j is not None:
            horizon = calendar[j + 1 : j + 1 + int(th["retention_sessions"])]
            limit = listed_end.get(sym) or (calendar[-1] if calendar else end)
            after = [d for d in horizon if d <= limit]
            if any(d not in rows for d in after):
                failures += 1
    share = failures / len(left) if left else 0.0
    return {
        "test": "survivorship",
        "population": len(left),
        "population_with_listed_end": sum(1 for s_ in left if s_ in listed_end),
        "absent_from_source": absent,
        "failures": failures,
        "fail_share": round(share, 4),
        "threshold": th["survivorship_max_fail_share"],
        "pass": share <= th["survivorship_max_fail_share"],
    }


_SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 1.5, 0.5, 1 / 3, 0.25, 0.2, 2 / 3, 0.75)


def corporate_action_test(
    prices: list[dict[str, Any]], th: Mapping[str, Any] = THRESHOLDS
) -> dict[str, Any]:
    """Split days reconcile.  Tiingo rows: on a ``split_factor != 1`` day the raw
    close before it divided by the raw close after it must match the factor through
    the adjusted series (``adj_close`` continuous).  Stooq (adjusted only): no
    split-sized jump may remain in the adjusted series (a jump whose ratio is a
    simple split ratio is an unadjusted split)."""
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for r in prices:
        by_sym.setdefault(r["symbol"], []).append(r)
    split_days = 0
    reconciled = 0
    residual_jumps = 0
    for rows in by_sym.values():
        rows.sort(key=lambda r: r["date"])
        for a, b in zip(rows, rows[1:], strict=False):
            sf = b.get("split_factor")
            if sf not in ("", None) and float(sf) != 1.0:
                split_days += 1
                # adjusted series continuous: |adj ratio| far from the raw ratio moved by sf
                raw = float(b["close"]) / float(a["close"]) if a["close"] else 0.0
                adj = float(b["adj_close"]) / float(a["adj_close"]) if a["adj_close"] else 0.0
                if abs(raw * float(sf) - adj) <= th["split_ratio_tolerance"] * max(adj, 1e-9) * 10:
                    reconciled += 1
                continue
            pa, pb = float(a["adj_close"]), float(b["adj_close"])
            if pa <= 0:
                continue
            ratio = pb / pa
            if abs(ratio - 1) >= th["split_jump_min"] and any(
                abs(ratio - s) <= th["split_ratio_tolerance"] * s for s in _SPLIT_RATIOS
            ):
                residual_jumps += 1
    return {
        "test": "corporate_actions",
        "split_days": split_days,
        "split_days_reconciled": reconciled,
        "residual_split_jumps": residual_jumps,
        "pass": residual_jumps == 0 and reconciled == split_days,
    }


def calendar_test(
    prices: list[dict[str, Any]], calendar: list[str], th: Mapping[str, Any] = THRESHOLDS
) -> dict[str, Any]:
    """The panel's sessions equal the exchange calendar (French dates) inside the
    panel's own date range; weekend rows are a failure in themselves."""
    sessions = _sessions_of(prices, th["calendar_session_min_share"])
    if not sessions:
        return {"test": "calendar", "panel_sessions": 0, "pass": False}
    lo, hi = sessions[0], sessions[-1]
    cal = [d for d in calendar if lo <= d <= hi]
    s, c = set(sessions), set(cal)
    weekend = sum(1 for d in s if _dt.date.fromisoformat(d).weekday() >= 5)
    return {
        "test": "calendar",
        "panel_sessions": len(s),
        "calendar_sessions": len(c),
        "panel_not_in_calendar": len(s - c),
        "calendar_not_in_panel": len(c - s),
        "weekend_sessions": weekend,
        "pass": s == c and weekend == 0,
    }


def open_close_test(
    prices: list[dict[str, Any]], convention: str, th: Mapping[str, Any] = THRESHOLDS
) -> dict[str, Any]:
    """D3 (G4): the open/close convention is written down, and the opens are not
    the previous closes in disguise (share of ``open == prev close`` rows)."""
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for r in prices:
        by_sym.setdefault(r["symbol"], []).append(r)
    pairs = equal = 0
    for rows in by_sym.values():
        rows.sort(key=lambda r: r["date"])
        for a, b in zip(rows, rows[1:], strict=False):
            pairs += 1
            if float(b["open"]) == float(a["close"]):
                equal += 1
    share = equal / pairs if pairs else 1.0
    return {
        "test": "open_close_convention",
        "convention": convention,
        "pairs": pairs,
        "open_equals_prev_close_share": round(share, 4),
        "threshold": th["open_equals_prev_close_max_share"],
        "pass": bool(convention.strip()) and share <= th["open_equals_prev_close_max_share"],
    }


def membership_test(
    membership: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    per_year: Mapping[str, Mapping[str, Any]],
    window: tuple[str | None, str | None],
    th: Mapping[str, Any] = THRESHOLDS,
) -> dict[str, Any]:
    """Intervals ordered and non-overlapping per (index, symbol); every member
    has at least one price row inside one of its intervals; years below the
    reliability bar are listed (excluded from membership-dependent families),
    and the test fails only when no year in the window qualifies or an interval
    is malformed."""
    bad_order = overlaps = 0
    by_key: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
    for r in membership:
        if r["end"] is not None and r["end"] <= r["start"]:
            bad_order += 1
        by_key.setdefault((r["index"], r["symbol"]), []).append((r["start"], r["end"]))
    for ivs in by_key.values():
        ivs.sort()
        for (_s1, e1), (s2, _e2) in zip(ivs, ivs[1:], strict=False):
            if e1 is None or s2 < e1:
                overlaps += 1
    have: dict[str, list[str]] = {}
    for r in prices:
        have.setdefault(r["symbol"], []).append(r["date"])
    unpriced = 0
    for (_, sym), ivs in by_key.items():
        ds = have.get(sym, [])
        if not any(any(s <= d and (e is None or d < e) for d in ds) for s, e in ivs):
            unpriced += 1
    lo, hi = window
    years_in = sorted(
        y for y in per_year if (lo is None or y >= lo[:4]) and (hi is None or y <= hi[:4])
    )
    excluded = []
    for y in years_in:
        st = per_year[y]
        gap = st.get("median_gap_days")
        if st.get("reliable_share", 0.0) < th["membership_reliable_min_share"] or (
            gap is not None and gap > th["membership_median_gap_max_days"]
        ):
            excluded.append(y)
    qualifying = [y for y in years_in if y not in excluded]
    return {
        "test": "membership_intervals",
        "intervals": len(membership),
        "keys": len(by_key),
        "bad_order": bad_order,
        "overlaps": overlaps,
        "members_without_prices": unpriced,
        "years_in_window": years_in,
        "years_excluded": excluded,
        "pass": bad_order == 0 and overlaps == 0 and unpriced == 0 and bool(qualifying),
    }


def dd1_tests(
    dev: Tables,
    *,
    convention: str,
    per_year: Mapping[str, Mapping[str, Any]],
    window: tuple[str | None, str | None],
    th: Mapping[str, Any] = THRESHOLDS,
    listed_end: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """All five DD1 tests on the development partition; ``pass`` is their conjunction."""
    calendar = [r["date"] for r in dev.factors]
    results = {
        "survivorship": survivorship_test(dev.membership, dev.prices, calendar, th, listed_end),
        "corporate_actions": corporate_action_test(dev.prices, th),
        "calendar": calendar_test(dev.prices, calendar, th),
        "open_close_convention": open_close_test(dev.prices, convention, th),
        "membership_intervals": membership_test(dev.membership, dev.prices, per_year, window, th),
    }
    return {
        "pass": all(r["pass"] for r in results.values()),
        "results": results,
        "thresholds": dict(th),
    }


# ---------------------------------------------------------------- the write --
def _csv_bytes(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=list(fields), lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    return buf.getvalue().encode("utf-8")


def serialise(
    name: str, t: Tables, window: tuple[str | None, str | None], year: int
) -> list[tuple[str, bytes]]:
    """The files of one partition, deterministic order and bytes."""
    metadata = {
        "schema": SCHEMA,
        "partition": name,
        "purchase_year": year,
        "window": list(window),
        "row_counts": t.counts(),
        "fields": {
            "prices.csv": list(PRICE_FIELDS),
            "membership.csv": list(MEMBERSHIP_FIELDS),
            "filings.csv": list(FILING_FIELDS),
            "factors.csv": list(FACTOR_FIELDS),
        },
        "vendor": t.vendor,
        "open_close_convention": OPEN_CLOSE_CONVENTION.get(
            t.vendor.get("prices", {}).get("source", ""), ""
        ),
    }
    return [
        (
            "prices.csv",
            _csv_bytes(sorted(t.prices, key=lambda r: (r["symbol"], r["date"])), PRICE_FIELDS),
        ),
        (
            "membership.csv",
            _csv_bytes(
                sorted(t.membership, key=lambda r: (r["index"], r["symbol"], r["start"])),
                MEMBERSHIP_FIELDS,
            ),
        ),
        (
            "filings.csv",
            _csv_bytes(
                sorted(t.filings, key=lambda r: (r["filing_date"], r["cik"], r["form"], r["path"])),
                FILING_FIELDS,
            ),
        ),
        ("factors.csv", _csv_bytes(t.factors, FACTOR_FIELDS)),
        ("metadata.json", (json.dumps(metadata, indent=1, sort_keys=True) + "\n").encode("utf-8")),
    ]


def file_hashes(files: list[tuple[str, bytes]]) -> dict[str, str]:
    return {n: hashlib.sha256(b).hexdigest() for n, b in files}


def sec_contact_ok(user_agent: str) -> bool:
    """SEC fair access asks for a declared name and an email address in the
    User-Agent (``https://www.sec.gov/os/accessing-edgar-data``); without one
    every EDGAR request answers 403 (run 40284491015)."""
    return bool(re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", user_agent or ""))


def _preflight(step: str, fn: Callable[[], Any], log) -> Any:
    """Run one source step before the long membership walk, so a refusal by a
    source costs seconds, not the walk (D-88)."""
    try:
        return fn()
    except urllib.error.HTTPError as exc:
        log(f"refused: {step} answered HTTP {exc.code}")
        raise IngestRefused(
            2, f"{step} answered HTTP {exc.code} before the membership pull"
        ) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        log(f"refused: {step} unreachable ({type(exc).__name__})")
        raise IngestRefused(2, f"{step} unreachable before the membership pull: {exc}") from exc


def _probe_rest(transport, *, adapter_kw, log, today: _dt.date) -> None:
    """``--probe``: one sample of every request pattern the long steps make, so a
    refusing endpoint shows in minutes, not after the walk (D-100)."""
    wsrc = wiki_membership.WikiMembershipSource(transport, **(adapter_kw or {}))
    for index in ("sp500", "ndx100"):
        title, _ = PAGES[index]
        revs = _preflight(
            f"Wikipedia revisions ({index})", lambda t=title: wsrc.list_revisions(t), log
        )
        if not revs:
            raise IngestRefused(2, f"Wikipedia listed no revisions for {index}")
        rid = revs[-1][0]
        syms = _preflight(
            f"Wikipedia revision ({index})", lambda r=rid: wsrc.revision_symbols(r), log
        )
        log(f"probe: {index} latest revision parsed, {len(syms)} symbols")
    esrc = edgar.EdgarSource(transport, **(adapter_kw or {}))
    last = (today.year, (today.month - 1) // 3 + 1)
    for y, q in ((EDGAR_FIRST_YEAR, 1), last):
        text = _preflight(
            f"EDGAR form index {y}Q{q}",
            lambda y=y, q=q: esrc._get_text(edgar.FULL_INDEX_URL.format(year=y, quarter=q)),
            log,
        )
        n = sum(1 for r in parse_form_index_rows(text) if r[0] in FORMS)
        log(f"probe: EDGAR {y}Q{q} parsed, {n} rows of {', '.join(FORMS)}")


# ------------------------------------------------------------------ the run --
def ingest(
    *,
    year: int,
    dev_root: Path,
    transport=urllib_transport,
    sealed_transport: SealedTransport = pipe_sealed_transport,
    identity: str | None = None,
    environ: Mapping[str, str] | None = None,
    today: _dt.date | None = None,
    thresholds: Mapping[str, Any] = THRESHOLDS,
    plausible: Mapping[str, tuple[int, int]] | None = None,
    adapter_kw: Mapping[str, Any] | None = None,
    log: redact.RedactingLog | None = None,
    stooq_zip: Path | None = None,
    stooq_sha256: str = "",
    cache_root: Path | None = None,
    require_sec_contact: bool = True,
    probe: bool = False,
) -> dict[str, Any]:
    """The whole job (module docstring).  Returns the record written to
    ``dev_root/dd1_<year>/dataset.json``; raises :class:`IngestRefused`.
    ``plausible`` and ``adapter_kw`` (injected clock) are for the dry run.
    ``stooq_zip`` + ``stooq_sha256``: the owner's downloaded archive (DR-15),
    verified before the first request.  ``cache_root``: the revision cache
    (D-87); never under ``dev_root``, which the development identity reads."""
    env = os.environ if environ is None else environ
    log = log or redact.RedactingLog([str(dev_root)])
    if cache_root is not None and _under(cache_root, dev_root):
        log("refused: the revision cache root is under the development root")
        raise IngestRefused(
            3, "the revision cache spans the sealed windows: it may not live under --dev-root"
        )
    today = today or _dt.date.today()
    who = identity if identity is not None else current_identity()
    if who != RUNNER_IDENTITY:
        log("refused: process identity is not the runner account")
        raise IngestRefused(3, "the DD1 ingestion runs only as the runner account")
    ss = _sealed_service_module()
    out_dir = Path(dev_root) / f"dd1_{year}"
    if not probe and (out_dir / "dataset.json").is_file():
        raise IngestRefused(
            3, "a record for this purchase year exists; a re-ingestion is a new year"
        )
    local_blob = None
    if stooq_zip is not None:
        local_blob, digest = read_local_archive(stooq_zip, stooq_sha256)
        log(f"local Stooq archive verified: sha256 {digest[:16]}")
    if require_sec_contact and not sec_contact_ok(edgar.DECLARATION.user_agent):
        log("refused: no SEC contact email in the runner environment")
        raise IngestRefused(
            3,
            f"set {edgar.CONTACT_ENV}='<name> <email>' in the runner's environment (SEC fair access)",
        )

    # -- preflight: every step that does not need the universe, before the walk
    tables = Tables()
    tickers_text = _preflight(
        "EDGAR company tickers",
        lambda: edgar.EdgarSource(transport, **(adapter_kw or {}))._get_text(edgar.TICKERS_URL),
        log,
    )
    tables.factors, french_meta = _preflight(
        "French factors", lambda: pull_factors(transport, adapter_kw=adapter_kw, log=log), log
    )
    listed_end, listed_meta = _preflight(
        "Tiingo ticker list",
        lambda: pull_listed_ends(transport, adapter_kw=adapter_kw, log=log),
        log,
    )
    for part in SEALED.values():
        try:
            st = sealed_transport(part, {"op": "status"}, [])
        except Exception as exc:  # the pipe itself: absent service, denied connect
            log(f"refused: sealed service {part} did not answer ({type(exc).__name__})")
            raise IngestRefused(2, f"sealed service {part} did not answer: {exc}") from exc
        if not st.get("ok"):
            log(f"refused: sealed service {part} refused status: {st.get('error')}")
            raise IngestRefused(2, f"sealed service {part} refused this caller: {st.get('error')}")
        if st.get("pending"):
            log(f"refused: sealed service {part} has a pending checkpoint")
            raise IngestRefused(2, f"sealed service {part} has a pending checkpoint")
    log("preflight passed: EDGAR, French, Tiingo and the sealed services answered")
    if probe:
        _probe_rest(transport, adapter_kw=adapter_kw, log=log, today=today)
        for root in (Path(dev_root), Path(cache_root) if cache_root is not None else None):
            if root is None:
                continue
            root.mkdir(parents=True, exist_ok=True)
            mark = root / ".dd1_probe"
            mark.write_text("probe", encoding="utf-8")
            mark.unlink()
        log("probe passed: every source and the sealed services answered; nothing written")
        return {"status": "probe_passed"}

    # -- pull
    tables.membership, wiki_meta = pull_membership(
        transport, plausible=plausible, adapter_kw=adapter_kw, log=log, cache_root=cache_root
    )
    universe = {r["symbol"] for r in tables.membership}
    if not universe:
        raise IngestRefused(2, "no membership intervals: nothing to ingest")
    key = env.get(tiingo.KEY_ENV, "")
    if key:
        tables.prices, price_meta = pull_prices_tiingo(
            transport, universe, key, adapter_kw=adapter_kw, log=log
        )
    else:
        tables.prices, price_meta = pull_prices_stooq(
            transport, universe, adapter_kw=adapter_kw, log=log, local_blob=local_blob
        )
    first_year = min((r["date"][:4] for r in tables.prices), default=str(year))
    tables.filings, edgar_meta = pull_filings(
        transport,
        universe,
        range(int(first_year), year + 1),
        today=today,
        adapter_kw=adapter_kw,
        log=log,
        tickers_text=tickers_text,
        cache_root=cache_root,
    )
    tables.vendor = {
        "prices": price_meta,
        "membership": wiki_meta,
        "filings": edgar_meta,
        "factors": french_meta,
        "listed_ends": listed_meta,
        "pulled_utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }

    # -- cut and test
    parts = cut(tables, year)
    win = windows(year)
    per_year: dict[str, Any] = {}
    for stat in wiki_meta.values():  # the S&P 500 page decides the year bar (D1/D2 universe)
        per_year = stat.get("per_year") or per_year
        break
    tests = dd1_tests(
        parts["development"],
        convention=OPEN_CLOSE_CONVENTION.get(price_meta["source"], ""),
        per_year=per_year,
        window=win["development"],
        th=thresholds,
        listed_end=listed_end,
    )
    for name, res in tests["results"].items():
        log(f"dd1 test {name}: {'pass' if res['pass'] else 'FAIL'}")

    # -- write the development-readable partitions and the record
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "kind": "daily_pit",
        "purchase_year": year,
        "route": "daily_pit",
        "sources": {
            k: v.get("source")
            for k, v in tables.vendor.items()
            if isinstance(v, dict) and "source" in v
        },
        "windows": {k: list(v) for k, v in win.items()},
        "partitions": {},
        "dd1_tests": tests,
        "status": "incomplete",
        "evidence_class": "mechanically isolated",
        "utc": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }
    for name in ("development", "validation"):
        files = serialise(name, parts[name], win[name], year)
        hashes = file_hashes(files)
        pdir = out_dir / name
        pdir.mkdir(parents=True, exist_ok=True)
        for n, b in files:
            (pdir / n).write_bytes(b)
        record["partitions"][name] = {
            "location": "development-readable",
            "dir": name,
            "dataset_hash_id": ss.content_hash(hashes),
            "files": hashes,
            "row_counts": parts[name].counts(),
        }
        log(f"partition {name}: {len(files)} files written for the development identity")

    # -- seal (only when every test passed)
    sealed_ok = True
    if tests["pass"]:
        for name, part in SEALED.items():
            files = serialise(name, parts[name], win[name], year)
            hashes = file_hashes(files)
            hid = ss.content_hash(hashes)
            resp = sealed_transport(
                part,
                {
                    "op": "ingest",
                    "dataset_hash_id": hid,
                    "files": hashes,
                    "row_counts": parts[name].counts(),
                },
                files,
            )
            if not resp.get("ok") and resp.get("error") == "dataset_exists":
                # the id is the content hash: the service already holds these exact files
                log(f"partition {name}: already sealed in {part} (same content hash)")
                record["partitions"][name] = {
                    "location": f"sealed-{part}",
                    "dataset_hash_id": hid,
                    "files": hashes,
                    "row_counts": parts[name].counts(),
                    "chain_head": None,
                    "note": "already recorded by the service on an earlier run (dataset_exists)",
                }
                continue
            if not resp.get("ok"):
                sealed_ok = False
                log(f"service {part} refused the ingest: {resp.get('error')}")
                record["partitions"][name] = {
                    "location": f"sealed-{part}",
                    "refused": resp.get("error"),
                }
                continue
            record["partitions"][name] = {
                "location": f"sealed-{part}",
                "dataset_hash_id": resp["dataset_hash_id"],
                "files": resp["files"],
                "row_counts": resp["row_counts"],  # the service's record of the job's own counts
                "chain_head": resp.get("chain_head"),
            }
            log(
                f"partition {name}: sealed in {part}, {len(resp['files'])} files, chain head {str(resp.get('chain_head'))[:16]}"
            )
    else:
        log("a DD1 test failed: nothing sealed")
    record["status"] = "complete" if tests["pass"] and sealed_ok else "incomplete"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dataset.json").write_text(
        json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    log(f"record written: status {record['status']}")
    if record["status"] != "complete":
        raise IngestRefused(2, "ingestion incomplete (see the record)")
    return record


# ------------------------------------------------------------------ dry run --
#: The fixture's constituent tables hold about a dozen names, not 500.
DRY_RUN_PLAUSIBLE = {"sp500": (8, 20), "ndx100": (5, 20)}


def dry_run(year: int, dev_root: Path, sealed_root: Path, **knobs: Any) -> dict[str, Any]:
    """CI: synthetic fixtures, mocked identity, in-process sealed services under
    ``sealed_root``, everything under the two given directories."""
    from ingest import dd1_fixture

    ss = _sealed_service_module()
    local_zip = knobs.pop("local_zip", False)
    cache_root = knobs.pop("cache_root", None)
    sha256_override = knobs.pop("sha256_override", "")
    require_sec_contact = bool(knobs.pop("require_sec_contact", False))
    fx = dd1_fixture.build(year, **knobs)
    zip_kw: dict[str, Any] = {}
    if local_zip:  # the owner's download (DR-15): the fixture archive, served from disk
        zpath = Path(sealed_root).parent / "d_us_txt.zip"
        zpath.parent.mkdir(parents=True, exist_ok=True)
        zpath.write_bytes(fx.urls.pop(STOOQ_BULK_URL))
        fx.urls.pop(stooq.LISTING_URL, None)
        digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
        zip_kw = {"stooq_zip": zpath, "stooq_sha256": sha256_override or digest}
    services: dict[str, Any] = {}
    for part in SEALED.values():
        root = Path(sealed_root) / part
        state = root / "service"
        state.mkdir(parents=True, exist_ok=True)
        (state / "trust_roots.json").write_text(json.dumps({"maintainers": {"dry-run": "00" * 32}}))
        (state / "config.json").write_text(json.dumps({"allowed_accounts": ["iap-runner"]}))
        services[part] = ss.SealedService(ss.ServiceConfig.load(root, part))

    def sealed_transport(part, request, files):
        buf = io.BytesIO()
        ss.send_files(buf, files)
        buf.seek(0)
        incoming = list(ss._receive_files(buf))
        resp, _ = services[part].handle(request, ss.Caller(account="iap-runner", pid=1), incoming)
        return resp

    log = redact.RedactingLog([str(dev_root), str(sealed_root)])
    tick = [0.0]

    def clock() -> float:  # the rate limiters never sleep in a dry run
        tick[0] += 10.0
        return tick[0]

    try:
        rec = ingest(
            year=year,
            dev_root=Path(dev_root),
            transport=fx.transport,
            sealed_transport=sealed_transport,
            identity="iap-runner",
            environ={"TIINGO_API_KEY": "dry-run"} if knobs.get("tiingo") else {},
            today=fx.last,
            plausible=DRY_RUN_PLAUSIBLE,
            adapter_kw={"clock": clock, "sleep": lambda _s: None},
            log=log,
            cache_root=cache_root,
            require_sec_contact=require_sec_contact,
            **zip_kw,
        )
    except IngestRefused as exc:
        rec_path = Path(dev_root) / f"dd1_{year}" / "dataset.json"
        rec = (
            json.loads(rec_path.read_text(encoding="utf-8"))
            if rec_path.is_file()
            else {"status": "refused", "detail": str(exc)}
        )
        rec["exit"] = exc.code
    rec["log"] = log.text()
    rec["requests"] = len(fx.requests)
    rec["wiki_requests"] = sum(WIKI_HOST in u for u in fx.requests)
    rec["wiki_content_requests"] = sum(WIKI_HOST in u and "revids=" in u for u in fx.requests)
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="pull, cut, test, seal, record")
    r.add_argument("--year", type=int, default=_dt.date.today().year, help="purchase year Y")
    r.add_argument("--dev-root", type=Path, required=True, help="development-readable output root")
    r.add_argument(
        "--dry-run", action="store_true", help="synthetic fixtures, no network, no identity"
    )
    r.add_argument(
        "--sealed-root", type=Path, default=None, help="dry run: in-process services here"
    )
    r.add_argument("--log", type=Path, default=None, help="write the redacted log here")
    r.add_argument(
        "--stooq-zip", type=Path, default=None, help="the owner's downloaded d_us_txt.zip (DR-15)"
    )
    r.add_argument("--stooq-sha256", default="", help="sha256 the owner recorded for --stooq-zip")
    r.add_argument(
        "--probe",
        action="store_true",
        help="check every source and the sealed services in minutes; write no data (D-100)",
    )
    r.add_argument(
        "--cache-root",
        type=Path,
        default=None,
        help=f"revision cache, runner-only (default {DEFAULT_CACHE_ROOT}; dry run: none)",
    )
    a = ap.parse_args(argv)
    if a.dry_run:
        sealed_root = a.sealed_root or Path(tempfile.mkdtemp(prefix="dd1-sealed-"))
        rec = dry_run(a.year, a.dev_root, sealed_root, cache_root=a.cache_root)
        text = rec.pop("log")
        if a.log:
            a.log.write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        print(json.dumps({"status": rec["status"], "requests": rec["requests"]}))
        return 0 if rec["status"] == "complete" else 2
    log = redact.RedactingLog([str(a.dev_root)])
    code = 0
    try:
        rec = ingest(
            year=a.year,
            dev_root=a.dev_root,
            log=log,
            stooq_zip=a.stooq_zip,
            stooq_sha256=a.stooq_sha256,
            cache_root=a.cache_root or DEFAULT_CACHE_ROOT,
            probe=a.probe,
        )
        log(f"done: {rec['status']}")
    except IngestRefused as exc:
        code = exc.code
        log(f"exit {code}")
    except Exception as exc:  # the log must reach the artifact whatever failed (D-100)
        code = 1
        log(f"failed: {type(exc).__name__} (message withheld: the log carries no values)")
        raise
    finally:
        if a.log:
            a.log.parent.mkdir(parents=True, exist_ok=True)
            a.log.write_text(log.text(), encoding="utf-8")
        sys.stdout.write(log.text())
    return code


if __name__ == "__main__":
    raise SystemExit(main())

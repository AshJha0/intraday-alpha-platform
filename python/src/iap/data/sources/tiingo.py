"""Tiingo end-of-day adapter (free tier; U0 DR-6 route, daily prices option 1).

Endpoints (``https://api.tiingo.com``):

- ``/tiingo/daily/<ticker>``            metadata: ``startDate``, ``endDate``, exchange, name
- ``/tiingo/daily/<ticker>/prices``     the price rows (``fetch`` only; never read here)
- ``https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip``
  the public supported-tickers list: one CSV row per ticker with exchange, asset
  type, currency and the first and last dates Tiingo holds.  It needs no key,
  and it is all ``coverage()`` reads: a ticker whose ``endDate`` stops well
  before today is a **delisted or inactive name still listed**, which is the
  survivorship fact DD1 needs before any ingestion.

The key lives in ``TIINGO_API_KEY`` and is sent as ``Authorization: Token ...``;
it is never logged or returned.  Free-tier quotas as published on the pricing
page (read 2026-10-25): 50 requests per hour, 1,000 per day, 500 unique symbols
per month; the limiter here enforces the hourly figure with a one-per-second
ceiling.  Account creation is the owner's (no card is required, DR-6), and this
module never signs up.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import zipfile
from collections import Counter
from typing import Any

from iap.data.contracts import AvailabilityRule, DatasetContract, dataset
from iap.data.sources.base import (
    Coverage,
    RateLimiter,
    SourceAdapter,
    SourceDeclaration,
    Transport,
    parse_date,
)

__all__ = ["DECLARATION", "TiingoSource", "parse_supported_tickers", "summarise_tickers"]

KEY_ENV = "TIINGO_API_KEY"
SUPPORTED_TICKERS_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
META_URL = "https://api.tiingo.com/tiingo/daily/{ticker}"
PRICES_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
#: Exchanges counted as US equity listings in the supported-tickers file.
US_EXCHANGES = frozenset({"NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS", "NYSE NAT"})
#: A ticker whose last date is older than this many days is counted as inactive (delisted or halted).
INACTIVE_AFTER_DAYS = 30

DECLARATION = SourceDeclaration(
    contract=dataset(
        DatasetContract(
            dataset_id="tiingo.daily",
            source="tiingo",
            availability=AvailabilityRule("period_end", 1),
            adjustment="split_dividend",
            missing_policy="ineligible",
            route="daily_pit",
            leakage_test_id="LK-CAUSALITY",
            fields=(
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "adjOpen",
                "adjHigh",
                "adjLow",
                "adjClose",
                "adjVolume",
                "divCash",
                "splitFactor",
            ),
        )
    ),
    licence_id="tiingo",
    access="free tier with an account (no card); key in TIINGO_API_KEY; the supported-tickers list is public",
    user_agent="intraday-alpha-platform/2.0 (free data route coverage; metadata only)",
    requests_per_second=1.0,
)


def parse_supported_tickers(blob: bytes) -> list[dict[str, str]]:
    """Rows of ``supported_tickers.csv`` inside the zip (or a bare CSV)."""
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError("supported_tickers.zip holds no CSV")
            text = zf.read(names[0]).decode("utf-8", errors="replace")
    else:
        text = blob.decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    need = {"ticker", "exchange", "assetType", "priceCurrency", "startDate", "endDate"}
    if rows and not need.issubset(rows[0].keys()):
        raise ValueError(f"unexpected supported_tickers columns: {sorted(rows[0].keys())}")
    return rows


def summarise_tickers(rows: list[dict[str, str]], today: _dt.date) -> Coverage:
    """Counts and ranges only: never a price."""
    cov = Coverage(source="tiingo", as_of=today.isoformat())
    us = [r for r in rows if (r.get("exchange") or "").upper() in US_EXCHANGES]
    stocks = [r for r in us if (r.get("assetType") or "").lower() == "stock"]
    dated = [r for r in stocks if r.get("startDate") and r.get("endDate")]
    cutoff = today - _dt.timedelta(days=INACTIVE_AFTER_DAYS)
    inactive = [r for r in dated if parse_date(r["endDate"]) < cutoff]
    by_exchange = Counter((r.get("exchange") or "").upper() for r in stocks)
    cov.counts = {
        "tickers_total": len(rows),
        "tickers_us": len(us),
        "stocks_us": len(stocks),
        "stocks_us_with_dates": len(dated),
        "stocks_us_inactive_before_cutoff": len(inactive),
        "stocks_us_undated": len(stocks) - len(dated),
        "etfs_us": sum(1 for r in us if (r.get("assetType") or "").lower() == "etf"),
    }
    for ex, n in sorted(by_exchange.items()):
        cov.counts[f"stocks_{ex.lower().replace(' ', '_')}"] = n
    if dated:
        starts = sorted(parse_date(r["startDate"]) for r in dated)
        ends = sorted(parse_date(r["endDate"]) for r in dated)
        cov.ranges["stocks_us_start_dates"] = (starts[0].isoformat(), starts[-1].isoformat())
        cov.ranges["stocks_us_end_dates"] = (ends[0].isoformat(), ends[-1].isoformat())
        pre_2010 = sum(1 for s in starts if s < _dt.date(2010, 1, 1))
        cov.counts["stocks_us_history_before_2010"] = pre_2010
    if inactive:
        ends = sorted(parse_date(r["endDate"]) for r in inactive)
        cov.ranges["inactive_end_dates"] = (ends[0].isoformat(), ends[-1].isoformat())
    cov.notes.append(
        f"inactive = last date older than {INACTIVE_AFTER_DAYS} days before {today.isoformat()}: "
        "delisted names Tiingo still lists (the survivorship test's population); the list does not "
        "say whether their price history is complete, which DD1 checks on ingestion"
    )
    return cov


class TiingoSource(SourceAdapter):
    declaration = DECLARATION

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        key: str | None = None,
        environ: dict[str, str] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(transport, **kw)
        env = os.environ if environ is None else environ
        self._key = key if key is not None else env.get(KEY_ENV, "")
        self.limiter = RateLimiter(
            1.0, per_hour=50, clock=self.limiter._clock, sleep=self.limiter._sleep
        )

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def _auth(self) -> dict[str, str]:
        if not self._key:
            raise PermissionError(f"Tiingo endpoint needs {KEY_ENV} (owner's free account)")
        return {"Authorization": f"Token {self._key}", "Content-Type": "application/json"}

    def coverage(self, *, probe_ticker: str | None = None) -> Coverage:
        """The public supported-tickers list; with a key and ``probe_ticker``, one
        metadata request (dates only) proves the key works."""
        rows = parse_supported_tickers(self._get(SUPPORTED_TICKERS_URL))
        cov = summarise_tickers(rows, self.today)
        if probe_ticker:
            if not self.has_key:
                cov.notes.append(f"needs owner's {KEY_ENV}: metadata probe skipped")
            else:
                import json

                meta = json.loads(self._get_text(META_URL.format(ticker=probe_ticker), self._auth()))
                cov.ranges[f"metadata_{probe_ticker}"] = (
                    str(meta.get("startDate", ""))[:10],
                    str(meta.get("endDate", ""))[:10],
                )
                cov.counts["metadata_probes"] = 1
        else:
            cov.notes.append(
                f"key {'present' if self.has_key else 'absent'}: no authenticated request made"
            )
        cov.requests_made = self.requests_made
        return cov

    # fetch(): inherited refusal (identity + sealed target, then SealedServiceUnavailable)


#!/usr/bin/env python3
"""tools/data_pricing.py: DP1 data pricing (v2.0 U0 stage 0). Metadata and cost endpoints only; never a download.

Prints per-item cost and coverage for the three data families the programme
prices before any purchase decision (plan section 3, U0 stage 0):

  (i)   a daily point-in-time US-equity set: prices with opens and closes, constituents,
        delistings, corporate actions and an announcement calendar
  (ii)  intraday 1-minute bars with quotes for 500+ names over 24-36 months
  (iii) bounded order-level data: the approved tier-2 list (research/r7/r7_tier2_dates.json)

Vendors and the environment variables that hold their keys (never printed, never on
the command line; error text is scrubbed of them):

  DATABENTO_API_KEY         Databento: ``metadata.get_cost`` prices a request exactly without
                            downloading it; ``metadata.get_dataset_range`` gives coverage;
                            ``metadata.list_unit_prices`` gives $/GB per schema
  POLYGON_API_KEY           Polygon (now Massive): subscription-priced, no cost endpoint; with
                            ``--probe`` one reference-metadata request per needed table
                            (``limit=1``) reports whether the key is entitled to it
  NASDAQ_DATA_LINK_API_KEY  Nasdaq Data Link / Sharadar: subscription-priced; with ``--probe``
                            the ``/metadata.json`` endpoint of each needed table (columns only,
                            no rows) reports entitlement

Without a key a vendor's rows read ``awaiting key``.  The Databento client is
wrapped so that only its ``metadata`` namespace is reachable: ``timeseries`` and
``batch`` raise.  ``--probe`` is off by default; even on, every request is a
metadata call.  Nothing here purchases, subscribes or downloads market data.

    python tools/data_pricing.py                       # Markdown table to stdout
    python tools/data_pricing.py --json out.json       # plus the machine-readable rows
    python tools/data_pricing.py --symbols-file universe.txt --as-of 2026-10-01
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TIER2_DATES = ROOT / "research" / "r7" / "r7_tier2_dates.json"

KEYS = {
    "databento": "DATABENTO_API_KEY",
    "polygon": "POLYGON_API_KEY",
    "ndl": "NASDAQ_DATA_LINK_API_KEY",
}
AWAITING = "awaiting key"
DAILY_START = "2006-01-01"
INTRADAY_MONTHS = 36

#: Databento requests, one per row: (item, label, dataset, schema, window, notes).  ``ALL_SYMBOLS`` is an
#: upper bound for a 500-name universe; ``--symbols-file`` replaces it with the owner's list.
DATABENTO_PLAN: list[dict[str, str]] = [
    {
        "item": "i",
        "label": "daily OHLCV, consolidated (EQUS.SUMMARY ohlcv-1d)",
        "dataset": "EQUS.SUMMARY",
        "schema": "ohlcv-1d",
        "window": "daily",
        "notes": "prices only: constituents, delistings, actions and announcements are separate products (see doc)",
    },
    {
        "item": "i",
        "label": "daily OHLCV, Nasdaq venue (XNAS.ITCH ohlcv-1d)",
        "dataset": "XNAS.ITCH",
        "schema": "ohlcv-1d",
        "window": "daily",
        "notes": "venue-only daily bars; alternative to the consolidated summary",
    },
    {
        "item": "ii",
        "label": "1-minute bars, Nasdaq venue (XNAS.ITCH ohlcv-1m)",
        "dataset": "XNAS.ITCH",
        "schema": "ohlcv-1m",
        "window": "intraday",
        "notes": "pair with bbo-1m for quotes",
    },
    {
        "item": "ii",
        "label": "1-minute BBO snapshots, Nasdaq venue (XNAS.ITCH bbo-1m)",
        "dataset": "XNAS.ITCH",
        "schema": "bbo-1m",
        "window": "intraday",
        "notes": "quotes synchronised to the bar grid",
    },
    {
        "item": "ii",
        "label": "1-minute bars, consolidated (EQUS.MINI ohlcv-1m)",
        "dataset": "EQUS.MINI",
        "schema": "ohlcv-1m",
        "window": "intraday",
        "notes": "consolidated across US venues",
    },
    {
        "item": "ii",
        "label": "1-minute BBO snapshots, consolidated (EQUS.MINI bbo-1m)",
        "dataset": "EQUS.MINI",
        "schema": "bbo-1m",
        "window": "intraday",
        "notes": "consolidated NBBO-like quotes on the bar grid",
    },
]

#: Polygon / Massive reference tables item (i) needs; probed with limit=1 (metadata, no market data).
POLYGON_PROBES: list[tuple[str, str]] = [
    ("tickers incl. delisted", "/v3/reference/tickers?active=false&limit=1"),
    ("splits", "/v3/reference/splits?limit=1"),
    ("dividends", "/v3/reference/dividends?limit=1"),
    ("financials (announcement dates)", "/vX/reference/financials?limit=1"),
    (
        "minute aggregates (entitlement)",
        "/v2/aggs/ticker/SPY/range/1/minute/2026-10-01/2026-10-01?limit=1",
    ),
]
POLYGON_BASE = "https://api.polygon.io"
#: Sharadar tables item (i) needs; ``/metadata.json`` returns columns and status, no rows.
NDL_TABLES = [
    "SHARADAR/SEP",
    "SHARADAR/TICKERS",
    "SHARADAR/ACTIONS",
    "SHARADAR/EVENTS",
    "SHARADAR/SP500",
    "SHARADAR/DAILY",
]
NDL_BASE = "https://data.nasdaq.com/api/v3/datatables"

#: Public list prices read on 2026-10-24 (docs/review/DATA_PRICING_v2.md section 3); not entitlements.
LIST_PRICES = {
    "polygon": "Massive Stocks Advanced: $199/month, individual use, 20+ years, minute aggregates, quotes, trades, "
    "reference data and corporate actions, financials (list price read 2026-10-24)",
    "ndl": "Sharadar bundle on Nasdaq Data Link: subscription; price page not readable without login (owner reads before purchase)",
}


# --------------------------------------------------------------------------- helpers
def scrub(text: str, secrets: Sequence[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text


def months_back(as_of: _dt.date, months: int) -> _dt.date:
    y, m = as_of.year, as_of.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return _dt.date(y, m, 1)


def windows(as_of: _dt.date) -> dict[str, tuple[str, str]]:
    end = as_of.replace(day=1).isoformat()  # the last complete month
    return {
        "daily": (DAILY_START, end),
        "intraday": (months_back(as_of, INTRADAY_MONTHS).isoformat(), end),
    }


def load_symbols(path: str | None) -> list[str] | str:
    if path is None:
        return "ALL_SYMBOLS"
    syms = [
        s.strip().upper()
        for s in Path(path).read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.startswith("#")
    ]
    if not syms:
        raise SystemExit(f"{path}: no symbols")
    if len(syms) > 2000:
        raise SystemExit(f"{path}: {len(syms)} symbols; Databento accepts 2,000 per request")
    return syms


class MetadataOnly:
    """The Databento client with everything but ``metadata`` removed: a download is impossible by construction."""

    def __init__(self, client: Any) -> None:
        self._metadata = client.metadata

    @property
    def metadata(self) -> Any:
        return self._metadata

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"data_pricing uses metadata endpoints only; {name!r} is not reachable"
        )


def _databento_client(key: str) -> MetadataOnly:
    try:
        import databento  # the network client: tools only, never imported by iap
    except ImportError as exc:
        raise SystemExit("needs the databento client: pip install 'iap[databento]'") from exc
    return MetadataOnly(databento.Historical(key=key))


# --------------------------------------------------------------------------- vendors
def databento_rows(
    client: MetadataOnly | None, as_of: _dt.date, symbols: list[str] | str, tier2: dict | None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    win = windows(as_of)
    sym_label = symbols if isinstance(symbols, str) else f"{len(symbols)} symbols from file"
    for req in DATABENTO_PLAN:
        start, end = win[req["window"]]
        row: dict[str, Any] = {
            "vendor": "databento",
            "item": req["item"],
            "product": req["label"],
            "window": f"{start}..{end}",
            "symbols": sym_label,
            "coverage": AWAITING,
            "cost_usd": AWAITING,
            "unit_price_usd_per_gb": AWAITING,
            "notes": req["notes"],
        }
        if client is not None:
            row.update(_databento_price(client, req["dataset"], req["schema"], symbols, start, end))
        rows.append(row)
    # (iii) the tier-2 list, through the existing estimator (same request as tools/r7_databento_fetch.py)
    row = {
        "vendor": "databento",
        "item": "iii",
        "product": "XNAS.ITCH mbo, tier-2 list (40 sessions, AAPL/MSFT/QQQ)",
        "window": f"{tier2['dates'][0]}..{tier2['dates'][-1]}"
        if tier2
        else "research/r7/r7_tier2_dates.json",
        "symbols": tier2["symbols"] if tier2 else "AAPL,MSFT,QQQ",
        "coverage": AWAITING,
        "cost_usd": AWAITING,
        "unit_price_usd_per_gb": AWAITING,
        "notes": "the approved bounded order-level set; H4 only",
    }
    if client is not None and tier2 is not None:
        fetch = _load_tool("r7_databento_fetch")
        try:
            est = fetch.estimate(client, tier2["dates"], tier2["symbols"].split(","))
            row["cost_usd"] = round(sum(r["cost_usd"] for r in est), 2)
            row["coverage"] = f"{len(est)} sessions priced"
            row["unit_price_usd_per_gb"] = _unit_price(client, "XNAS.ITCH", "mbo")
        except Exception as exc:  # noqa: BLE001 - reported, never raised with a key in it
            row["cost_usd"] = f"error: {type(exc).__name__}"
            row["notes"] += f"; {scrub(str(exc), [os.environ.get(KEYS['databento'], '')])}"
    rows.append(row)
    return rows


def _databento_price(
    client: MetadataOnly, dataset: str, schema: str, symbols: list[str] | str, start: str, end: str
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    key = os.environ.get(KEYS["databento"], "")
    try:
        rng = client.metadata.get_dataset_range(dataset=dataset)
        out["coverage"] = f"{_field(rng, 'start')}..{_field(rng, 'end')}"
        s0 = max(
            start, str(_field(rng, "start"))[:10]
        )  # never ask for history the dataset does not have
        cost = client.metadata.get_cost(
            dataset=dataset,
            schema=schema,
            symbols=symbols,
            stype_in="raw_symbol",
            start=s0,
            end=end,
        )
        out["cost_usd"] = round(float(cost), 2)
        out["priced_window"] = f"{s0}..{end}"
        out["unit_price_usd_per_gb"] = _unit_price(client, dataset, schema)
    except Exception as exc:  # noqa: BLE001
        out["cost_usd"] = f"error: {type(exc).__name__}"
        out["notes_error"] = scrub(str(exc), [key])
    return out


def _unit_price(client: MetadataOnly, dataset: str, schema: str) -> Any:
    try:
        for entry in client.metadata.list_unit_prices(dataset=dataset):
            if _field(entry, "mode") == "historical":
                return _field(entry, "unit_prices").get(schema, "n/a")
    except Exception:  # noqa: BLE001
        return "n/a"
    return "n/a"


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _load_tool(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _http_status(
    url: str, headers: dict[str, str], opener: Callable[..., Any] | None = None
) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=20) as resp:  # noqa: S310 - https vendor API
            body = resp.read(4096).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001
        return 0, type(exc).__name__


def polygon_rows(
    key: str | None, probe: bool, opener: Callable[..., Any] | None = None
) -> list[dict[str, Any]]:
    base = {
        "vendor": "polygon (massive)",
        "item": "i+ii",
        "product": "Stocks plan (minute aggregates, quotes, trades, reference, corporate actions, financials)",
        "window": "20+ years on the Advanced tier",
        "symbols": "all US listings",
        "coverage": "subscription",
        "cost_usd": LIST_PRICES["polygon"],
        "unit_price_usd_per_gb": "n/a (subscription)",
        "notes": "no cost endpoint; entitlement "
        + ("probed" if (key and probe) else AWAITING if not key else "not probed (--probe)"),
    }
    rows = [base]
    if key and probe:
        for label, path in POLYGON_PROBES:
            status, _ = _http_status(
                POLYGON_BASE + path, {"Authorization": f"Bearer {key}"}, opener
            )
            rows.append(
                {
                    "vendor": "polygon (massive)",
                    "item": "i",
                    "product": f"entitlement: {label}",
                    "window": "-",
                    "symbols": "-",
                    "coverage": "entitled" if status == 200 else f"HTTP {status}",
                    "cost_usd": "included in plan" if status == 200 else "not in plan",
                    "unit_price_usd_per_gb": "n/a",
                    "notes": "metadata request, limit=1",
                }
            )
    return rows


def ndl_rows(
    key: str | None, probe: bool, opener: Callable[..., Any] | None = None
) -> list[dict[str, Any]]:
    base = {
        "vendor": "nasdaq data link (sharadar)",
        "item": "i",
        "product": "Sharadar SEP + TICKERS + ACTIONS + EVENTS + SP500 + DAILY (point-in-time, delisted names included)",
        "window": "1990s onward (vendor statement)",
        "symbols": "all US listings incl. delisted",
        "coverage": "subscription",
        "cost_usd": LIST_PRICES["ndl"],
        "unit_price_usd_per_gb": "n/a (subscription)",
        "notes": "no cost endpoint; entitlement "
        + ("probed" if (key and probe) else AWAITING if not key else "not probed (--probe)"),
    }
    rows = [base]
    if key and probe:
        for table in NDL_TABLES:
            url = f"{NDL_BASE}/{table}/metadata.json?" + urllib.parse.urlencode({"api_key": key})
            status, body = _http_status(url, {}, opener)
            cols = ""
            if status == 200 and body:
                try:
                    cols = str(len(json.loads(body)["datatable"]["columns"])) + " columns"
                except Exception:  # noqa: BLE001
                    cols = "metadata"
            rows.append(
                {
                    "vendor": "nasdaq data link (sharadar)",
                    "item": "i",
                    "product": f"entitlement: {table}",
                    "window": "-",
                    "symbols": "-",
                    "coverage": ("entitled, " + cols) if status == 200 else f"HTTP {status}",
                    "cost_usd": "included in subscription" if status == 200 else "not subscribed",
                    "unit_price_usd_per_gb": "n/a",
                    "notes": "metadata endpoint, no rows",
                }
            )
    return rows


# --------------------------------------------------------------------------- rendering
COLUMNS = (
    "vendor",
    "item",
    "product",
    "window",
    "symbols",
    "coverage",
    "cost_usd",
    "unit_price_usd_per_gb",
    "notes",
)


def render(rows: list[dict[str, Any]]) -> str:
    out = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")).replace("|", "/") for c in COLUMNS) + " |")
    return "\n".join(out)


def run(
    argv: Sequence[str] | None = None,
    *,
    databento_client: Any | None = None,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--as-of", default=None, help="pricing date (YYYY-MM-DD); windows end at that month's start"
    )
    p.add_argument(
        "--symbols-file",
        default=None,
        help="one symbol per line for the intraday and daily estimates",
    )
    p.add_argument("--tier2", default=str(TIER2_DATES), help="the tier-2 date list (item iii)")
    p.add_argument(
        "--probe",
        action="store_true",
        help="also probe Polygon/NDL entitlement with metadata requests",
    )
    p.add_argument("--json", default=None, help="write the rows as JSON here")
    p.add_argument("--vendor", choices=["all", *KEYS], default="all")
    a = p.parse_args(argv)
    as_of = _dt.date.fromisoformat(a.as_of) if a.as_of else _dt.date.today()
    symbols = load_symbols(a.symbols_file)
    tier2 = (
        json.loads(Path(a.tier2).read_text(encoding="utf-8")) if Path(a.tier2).is_file() else None
    )
    secrets = [os.environ.get(v, "") for v in KEYS.values()]
    rows: list[dict[str, Any]] = []
    try:
        if a.vendor in ("all", "databento"):
            key = os.environ.get(KEYS["databento"])
            client = (
                databento_client
                if databento_client is not None
                else (_databento_client(key) if key else None)
            )
            rows += databento_rows(client, as_of, symbols, tier2)
        if a.vendor in ("all", "polygon"):
            rows += polygon_rows(os.environ.get(KEYS["polygon"]), a.probe, opener)
        if a.vendor in ("all", "ndl"):
            rows += ndl_rows(os.environ.get(KEYS["ndl"]), a.probe, opener)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(scrub(f"{type(exc).__name__}: {exc}", secrets)) from None
    doc = {
        "schema": "iap.data_pricing/v1",
        "as_of": as_of.isoformat(),
        "keys_present": {v: bool(os.environ.get(e)) for v, e in KEYS.items()},
        "downloads": 0,
        "purchases": 0,
        "rows": rows,
    }
    text = scrub(render(rows), secrets)
    print(
        f"DP1 data pricing as of {as_of.isoformat()}; keys present: {doc['keys_present']}; downloads 0, purchases 0"
    )
    print(text)
    if a.json:
        Path(a.json).write_text(
            scrub(json.dumps(doc, indent=2, sort_keys=True), secrets) + "\n", encoding="utf-8"
        )
    return rows


if __name__ == "__main__":
    run(sys.argv[1:])

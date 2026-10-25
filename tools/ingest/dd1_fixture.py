"""Synthetic vendor-format fixtures for the DD1 dry run (no vendor is contacted).

:func:`build` returns a :class:`Fixture`: a transport ``(url, headers) -> bytes``
that answers exactly the URLs ``dd1_daily.py`` asks, with bytes in each vendor's
own format (a Stooq bulk zip, Tiingo price CSVs, MediaWiki revision JSON with
constituent tables in wikitext, EDGAR ``form.idx`` and ``company_tickers.json``,
Kenneth French daily factor zips), plus the truth the DD1 tests are checked
against (the calendar, the planted delisting, the split day).  Deterministic in
``seed`` (SplitMix64).  Any URL outside the set raises, so a test sees every
unexpected request.

Knobs for the positive controls of the coverage report's section 5 item 6:

``truncate_delisted``   the delisted name's price history stops before its
                        last membership interval (the survivorship test must fail);
``unadjusted_split``    the split day is left unadjusted in an adjusted series
                        (the corporate-action test must fail);
``drop_sessions``       price rows are removed on calendar sessions (the calendar
                        test must fail).

The membership-sanity positive control is a crafted interval list in the tests
(``dd1_daily.membership_test``), not a fixture knob.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import urllib.parse
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field

SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK", "LLL")
#: the name that leaves the S&P 500 proxy and delists (survivorship population)
DELISTED = "GGG"
#: the name that joins late (its interval starts after the first revision)
JOINER = "LLL"
#: the name with a 2-for-1 split
SPLIT_SYMBOL = "CCC"
#: fixed-date holidays of the synthetic exchange calendar (month, day)
HOLIDAYS = ((1, 1), (7, 4), (12, 25))
WIKI_API = "https://en.wikipedia.org/w/api.php"


class SplitMix64:
    def __init__(self, seed: int) -> None:
        self.s = seed & 0xFFFFFFFFFFFFFFFF

    def next_u64(self) -> int:
        self.s = (self.s + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
        return z ^ (z >> 31)

    def uniform(self) -> float:
        return (self.next_u64() >> 11) / float(1 << 53)

    def normal(self) -> float:
        import math

        u1 = max(self.uniform(), 1e-12)
        u2 = self.uniform()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)


def sessions(first: _dt.date, last: _dt.date) -> list[_dt.date]:
    """Weekdays minus the fixed holidays (observed on the weekday when they fall on a weekend)."""
    out = []
    d = first
    while d <= last:
        if d.weekday() < 5 and not _holiday(d):
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


def _holiday(d: _dt.date) -> bool:
    for m, day in HOLIDAYS:
        h = _dt.date(d.year, m, day)
        if h.weekday() == 5:
            h -= _dt.timedelta(days=1)
        elif h.weekday() == 6:
            h += _dt.timedelta(days=1)
        if d == h:
            return True
    return False


@dataclass
class Fixture:
    year: int
    first: _dt.date
    last: _dt.date
    calendar: list[_dt.date]
    split_date: _dt.date
    delist_date: _dt.date  # last traded session of DELISTED
    delisted_exit_date: _dt.date  # the revision date on which DELISTED left the table
    joiner_date: _dt.date
    ciks: dict[str, int]
    urls: dict[str, bytes] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)

    def transport(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.requests.append(url)
        key = self._key(url)
        if key not in self.urls:
            raise KeyError(f"fixture has no answer for {url}")
        return self.urls[key]

    @staticmethod
    def _key(url: str) -> str:
        if url.startswith(WIKI_API):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            if "revids" in q:
                return f"wiki:content:{q['revids'][0]}"
            title = q.get("titles", [""])[0]
            cont = q.get("rvcontinue", [""])[0]
            return f"wiki:list:{title}:{cont}"
        return url


def build(
    year: int = 2026,
    *,
    seed: int = 20261025,
    first: _dt.date | None = None,
    truncate_delisted: bool = False,
    unadjusted_split: bool = False,
    drop_sessions: int = 0,
    tiingo: bool = False,
) -> Fixture:
    """The fixture for purchase year ``year`` (history from ``first``, default Y-7)."""
    first = first or _dt.date(year - 7, 1, 2)
    last = _dt.date(year, 6, 30)
    cal = sessions(first, last)
    rng = SplitMix64(seed)
    split_date = cal[len(cal) // 2]
    # the delisted name leaves the index three quarters into the development window
    dev_end = _dt.date(year - 4, 12, 31)
    dev_cal = [d for d in cal if d <= dev_end]
    exit_i = (3 * len(dev_cal)) // 4
    delisted_exit = dev_cal[exit_i]
    delist_date = dev_cal[min(exit_i + 70, len(dev_cal) - 1)]  # trades 70 sessions after exit
    joiner_date = dev_cal[len(dev_cal) // 3]
    ciks = {s: 1000 + i for i, s in enumerate(SYMBOLS)}
    fx = Fixture(year, first, last, cal, split_date, delist_date, delisted_exit, joiner_date, ciks)

    # -- prices ----------------------------------------------------------------
    prices: dict[str, list[tuple[_dt.date, float, float, float, float, int]]] = {}
    for sym in SYMBOLS:
        level = 20.0 + 80.0 * rng.uniform()
        rows = []
        for d in cal:
            if sym == DELISTED and d > delist_date:
                break
            if sym == DELISTED and truncate_delisted and d > delisted_exit:
                break
            ret = 0.01 * rng.normal()
            o = level * (1 + 0.004 * rng.normal())
            level = max(1.0, level * (1 + ret))
            c = level
            hi = max(o, c) * (1 + 0.003 * abs(rng.normal()))
            lo = min(o, c) * (1 - 0.003 * abs(rng.normal()))
            vol = int(1e5 + 1e5 * rng.uniform())
            rows.append((d, o, hi, lo, c, vol))
        prices[sym] = rows
    # the split: the random walk IS the adjusted (continuous) series; the raw
    # series is twice as high before the split day.  Stooq publishes the adjusted
    # series, so the clean fixture leaves it alone; `unadjusted_split` plants the
    # raw jump into it (the corporate-action test must then fail).
    adj = {}
    for sym, rows in prices.items():
        if sym == SPLIT_SYMBOL and unadjusted_split:
            adj[sym] = [
                (d, o * 2, hi * 2, lo * 2, c * 2, vol // 2)
                if d < split_date
                else (d, o, hi, lo, c, vol)
                for d, o, hi, lo, c, vol in rows
            ]
        else:
            adj[sym] = list(rows)
    dropped: set[_dt.date] = set()
    if drop_sessions:
        dropped = set(dev_cal[10 : 10 + drop_sessions])
    if tiingo:
        for sym in SYMBOLS:
            lines = [
                "date,close,high,low,open,volume,adjClose,adjHigh,adjLow,adjOpen,adjVolume,divCash,splitFactor"
            ]
            for d, o, hi, lo, c, vol in adj[sym]:
                if d in dropped:
                    continue
                raw = 2.0 if (sym == SPLIT_SYMBOL and d < split_date) else 1.0
                sf = 2.0 if (sym == SPLIT_SYMBOL and d == split_date) else 1.0
                lines.append(
                    f"{d.isoformat()},{c * raw:.4f},{hi * raw:.4f},{lo * raw:.4f},{o * raw:.4f},"
                    f"{int(vol / raw)},{c:.4f},{hi:.4f},{lo:.4f},{o:.4f},{vol},0.0,{sf}"
                )
            fx.urls[
                f"https://api.tiingo.com/tiingo/daily/{sym.lower()}/prices?startDate=1900-01-01&format=csv"
            ] = ("\n".join(lines) + "\n").encode()
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sym in SYMBOLS:
                lines = ["<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>"]
                for d, o, hi, lo, c, vol in adj[sym]:
                    if d in dropped:
                        continue
                    lines.append(
                        f"{sym}.US,D,{d:%Y%m%d},000000,{o:.4f},{hi:.4f},{lo:.4f},{c:.4f},{vol},0"
                    )
                market = "nasdaq stocks" if SYMBOLS.index(sym) % 2 else "nyse stocks"
                zf.writestr(
                    f"data/daily/us/{market}/1/{sym.lower()}.us.txt", "\n".join(lines) + "\n"
                )
            zf.writestr(
                "data/daily/us/nyse etfs/1/zzz.us.txt", "<TICKER>,<PER>\n"
            )  # not in universe
        fx.urls["https://stooq.com/db/d/?b=d_us_txt"] = buf.getvalue()

    # -- Wikipedia revisions ---------------------------------------------------
    # one revision per month from `first`; DELISTED leaves on delisted_exit, JOINER
    # joins on joiner_date; a few revisions are vandalised (a two-row table)
    months: list[_dt.date] = []
    d = _dt.date(first.year, first.month, 1)
    while d <= last:
        months.append(d)
        d = _dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    rev_dates = sorted(set(months) | {delisted_exit, joiner_date})
    revisions: list[tuple[int, _dt.date, list[str], bool]] = []
    for i, rd in enumerate(rev_dates):
        syms = [
            s
            for s in SYMBOLS
            if not (s == DELISTED and rd >= delisted_exit)
            and not (s == JOINER and rd < joiner_date)
        ]
        vandal = i % 17 == 5
        revisions.append((100000 + i, rd, syms, vandal))
    fx.urls["wiki:list:List of S&P 500 companies:"] = json.dumps(
        {
            "query": {
                "pages": [
                    {
                        "revisions": [
                            {"revid": rid, "timestamp": f"{rd.isoformat()}T12:00:00Z"}
                            for rid, rd, _, _ in revisions
                        ]
                    }
                ]
            }
        }
    ).encode()
    for rid, rd, syms, vandal in revisions:
        fx.urls[f"wiki:content:{rid}"] = json.dumps(
            {
                "query": {
                    "pages": [
                        {
                            "revisions": [
                                {
                                    "revid": rid,
                                    "timestamp": f"{rd.isoformat()}T12:00:00Z",
                                    "slots": {
                                        "main": {"content": _wikitext(syms[:2] if vandal else syms)}
                                    },
                                }
                            ]
                        }
                    ]
                }
            }
        ).encode()
    # the Nasdaq-100 proxy page: the same symbols minus two, one revision per quarter
    ndx = [s for s in SYMBOLS if s not in (DELISTED, "KKK")]
    ndx_revs = [(200000 + i, rd) for i, rd in enumerate(months) if rd.month in (1, 4, 7, 10)]
    fx.urls["wiki:list:Nasdaq-100:"] = json.dumps(
        {
            "query": {
                "pages": [
                    {
                        "revisions": [
                            {"revid": rid, "timestamp": f"{rd.isoformat()}T12:00:00Z"}
                            for rid, rd in ndx_revs
                        ]
                    }
                ]
            }
        }
    ).encode()
    for rid, rd in ndx_revs:
        fx.urls[f"wiki:content:{rid}"] = json.dumps(
            {
                "query": {
                    "pages": [
                        {
                            "revisions": [
                                {
                                    "revid": rid,
                                    "timestamp": f"{rd.isoformat()}T12:00:00Z",
                                    "slots": {"main": {"content": _wikitext(ndx)}},
                                }
                            ]
                        }
                    ]
                }
            }
        ).encode()

    # -- EDGAR -----------------------------------------------------------------
    fx.urls["https://www.sec.gov/files/company_tickers.json"] = json.dumps(
        {
            str(i): {"cik_str": cik, "ticker": sym, "title": f"{sym} Corp"}
            for i, (sym, cik) in enumerate(ciks.items())
        }
    ).encode()
    for y in range(first.year, last.year + 1):
        for q in range(1, 5):
            qs = _dt.date(y, 3 * (q - 1) + 1, 1)
            if qs > last:
                continue
            lines = [
                "Description: Master Index of EDGAR Dissemination Feed by Form Type",
                "",
                "Form Type   Company Name                                                  CIK         Date Filed  File Name",
                "-" * 140,
            ]
            for sym, cik in ciks.items():
                for k, form in enumerate(("8-K", "10-Q", "10-K")):
                    fd = qs + _dt.timedelta(days=10 + 7 * k + (cik % 5))
                    if fd > last or (sym == DELISTED and fd > delist_date):
                        continue
                    lines.append(
                        f"{form:<12}{sym + ' Corp':<62}{cik:<12}{fd.isoformat():<12}"
                        f"edgar/data/{cik}/{y}{q}{k}.txt"
                    )
            fx.urls[f"https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/form.idx"] = (
                "\n".join(lines) + "\n"
            ).encode()

    # -- French factors (the calendar) -----------------------------------------
    base = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    fx.urls[base + "F-F_Research_Data_Factors_daily_CSV.zip"] = _french_zip(
        "F-F_Research_Data_Factors_daily.CSV", ("Mkt-RF", "SMB", "HML", "RF"), cal, rng
    )
    fx.urls[base + "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"] = _french_zip(
        "F-F_Research_Data_5_Factors_2x3_daily.CSV",
        ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"),
        cal,
        rng,
    )
    fx.urls[base + "F-F_Momentum_Factor_daily_CSV.zip"] = _french_zip(
        "F-F_Momentum_Factor_daily.CSV", ("Mom",), cal, rng
    )
    # the Stooq listing page (vendor metadata read by the job)
    fx.urls["https://stooq.com/db/h/"] = (
        "<table><tr><td><a href='d_us_txt.zip'>d_us_txt.zip</a></td>"
        f"<td>{last.isoformat()}</td><td>123,456,789</td></tr></table>"
    ).encode()
    # Tiingo's public supported-tickers list (vendor metadata; survivorship population)
    rows = ["ticker,exchange,assetType,priceCurrency,startDate,endDate"]
    for sym in SYMBOLS:
        end = delist_date if sym == DELISTED else last
        rows.append(f"{sym},NYSE,Stock,USD,{first.isoformat()},{end.isoformat()}")
    fx.urls["https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"] = (
        "\n".join(rows) + "\n"
    ).encode()
    return fx


def _wikitext(symbols: list[str], overlap: bool = False) -> str:
    rows = "\n".join(f"|-\n| {{{{NYSE|{s}}}}} || {s} Corp || Industrials" for s in symbols)
    text = (
        '{{Short description|List}}\n== Constituents ==\n{| class="wikitable sortable"\n'
        "! Symbol !! Security !! Sector\n" + rows + "\n|}\n"
    )
    if overlap:
        # a second table listing the first symbol again does not change the parsed
        # constituent set (largest table wins); overlapping intervals are planted by
        # dd1_daily's membership assembly only when a symbol is listed in two indices
        # with the same id, which this fixture emulates by naming the index twice
        text += "\n== Recent changes ==\n{|\n|-\n| {{NYSE|" + symbols[0] + "}} || added\n|}\n"
    return text


def _french_zip(name: str, columns: tuple[str, ...], cal: list[_dt.date], rng: SplitMix64) -> bytes:
    lines = [
        "This file was created by CMPT_ME_BEME_RETS using the 202609 CRSP database.",
        "",
        "," + ",".join(columns),
    ]
    for d in cal:
        vals = ",".join(f"{0.05 * rng.normal():.2f}" for _ in columns)
        lines.append(f"{d:%Y%m%d},{vals}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()

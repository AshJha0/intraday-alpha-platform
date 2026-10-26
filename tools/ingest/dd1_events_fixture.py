"""Synthetic EDGAR responses in the real formats for ``dd1_events.py --dry-run`` and its tests.

Two issuers, quarterly item-2.02 8-Ks at before-open, during and after-close
times (one accepted at 17:45 so its ``Date Filed`` is the next day, one on the
DST change Monday, one on a Saturday), a non-earnings 8-K each quarter, an
``8-K/A`` repeating an original and one standalone ``8-K/A``.  The index lines
are fixed width as ``form.idx``; the headers are ``-index-headers.html`` pages
with the SGML tags HTML-escaped, as EDGAR serves them.  No real filing is used.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field

from iap.data.sources import edgar, edgar_events, french

ISSUERS = {"AAA": 1000001, "BBB": 1000002}


@dataclass
class Fixture:
    universe: list[str]
    today: _dt.date
    first_year: int
    urls: dict[str, bytes] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    expected: dict[str, dict[str, str]] = field(default_factory=dict)  # accession -> timing, usable

    def transport(self, url: str, headers) -> bytes:
        self.requests.append(url)
        if "User-Agent" not in headers:
            raise AssertionError("no User-Agent")
        try:
            return self.urls[url]
        except KeyError:
            raise AssertionError(f"unexpected request {url}") from None


HOLIDAYS = {"2024-07-04", "2025-12-25", "2023-01-02"}


def header_page(
    acc: str, form: str, cik: int, accepted: str, items: list[str], filed: str
) -> bytes:
    lines = [
        "<html><head><title>EDGAR header</title></head><body><pre>",
        f"&lt;SEC-HEADER&gt;{acc}.hdr.sgml : {filed}",
        f"&lt;ACCEPTANCE-DATETIME&gt;{accepted}",
        f"ACCESSION NUMBER:\t\t{acc}",
        f"CONFORMED SUBMISSION TYPE:\t{form}",
        "PUBLIC DOCUMENT COUNT:\t\t3",
        f"CONFORMED PERIOD OF REPORT:\t{filed}",
        *[f"ITEM INFORMATION:\t\t{i}" for i in items],
        f"FILED AS OF DATE:\t\t{filed}",
        "",
        "FILER:",
        "",
        "\tCOMPANY DATA:\t",
        "\t\tCOMPANY CONFORMED NAME:\t\t\tSYNTHETIC CORP",
        f"\t\tCENTRAL INDEX KEY:\t\t\t{cik:010d}",
        "\t\tSTANDARD INDUSTRIAL CLASSIFICATION:\t[7372]",
        "&lt;/SEC-HEADER&gt;",
        "</pre></body></html>",
    ]
    return ("\n".join(lines) + "\n").encode()


def _calendar(first_year: int, last: _dt.date) -> list[str]:
    d, out = _dt.date(first_year, 1, 1), []
    while d <= last:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def _french_csv(days: list[str], ncols: int) -> bytes:
    head = "," + ",".join(f"C{i}" for i in range(ncols))
    rows = [d.replace("-", "") + "," + ",".join("0.01" for _ in range(ncols)) for d in days]
    return ("synthetic\n\n" + head + "\n" + "\n".join(rows) + "\n").encode()


def build(year: int = 2026, *, first_year: int = 2021, unreadable: int = 0) -> Fixture:
    """``unreadable`` headers lose their acceptance tag (a positive control of the
    header-parse test)."""
    today = _dt.date(year, 3, 31)
    fx = Fixture(universe=list(ISSUERS) + ["NOCIK"], today=today, first_year=first_year)
    cal_end = _dt.date(year - 1, 12, 31)  # French files lag; the tail is weekday-extended
    days = _calendar(first_year, cal_end)
    for name, ncols in zip(french.FILES.values(), (4, 6, 1), strict=True):
        fx.urls[french.BASE + name] = _french_csv(days, ncols)
    fx.urls[edgar.TICKERS_URL] = json.dumps(
        {
            str(i): {"cik_str": c, "ticker": s, "title": f"{s} Corp"}
            for i, (s, c) in enumerate(ISSUERS.items())
        }
    ).encode()
    seq = [0]

    def filing(
        cik: int, form: str, accepted: _dt.datetime, items: list[str], filed: _dt.date | None = None
    ):
        seq[0] += 1
        acc = f"{cik:010d}-{accepted.year % 100:02d}-{seq[0]:06d}"
        filed = filed or accepted.date()
        stamp = accepted.strftime("%Y%m%d%H%M%S")
        page = header_page(acc, form, cik, stamp, items, filed.strftime("%Y%m%d"))
        if unreadable and seq[0] <= unreadable:
            page = page.replace(b"&lt;ACCEPTANCE-DATETIME&gt;", b"")
        path = f"edgar/data/{cik}/{acc}.txt"
        row = edgar_events.IndexRow(form, cik, filed.isoformat(), path)
        fx.urls[edgar_events.header_url(row)] = page
        return acc, f"{form:<12}{'SYNTHETIC CORP':<62}{cik:<12}{filed.isoformat():<12}{path}"

    earn = ["Results of Operations and Financial Condition", "Financial Statements and Exhibits"]
    times = [_dt.time(7, 0), _dt.time(12, 15), _dt.time(16, 5), _dt.time(17, 45)]
    for y in range(first_year, year + 1):
        for q in range(1, 5):
            qs = _dt.date(y, 3 * (q - 1) + 1, 1)
            if qs > today:
                continue
            lines = [
                "Description:           Master Index of EDGAR Dissemination Feed by Form Type",
                "",
                "Form Type   Company Name                                                  CIK         Date Filed  File Name",
                "-" * 140,
            ]
            for k, cik in enumerate(ISSUERS.values()):
                d = qs + _dt.timedelta(days=24 + k)
                while d.weekday() >= 5:
                    d += _dt.timedelta(days=1)
                t = times[(y + q + k) % len(times)]
                filed = (
                    d + _dt.timedelta(days=3 if d.weekday() == 4 else 1)
                    if t >= _dt.time(17, 30)
                    else d
                )
                acc, line = filing(cik, "8-K", _dt.datetime.combine(d, t), earn, filed)
                lines.append(line)
                lines.append(
                    filing(
                        cik,
                        "8-K",
                        _dt.datetime.combine(d + _dt.timedelta(days=7), _dt.time(8, 0)),
                        ["Other Events"],
                    )[1]
                )
                if q == 2 and k == 0:  # an amendment of the original, 3 days later
                    lines.append(
                        filing(
                            cik,
                            "8-K/A",
                            _dt.datetime.combine(d + _dt.timedelta(days=3), _dt.time(10, 0)),
                            earn,
                        )[1]
                    )
            if (y, q) == (year - 2, 1):
                # DST Monday (second Sunday of March + 1), before the open; a Saturday filing;
                # a standalone 8-K/A with no original in the data
                dst = edgar_events._nth_sunday(y, 3, 2) + _dt.timedelta(days=1)
                lines.append(
                    filing(1000001, "8-K", _dt.datetime.combine(dst, _dt.time(6, 30)), earn)[1]
                )
                sat = dst + _dt.timedelta(days=5)
                lines.append(
                    filing(1000002, "8-K", _dt.datetime.combine(sat, _dt.time(11, 0)), earn)[1]
                )
                lines.append(
                    filing(1000002, "8-K/A", _dt.datetime(y, 3, 5, 9, 0), ["Item 2.02"])[1]
                )
            # a non-universe issuer: never requested
            lines.append(
                f"{'8-K':<12}{'OTHER CORP':<62}{'9999999':<12}{qs.isoformat():<12}edgar/data/9999999/x.txt"
            )
            fx.urls[edgar.FULL_INDEX_URL.format(year=y, quarter=q)] = (
                "\n".join(lines) + "\n"
            ).encode()
    return fx

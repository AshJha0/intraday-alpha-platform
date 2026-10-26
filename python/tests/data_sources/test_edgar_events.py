"""EDGAR earnings calendar adapter (``iap.data.sources.edgar_events``) on recorded
synthetic responses in EDGAR's formats: ``form.idx`` lines, ``-index-headers.html``
(escaped SGML tags) and ``.hdr.sgml`` headers; timing classes against a session
calendar; the US DST rule; amendments; the runner-only fetch and the User-Agent."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from iap.data.sources import edgar_events as ev
from iap.data.sources.base import SealingRefused

FIX = Path(__file__).parent / "fixtures"
DT = _dt.datetime


def _sessions(first: str, last: str, holidays: tuple[str, ...] = ()) -> list[str]:
    d, end, out = _dt.date.fromisoformat(first), _dt.date.fromisoformat(last), []
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


T0 = DT(2024, 2, 1, 16, 5)


def _hdr(form="8-K", cik=1, when=T0, items=("2.02",), acc=None):
    return ev.FilingHeader(
        acc or f"{cik}-{form}-{when:%Y%m%d%H%M}", form, cik, when, items, when.date().isoformat()
    )


# ----------------------------------------------------------------- declaration --
def test_declaration_contract_and_access():
    d = ev.DECLARATION
    assert d.contract.dataset_id == "edgar.earnings_8k" and d.contract.route == "daily_pit"
    assert (
        d.contract.availability.basis == "publication_date"
        and d.contract.availability.lag_periods == 1
    )
    assert d.to_dict()["dataset_id"] == "edgar.earnings_8k"
    assert d.requests_per_second <= 10 and d.licence_id == "sec_edgar"
    assert "contact" in d.access or "User-Agent" in d.access
    assert set(ev.EVENT_FIELDS) <= set(d.contract.fields)


# ------------------------------------------------------------------- the index --
def test_index_keeps_8k_and_8k_a_only_and_filters_ciks():
    text = (FIX / "edgar_form_idx_8k.txt").read_text(encoding="utf-8")
    rows = ev.parse_form_index_8k(text)
    assert [r.form for r in rows] == ["8-K", "8-K", "8-K/A", "8-K"]  # not 8-K12B, not 10-Q
    rows = ev.parse_form_index_8k(text, {1000001, 1000002})
    assert {r.cik for r in rows} == {1000001, 1000002} and len(rows) == 3
    r = rows[0]
    assert r.accession == "0001000001-24-000017" and r.date_filed == "2024-02-02"
    assert ev.header_url(r) == (
        "https://www.sec.gov/Archives/edgar/data/1000001/000100000124000017/"
        "0001000001-24-000017-index-headers.html"
    )


# ------------------------------------------------------------------ the header --
def test_index_headers_page_with_escaped_tags():
    h = ev.parse_header((FIX / "edgar_8k_index_headers.html").read_text(encoding="utf-8"))
    assert h.form == "8-K" and h.cik == 1000001 and h.accession == "0001000001-24-000017"
    assert h.acceptance_et == DT(2024, 2, 1, 17, 45, 12)
    assert h.date_filed == "2024-02-02"  # accepted after 17:30: dated the next business day
    assert h.is_earnings and "financial statements and exhibits" in h.items


def test_sgml_header_old_item_12_amendment():
    h = ev.parse_header((FIX / "edgar_8k_a_hdr.sgml").read_text(encoding="utf-8"))
    assert h.form == "8-K/A" and h.items == ("12",) and h.is_earnings
    assert h.acceptance_et == DT(2004, 3, 5, 8, 30)


@pytest.mark.parametrize(
    "item,earn",
    [
        ("2.02", True),
        ("Item 2.02 Results", True),
        ("Results of Operations and Financial Condition", True),
        ("8.01", False),
        ("Other Events", False),
        ("Item 12", True),
        ("2.03", False),
    ],
)
def test_item_recognition(item, earn):
    page = f"<ACCEPTANCE-DATETIME>20240201070000\nCONFORMED SUBMISSION TYPE:\t8-K\nITEM INFORMATION:\t{item}\n"
    assert ev.parse_header(page).is_earnings is earn


def test_header_without_timestamp_or_form_is_refused():
    with pytest.raises(ValueError):
        ev.parse_header("CONFORMED SUBMISSION TYPE:\t8-K\n")
    with pytest.raises(ValueError):
        ev.parse_header("<ACCEPTANCE-DATETIME>20240201070000\nITEM INFORMATION:\t2.02\n")


# ---------------------------------------------------------------------- timing --
CAL = _sessions("2024-01-01", "2024-12-31", holidays=("2024-07-04", "2024-11-28"))


@pytest.mark.parametrize(
    "when,timing,as_of,usable",
    [
        (DT(2024, 2, 1, 6, 0), "before_open", "2024-01-31", "2024-02-01"),
        (DT(2024, 2, 1, 9, 29, 59), "before_open", "2024-01-31", "2024-02-01"),
        (DT(2024, 2, 1, 9, 30), "during", "2024-02-01", "2024-02-02"),
        (DT(2024, 2, 1, 15, 59, 59), "during", "2024-02-01", "2024-02-02"),
        (DT(2024, 2, 1, 16, 0), "after_close", "2024-02-01", "2024-02-02"),
        (DT(2024, 2, 2, 17, 45), "after_close", "2024-02-02", "2024-02-05"),  # Friday -> Monday
        (DT(2024, 2, 3, 11, 0), "non_session", "2024-02-02", "2024-02-05"),  # Saturday
        (DT(2024, 7, 4, 8, 0), "non_session", "2024-07-03", "2024-07-05"),  # holiday
        (DT(2024, 7, 3, 16, 30), "after_close", "2024-07-03", "2024-07-05"),  # eve of holiday
        (DT(2024, 3, 11, 7, 0), "before_open", "2024-03-08", "2024-03-11"),  # DST Monday
        (DT(2024, 11, 4, 8, 0), "before_open", "2024-11-01", "2024-11-04"),  # first EST Monday
        (DT(2024, 12, 31, 16, 30), "after_close", "2024-12-31", None),  # calendar ends
    ],
)
def test_classify(when, timing, as_of, usable):
    assert ev.classify(when, CAL) == (timing, as_of, usable)


def test_classification_is_on_eastern_wall_time_not_utc():
    # 07:00 EDT in July is 11:00 UTC, 07:00 EST in January 12:00 UTC: both before the
    # open; a UTC reading would call neither "during" but would misplace 05:30 vs 09:30
    assert ev.classify(DT(2024, 7, 10, 7, 0), CAL)[0] == "before_open"
    assert ev.eastern_to_utc(DT(2024, 7, 10, 7, 0)) == DT(2024, 7, 10, 11, 0, tzinfo=_dt.UTC)
    assert ev.eastern_to_utc(DT(2024, 1, 10, 7, 0)) == DT(2024, 1, 10, 12, 0, tzinfo=_dt.UTC)


@pytest.mark.parametrize(
    "local,offset",
    [
        (DT(2024, 3, 10, 1, 59), 5),
        (DT(2024, 3, 10, 3, 0), 4),  # spring forward 2024
        (DT(2024, 3, 10, 2, 30), 5),  # skipped hour read as EST
        (DT(2024, 11, 3, 0, 59), 4),
        (DT(2024, 11, 3, 1, 30), 4),  # repeated hour: first (EDT)
        (DT(2024, 11, 3, 2, 0), 5),
        (DT(2006, 4, 2, 3, 0), 4),
        (DT(2006, 3, 15, 12, 0), 5),  # pre-2007 rule: first Sunday of April
        (DT(2006, 10, 29, 2, 0), 5),
        (DT(2006, 10, 28, 12, 0), 4),  # last Sunday of October
        (DT(2007, 3, 11, 3, 0), 4),
        (DT(2007, 11, 4, 2, 0), 5),
    ],
)
def test_dst_rule(local, offset):
    assert ev.eastern_to_utc(local) == (local + _dt.timedelta(hours=offset)).replace(tzinfo=_dt.UTC)


def test_dst_rule_agrees_with_tz_database_when_present():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        ny = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("no tz database")
    t = DT(2004, 1, 1, 12, 0)
    while t.year < 2027:
        assert ev.eastern_to_utc(t) == t.replace(tzinfo=ny).astimezone(_dt.UTC), t
        t += _dt.timedelta(hours=37)


# ------------------------------------------------------------------ amendments --
def test_amendment_of_an_original_is_folded():
    orig = _hdr(when=DT(2024, 2, 1, 16, 5))
    amend = _hdr(form="8-K/A", when=DT(2024, 2, 6, 10, 0))
    kept, stats = ev.dedupe_amendments([amend, orig])
    assert kept == [orig] and stats["amendment_of_original"] == 1


def test_standalone_or_late_amendment_is_kept_and_flagged():
    orig = _hdr(when=DT(2024, 2, 1, 16, 5))
    late = _hdr(form="8-K/A", when=DT(2024, 4, 1, 10, 0))  # 60 days later
    other = _hdr(form="8-K/A", cik=2, when=DT(2024, 2, 2, 10, 0))  # another issuer
    before = _hdr(form="8-K/A", when=DT(2024, 1, 31, 10, 0))  # before the original
    events, stats = ev.announcements([orig, late, other, before], CAL)
    assert [e.header for e in events] == [before, orig, other, late]
    assert [e.amendment_only for e in events] == [True, False, True, True]
    assert stats["amendment_of_original"] == 0


def test_non_earnings_and_duplicate_accessions_are_dropped():
    a = _hdr(acc="X")
    events, stats = ev.announcements([a, a, _hdr(items=("8.01",), acc="Y")], CAL)
    assert len(events) == 1 and stats["duplicate_accession"] == 1


def test_row_fields():
    e = ev.announcements([_hdr(when=DT(2024, 3, 11, 7, 0))], CAL)[0][0].row()
    assert tuple(e) == ev.EVENT_FIELDS
    assert e["acceptance_utc"] == "2024-03-11T11:00:00+00:00" and e["timing"] == "before_open"


# --------------------------------------------------------- adapter and access --
def _transport(urls, seen):
    def t(url, headers):
        seen.append((url, dict(headers)))
        return urls[url]

    return t


def test_coverage_counts_only_with_user_agent_and_rate_limit():
    idx = (FIX / "edgar_form_idx_8k.txt").read_bytes()
    page = (FIX / "edgar_8k_index_headers.html").read_bytes()
    urls = {ev.FULL_INDEX_URL.format(year=2024, quarter=1): idx}
    for r in ev.parse_form_index_8k(idx.decode()):
        urls[ev.header_url(r)] = page
    seen: list = []
    clock = iter(i * 0.01 for i in range(1000))
    sleeps: list[float] = []
    src = ev.EdgarEventsSource(
        _transport(urls, seen),
        clock=lambda: next(clock),
        sleep=sleeps.append,
        today=_dt.date(2024, 4, 1),
    )
    cov = src.coverage(year=2024, quarter=1, ciks={1000001, 1000002})
    assert cov.counts["index_8k"] == 2 and cov.counts["index_8k_a"] == 1
    assert cov.counts["headers_item_202"] == 3 and cov.counts["item_202_after_close"] == 3
    assert cov.requests_made == 4 and len(seen) == 4
    assert all("contact" not in h or h for _, h in seen)
    assert all(h["User-Agent"].startswith("intraday-alpha-platform") for _, h in seen)
    assert not any("9999999" in u for u, _ in seen), "a non-universe filing is never requested"
    assert all(u.endswith("-index-headers.html") or u.endswith("form.idx") for u, _ in seen)
    text = repr(cov.to_dict())
    assert "1000001" not in text and "17:45" not in text  # counts, not records


def test_fetch_is_runner_only():
    src = ev.EdgarEventsSource(None)
    with pytest.raises(SealingRefused):
        src.fetch(r"C:\iap-sealed\b\x", identity="ashis")
    with pytest.raises(SealingRefused):
        src.fetch(r"C:\temp\x", identity="iap-runner")

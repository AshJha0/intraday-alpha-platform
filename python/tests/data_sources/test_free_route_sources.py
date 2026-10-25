"""v2.0 stage 2 DD1: free-route source adapters (U0 DR-6).

Recorded or hand-written fixtures only; no request leaves this file (a transport
that is called with an unexpected URL fails the test).  What is checked:

- every adapter registers a dataset contract with the six lineage facts and a
  licence id that names a section of ``docs/DATA_SOURCES.md``;
- ``coverage()`` returns counts, ranges and notes and nothing else (no value
  field can appear);
- ``fetch()`` refuses outside the ``iap-runner`` identity, refuses an unsealed
  target, and with both right still raises until the D2 sealed service exists;
- the rate limiter honours per-second and per-hour ceilings without sleeping;
- the parsers behind each coverage call (Tiingo supported-tickers CSV, Stooq
  listing, Wikipedia constituent table in four spellings, EDGAR form index and
  submissions JSON, French daily CSV);
- ``tools/free_data_coverage.py`` composes them, refuses off-list hosts and
  renders without a key.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from iap.data import contracts as C
from iap.data.sources import (
    ADAPTERS,
    EdgarSource,
    FrenchSource,
    SealedServiceUnavailable,
    SealingRefused,
    StooqSource,
    TiingoSource,
    WikiMembershipSource,
)
from iap.data.sources import base as B
from iap.data.sources import edgar, french, stooq, tiingo, wiki_membership

FIX = Path(__file__).parent / "fixtures"
TODAY = _dt.date(2026, 10, 25)


class Recorder:
    """A transport that serves canned bytes by URL prefix and records every call."""

    def __init__(self, routes: dict[str, bytes | str]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers) -> bytes:
        self.calls.append((url, dict(headers)))
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return body.encode("utf-8") if isinstance(body, str) else body
        raise AssertionError(f"unexpected request in a unit test: {url}")


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


# ----------------------------------------------------------------------------- contracts
@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_every_adapter_declares_a_registered_contract(name):
    decl = ADAPTERS[name].declaration
    c = decl.contract
    assert C.datasets()[c.dataset_id] == c
    assert c.route == "daily_pit"
    assert c.leakage_test_id in C.LEAKAGE_TESTS
    assert c.availability.basis in ("period_end", "publication_date")
    assert c.adjustment in C.ADJUSTMENTS and c.missing_policy in C.MISSING_POLICIES
    assert decl.licence_id and decl.access and decl.requests_per_second > 0
    doc = decl.to_dict()
    assert doc["licence_id"] == decl.licence_id and doc["source"] == c.source


def test_licence_ids_name_sections_of_data_sources_md():
    text = (REPO_ROOT / "docs" / "DATA_SOURCES.md").read_text(encoding="utf-8")
    for name, cls in ADAPTERS.items():
        lid = cls.declaration.licence_id
        assert f"licence id `{lid}`" in text, f"{name}: no section with licence id {lid!r}"


def test_sources_package_imports_no_network_module():
    forbidden = re.compile(r"^\s*(?:import|from)\s+(urllib|http|socket|requests|httpx|ssl)\b", re.M)
    for path in (REPO_ROOT / "python" / "src" / "iap" / "data" / "sources").glob("*.py"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name


# ----------------------------------------------------------------------------- sealing
@pytest.mark.parametrize("cls", list(ADAPTERS.values()))
def test_fetch_refuses_wrong_identity(cls):
    with pytest.raises(SealingRefused, match="iap-runner"):
        cls(Recorder({})).fetch(r"C:\iap-sealed\b\daily", identity="iap-dev")


@pytest.mark.parametrize(
    "target", [r"C:\iap-sealed", r"C:\Users\x\data", "/tmp/x", "/iap-sealed", "", "relative/path"]
)
def test_fetch_refuses_unsealed_target(target):
    with pytest.raises(SealingRefused, match="sealed partition"):
        TiingoSource(Recorder({})).fetch(target, identity="iap-runner")


@pytest.mark.parametrize("target", [r"C:\iap-sealed\b\daily", r"c:\IAP-SEALED\c", "/iap-sealed/a/x"])
def test_fetch_with_runner_and_sealed_target_waits_for_d2(target):
    rec = Recorder({})
    with pytest.raises(SealedServiceUnavailable, match="D2"):
        StooqSource(rec).fetch(target, identity="iap-runner")
    assert rec.calls == []  # nothing fetched before the service exists


def test_fetch_reads_the_process_identity_by_default(monkeypatch):
    monkeypatch.setenv("USERNAME", "someone-else")
    with pytest.raises(SealingRefused, match="someone-else"):
        FrenchSource(Recorder({})).fetch(r"C:\iap-sealed\a\f")
    monkeypatch.setenv("USERNAME", "iap-runner")
    with pytest.raises(SealedServiceUnavailable):
        FrenchSource(Recorder({})).fetch(r"C:\iap-sealed\a\f")


def test_is_sealed_target_edge_cases():
    assert B.is_sealed_target(r"C:\iap-sealed\runner\in")
    assert not B.is_sealed_target(None)
    assert not B.is_sealed_target(r"D:\iap-sealed\b")
    assert not B.is_sealed_target(r"C:\iap-sealed-not\b")


# ----------------------------------------------------------------------------- rate limiter
def test_rate_limiter_per_second_and_per_hour_without_sleeping():
    t = [0.0]
    slept: list[float] = []

    def clock():
        return t[0]

    def sleep(s):
        slept.append(s)
        t[0] += s

    lim = B.RateLimiter(2.0, per_hour=5, clock=clock, sleep=sleep)
    for _ in range(2):
        lim.acquire()
    assert slept == []
    lim.acquire()  # third in the same second: waits the rest of the second
    assert slept and abs(slept[-1] - 1.0) < 1e-9
    t[0] += 10
    lim.acquire()
    lim.acquire()  # fifth
    t[0] += 10
    lim.acquire()  # sixth within the hour: waits until the first falls out of the window
    assert slept[-1] > 3000
    with pytest.raises(ValueError):
        B.RateLimiter(0)


def test_adapter_get_requires_https_and_a_transport():
    with pytest.raises(RuntimeError, match="no transport"):
        TiingoSource(None, environ={}).coverage()
    with pytest.raises(ValueError, match="https only"):
        StooqSource(Recorder({}))._get("http://stooq.com/db/h/")


# ----------------------------------------------------------------------------- tiingo
TIINGO_CSV = "\n".join(
    [
        "ticker,exchange,assetType,priceCurrency,startDate,endDate",
        "AAA,NYSE,Stock,USD,1990-01-02,2026-10-24",
        "BBB,NASDAQ,Stock,USD,2005-03-01,2026-10-24",
        "CCC,NASDAQ,Stock,USD,1999-06-01,2014-02-28",  # inactive: delisted name still listed
        "DDD,NYSE,ETF,USD,2001-01-02,2026-10-24",
        "EEE,LSE,Stock,GBP,1995-01-02,2026-10-24",  # not US
        "FFF,NYSE,Stock,USD,,",  # undated
    ]
)


def test_tiingo_coverage_counts_and_delisted_without_a_key(monkeypatch):
    monkeypatch.delenv(tiingo.KEY_ENV, raising=False)
    rec = Recorder({tiingo.SUPPORTED_TICKERS_URL: _zip("supported_tickers.csv", TIINGO_CSV)})
    cov = TiingoSource(rec, today=TODAY).coverage()
    assert cov.counts["tickers_total"] == 6
    assert cov.counts["tickers_us"] == 5
    assert cov.counts["stocks_us"] == 4
    assert cov.counts["stocks_us_with_dates"] == 3
    assert cov.counts["stocks_us_inactive_before_cutoff"] == 1
    assert cov.counts["stocks_us_undated"] == 1
    assert cov.counts["etfs_us"] == 1
    assert cov.ranges["stocks_us_start_dates"] == ("1990-01-02", "2005-03-01")
    assert cov.ranges["inactive_end_dates"] == ("2014-02-28", "2014-02-28")
    assert any("key absent" in n for n in cov.notes)
    assert cov.requests_made == 1 and len(rec.calls) == 1
    assert "Authorization" not in rec.calls[0][1]
    assert set(cov.to_dict()) == {"source", "as_of", "counts", "ranges", "notes", "requests_made"}


def test_tiingo_probe_needs_key_and_never_leaks_it():
    rec = Recorder(
        {
            tiingo.SUPPORTED_TICKERS_URL: TIINGO_CSV.encode(),
            "https://api.tiingo.com/tiingo/daily/AAA": json.dumps(
                {"startDate": "1990-01-02", "endDate": "2026-10-24", "name": "Alpha"}
            ),
        }
    )
    cov = TiingoSource(rec, today=TODAY, environ={}).coverage(probe_ticker="AAA")
    assert any("needs owner's TIINGO_API_KEY" in n for n in cov.notes)
    src = TiingoSource(rec, today=TODAY, environ={tiingo.KEY_ENV: "sk-SECRET"})
    cov = src.coverage(probe_ticker="AAA")
    assert cov.ranges["metadata_AAA"] == ("1990-01-02", "2026-10-24")
    assert rec.calls[-1][1]["Authorization"] == "Token sk-SECRET"
    assert "SECRET" not in json.dumps(cov.to_dict())
    assert src.limiter.per_hour == 50


def test_tiingo_rejects_unexpected_columns():
    with pytest.raises(ValueError, match="columns"):
        tiingo.parse_supported_tickers(b"a,b\n1,2\n")


# ----------------------------------------------------------------------------- stooq
def test_stooq_listing_parse_and_coverage():
    html = (FIX / "stooq_db_h.html").read_text(encoding="utf-8")
    listing = stooq.parse_listing(html)
    assert set(listing) == {"d_us_txt.zip", "h_us_txt.zip", "d_pl_txt.zip"}
    assert listing["d_us_txt.zip"]["date"] == "2026-10-24"
    assert listing["d_us_txt.zip"]["size_bytes"] == int(512.3 * (1 << 20))
    assert listing["d_pl_txt.zip"]["size_bytes"] == 40128 * 1024
    cov = StooqSource(Recorder({stooq.LISTING_URL: html}), today=TODAY).coverage()
    assert cov.counts["archives_us"] == 2 and cov.counts["bulk_daily_us_present"] == 1
    assert cov.ranges["bulk_daily_us_last_update"] == ("2026-10-24", "2026-10-24")
    assert "symbol counts" in cov.notes[0]


def test_stooq_challenge_page_yields_zero_archives():
    cov = StooqSource(Recorder({stooq.LISTING_URL: "<html><body><noscript>JS</noscript></body></html>"})).coverage()
    assert cov.counts["archives_listed"] == 0 and cov.counts["bulk_daily_us_present"] == 0


# ----------------------------------------------------------------------------- wikipedia
def test_wiki_parse_constituents_four_spellings_largest_table_wins():
    text = (FIX / "wiki_sp500_recent.wikitext").read_text(encoding="utf-8")
    assert wiki_membership.parse_constituents(text) == frozenset({"AAA", "BBB", "CCC", "DDD", "EEE", "FFF.B"})


def test_wiki_intervals_from_snapshots():
    d = _dt.date
    snaps = [
        (d(2020, 1, 1), 1, frozenset({"A", "B"})),
        (d(2020, 2, 1), 2, frozenset({"A", "B", "C"})),
        (d(2020, 3, 1), 3, frozenset({"A", "C"})),
    ]
    iv = wiki_membership.intervals_from_snapshots("sp500", snaps)
    by = {(i.symbol, i.start): i for i in iv}
    assert by[("B", d(2020, 1, 1))].end == d(2020, 3, 1) and by[("B", d(2020, 1, 1))].revision_id == 1
    assert by[("A", d(2020, 1, 1))].end is None
    assert by[("C", d(2020, 2, 1))].end is None and by[("C", d(2020, 2, 1))].revision_id == 2
    assert all(i.label == "wikipedia-proxy" for i in iv)
    assert iv[0].to_dict()["index"] == "sp500"


def _wiki_api(revisions: list[tuple[int, str]], contents: dict[int, str]) -> Recorder:
    """A MediaWiki API stand-in: paged revision lists (2 per page) and content by revid."""

    class Api(Recorder):
        def __call__(self, url, headers):
            self.calls.append((url, dict(headers)))
            assert url.startswith(wiki_membership.API)
            assert "intraday-alpha-platform" in headers["User-Agent"]
            if "revids=" in url:
                rid = int(re.search(r"revids=(\d+)", url).group(1))
                doc = {"query": {"pages": [{"revisions": [{"revid": rid, "slots": {"main": {"content": contents[rid]}}}]}]}}
                return json.dumps(doc).encode()
            m = re.search(r"rvcontinue=(\d+)", url)
            start = int(m.group(1)) if m else 0
            page = revisions[start : start + 2]
            doc = {"query": {"pages": [{"revisions": [{"revid": r, "timestamp": ts} for r, ts in page]}]}}
            if start + 2 < len(revisions):
                doc["continue"] = {"rvcontinue": str(start + 2)}
            return json.dumps(doc).encode()

    return Api({})


def _table(symbols):
    rows = "\n".join(f"|-\n| {{{{NYSE|{s}}}}} || x" for s in symbols)
    return "{| class=\"wikitable\"\n! Symbol !! Security\n" + rows + "\n|}"


def test_wiki_coverage_samples_months_and_counts_only(monkeypatch):
    monkeypatch.setitem(wiki_membership.PAGES, "tiny", ("Tiny index", (3, 4)))
    revs = [
        (1, "2019-12-15T00:00:00Z"),
        (2, "2020-01-05T00:00:00Z"),
        (3, "2020-01-20T00:00:00Z"),
        (4, "2020-02-02T00:00:00Z"),
        (5, "2020-03-01T00:00:00Z"),
    ]
    contents = {
        1: _table(["A"]),  # implausible (vandalised / incomplete)
        2: _table(["A", "B", "C"]),
        4: _table(["A", "B", "D"]),
        5: _table(["A", "B", "D"]),
    }
    api = _wiki_api(revs, contents)
    cov = WikiMembershipSource(api, today=TODAY).coverage(indices=("tiny",))
    assert cov.counts["tiny_revisions"] == 5
    assert cov.ranges["tiny_revision_dates"] == ("2019-12-15", "2020-03-01")
    assert cov.counts["tiny_months_sampled"] == 4  # 2019-12, 2020-01, 2020-02, 2020-03
    assert cov.counts["tiny_months_reliable"] == 3
    assert cov.counts["tiny_months_unparsed_or_implausible"] == 1
    assert cov.ranges["tiny_reliable_revisions"] == ("2020-01-05", "2020-03-01")
    assert cov.counts["tiny_membership_intervals"] == 4  # A, B open; C closed; D open
    assert cov.counts["tiny_closed_intervals"] == 1
    assert cov.counts["tiny_distinct_symbols"] == 4
    assert 3 not in contents  # revision 3 (second in its month) was never fetched
    assert not any("revids=3" in u for u, _ in api.calls)
    text = json.dumps(cov.to_dict())
    assert "\"A\"" not in text and "NYSE" not in text  # counts only, no symbols leave the call
    assert "proxy" in cov.notes[0]


def test_wiki_quote_matches_percent_encoding():
    assert wiki_membership._quote("List of S&P 500 companies") == "List%20of%20S%26P%20500%20companies"
    assert wiki_membership._quote("ids|timestamp") == "ids|timestamp"


# ----------------------------------------------------------------------------- edgar
def test_edgar_form_index_counts():
    counts = edgar.parse_form_index((FIX / "edgar_form_idx_head.txt").read_text(encoding="utf-8"))
    assert counts["8-K"] == 3 and counts["10-Q"] == 2 and counts["10-K"] == 1 and counts["4"] == 1


def _submissions(cik, forms, dates, accepted=None):
    accepted = accepted if accepted is not None else [d + "T16:05:00.000Z" for d in dates]
    return {
        "cik": str(cik),
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": dates,
                "acceptanceDateTime": accepted,
                "accessionNumber": [f"0000000000-26-{i:06d}" for i in range(len(forms))],
            },
            "files": [{"name": "older.json"}] if cik == 1000002 else [],
        },
    }


def test_edgar_coverage_over_a_cik_sample_and_user_agent():
    tickers = {"0": {"cik_str": 1000001, "ticker": "AAA"}, "1": {"cik_str": 1000002, "ticker": "BBB"}, "2": {"cik_str": 1000001}}
    rec = Recorder(
        {
            edgar.TICKERS_URL: json.dumps(tickers),
            edgar.SUBMISSIONS_URL.format(cik=1000001): json.dumps(
                _submissions(1000001, ["8-K", "10-Q", "8-K", "4"], ["2026-09-01", "2026-08-05", "2025-01-10", "2026-09-03"])
            ),
            edgar.SUBMISSIONS_URL.format(cik=1000002): json.dumps(
                _submissions(1000002, ["10-K", "8-K"], ["2026-07-15", "2026-09-02"], accepted=["2026-07-15T06:00:00.000Z", ""])
            ),
        }
    )
    cov = EdgarSource(rec, today=TODAY).coverage(sample_size=2)
    assert cov.counts["ciks_sampled"] == 2
    assert cov.counts["8k_filings"] == 3 and cov.counts["10q_filings"] == 1 and cov.counts["10k_filings"] == 1
    assert cov.counts["8k_with_acceptance_ts"] == 2
    assert cov.ranges["8k_filing_dates"] == ("2025-01-10", "2026-09-02")
    assert cov.counts["ciks_with_older_filing_files"] == 1
    assert cov.requests_made == 3
    for _, h in rec.calls:
        assert h["User-Agent"].startswith("intraday-alpha-platform research ")
    assert EdgarSource.declaration.requests_per_second <= 10
    summary = edgar.summarise_submissions({"cik": "7", "filings": {}})
    assert summary["8-K_count"] == 0 and summary["older_files"] == 0


# ----------------------------------------------------------------------------- french
FRENCH_CSV = (
    "This file was created by CMPT_ME_BEME_RETS using the 202608 CRSP database.\n"
    ",Mkt-RF,SMB,HML,RF\n"
    "19260701,0.10,-0.25,-0.27,0.009\n"
    "19260702,0.45,-0.33,-0.06,0.009\n"
    "20260831,-0.12,0.08,0.31,0.017\n"
)


def test_french_summary_and_coverage():
    info = french.summarise_factor_csv(_zip("F-F_Research_Data_Factors_daily.CSV", FRENCH_CSV))
    assert info == {"rows": 3, "first": "1926-07-01", "last": "2026-08-31", "columns": ["Mkt-RF", "SMB", "HML", "RF"]}
    rec = Recorder({french.BASE: _zip("x.csv", FRENCH_CSV)})
    cov = FrenchSource(rec, today=TODAY).coverage(files={"ff3_daily": "F-F_Research_Data_Factors_daily_CSV.zip"})
    assert cov.counts == {"ff3_daily_rows": 3, "ff3_daily_columns": 4}
    assert cov.ranges["ff3_daily_dates"] == ("1926-07-01", "2026-08-31")
    assert "0.10" not in json.dumps(cov.to_dict())
    with pytest.raises(ValueError):
        french.summarise_factor_csv(b"no rows here")
    assert FrenchSource.declaration.contract.availability.lag_periods >= 20


# ----------------------------------------------------------------------------- the tool
def _tool():
    spec = importlib.util.spec_from_file_location("free_data_coverage", REPO_ROOT / "tools" / "free_data_coverage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tool_runs_adapters_with_an_injected_transport_and_renders():
    tool = _tool()
    rec = Recorder(
        {
            tiingo.SUPPORTED_TICKERS_URL: TIINGO_CSV.encode(),
            french.BASE: _zip("x.csv", FRENCH_CSV),
            stooq.LISTING_URL: (FIX / "stooq_db_h.html").read_text(encoding="utf-8"),
        }
    )
    out = tool.run(["tiingo", "french", "stooq"], transport=rec, today=TODAY)
    assert set(out) == {"tiingo", "french", "stooq"}
    text = tool.render(out)
    assert "stocks_us_inactive_before_cutoff: 1" in text and "ff3_daily_dates: 1926-07-01 .. 2026-08-31" in text

    def broken(url, headers):
        raise ValueError("no network in tests")

    out = tool.run(["french"], transport=broken, today=TODAY)
    assert out["french"]["error"] == "ValueError"
    assert "error: ValueError" in tool.render(out)


def test_tool_transport_refuses_off_list_hosts():
    tool = _tool()
    with pytest.raises(ValueError, match="allow-list"):
        tool.urllib_transport("https://example.com/x", {})
    with pytest.raises(ValueError, match="allow-list"):
        tool.urllib_transport("http://stooq.com/db/h/", {})


def test_tool_main_rejects_unknown_source(capsys):
    tool = _tool()
    with pytest.raises(SystemExit):
        tool.main(["--source", "bloomberg"])

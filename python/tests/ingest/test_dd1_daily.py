"""DD1 ingestion job (``tools/ingest/dd1_daily.py``) on synthetic vendor-format
fixtures: identity refusal, the dry run end to end (four partitions, two of them
through the sealed service's ``ingest`` op, the record the manifest lists), the
five DD1 tests with their positive controls, the Tiingo path, and the rule that
the development identity never learns a value (redacted log, counts-only record)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tools"))

from ingest import dd1_daily as job  # noqa: E402
from ingest import dd1_fixture as fx  # noqa: E402


def _tool(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory):
    t = tmp_path_factory.mktemp("dd1")
    rec = job.dry_run(2026, t / "dev", t / "sealed")
    return t, rec


# ---------------------------------------------------------------- identity --
def test_refuses_any_identity_but_the_runner(tmp_path):
    for who in ("ashis", "iap-sealed-b", "Administrator", "", "IAP-RUNNER "):
        with pytest.raises(job.IngestRefused) as exc:
            job.ingest(year=2026, dev_root=tmp_path, transport=None, identity=who)
        assert exc.value.code == 3
    assert not list(tmp_path.iterdir()), "a refused run writes nothing"


def test_refuses_a_second_ingestion_of_the_same_year(clean_run):
    t, _ = clean_run
    with pytest.raises(job.IngestRefused) as exc:
        job.ingest(year=2026, dev_root=t / "dev", transport=None, identity="iap-runner")
    assert exc.value.code == 3


# ----------------------------------------------------------------- dry run --
def test_dry_run_completes_and_seals_two_partitions(clean_run):
    t, rec = clean_run
    assert rec["status"] == "complete" and "exit" not in rec
    assert rec["dd1_tests"]["pass"] is True
    assert set(rec["partitions"]) == {"development", "validation", "b", "c"}
    for name in ("development", "validation"):
        p = rec["partitions"][name]
        assert p["location"] == "development-readable"
        d = t / "dev" / "dd1_2026" / name
        assert {x.name for x in d.iterdir()} == set(p["files"])
        for n, sha in p["files"].items():
            import hashlib

            assert hashlib.sha256((d / n).read_bytes()).hexdigest() == sha
    for name, part in (("b", "b"), ("c", "c")):
        p = rec["partitions"][name]
        assert p["location"] == f"sealed-{part}"
        ds = json.loads((t / "sealed" / part / "service" / "datasets.json").read_text())
        entry = ds[p["dataset_hash_id"]]
        assert entry["files"] == p["files"]
        assert entry["row_counts"] == p["row_counts"]  # the service's record of the job's counts
        assert (t / "sealed" / part / entry["dir"] / "prices.csv").is_file()
        audit = (t / "sealed" / part / "service" / "audit.jsonl").read_text().splitlines()
        assert json.loads(audit[-1])["decision"] == "ingested"
    # nothing of the sealed partitions lives under the development-readable root
    assert not (t / "dev" / "dd1_2026" / "b").exists()
    assert not (t / "dev" / "dd1_2026" / "c").exists()


def test_partitions_are_cut_by_calendar_and_disjoint(clean_run):
    t, rec = clean_run
    assert rec["windows"] == {
        "development": [None, "2022-12-31"],
        "validation": ["2023-01-01", "2023-12-31"],
        "b": ["2024-01-01", "2024-12-31"],
        "c": ["2025-01-01", None],
    }
    dates = {}
    for name, part in (("development", None), ("validation", None), ("b", "b"), ("c", "c")):
        root = t / "dev" / "dd1_2026" / name if part is None else t / "sealed" / part
        if part:
            ds = json.loads((root / "service" / "datasets.json").read_text())
            root = root / next(iter(ds.values()))["dir"]
        rows = (root / "prices.csv").read_text().splitlines()[1:]
        dates[name] = {r.split(",")[1] for r in rows}
    for a in dates:
        for b in dates:
            if a != b:
                assert not (dates[a] & dates[b]), f"{a} and {b} share sessions"
    assert max(dates["development"]) <= "2022-12-31"
    assert min(dates["c"]) >= "2025-01-01"
    # the B/C boundary: Track B's test year and Track C's confirmation are disjoint (G3)
    assert max(dates["b"]) < min(dates["c"])


def test_record_and_log_carry_counts_and_hashes_only(clean_run):
    t, rec = clean_run
    text = json.dumps({k: v for k, v in rec.items() if k != "log"})
    for sym in fx.SYMBOLS:
        assert re.search(rf"\b{sym}\b", text) is None, f"symbol {sym} leaked into the record"
    assert "\\\\" not in rec["log"] and str(t) not in rec["log"]
    assert "C:" not in rec["log"]
    assert re.search(r"\d+\.\d{4}", rec["log"]) is None, "no price-like number in the log"
    dev = rec["partitions"]["development"]
    assert set(dev["row_counts"]) == {"prices.csv", "membership.csv", "filings.csv", "factors.csv"}
    assert dev["row_counts"]["membership.csv"] == 22
    assert rec["evidence_class"] == "mechanically isolated"


def test_membership_rows_carry_the_proxy_label_and_open_intervals_at_window_end(clean_run):
    t, _ = clean_run
    rows = (t / "dev" / "dd1_2026" / "development" / "membership.csv").read_text().splitlines()
    head, body = rows[0].split(","), [r.split(",") for r in rows[1:]]
    assert head == list(job.MEMBERSHIP_FIELDS)
    labels = {r[head.index("label")] for r in body}
    assert labels == {"wikipedia-proxy"}
    ends = [r[head.index("end")] for r in body if r[head.index("index")] == "sp500"]
    assert ends.count("") >= 10, "names still in the index at the window end stay open"
    closed = [r for r in body if r[head.index("end")] and r[head.index("index")] == "sp500"]
    assert len(closed) == 1 and closed[0][head.index("symbol")] == fx.DELISTED


# ------------------------------------------------------------- the tests --
@pytest.mark.parametrize(
    "knob, failing",
    [
        ({"truncate_delisted": True}, "survivorship"),
        ({"unadjusted_split": True}, "corporate_actions"),
        ({"drop_sessions": 3}, "calendar"),
    ],
)
def test_positive_controls_fail_the_named_test_and_seal_nothing(tmp_path, knob, failing):
    rec = job.dry_run(2026, tmp_path / "dev", tmp_path / "sealed", **knob)
    assert rec["status"] == "incomplete" and rec["exit"] == 2
    assert rec["dd1_tests"]["results"][failing]["pass"] is False
    assert "b" not in rec["partitions"] and "c" not in rec["partitions"]
    assert not (tmp_path / "sealed" / "b" / "datasets").exists()
    assert (tmp_path / "dev" / "dd1_2026" / "development" / "prices.csv").is_file()


def test_membership_test_positive_controls():
    good = [
        {"index": "sp500", "symbol": "AAA", "start": "2020-01-02", "end": "2020-06-01"},
        {"index": "sp500", "symbol": "AAA", "start": "2020-06-01", "end": None},
    ]
    prices = [{"symbol": "AAA", "date": "2020-03-02"}]
    per_year = {"2020": {"reliable_share": 1.0, "median_gap_days": 30}}
    ok = job.membership_test(good, prices, per_year, ("2020-01-01", "2020-12-31"))
    assert ok["pass"] and ok["overlaps"] == 0
    overlap = [dict(good[0]), {**good[1], "start": "2020-05-01"}]
    assert (
        job.membership_test(overlap, prices, per_year, ("2020-01-01", "2020-12-31"))["overlaps"]
        == 1
    )
    bad_order = [{**good[0], "end": "2019-12-31"}]
    assert (
        job.membership_test(bad_order, prices, per_year, ("2020-01-01", "2020-12-31"))["bad_order"]
        == 1
    )
    unpriced = job.membership_test(good, [], per_year, ("2020-01-01", "2020-12-31"))
    assert unpriced["members_without_prices"] == 1 and not unpriced["pass"]
    weak = {"2020": {"reliable_share": 0.5, "median_gap_days": 30}}
    res = job.membership_test(good, prices, weak, ("2020-01-01", "2020-12-31"))
    assert res["years_excluded"] == ["2020"] and not res["pass"]


def test_open_close_convention_must_be_documented_and_opens_not_prev_closes():
    rows = [
        {"symbol": "A", "date": "2020-01-02", "open": 1.0, "close": 2.0},
        {"symbol": "A", "date": "2020-01-03", "open": 2.0, "close": 3.0},
    ]
    assert not job.open_close_test(rows, "")["pass"]
    assert not job.open_close_test(rows, "documented")["pass"]  # open == previous close
    rows[1]["open"] = 2.5
    assert job.open_close_test(rows, "documented")["pass"]


def test_tiingo_path_when_the_key_is_present(tmp_path):
    rec = job.dry_run(2026, tmp_path / "dev", tmp_path / "sealed", tiingo=True)
    assert rec["status"] == "complete"
    assert rec["sources"]["prices"] == "tiingo"
    ca = rec["dd1_tests"]["results"]["corporate_actions"]
    assert ca["split_days"] == 1 and ca["split_days_reconciled"] == 1
    assert rec["log"].count("dry-run") == 0, "the key never reaches the log"


def test_stooq_is_the_default_without_a_key(clean_run):
    _, rec = clean_run
    assert rec["sources"]["prices"] == "stooq"
    assert rec["requests"] > 100  # every revision parsed, every quarter index read


# ------------------------------------------- the owner's Stooq download (DR-15) --
def test_local_stooq_archive_replaces_both_stooq_requests(tmp_path):
    rec = job.dry_run(2026, tmp_path / "dev", tmp_path / "sealed", local_zip=True)
    assert rec["status"] == "complete"
    assert rec["sources"]["prices"] == "stooq"
    assert "local Stooq archive verified" in rec["log"]
    assert rec["requests"] > 100


def test_local_stooq_archive_with_the_wrong_sha256_refuses_before_any_request(tmp_path):
    rec = job.dry_run(
        2026, tmp_path / "dev", tmp_path / "sealed", local_zip=True, sha256_override="0" * 64
    )
    assert rec["status"] == "refused" and rec["exit"] == 3
    assert rec["requests"] == 0
    assert not (tmp_path / "dev").exists()


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64])
def test_local_stooq_archive_needs_a_recorded_sha256(tmp_path, digest):
    z = tmp_path / "d_us_txt.zip"
    z.write_bytes(b"PK\x05\x06" + b"\0" * 18)  # an empty zip
    with pytest.raises(job.IngestRefused) as exc:
        job.read_local_archive(z, digest)
    assert exc.value.code == 3


def test_local_stooq_archive_that_is_not_a_zip_refuses(tmp_path):
    import hashlib

    z = tmp_path / "d_us_txt.zip"
    z.write_bytes(b"<!DOCTYPE html><html>browser check</html>")
    with pytest.raises(job.IngestRefused, match="not a zip"):
        job.read_local_archive(z, hashlib.sha256(z.read_bytes()).hexdigest())
    with pytest.raises(job.IngestRefused, match="missing"):
        job.read_local_archive(tmp_path / "absent.zip", "0" * 64)


def test_a_browser_check_page_from_stooq_is_a_named_refusal():
    pages = {
        job.stooq.LISTING_URL: b"<html></html>",
        job.STOOQ_BULK_URL: b"<!DOCTYPE html><html>This site requires JavaScript</html>",
    }

    def transport(url, headers):
        return pages[url]

    tick = [0.0]

    def clock() -> float:  # advances, so the rate limiter never waits
        tick[0] += 10.0
        return tick[0]

    with pytest.raises(job.IngestRefused, match="--stooq-zip") as exc:
        job.pull_prices_stooq(
            transport, {"AAPL"}, adapter_kw={"clock": clock, "sleep": lambda _s: None}
        )
    assert exc.value.code == 2


def test_sealed_refusal_leaves_the_record_incomplete(tmp_path):
    def refusing(part, request, files):  # answers the preflight, refuses the ingest itself
        if request.get("op") == "status":
            return {"ok": True, "partition": part, "pending": None}
        return {"ok": False, "error": "caller_account"}

    f = fx.build(2026)
    log = job.redact.RedactingLog([])
    tick = [0.0]

    def clock():
        tick[0] += 10
        return tick[0]

    with pytest.raises(job.IngestRefused) as exc:
        job.ingest(
            year=2026,
            dev_root=tmp_path,
            transport=f.transport,
            sealed_transport=refusing,
            identity="iap-runner",
            environ={},
            today=f.last,
            plausible=job.DRY_RUN_PLAUSIBLE,
            adapter_kw={"clock": clock, "sleep": lambda _s: None},
            log=log,
            require_sec_contact=False,
        )
    assert exc.value.code == 2
    rec = json.loads((tmp_path / "dd1_2026" / "dataset.json").read_text())
    assert rec["status"] == "incomplete"
    assert rec["partitions"]["b"]["refused"] == "caller_account"
    assert "refused" in log.text()


# --------------------------------------------------------------- manifest --
def test_data_manifest_lists_the_ingestion_record_with_partition_ids(clean_run, tmp_path):
    t, rec = clean_run
    dm = _tool("data_manifest_dd1", "tools/data_manifest.py")
    doc = dm.generate(t / "dev")
    entries = [d for d in doc["datasets"] if d["kind"] == "daily_pit"]
    assert len(entries) == 1
    e = entries[0]
    assert e["path"] == "dd1_2026" and e["evidence"]["verdict"] == "complete"
    assert set(e["partitions"]) == {"development", "validation", "b", "c"}
    assert e["partitions"]["b"]["row_counts"] == rec["partitions"]["b"]["row_counts"]
    ids = dm.partition_ids(doc)
    assert rec["partitions"]["c"]["dataset_hash_id"] in ids
    assert rec["partitions"]["c"]["dataset_hash_id"] in dm.listed_versions(doc)
    # an incomplete record's partitions fail the citation check
    bad = json.loads(json.dumps(doc))
    bad["datasets"][0]["evidence"]["verdict"] = "incomplete"
    cited = {rec["partitions"]["b"]["dataset_hash_id"]}
    assert dm.evidence_problems(bad, cited)
    assert not dm.evidence_problems(doc, cited)
    # nothing in the manifest entry is a value: symbols and prices never appear
    text = json.dumps(e)
    for sym in fx.SYMBOLS:
        assert re.search(rf"\b{sym}\b", text) is None


# --------------------------------------------------------- the CLI entry --
def test_cli_dry_run_exit_codes(tmp_path, capsys):
    code = job.main(
        [
            "run",
            "--dry-run",
            "--year",
            "2026",
            "--dev-root",
            str(tmp_path / "dev"),
            "--sealed-root",
            str(tmp_path / "sealed"),
            "--log",
            str(tmp_path / "log.txt"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"status": "complete"' in out
    assert str(tmp_path) not in (tmp_path / "log.txt").read_text()
    assert (tmp_path / "dev" / "dd1_2026" / "dataset.json").is_file()


def test_windows_follow_u0_section_2():
    assert job.windows(2030) == {
        "development": (None, "2026-12-31"),
        "validation": ("2027-01-01", "2027-12-31"),
        "b": ("2028-01-01", "2028-12-31"),
        "c": ("2029-01-01", None),
    }


def test_form_index_parser_reads_fixed_width_rows():
    text = (
        "header\n---\n"
        f"{'8-K':<12}{'X Corp':<62}{'1001':<12}{'2020-02-03':<12}edgar/data/1001/x.txt\n"
        f"{'S-1':<12}{'Y Corp':<62}{'1002':<12}{'2020-02-04':<12}edgar/data/1002/y.txt\n"
        "garbage line\n"
    )
    rows = list(job.parse_form_index_rows(text))
    assert rows == [
        ("8-K", 1001, "2020-02-03", "edgar/data/1001/x.txt"),
        ("S-1", 1002, "2020-02-04", "edgar/data/1002/y.txt"),
    ]


# ------------------------------------------------- revision cache (D-87) --
def test_a_rerun_with_the_cache_fetches_no_revision_again(tmp_path):
    cache = tmp_path / "runner" / "cache"
    first = job.dry_run(2026, tmp_path / "dev1", tmp_path / "sealed1", cache_root=cache)
    assert first["status"] == "complete"
    assert first["wiki_content_requests"] > 0
    stored = len(list((cache / "wiki_revisions").glob("*.json")))
    assert stored == first["wiki_content_requests"]
    assert "0 revision cache hits" in first["log"]

    second = job.dry_run(2026, tmp_path / "dev2", tmp_path / "sealed2", cache_root=cache)
    assert second["status"] == "complete"
    assert second["wiki_content_requests"] == 0
    assert f"{stored} revision cache hits" in second["log"]
    assert f"{second['wiki_requests']} Wikipedia requests" in second["log"]
    # the cache changes no value: every partition's membership file is the same
    for part in ("development", "validation", "b", "c"):
        f1, f2 = first["partitions"][part]["files"], second["partitions"][part]["files"]
        names = [n for n in f1 if n.startswith("membership")]
        assert names and all(f1[n] == f2[n] for n in names)


def test_a_corrupt_cache_entry_is_fetched_again(tmp_path):
    cache = tmp_path / "cache"
    job.dry_run(2026, tmp_path / "dev1", tmp_path / "sealed1", cache_root=cache)
    victim = sorted((cache / "wiki_revisions").glob("*.json"))[0]
    victim.write_text("not json", encoding="utf-8")
    rec = job.dry_run(2026, tmp_path / "dev2", tmp_path / "sealed2", cache_root=cache)
    assert rec["status"] == "complete" and rec["wiki_content_requests"] == 1


@pytest.mark.parametrize("sub", ["", "cache", "dd1_2026/wiki"])
def test_refuses_a_cache_root_under_the_dev_root(tmp_path, sub):
    dev = tmp_path / "dev"
    rec = job.dry_run(2026, dev, tmp_path / "sealed", cache_root=dev / sub)
    assert rec["status"] == "refused" and rec["exit"] == 3
    assert rec["requests"] == 0
    assert not dev.exists() or not any(dev.rglob("*")), "a refused run writes nothing"


def test_cli_refuses_a_cache_root_under_the_dev_root(tmp_path):
    code = job.main(
        [
            "run",
            "--year",
            "2026",
            "--dev-root",
            str(tmp_path / "dev"),
            "--cache-root",
            str(tmp_path / "dev" / "cache"),
        ]
    )
    assert code == 3


def test_the_default_cache_root_is_the_runners_sealed_directory():
    from iap.data.sources.base import is_sealed_target

    assert is_sealed_target(str(job.DEFAULT_CACHE_ROOT))
    assert not job._under(job.DEFAULT_CACHE_ROOT, job.Path(r"C:\iap\daily"))


# ------------------------------------- SEC contact and source preflight (D-88) --
@pytest.mark.parametrize(
    "ua, ok",
    [
        ("intraday-alpha-platform research https://github.com/x/y", False),
        ("intraday-alpha-platform research Jane Doe", False),
        ("intraday-alpha-platform research Jane Doe jane@example.com", True),
    ],
)
def test_sec_contact_needs_an_email(ua, ok):
    assert job.sec_contact_ok(ua) is ok


def _tick_clock():
    tick = [0.0]

    def clock() -> float:  # advances, so the rate limiter never waits
        tick[0] += 10.0
        return tick[0]

    return clock


def test_ingest_without_an_sec_contact_refuses_before_any_request(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        job.edgar,
        "DECLARATION",
        dataclasses.replace(job.edgar.DECLARATION, user_agent="iap research https://x"),
    )
    calls = []

    def transport(url, headers):
        calls.append(url)
        raise AssertionError("no request may be made")

    with pytest.raises(job.IngestRefused, match="IAP_SEC_CONTACT") as exc:
        job.ingest(year=2026, dev_root=tmp_path / "dev", transport=transport, identity="iap-runner")
    assert exc.value.code == 3 and calls == []


def test_a_403_from_edgar_stops_the_job_before_the_membership_walk(tmp_path):
    import io
    import urllib.error
    from urllib.parse import urlsplit

    calls = []

    def transport(url, headers):
        calls.append(urlsplit(url).hostname)
        if urlsplit(url).hostname == "www.sec.gov":
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b""))
        raise AssertionError(f"request after the EDGAR refusal: {url}")

    with pytest.raises(job.IngestRefused, match="EDGAR company tickers answered HTTP 403") as exc:
        job.ingest(
            year=2026,
            dev_root=tmp_path / "dev",
            transport=transport,
            identity="iap-runner",
            require_sec_contact=False,
            adapter_kw={"clock": _tick_clock(), "sleep": lambda _s: None},
        )
    assert exc.value.code == 2
    assert calls == ["www.sec.gov"], "the membership walk must not have started"


def test_preflight_runs_before_the_membership_walk(clean_run):
    _, rec = clean_run
    log = rec["log"]
    assert log.index("preflight passed") < log.index("membership ")


# ------------------------------------ run hardening after run 40294681732 (D-96..D-100) --
class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self.body


def _http_error(code: int, retry_after: str | None = None):
    import email.message
    import io
    import urllib.error

    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://www.sec.gov/x", code, "x", h, io.BytesIO(b""))


def test_transport_retries_busy_answers_then_succeeds():
    answers = [_http_error(503), _http_error(429, retry_after="7"), _Resp(b"ok")]
    slept = []

    def opener(req, timeout):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    out = job.urllib_transport("https://www.sec.gov/x", {}, sleep=slept.append, opener=opener)
    assert out == b"ok"
    assert slept == [job.RETRY_WAITS[0], 7.0], "scheduled wait, then the Retry-After value"


def test_transport_gives_up_after_the_last_wait_and_never_retries_a_404():
    import urllib.error

    calls = [0]

    def busy(req, timeout):
        calls[0] += 1
        raise _http_error(503)

    with pytest.raises(urllib.error.HTTPError):
        job.urllib_transport("https://www.sec.gov/x", {}, sleep=lambda s: None, opener=busy)
    assert calls[0] == len(job.RETRY_WAITS) + 1

    calls[0] = 0

    def missing(req, timeout):
        calls[0] += 1
        raise _http_error(404)

    with pytest.raises(urllib.error.HTTPError):
        job.urllib_transport("https://www.sec.gov/x", {}, sleep=lambda s: None, opener=missing)
    assert calls[0] == 1


def _edgar_transport(seen):
    def transport(url, headers):
        seen.append(url)
        if url == job.edgar.TICKERS_URL:
            return b'{"0": {"cik_str": 320193, "ticker": "AAPL"}}'
        row = f"{'10-Q':<12}{'APPLE INC':<62}{320193:<12}{'1993-05-02':<12}edgar/data/320193/x.txt"
        head = "Form Type   Company Name" + " " * 50 + "CIK         Date Filed  File Name"
        return ("\n".join([head, "-" * 140, row]) + "\n").encode()

    return transport


def test_filings_start_at_1993_and_finished_quarters_come_from_the_cache(tmp_path):
    import datetime as dt

    seen: list[str] = []
    kw = {"clock": _tick_clock(), "sleep": lambda _s: None}
    today = dt.date(2026, 10, 26)
    first, _ = job.pull_filings(
        _edgar_transport(seen),
        {"AAPL"},
        range(1990, 1995),
        today=today,
        adapter_kw=kw,
        cache_root=tmp_path,
    )
    assert first, "the fixed-width row parses"
    idx = [u for u in seen if "full-index" in u]
    assert idx and all("/1990/" not in u and "/1992/" not in u for u in idx)
    assert len(idx) == 8, "1993 and 1994, four quarters each"
    seen.clear()
    rows, meta = job.pull_filings(
        _edgar_transport(seen),
        {"AAPL"},
        range(1990, 1995),
        today=today,
        adapter_kw=kw,
        cache_root=tmp_path,
    )
    assert [u for u in seen if "full-index" in u] == [], "every finished quarter is cached"
    assert meta["quarters_from_cache"] == 8
    assert rows == first, "the cache returns what the network returned"


def test_the_running_quarter_is_never_cached(tmp_path):
    import datetime as dt

    seen: list[str] = []
    kw = {"clock": _tick_clock(), "sleep": lambda _s: None}
    for _ in range(2):
        job.pull_filings(
            _edgar_transport(seen),
            {"AAPL"},
            [2026],
            today=dt.date(2026, 10, 26),
            adapter_kw=kw,
            cache_root=tmp_path,
        )
    q4 = [u for u in seen if "/2026/QTR4/" in u]
    assert len(q4) == 2, "Q4 2026 is still being written by EDGAR: fetched each run"


def _ingest_kw(f, tmp_path, sealed):
    return dict(
        year=2026,
        dev_root=tmp_path / "dev",
        transport=f.transport,
        sealed_transport=sealed,
        identity="iap-runner",
        environ={},
        today=f.last,
        plausible=job.DRY_RUN_PLAUSIBLE,
        adapter_kw={"clock": _tick_clock(), "sleep": lambda _s: None},
        require_sec_contact=False,
    )


@pytest.mark.parametrize(
    "answer, reason",
    [
        ({"ok": False, "error": "caller_image"}, "refused this caller"),
        ({"ok": True, "pending": {"seq": 3}}, "pending checkpoint"),
    ],
)
def test_sealed_services_are_checked_before_the_membership_walk(tmp_path, answer, reason):
    f = fx.build(2026)

    def sealed(part, request, files):
        assert request["op"] == "status", "nothing but status before the data exist"
        return answer

    with pytest.raises(job.IngestRefused, match=reason) as exc:
        job.ingest(**_ingest_kw(f, tmp_path, sealed))
    assert exc.value.code == 2
    assert not any("wikipedia" in u and "rvprop" in u for u in f.requests)


def test_an_already_sealed_identical_partition_completes_the_record(tmp_path):
    f = fx.build(2026)

    def sealed(part, request, files):
        if request["op"] == "status":
            return {"ok": True, "pending": None}
        return {"ok": False, "error": "dataset_exists"}

    rec = job.ingest(**_ingest_kw(f, tmp_path, sealed))
    assert rec["status"] == "complete"
    for name in ("b", "c"):
        assert rec["partitions"][name]["note"].startswith("already recorded")
        assert len(rec["partitions"][name]["dataset_hash_id"]) == 64


def test_probe_touches_every_source_and_writes_no_data(tmp_path):
    f = fx.build(2026)
    calls = []

    def sealed(part, request, files):
        calls.append((part, request["op"]))
        return {"ok": True, "pending": None}

    rec = job.ingest(**_ingest_kw(f, tmp_path, sealed), probe=True, cache_root=tmp_path / "cache")
    assert rec == {"status": "probe_passed"}
    assert sorted(calls) == [("b", "status"), ("c", "status")]
    hosts = {u.split("/")[2] for u in f.requests}
    assert {"www.sec.gov", "mba.tuck.dartmouth.edu", "apimedia.tiingo.com"} <= hosts
    assert any("wikipedia" in h for h in hosts)
    assert any("full-index/1993/QTR1" in u for u in f.requests)
    assert not (tmp_path / "dev" / "dd1_2026").exists(), "a probe writes no partition or record"
    assert len(f.requests) < 60, "a probe is a sample, not the walk"


def test_the_redacted_log_is_written_when_the_job_crashes(tmp_path, monkeypatch):
    def boom(**kw):
        kw["log"]("membership sp500: 10 revisions")
        raise RuntimeError("simulated crash mid-run")

    monkeypatch.setattr(job, "ingest", boom)
    out = tmp_path / "log.txt"
    with pytest.raises(RuntimeError):
        job.main(["run", "--dev-root", str(tmp_path / "dev"), "--log", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "membership sp500" in text and "failed: RuntimeError" in text


# ----------------------------------------- development-only ingestion (DR-18) --
def test_dev_only_never_contacts_the_sealed_services_and_saves_no_test_years(tmp_path):
    f = fx.build(2026)

    def sealed(part, request, files):
        raise AssertionError("a development-only run must not contact a sealed service")

    rec = job.ingest(**_ingest_kw(f, tmp_path, sealed), dev_only=True)
    assert rec["status"] == "development_only"
    for name in ("b", "c"):
        p = rec["partitions"][name]
        assert p["location"] == "not saved" and "files" not in p and "dataset_hash_id" not in p
    for name in ("development", "validation"):
        assert len(rec["partitions"][name]["dataset_hash_id"]) == 64
    written = {q.name for q in (tmp_path / "dev" / "dd1_2026").iterdir()}
    assert written == {"development", "validation", "dataset.json"}, "no b/ or c/ on disk"


def test_the_panel_reads_a_development_only_record(tmp_path):
    from iap.research import dd1_panel

    f = fx.build(2026)

    def sealed(part, request, files):
        raise AssertionError("not contacted")

    job.ingest(**_ingest_kw(f, tmp_path, sealed), dev_only=True)
    rec = dd1_panel.read_record(tmp_path / "dev" / "dd1_2026")
    assert rec["status"] == "development_only"


# ------------------------------------- real-format parsers (D-104, D-105) --
#: three rows of the real 2026 Q4 form.idx (public SEC data): a 17-wide form column under
#: a 12-wide header, a form type with a space, trailing blanks as served
REAL_IDX = (
    "Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
    + "-" * 141
    + "\n"
    "1-A POS          Ark7 Properties Advance LLC                                   2025051     2026-10-19  edgar/data/2025051/0002025051-26-000007.txt         \n"
    "8-K              1 800 FLOWERS COM INC                                         1084869     2026-10-23  edgar/data/1084869/0001084869-26-000041.txt         \n"
    "10-Q             1ST SOURCE CORP                                               34782       2026-10-22  edgar/data/34782/0000034782-26-000055.txt         \n"
)


def test_form_index_rows_parse_the_real_2026_layout():
    rows = list(job.parse_form_index_rows(REAL_IDX))
    assert rows == [
        ("1-A POS", 2025051, "2026-10-19", "edgar/data/2025051/0002025051-26-000007.txt"),
        ("8-K", 1084869, "2026-10-23", "edgar/data/1084869/0001084869-26-000041.txt"),
        ("10-Q", 34782, "2026-10-22", "edgar/data/34782/0000034782-26-000055.txt"),
    ]


def test_ndx100_membership_walks_the_list_page_too():
    f = fx.build(2026)
    rows, meta = job.pull_membership(
        f.transport,
        plausible=job.DRY_RUN_PLAUSIBLE,
        adapter_kw={"clock": _tick_clock(), "sleep": lambda _s: None},
    )
    assert meta["ndx100"]["pages"] == ["Nasdaq-100", "List of NASDAQ-100 companies"]
    assert any("List" in u and "NASDAQ-100" in u for u in f.requests)
    assert any(r["index"] == "ndx100" for r in rows)


def test_probe_refuses_when_the_current_edgar_quarter_parses_to_nothing(tmp_path):
    f = fx.build(2026)
    last = f.last
    q = (last.month - 1) // 3 + 1
    key = f"https://www.sec.gov/Archives/edgar/full-index/{last.year}/QTR{q}/form.idx"
    f.urls[key] = b"Form Type   Company Name\n" + b"-" * 40 + b"\nsomething unparseable\n"

    def sealed(part, request, files):
        return {"ok": True, "pending": None}

    with pytest.raises(job.IngestRefused, match="parsed to no rows"):
        job.ingest(**_ingest_kw(f, tmp_path, sealed), probe=True)

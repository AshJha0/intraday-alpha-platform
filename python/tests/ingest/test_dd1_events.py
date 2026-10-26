"""DD1 events job (``tools/ingest/dd1_events.py``) on the synthetic EDGAR fixture:
identity refusal, the dry run end to end with the DD1 partition windows, the
point-in-time cut, the tests' positive controls and the redacted log."""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import sys

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "tools"))

from ingest import dd1_daily  # noqa: E402
from ingest import dd1_events as job  # noqa: E402
from ingest import dd1_events_fixture as fx  # noqa: E402


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory):
    t = tmp_path_factory.mktemp("dd1ev")
    return t, job.dry_run(2026, t / "dev", t / "sealed")


def _rows(t, part):
    text = (t / "dev" / "dd1_events_2026" / part / "earnings_8k.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def test_refuses_any_identity_but_the_runner(tmp_path):
    for who in ("ashis", "iap-sealed-b", "", "IAP-RUNNER "):
        with pytest.raises(job.IngestRefused) as exc:
            job.ingest(year=2026, dev_root=tmp_path, universe=["A"], transport=None, identity=who)
        assert exc.value.code == 3
    assert not list(tmp_path.iterdir())


def test_refuses_empty_universe_and_second_ingestion(clean_run, tmp_path):
    with pytest.raises(job.IngestRefused):
        job.ingest(year=2026, dev_root=tmp_path, universe=[], transport=None, identity="iap-runner")
    t, _ = clean_run
    with pytest.raises(job.IngestRefused) as exc:
        job.ingest(
            year=2026, dev_root=t / "dev", universe=["A"], transport=None, identity="iap-runner"
        )
    assert exc.value.code == 3


def test_dry_run_completes_with_dd1_windows(clean_run):
    t, rec = clean_run
    assert rec["status"] == "complete" and "exit" not in rec, rec.get("log")
    assert rec["windows"] == {k: list(v) for k, v in dd1_daily.windows(2026).items()}
    assert set(rec["partitions"]) == {"development", "validation", "b", "c"}
    assert rec["partitions"]["b"]["location"] == "sealed-b"
    v = rec["vendor"]
    assert v["symbols_with_cik"] == 2 and v["header_failures"] == 0
    assert v["dropped"]["amendment_of_original"] == 5  # one per year's Q2
    assert all(v["timing"][k] > 0 for k in ("before_open", "during", "after_close", "non_session"))
    assert v["calendar_extended"] > 0  # the French files end before ``today``
    assert rec["requests"] == v["requests"] + 3 and rec["outside_calendar"] == 0
    assert not any("9999999" in u for u in fx.build(2026).urls if "x.txt" in u)


def test_partitions_cut_by_usable_session(clean_run):
    t, _ = clean_run
    for part, (lo, hi) in (
        ("development", (None, "2022-12-31")),
        ("validation", ("2023-01-01", "2023-12-31")),
    ):
        rows = _rows(t, part)
        assert rows
        for r in rows:
            assert (lo is None or r["usable_session"] >= lo) and r["usable_session"] <= hi
            assert r["usable_session"] > r["as_of_session"]
            assert (r["timing"] == "before_open") == (
                r["usable_session"] == r["acceptance_et"][:10]
            )


def test_accepted_late_dated_next_day_uses_acceptance(clean_run):
    t, _ = clean_run
    late = [
        r
        for p in ("development", "validation")
        for r in _rows(t, p)
        if r["acceptance_et"][11:16] == "17:45"
    ]
    assert late and all(r["date_filed"] > r["acceptance_et"][:10] for r in late)
    assert all(
        r["timing"] == "after_close" and r["as_of_session"] == r["acceptance_et"][:10] for r in late
    )


def test_standalone_amendment_kept_in_its_partition(clean_run):
    t, rec = clean_run
    # the fixture's standalone 8-K/A is in year Y-2 = Track B (sealed): counted, not read
    assert rec["partitions"]["b"]["row_counts"]["earnings_8k.csv"] > 0
    dev = _rows(t, "development") + _rows(t, "validation")
    assert all(r["amendment_only"] == "0" for r in dev)


def test_log_and_record_reveal_no_identifiers(clean_run):
    t, rec = clean_run
    text = rec["log"] + json.dumps({k: v for k, v in rec.items() if k != "log"})
    for needle in ("1000001", "1000002", "AAA", "BBB", "17:45"):
        assert needle not in text


def test_unreadable_headers_fail_the_parse_test_and_seal_nothing(tmp_path, monkeypatch):
    real = fx.build
    monkeypatch.setattr(fx, "build", lambda year, **kw: real(year, unreadable=5))
    rec = job.dry_run(2026, tmp_path / "dev", tmp_path / "sealed")
    assert rec["status"] == "incomplete" and rec["exit"] == 2
    assert rec["events_tests"]["results"]["header_parse"]["pass"] is False
    assert "b" not in rec["partitions"]


def test_session_tests_positive_controls():
    meta = {"headers_read": 1, "header_failures": 0}
    bad_order = {
        "as_of_session": "2024-02-02",
        "usable_session": "2024-02-02",
        "timing": "after_close",
        "acceptance_et": "2024-02-02T16:30:00",
    }
    assert job.events_tests([bad_order], meta)["results"]["session_order"]["pass"] is False
    bad_timing = {
        "as_of_session": "2024-02-01",
        "usable_session": "2024-02-05",
        "timing": "before_open",
        "acceptance_et": "2024-02-02T16:30:00",
    }
    assert job.events_tests([bad_timing], meta)["results"]["timing_rule"]["pass"] is False


def test_expected_requests_figure():
    e = job.expected_requests(650, 2004, _dt.date(2026, 10, 26))
    assert e["quarters"] == 92 and e["headers"] == 650 * 23 * 12
    assert e["requests"] == 4 + 92 + e["headers"]


def test_cli_dry_run(tmp_path, capsys):
    assert (
        job.main(
            [
                "run",
                "--dry-run",
                "--year",
                "2026",
                "--dev-root",
                str(tmp_path / "d"),
                "--sealed-root",
                str(tmp_path / "s"),
            ]
        )
        == 0
    )
    assert '"status": "complete"' in capsys.readouterr().out

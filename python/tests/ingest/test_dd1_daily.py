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


def test_sealed_refusal_leaves_the_record_incomplete(tmp_path):
    def refusing(part, request, files):
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

"""v1.18 REL-04c: the universe rule and the job's refusals.

Synthetic fixtures only (a tiny encoded ITCH day, fake ingest / benchmark /
registry seams); no real dataset or ITCH file is read."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from iap.research import rel04c_job as job
from iap.research import rel04c_universe as ru
from itch50_encoder import Itch50Encoder, hms_ns

REPO = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ rule --
def _row(sym, vol, price, **kw):
    r = {
        "symbol": sym,
        "displayed_volume": vol,
        "close_cross_price_e4": int(price * 10_000) if price is not None else None,
        "last_print_price_e4": None,
        "etp": False,
        "test_security": False,
        "market_category": "Q",
        "issue_classification": "C",
    }
    r.update(kw)
    return r


def _measured(rows):
    return {"symbols": {r["symbol"]: r for r in rows}, "messages_read": 1, "limit_reached": False}


def _market(n=1600):
    """n names, volume descending with rank; price cycles through the buckets."""
    prices = (8.0, 50.0, 150.0)
    rows = [_row(f"S{i:04d}", 10_000_000 - i, prices[i % 3]) for i in range(n)]
    rows.append(_row("AAPL", 20_000_000, 235.0))
    rows.append(_row("MSFT", 19_000_000, 137.0))
    return rows


def _all(rows):
    syms = [r["symbol"] for r in rows]
    return {"2021-07-13": syms, "2021-08-13": syms}


def test_rule_picks_two_per_cell_plus_anchors():
    rows = _market()
    doc = ru.select(_measured(rows), present_later=_all(rows))
    assert doc["rule_version"] == ru.RULE_VERSION
    assert doc["symbols"][:2] == ["AAPL", "MSFT"]
    assert len(doc["symbols"]) == 20 and len(set(doc["symbols"])) == 20
    assert all(c["shortfall"] == 0 for c in doc["cells"].values()) and len(doc["cells"]) == 9
    by = {r["symbol"]: r for r in doc["universe"]}
    assert by["AAPL"]["liq_bucket"] == "L1" and by["AAPL"]["price_bucket"] == "P3"
    for r in doc["universe"]:
        lo, hi = {b[0]: (b[1], b[2]) for b in ru.LIQ_BUCKETS}[r["liq_bucket"]]
        assert lo <= r["rank"] <= hi
    assert sum(len(v) for v in doc["buckets"].values()) == 20


def test_rule_cell_positions_are_the_terciles_by_rank():
    rows = _market()
    doc = ru.select(_measured(rows), present_later=_all(rows))
    # L1 x P1: ranks 1-100 minus the two anchors (ranks 1, 2); names at index 0, 3, 6, ...
    l1p1 = [r for r in doc["universe"] if r["liq_bucket"] == "L1" and r["price_bucket"] == "P1"]
    n = doc["cells"]["L1P1"]["n_candidates"]
    assert n == 33
    cands = sorted(
        (r for r in rows if r["symbol"].startswith("S") and int(r["symbol"][1:]) % 3 == 0),
        key=lambda r: -r["displayed_volume"],
    )[:n]
    assert [r["symbol"] for r in l1p1] == [cands[n // 3]["symbol"], cands[2 * n // 3]["symbol"]]


def test_rule_filters_and_records_shortfall():
    rows = [
        _row("AAPL", 100, 200.0),
        _row("MSFT", 90, 120.0),
        _row("ETF1", 80, 50.0, etp=True),
        _row("TEST", 70, 50.0, test_security=True),
        _row("NYSE", 60, 50.0, market_category="N"),
        _row("PENNY", 50, 2.0),
        _row("PREF", 45, 50.0, issue_classification="P"),
        _row("GONE", 40, 50.0),
        _row("OK", 30, 50.0),
    ]
    later = {"2021-07-13": ["AAPL", "MSFT", "OK"], "2021-08-13": ["AAPL", "MSFT", "OK"]}
    doc = ru.select(_measured(rows), present_later=later)
    assert doc["symbols"] == ["AAPL", "MSFT", "OK"]
    assert doc["n_dropped_absent_later"] == 1
    assert doc["cells"]["L1P2"]["picked"] == ["OK"] and doc["cells"]["L1P2"]["shortfall"] == 1
    assert doc["cells"]["L3P1"]["shortfall"] == 2


def test_rule_price_falls_back_to_last_print_and_buckets_edges():
    assert ru.price_bucket(19.9999) == "P1" and ru.price_bucket(20.0) == "P2"
    assert ru.price_bucket(100.0) == "P3" and ru.price_bucket(4.99) is None
    assert ru.liq_bucket(100) == "L1" and ru.liq_bucket(101) == "L2" and ru.liq_bucket(1501) is None
    rows = [
        _row("AAPL", 10, None, last_print_price_e4=1_500_000),
        _row("MSFT", 9, 120.0),
    ]
    doc = ru.select(_measured(rows), present_later=_all(rows))
    assert doc["universe"][0]["price"] == 150.0


@pytest.mark.parametrize(
    "rows,later,limit,msg",
    [
        ([_row("MSFT", 9, 120.0)], None, False, "present_later"),
        ([_row("MSFT", 9, 120.0)], {"d": ["MSFT"]}, False, "AAPL"),
        ([_row("AAPL", 9, 3.0), _row("MSFT", 9, 120.0)], {"d": ["AAPL", "MSFT"]}, False, "AAPL"),
        ([_row("AAPL", 9, 150.0), _row("MSFT", 9, 120.0)], {"d": ["AAPL"]}, False, "MSFT"),
        ([_row("AAPL", 9, 150.0), _row("MSFT", 9, 120.0)], {"d": ["AAPL", "MSFT"]}, True, "limit"),
    ],
)
def test_rule_refusals(rows, later, limit, msg):
    m = _measured(rows)
    m["limit_reached"] = limit
    with pytest.raises(ru.UniverseRefused, match=msg):
        ru.select(m, present_later=later)


def _itch_day(tmp_path, name, *, trade=True):
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.stock_directory(2, hms_ns(3, 0, 2), "MSFT")
    enc.stock_directory(3, hms_ns(3, 0, 3), "QQQ", etp="Y")
    enc.system_event(hms_ns(4), "S")
    enc.system_event(hms_ns(9, 30), "Q")
    if trade:
        enc.add(1, hms_ns(10), 11, "S", 1_000, "AAPL", 2_350_000)
        enc.executed(1, hms_ns(10, 1), 11, 400, 1)
        enc.add(2, hms_ns(10, 2), 21, "S", 1_000, "MSFT", 1_370_000)
        enc.executed_with_price(2, hms_ns(10, 3), 21, 300, 2, 1_370_100)
        enc.trade(2, hms_ns(10, 4), "B", 50, "MSFT", 1_369_900, 3)
        enc.cross(1, hms_ns(16), 5_000, "AAPL", 2_360_000, 4, "C")
    enc.system_event(hms_ns(16), "M")
    # a late directory row (after Q) is not seen by the prefix read
    enc.stock_directory(4, hms_ns(16, 5), "LATE")
    return enc.write(tmp_path / name, compress=True)


def test_measure_day_and_directory_prefix_on_encoded_itch(tmp_path):
    src = _itch_day(tmp_path, "d1.gz")
    m = ru.measure_day(src)
    s = m["symbols"]
    assert s["AAPL"]["displayed_volume"] == 400 and s["AAPL"]["close_cross_price_e4"] == 2_360_000
    assert s["MSFT"]["displayed_volume"] == 300 and s["MSFT"]["last_print_price_e4"] == 1_369_900
    assert s["QQQ"]["etp"]
    assert ru.directory_symbols(src) == {"AAPL", "MSFT", "QQQ"}
    doc = ru.select(m, present_later={"x": ["AAPL", "MSFT"], "y": ["AAPL", "MSFT"]})
    assert doc["symbols"] == ["AAPL", "MSFT"]


# ------------------------------------------------------------------- job --
@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "itch" / "r7").mkdir(parents=True)
    days = []
    for i, d in enumerate(job.DAYS):
        body = f"day{i}".encode()
        (data / "itch" / "r7" / d["file"]).write_bytes(body)
        days.append({**d, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    monkeypatch.setattr(job, "DAYS", tuple(days))
    work = tmp_path / "work"
    work.mkdir()
    return job.Paths(data, work)


def _universe(paths, syms=("AAPL", "MSFT", "X1")):
    doc = {
        "rule_version": ru.RULE_VERSION,
        "symbols": list(syms),
        "buckets": {"L1": ["AAPL", "MSFT"], "L2": ["X1"], "L3": []},
    }
    paths.universe.write_text(json.dumps(doc))


def test_verify_accepts_pinned_and_refuses_missing_size_and_hash(env):
    assert len(job.stage_verify(env)["inputs"]) == 3
    f = env.inputs / job.DAYS[1]["file"]
    f.write_bytes(b"dayX")  # same size, other content
    with pytest.raises(job.JobRefused, match="sha256"):
        job.stage_verify(env)
    f.write_bytes(b"longer")
    with pytest.raises(job.JobRefused, match="bytes"):
        job.stage_verify(env)
    f.unlink()
    with pytest.raises(job.JobRefused, match="missing"):
        job.stage_verify(env)


def test_real_pins_match_the_r7_dataset_inputs():
    # the pins are the hashes recorded when the r7 datasets were ingested
    assert [d["file"] for d in job.DAYS] == [
        "S101819-v50.txt.gz",
        "S071321-v50.txt.gz",
        "S081321-v50.txt.gz",
    ]
    assert all(len(d["sha256"]) == 64 for d in job.DAYS)
    assert job.DAYS[0]["date"] == ru.SELECTION_DATE and job.DAYS[0]["file"] == ru.SELECTION_FILE


def test_select_needs_verify_and_writes_the_universe(env):
    with pytest.raises(job.JobRefused, match="verify"):
        job.stage_select(env)
    env.mark("verify")
    rows = _market(200)
    syms = {r["symbol"] for r in rows}
    out = job.stage_select(env, measure=lambda p: _measured(rows), directory=lambda p: syms)
    doc = json.loads(env.universe.read_text())
    assert out["n_symbols"] == len(doc["symbols"]) and doc["source_sha256"] == job.DAYS[0]["sha256"]
    assert job._load_universe(env)["symbols"][:2] == ["AAPL", "MSFT"]


@pytest.mark.parametrize(
    "doc,msg",
    [
        ({"rule_version": "other", "symbols": ["AAPL", "MSFT"]}, "rule"),
        ({"rule_version": ru.RULE_VERSION, "symbols": ["AAPL"]}, "anchors"),
        ({"rule_version": ru.RULE_VERSION, "symbols": ["AAPL", "MSFT", "MSFT"]}, "duplicates"),
    ],
)
def test_universe_file_refusals(env, doc, msg):
    env.universe.write_text(json.dumps(doc))
    with pytest.raises(job.JobRefused, match=msg):
        job._load_universe(env)


def _fake_ingest(paths, calls):
    def run(argv):
        calls.append(argv)
        date = argv[argv.index("--date") + 1]
        man = paths.dataset / "dataset.json"
        doc = json.loads(man.read_text()) if man.is_file() else {"sessions": {}}
        doc["sessions"][date] = {"per_symbol": {}}
        paths.dataset.mkdir(parents=True, exist_ok=True)
        man.write_text(json.dumps(doc))
        return 0

    return run


def test_ingest_runs_in_order_with_auto_partition_and_defers(env):
    env.mark("verify")
    _universe(env)
    calls: list = []
    fake = _fake_ingest(env, calls)
    d0, d1, d2 = (d["date"] for d in job.DAYS)
    with pytest.raises(job.JobRefused, match="select"):
        job.stage_ingest(env, d0, ingest_main=fake, ram_gb=lambda: 8.0)
    env.mark("select")
    with pytest.raises(job.JobRefused, match="date order"):
        job.stage_ingest(env, d1, ingest_main=fake, ram_gb=lambda: 8.0)
    for d in (d0, d1, d2):
        rec = job.stage_ingest(env, d, ingest_main=fake, ram_gb=lambda: 8.0)
        env.mark(f"ingest_{d}")
        assert rec["partition"]["source"] == "auto"
    assert ["--defer-normalize" in c for c in calls] == [True, True, False]
    assert all(c[c.index("--partition-instruments") + 1] == "3" for c in calls)  # min(10, 3 names)
    assert all("--slim" in c for c in calls)
    with pytest.raises(job.JobRefused, match="not one of"):
        job.stage_ingest(env, "2020-01-01", ingest_main=fake)


def test_ingest_wipes_a_dataset_with_unexpected_sessions(env):
    env.mark("verify")
    env.mark("select")
    _universe(env)
    d0, d1, _ = (d["date"] for d in job.DAYS)
    env.mark(f"ingest_{d0}")
    env.dataset.mkdir(parents=True)
    (env.dataset / "dataset.json").write_text(json.dumps({"sessions": {d0: {}, d1: {}}}))
    with pytest.raises(job.JobRefused, match="re-launch"):
        job.stage_ingest(env, d1, ingest_main=_fake_ingest(env, []))
    assert not env.dataset.exists() and not env.done(f"ingest_{d0}")


def test_ingest_failure_raises(env):
    env.mark("verify")
    env.mark("select")
    _universe(env)
    with pytest.raises(RuntimeError, match="exited 1"):
        job.stage_ingest(env, job.DAYS[0]["date"], ingest_main=lambda a: 1, ram_gb=lambda: None)


def _ingested(env):
    for s in ("verify", "select"):
        env.mark(s)
    env.dataset.mkdir(parents=True)
    sessions = {
        d["date"]: {"per_symbol": {"AAPL": {"volume": 8_000_000}, "MSFT": {"volume": 6_000_000}}}
        for d in job.DAYS
    }
    (env.dataset / "dataset.json").write_text(json.dumps({"sessions": sessions}))
    for d in job.DAYS:
        env.mark(f"ingest_{d['date']}")


def test_register_requires_in_sample(env):
    _ingested(env)
    ok = {"manifest_main": lambda a: 0, "seed_main": lambda a: 0}
    rec = job.stage_register(env, **ok, guard=lambda p, **k: {"state": "in_sample"})
    assert rec["holdout_state"] == "in_sample"
    for state in ("spent", "unread"):
        with pytest.raises(job.JobRefused, match=state):
            job.stage_register(env, **ok, guard=lambda p, s=state, **k: {"state": s})
    with pytest.raises(RuntimeError, match="seed"):
        job.stage_register(
            env, manifest_main=lambda a: 0, seed_main=lambda a: 1, guard=lambda p, **k: {}
        )


def test_register_refuses_before_all_ingests(env):
    with pytest.raises(job.JobRefused, match="ingest_"):
        job.stage_register(env, manifest_main=lambda a: 0, seed_main=lambda a: 0, guard=None)


def _yahoo(missing=False, source="https://finance.yahoo.com (retrieved 2026-10-19)"):
    vol = {d["date"]: {"AAPL": 30_000_000, "MSFT": 20_000_000} for d in job.DAYS}
    vol[job.DAYS[0]["date"]]["AAPL"] = 120_000_000  # split-adjusted x4
    split = {d["date"]: {"AAPL": 1, "MSFT": 1} for d in job.DAYS}
    split[job.DAYS[0]["date"]]["AAPL"] = 4  # the 2020-08-31 4:1 split
    if missing:
        vol[job.DAYS[2]["date"]]["MSFT"] = None
    return {"_source": source, "split_factor": split, "volume": vol}


def test_volume_report_undoes_splits_and_flags_outside_band():
    nas = {"2019-10-18": {"AAPL": 8_000_000, "MSFT": 19_000_000}}
    y = {
        "_source": "s",
        "split_factor": {"2019-10-18": {"AAPL": 4, "MSFT": 1}},
        "volume": {"2019-10-18": {"AAPL": 128_000_000, "MSFT": 20_000_000}},
    }
    rep = job.volume_report(nas, y)
    by = {r["symbol"]: r for r in rep["rows"]}
    assert by["AAPL"]["consolidated"] == 32_000_000 and by["AAPL"]["ratio"] == 0.25
    assert by["MSFT"]["flag"] == "outside_band" and rep["flagged"] == ["MSFT@2019-10-18"]
    assert rep["consolidated_adv"]["AAPL"] == 32_000_000


def test_volume_stage_refusals_and_outputs(env):
    _ingested(env)
    env.mark("register")
    with pytest.raises(job.JobRefused, match="missing"):
        job.stage_volume(env)
    env.yahoo.write_text(json.dumps(_yahoo(source=None)))
    with pytest.raises(job.JobRefused, match="_source"):
        job.stage_volume(env)
    env.yahoo.write_text(json.dumps(_yahoo(missing=True)))
    with pytest.raises(job.JobRefused, match="MSFT"):
        job.stage_volume(env)
    env.yahoo.write_text(json.dumps(_yahoo()))
    out = job.stage_volume(env)
    assert out["flagged"] == [] and 0.2 < out["median_ratio"] < 0.35
    adv = json.loads(env.consolidated_adv.read_text())
    assert adv["AAPL"] == 30_000_000 and adv["_source"].startswith("mean Yahoo")


def _predecl(**over):
    doc = json.loads((REPO / job.WORK_REL / "predeclaration.json").read_text())
    doc.update(status="frozen", frozen_by="owner", frozen_utc="2026-10-19T00:00:00+00:00")
    doc["parameters"] = {
        "algos": ["TWAP", "VWAP", "POV", "IS"],
        "policies": ["native", "aggressive", "passive"],
        "sizes_pct_venue_adv": [0.1, 0.5, 1, 2],
        "sizes_target_pct_consolidated_adv": [0.025, 0.125, 0.25, 0.5],
        "window_min": 30,
        "mde_report": "mde and mde_t per bucket x cell",
    }
    doc.update(over)
    return doc


def test_committed_predeclaration_is_a_draft_with_open_parameters(env):
    committed = REPO / job.WORK_REL / "predeclaration.json"
    env.predecl.write_text(committed.read_text())
    with pytest.raises(job.JobRefused, match="draft"):
        job.load_predeclaration(env)
    doc = json.loads(committed.read_text())
    assert doc["result_class"] == "secondary"
    assert set(job.REQUIRED_PARAMS) == set(doc["parameters"])
    assert [k for k, v in doc["parameters"].items() if v is None] == []  # all filled, still a draft


@pytest.mark.parametrize(
    "over,msg",
    [
        ({"status": "draft"}, "draft"),
        ({"frozen_by": None}, "draft"),
        ({"result_class": "primary"}, "SECONDARY"),
    ],
)
def test_predeclaration_refusals(env, over, msg):
    env.predecl.write_text(json.dumps(_predecl(**over)))
    with pytest.raises(job.JobRefused, match=msg):
        job.load_predeclaration(env)


def test_predeclaration_open_parameter_refuses(env):
    doc = _predecl()
    doc["parameters"]["window_min"] = None
    env.predecl.write_text(json.dumps(doc))
    with pytest.raises(job.JobRefused, match="window_min"):
        job.load_predeclaration(env)


def test_bench_runs_per_bucket_with_the_declared_grid(env):
    _ingested(env)
    env.mark("register")
    with pytest.raises(job.JobRefused, match="volume"):
        job.stage_bench(env, "L1", bench_main=lambda a: 0)
    env.mark("volume")
    _universe(env)
    env.predecl.write_text(json.dumps(_predecl()))
    with pytest.raises(job.JobRefused, match="consolidated_adv"):
        job.stage_bench(env, "L1", bench_main=lambda a: 0)
    env.consolidated_adv.write_text("{}")
    seen: list = []
    job.stage_bench(env, "L1", bench_main=lambda a: seen.append(a) or 0)
    a = seen[0]
    assert a[a.index("--symbols") + 1] == "AAPL,MSFT" and "--cover-all-cells" in a
    assert a[a.index("--sizes-pct") + 1] == "0.1,0.5,1,2" and a[a.index("--tag") + 1] == "rel04c_L1"
    with pytest.raises(job.JobRefused, match="no names"):
        job.stage_bench(env, "L3", bench_main=lambda a: 0)
    with pytest.raises(job.JobRefused, match="not in"):
        job.stage_bench(env, "L9", bench_main=lambda a: 0)


def test_cleanup_needs_every_bucket_and_keeps_normalized(env):
    _ingested(env)
    (env.dataset / "raw").mkdir()
    (env.dataset / "raw" / "x.jsonl").write_bytes(b"x" * 10)
    (env.dataset / "normalized").mkdir()
    with pytest.raises(job.JobRefused, match="bench_L1"):
        job.stage_cleanup(env)
    for b in job.BUCKETS:
        env.mark(f"bench_{b}")
    job.stage_cleanup(env)
    assert not (env.dataset / "raw").exists() and (env.dataset / "normalized").is_dir()


def test_cli_refusal_exits_2_logs_and_markers_skip(env, capsys):
    argv = ["--data-root", str(env.data_root), "--work", str(env.work)]
    assert job.main(["select", *argv]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert job.main(["verify", *argv]) == 0 and env.done("verify")
    assert job.main(["verify", *argv]) == 0
    recs = [json.loads(x) for x in env.log.read_text().splitlines()]
    assert [r["status"] for r in recs] == ["refused", "ok", "skipped (marker)"]
    assert "peak_rss_gb" in recs[1] and "seconds" in recs[1]


def test_cmd_is_crlf_and_runs_every_stage_in_order():
    raw = (REPO / job.WORK_REL / "run_rel04c.cmd").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("ascii")
    order = [
        "verify",
        "select",
        "ingest --day 2019-10-18",
        "ingest --day 2021-07-13",
        "ingest --day 2021-08-13",
        "register",
        "volume",
        "bench --bucket L1",
        "bench --bucket L2",
        "bench --bucket L3",
        "cleanup",
    ]
    pos = [text.index(f"%J% {s} ") for s in order]
    assert pos == sorted(pos) and text.count("|| goto fail") == len(order)


def _yahoo_unavail(n_unavail=1, reason="Yahoo: no data found (symbol delisted)"):
    y = _yahoo()
    y["_unavailable"] = {"MSFT": reason}
    for d in y["volume"].values():
        d.pop("MSFT")
    for d in y["split_factor"].values():
        d.pop("MSFT")
    return y


def test_volume_report_unavailable_recorded_not_refused():
    nas = {"2019-10-18": {"AAPL": 8_000_000, "MSFT": 19_000_000}}
    y = {
        "_source": "s",
        "_unavailable": {"MSFT": "delisted"},
        "split_factor": {"2019-10-18": {"AAPL": 4}},
        "volume": {"2019-10-18": {"AAPL": 128_000_000}},
    }
    rep = job.volume_report(nas, y)
    row = next(r for r in rep["rows"] if r["symbol"] == "MSFT")
    assert row["status"] == "unavailable" and row["ratio"] is None and row["consolidated"] is None
    assert "MSFT" not in rep["consolidated_adv"] and rep["validated_symbols"] == ["AAPL"]
    assert rep["median_ratio"] == 0.25 and rep["flagged"] == []


def test_volume_stage_unavailable_outputs_and_refusals(env):
    _ingested(env)
    env.mark("register")
    env.yahoo.write_text(json.dumps(_yahoo_unavail()))
    out = job.stage_volume(env)
    assert out["unavailable"] == ["MSFT"] and out["n_validated"] == 1
    adv = json.loads(env.consolidated_adv.read_text())
    assert "MSFT" not in adv and adv["_unavailable"] == {
        "MSFT": "Yahoo: no data found (symbol delisted)"
    }
    assert "validated names only" in adv["_note"]
    chk = json.loads(env.volume_report.read_text())
    assert chk["unavailable"] and {r["status"] for r in chk["rows"] if r["symbol"] == "MSFT"} == {
        "unavailable"
    }
    # neither filled nor unavailable
    y = _yahoo_unavail()
    y["_unavailable"] = {}
    env.yahoo.write_text(json.dumps(y))
    with pytest.raises(job.JobRefused, match="MSFT"):
        job.stage_volume(env)
    # missing _source
    y = _yahoo_unavail()
    y["_source"] = None
    env.yahoo.write_text(json.dumps(y))
    with pytest.raises(job.JobRefused, match="_source"):
        job.stage_volume(env)
    # missing split factor of a filled name
    y = _yahoo_unavail()
    del y["split_factor"]["2021-07-13"]["AAPL"]
    env.yahoo.write_text(json.dumps(y))
    with pytest.raises(job.JobRefused, match="AAPL"):
        job.stage_volume(env)
    # empty reason, filled and unavailable, unknown unavailable name
    for bad, match in (
        ({"MSFT": " "}, "reason"),
        ({"MSFT": "x", "AAPL": "x"}, "both"),
        ({"MSFT": "x", "ZZZZ": "x"}, "not in the dataset"),
    ):
        y = _yahoo_unavail()
        y["_unavailable"] = bad
        if "AAPL" in bad:
            pass
        env.yahoo.write_text(json.dumps(y))
        with pytest.raises(job.JobRefused, match=match):
            job.stage_volume(env)


def test_volume_report_refuses_more_than_six_unavailable():
    syms = [f"S{i}" for i in range(7)]
    y = {
        "_source": "s",
        "_unavailable": {s: "delisted" for s in syms},
        "split_factor": {},
        "volume": {},
    }
    with pytest.raises(job.JobRefused, match="more than 6"):
        job.volume_report({"2019-10-18": {s: 1 for s in syms}}, y)
    y["_unavailable"].pop("S6")
    assert len(job.volume_report({"2019-10-18": {s: 1 for s in syms[:6]}}, y)["rows"]) == 6


def test_shipped_yahoo_volume_is_consistent():
    root = Path(__file__).resolve().parents[2] / "research" / "execution" / "rel04c"
    y = json.loads((root / "yahoo_volume.json").read_text(encoding="utf-8"))
    pre = json.loads((root / "predeclaration.json").read_text(encoding="utf-8"))
    assert y["_source"] and len(y["_unavailable"]) == 6
    dates = [d["date"] for d in job.DAYS]
    assert sorted(y["volume"]) == sorted(y["split_factor"]) == sorted(dates)
    filled = set(y["volume"][dates[0]])
    assert len(filled) == 14 and not filled & set(y["_unavailable"])
    for d in dates:
        assert set(y["volume"][d]) == set(y["split_factor"][d]) == filled
    assert pre["status"] == "frozen" and pre["frozen_by"] and pre["frozen_utc"]
    assert pre["parameters"]["sizes_target_pct_consolidated_adv"] == [
        0.037,
        0.185,
        0.37,
        0.74,
    ]  # draft
    assert set(pre["volume_validation"]["validated"]) == filled
    assert set(pre["volume_validation"]["unavailable"]) == set(y["_unavailable"])

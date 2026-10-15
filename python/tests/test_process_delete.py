"""v1.17 A2: the partitioned normaliser is byte-identical to the in-memory
one, and the process-and-delete chain keeps only features + hashes."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import CONFIGS_DIR
from iap.core.codec import read_iap1
from iap.marketdata import process_delete as pdm
from iap.marketdata.feederrors import IngestError
from iap.marketdata.generator import MarketDataGenerator
from iap.marketdata.ingest import ingest
from iap.marketdata.normalize import normalize_run
from itch50_encoder import build_session

REPO = Path(__file__).resolve().parents[2]
DATE = "2019-12-30"
CFG = {
    "seed": 4321,
    "sessions": 1,
    "equities": {"slots_per_stream": 120},
    "fx": {"slots_per_pair": 100},
    "anomalies": {
        "gap_prob": 0.004,
        "gap_max_events": 3,
        "dup_prob": 0.01,
        "ooo_prob": 0.006,
        "invalid_prob": 0.004,
        "ts_violation_prob": 0.004,
    },
}


def _bytes(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(d.glob("*")) if p.is_file()}


@pytest.mark.parametrize("partition", [1, 3, 1000])
def test_partitioned_normalisation_is_byte_identical(refdata, tmp_path, partition):
    root = tmp_path
    MarketDataGenerator(refdata, CFG).generate_run(root / "raw")
    plain = normalize_run(root / "raw", root / "plain")
    part = normalize_run(root / "raw", root / "part", partition_instruments=partition)
    a, b = _bytes(root / "plain"), _bytes(root / "part")
    for name in a:
        if name.endswith(".parquet"):
            continue  # row groups differ (per window, not per file); rows compared below
        if name == "qc_report.json":
            continue
        assert a[name] == b[name], name
    assert plain["totals"] == part["totals"] and plain["per_stream"] == part["per_stream"]
    assert plain["files"] == part["files"] and plain["parquet"]["rows"] == part["parquet"]["rows"]
    pq = pytest.importorskip("pyarrow.parquet")
    ta, tb = (
        pq.read_table(root / "plain" / "events.parquet"),
        pq.read_table(root / "part" / "events.parquet"),
    )
    assert ta.num_rows == tb.num_rows and ta.to_pylist()[:5] == tb.to_pylist()[:5]
    assert ta.to_pydict() == tb.to_pydict()
    # the iap1 bytes decode to the same ordered events
    for f in (root / "part").glob("*.normalized.iap1"):
        assert [e.event_id for e in read_iap1(f)][:3] == [1, 2, 3]
    # no temp directory left behind
    assert not list((root / "part").glob(".norm_*"))


def test_ingest_with_partitions_matches_the_default(tmp_path):
    enc, _ = build_session(5, symbols=("AAPL", "MSFT", "QQQ"), n_actions=1500, etp_symbols=("QQQ",))
    src = enc.write(tmp_path / "day.itch.gz", compress=True)
    syms = ["AAPL", "MSFT", "QQQ"]
    a = ingest("itch50", [src], DATE, syms, tmp_path / "a", configs_dir=CONFIGS_DIR)
    b = ingest(
        "itch50",
        [src],
        DATE,
        syms,
        tmp_path / "b",
        configs_dir=CONFIGS_DIR,
        partition_instruments=2,
    )
    assert a["dataset_version"] == b["dataset_version"]
    assert a["normalized"]["files"] == b["normalized"]["files"]
    assert a["sessions"][DATE]["raw"]["sha256"] == b["sessions"][DATE]["raw"]["sha256"]


def _manifest(tmp_path, src: Path, date=DATE, fmt="itch50") -> Path:
    doc = {
        "symbols": "AAPL,MSFT",
        "days": [
            {
                "date": date,
                "file": src.name,
                "url": "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/" + src.name,
                "bytes": src.stat().st_size,
                "sha256": None,
                "role": "insample",
                "format": fmt,
            }
        ],
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _features_fake(ds_features_dir_from_cmd):
    """A ``subprocess.run`` stand-in that writes a features file where the
    real build would (the real build is exercised by the features tests)."""

    def run(cmd, **kw):
        out = Path(cmd[cmd.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "features_1.parquet").write_bytes(b"PAR1fake")
        (out / "feature_registry.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "features ok", "")

    return run


def test_process_day_keeps_features_and_hashes_then_deletes(tmp_path):
    enc, _ = build_session(9, symbols=("AAPL", "MSFT"), n_actions=800)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    src = enc.write(vendor / "12302019.NASDAQ_ITCH50.gz", compress=True)
    man = _manifest(tmp_path, src)
    out_root = tmp_path / "breadth"
    lines: list[str] = []
    rep = pdm.process_day(
        man,
        DATE,
        vendor,
        out_root,
        ["AAPL", "MSFT"],
        configs_dir=CONFIGS_DIR,
        partition_instruments=1,
        min_free_gb=0.0,
        log=lines.append,
        run=_features_fake(None),
    )
    ds = out_root / "ds_20191230"
    assert [s["step"] for s in rep["steps"]] == ["download", "ingest", "features", "keep", "delete"]
    assert rep["steps"][0]["status"] == "ok" and rep["steps"][0]["download"] == "present"
    keep = json.loads((ds / pdm.KEEP_NAME).read_text())
    assert "dataset.json" in keep["files"] and "features/features_1.parquet" in keep["files"]
    assert "normalized/qc_report.json" in keep["files"]
    assert any(k.startswith("configs/") for k in keep["files"])
    assert pdm.verify_keep(ds) == []
    # raw and normalized data are gone, the gz is gone, the QC report stays
    assert not (ds / "raw").exists()
    assert not list((ds / "normalized").glob("*.normalized.*"))
    assert (ds / "normalized" / "qc_report.json").is_file()
    assert not src.exists()
    assert (ds.parent / "ds_20191230.markers" / "_DONE_ALL").is_file() and (
        ds / "process_report.json"
    ).is_file()
    assert rep["steps"][-1]["freed_gb"] >= 0
    # a second run is a no-op: every step has its marker (the gz is gone, fine)
    rep2 = pdm.process_day(
        man,
        DATE,
        vendor,
        out_root,
        ["AAPL", "MSFT"],
        configs_dir=CONFIGS_DIR,
        min_free_gb=0.0,
        log=lines.append,
        run=_features_fake(None),
    )
    assert all(s["status"].startswith("skipped") for s in rep2["steps"])


def test_process_day_keep_flags_dry_run_and_refusals(tmp_path):
    enc, _ = build_session(3, symbols=("AAPL", "MSFT"), n_actions=400)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    src = enc.write(vendor / "12302019.NASDAQ_ITCH50.gz", compress=True)
    man = _manifest(tmp_path, src)
    out_root = tmp_path / "out"
    # dry run: the guard and the plan, nothing written
    rep = pdm.process_day(
        man,
        DATE,
        vendor,
        out_root,
        ["AAPL"],
        configs_dir=CONFIGS_DIR,
        dry_run=True,
        min_free_gb=0.0,
        run=_features_fake(None),
    )
    assert rep["dry_run"] and not (out_root / "ds_20191230").exists() and rep["disk"]["ok"]
    # the disk guard refuses an impossible margin and deletes nothing
    with pytest.raises(IngestError, match="disk guard refused"):
        pdm.process_day(
            man,
            DATE,
            vendor,
            out_root,
            ["AAPL"],
            configs_dir=CONFIGS_DIR,
            min_free_gb=1e9,
            run=_features_fake(None),
        )
    assert src.exists()
    # keep flags: raw and gz survive
    rep = pdm.process_day(
        man,
        DATE,
        vendor,
        out_root,
        ["AAPL", "MSFT"],
        configs_dir=CONFIGS_DIR,
        partition_instruments=1,
        keep_raw=True,
        keep_gz=True,
        min_free_gb=0.0,
        run=_features_fake(None),
    )
    ds = out_root / "ds_20191230"
    assert (
        src.exists()
        and (ds / "raw").is_dir()
        and list((ds / "normalized").glob("*.normalized.iap1"))
    )
    assert rep["steps"][-1]["kept_raw"] and rep["steps"][-1]["kept_gz"]
    # a missing vendor file without --download, and --download without approval, refuse
    src.unlink()
    out2 = tmp_path / "out2"
    with pytest.raises(IngestError, match="--download was not given"):
        pdm.process_day(
            man,
            DATE,
            vendor,
            out2,
            ["AAPL"],
            configs_dir=CONFIGS_DIR,
            min_free_gb=0.0,
            run=_features_fake(None),
        )
    with pytest.raises(IngestError, match="i-have-approval"):
        pdm.process_day(
            man,
            DATE,
            vendor,
            out2,
            ["AAPL"],
            configs_dir=CONFIGS_DIR,
            min_free_gb=0.0,
            download=True,
            run=_features_fake(None),
        )
    # a failing features build stops before anything is deleted
    enc.write(src, compress=True)

    def boom(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "no rust")

    out3 = tmp_path / "out3"
    with pytest.raises(IngestError, match="features failed"):
        pdm.process_day(
            man,
            DATE,
            vendor,
            out3,
            ["AAPL", "MSFT"],
            configs_dir=CONFIGS_DIR,
            partition_instruments=1,
            min_free_gb=0.0,
            run=boom,
        )
    assert src.exists() and (out3 / "ds_20191230" / "raw").is_dir()
    assert not (out3 / "ds_20191230" / pdm.KEEP_NAME).exists()


def test_estimate_guard_wrapper_and_jobs_entry(tmp_path):
    est = pdm.estimate_peak_gb(5_000_000_000, 100, vendor_present=False)
    assert (
        est["vendor_gb"] == 5.0
        and est["dataset_gb"] > 100
        and est["peak_gb"] > est["dataset_gb"] * 3
    )
    assert pdm.estimate_peak_gb(5_000_000_000, 3, vendor_present=True)["vendor_gb"] == 0.0
    g = pdm.disk_guard(tmp_path, 0.0, 0.0)
    assert g["ok"] and g["free_gb"] > 0
    assert pdm.disk_guard(tmp_path, 1e9, 0.0)["ok"] is False
    text = pdm.render_wrapper(
        python_dir="C:\\r\\python",
        manifest="M",
        date=DATE,
        vendor_dir="V",
        out_root="R",
        symbols_file="U",
        log="R\\p.log",
        extra_args="--engine rust",
    )
    assert (
        "process-day" in text
        and "echo EXIT %RC%" in text
        and text.endswith("\r\n")
        and "--date 2019-12-30" in text
    )
    entry = pdm.render_jobs_entry(DATE, "R\\run.cmd", "R\\p.log")
    assert entry["name"] == "process_20191230" and entry["restart_safe"] is True
    assert "process-da[y]" in entry["process_pattern"]
    reg = tmp_path / "jobs.json"
    pdm._add_job(reg, entry)
    pdm._add_job(reg, entry)  # idempotent by name
    assert len(json.loads(reg.read_text())["jobs"]) == 1


def test_cli_render_and_dry_run(tmp_path, capsys):
    enc, _ = build_session(4, symbols=("AAPL",), n_actions=300)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    src = enc.write(vendor / "12302019.NASDAQ_ITCH50.gz", compress=True)
    man = _manifest(tmp_path, src)
    uni = tmp_path / "universe.json"
    uni.write_text(json.dumps({"symbols": ["AAPL"]}), encoding="utf-8")
    cmd = tmp_path / "run.cmd"
    rc = pdm.main(
        [
            "--manifest",
            str(man),
            "--date",
            DATE,
            "--vendor-dir",
            str(vendor),
            "--out-root",
            str(tmp_path / "r"),
            "--symbols-file",
            str(uni),
            "--render-cmd",
            str(cmd),
            "--jobs-registry",
            str(tmp_path / "jobs.json"),
        ]
    )
    assert rc == 0 and cmd.is_file() and (tmp_path / "jobs.json").is_file()
    assert "--symbols-file" in cmd.read_text()
    rc = pdm.main(
        [
            "--manifest",
            str(man),
            "--date",
            DATE,
            "--vendor-dir",
            str(vendor),
            "--out-root",
            str(tmp_path / "r"),
            "--symbols-file",
            str(uni),
            "--dry-run",
            "--min-free-gb",
            "0",
            "--configs-dir",
            str(CONFIGS_DIR),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and '"dry_run": true' in out
    rc = pdm.main(
        [
            "--manifest",
            str(man),
            "--date",
            "2019-01-02",
            "--vendor-dir",
            str(vendor),
            "--out-root",
            str(tmp_path / "r"),
            "--symbols",
            "AAPL",
        ]
    )
    assert rc == 2


def test_import_policy_no_network_in_module():
    src = (REPO / "python" / "src" / "iap" / "marketdata" / "process_delete.py").read_text(
        encoding="utf-8"
    )
    assert "urllib" not in src and "requests" not in src and "socket" not in src
    assert "r7_download.py" in src  # the download is the approved tool, in a subprocess


def test_gz_sanity(tmp_path):
    enc, _ = build_session(1, symbols=("AAPL",), n_actions=50)
    p = enc.write(tmp_path / "x.gz", compress=True)
    with gzip.open(p, "rb") as f:
        assert f.read(2)
    assert sys.version_info >= (3, 11)

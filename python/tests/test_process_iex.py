"""v1.17 A4: the IEX overlap chain (process-iex-day) on synthetic pcapng
captures, the TOPS extract, the HIST manifest tools and the rolling-set rule."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import iex_encoder as ix
import pytest
from conftest import CONFIGS_DIR, REPO_ROOT
from iap.marketdata import process_iex as pix
from iap.marketdata.feederrors import BookDivergenceError, IngestError
from iap.marketdata.iex import PROTOCOL_DEEP_PLUS, PROTOCOL_TOPS, TopsQuotes
from iap.marketdata.itch50 import session_midnight_ns
from itch50_encoder import hms_ns

# v1.17 A4: the IEX codec and chain files run in the python-slow CI job (the
# coverage floor is measured there); the PR python job deselects them (-m 'not slow').
pytestmark = pytest.mark.slow

DATE = "2025-11-28"
MID = session_midnight_ns(DATE)
OPEN = hms_ns(9, 30)
NS = 1_000_000_000
SYMS = ["AAPL", "QQQ"]
APPROVAL = "A4 (IEX overlap days, needs your download approvals), = approved"


def _ep(ts: int) -> int:
    return MID + ts


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _book_capture(path: Path) -> int:
    """A DEEP+ pcapng day for AAPL and QQQ; returns the first order time."""
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS, pcapng=True)
    t = OPEN + 5 * NS
    msgs = [
        ix.system_event("O", _ep(hms_ns(3))),
        ix.directory("AAPL", _ep(hms_ns(3, 0, 1))),
        ix.directory("QQQ", _ep(hms_ns(3, 0, 2)), flags=0x20),
        ix.system_event("S", _ep(hms_ns(4))),
        ix.system_event("R", _ep(OPEN)),
        ix.add_order("AAPL", _ep(t), "B", 1, 100, 1_000_000),
        ix.add_order("AAPL", _ep(t), "S", 2, 200, 1_000_100),
        ix.add_order("QQQ", _ep(t), "B", 21, 300, 5_000_000),
        ix.add_order("QQQ", _ep(t), "S", 22, 300, 5_000_100),
        ix.add_order("AAPL", _ep(t + 1), "B", 3, 50, 1_000_000),
        ix.executed("AAPL", _ep(t + 2), 2, 200, 1_000_100, 5),
        ix.executed("QQQ", _ep(t + 3), 21, 100, 5_000_000, 6),
        ix.system_event("M", _ep(hms_ns(16))),
    ]
    for i in range(0, len(msgs), 4):
        w.segment(msgs[i : i + 4], send_time=_ep(hms_ns(3)) + i)
    w.write(path, compress=True)
    return t


def _tops_capture(path: Path, t: int, *, diverge: bool = False) -> Path:
    w = ix.IexPcapWriter(PROTOCOL_TOPS, pcapng=True)
    aapl_bid_2 = 100 if diverge else 150
    w.segment(
        [
            ix.quote("AAPL", _ep(t), 100, 1_000_000, 1_000_100, 200),
            ix.quote("QQQ", _ep(t), 300, 5_000_000, 5_000_100, 300),
            ix.quote("AAPL", _ep(t + 1), aapl_bid_2, 1_000_000, 1_000_100, 200),
            ix.quote("AAPL", _ep(t + 2), 150, 1_000_000, 0, 0),
            ix.quote("QQQ", _ep(t + 3), 200, 5_000_000, 5_000_100, 300),
        ],
        send_time=_ep(OPEN),
    )
    return w.write(path, compress=True)


def _manifest(tmp_path: Path, vendor: Path, *, approval: str | None = APPROVAL) -> Path:
    def entry(name: str) -> dict:
        p = vendor / name
        return {
            "file": name,
            "url": "https://www.googleapis.com/download/storage/v1/b/iex/o/x%2F" + name,
            "bytes": p.stat().st_size if p.is_file() else 1,
            "version": "1.0",
            "protocol": "IEXTP1",
        }

    doc = {
        "schema": pix.MANIFEST_SCHEMA,
        "source": "test",
        "approval": approval,
        "days": [
            {
                "date": DATE,
                "feeds": {
                    "TOPS": entry("20251128_IEXTP1_TOPS1.6.pcap.gz"),
                    "DEEP+": entry("20251128_IEXTP1_DPLS1.0.pcap.gz"),
                },
            },
            {"date": "2025-12-08", "feeds": {}, "note": "not on IEX HIST (no index entry)"},
        ],
    }
    p = tmp_path / "iex_manifest.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _features_fake(cmd, **kw):
    out = Path(cmd[cmd.index("--out-dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "features_1.parquet").write_bytes(b"PAR1fake")
    (out / "feature_registry.json").write_text("{}", encoding="utf-8")
    return subprocess.CompletedProcess(cmd, 0, "features ok", "")


def _vendor(tmp_path: Path, *, diverge: bool = False) -> tuple[Path, Path, Path]:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    book = vendor / "20251128_IEXTP1_DPLS1.0.pcap.gz"
    t = _book_capture(book)
    tops = _tops_capture(vendor / "20251128_IEXTP1_TOPS1.6.pcap.gz", t, diverge=diverge)
    return vendor, tops, book


# ------------------------------------------------------------ the extract


def test_tops_extract_round_trips_and_refuses_a_missing_symbol(tmp_path):
    _vendor_dir, tops, _book = _vendor(tmp_path)
    q = TopsQuotes.load(tops, SYMS, MID)
    out = q.save(tmp_path / "day.tops.json.gz", source={"file": tops.name, "sha256": "ab"})
    back = TopsQuotes.load(out, SYMS, MID)  # dispatched by the suffix
    assert back.by_symbol == q.by_symbol and back.quotes == q.quotes == 5
    assert back.source["sha256"] == "ab" and back.protocol == PROTOCOL_TOPS
    with pytest.raises(IngestError, match="no quotes for"):
        TopsQuotes.load_extract(out, ["MSFT"])
    with pytest.raises(IngestError, match="must end with"):
        q.save(tmp_path / "day.json")


# ------------------------------------------------------------ the manifest


def test_iex_manifest_validation(tmp_path):
    vendor = tmp_path / "v"
    vendor.mkdir()
    man = _manifest(tmp_path, vendor)
    doc = pix.load_iex_manifest(man)
    assert [d["date"] for d in doc["days"]] == [DATE, "2025-12-08"]
    bad = json.loads(man.read_text())
    bad["days"][0]["feeds"]["TOPS"]["url"] = "https://evil.example/x.gz"
    man.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="allowlist"):
        pix.load_iex_manifest(man)
    bad["days"][0]["feeds"]["TOPS"]["url"] = "https://www.googleapis.com/a"
    bad["days"][0]["feeds"]["TOPS"]["file"] = "../x.gz"
    man.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="bare file name"):
        pix.load_iex_manifest(man)
    bad["schema"] = "other"
    man.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="schema"):
        pix.load_iex_manifest(man)


# --------------------------------------------------------------- the chain


def test_process_iex_day_runs_the_chain_and_keeps_features_and_hashes(tmp_path):
    vendor, tops, book = _vendor(tmp_path)
    man = _manifest(tmp_path, vendor)
    out_root = tmp_path / "iex"
    lines: list[str] = []
    rep = pix.process_iex_day(
        man,
        DATE,
        vendor,
        out_root,
        SYMS,
        configs_dir=CONFIGS_DIR,
        partition_instruments=1,
        min_free_gb=0.0,
        log=lines.append,
        run=_features_fake,
    )
    ds = out_root / "iex_20251128"
    assert [s["step"] for s in rep["steps"]] == [
        "download_tops",
        "tops_extract",
        "delete_tops",
        "download_book",
        "ingest",
        "delete_raw",
        "features",
        "keep",
        "delete",
    ]
    steps = {s["step"]: s for s in rep["steps"]}
    assert steps["download_tops"]["download"] == "present"
    assert (
        steps["tops_extract"]["tops_quotes"] == 5
        and len(steps["tops_extract"]["tops_sha256"]) == 64
    )
    assert steps["download_book"]["download"] == "present"  # TOPS was gone by then
    ing = steps["ingest"]
    assert ing["book_verification"] == {
        "AAPL": {"status": "match", "compared": 3, "mismatched": 0},
        "QQQ": {"status": "match", "compared": 2, "mismatched": 0},
    }
    assert ing["events"] > 0 and ing["messages"] > 0 and ing["iex"]["protocol"] == "DEEP+"
    assert ing["iex"]["tops_oracle"] is True and len(ing["book_sha256"]) == 64
    manifest = json.loads((ds / "dataset.json").read_text())
    assert manifest["source"] == {"format": "iex-deep-plus", "venue": "IEXG", "venue_id": 102}
    assert manifest["dataset_version"] == ing["dataset_version"]
    assert manifest["sessions"][DATE]["book_verification"]["AAPL"]["status"] == "match"
    assert manifest.get("layout") == "slim"
    keep = json.loads((ds / pix.KEEP_NAME).read_text())
    assert "dataset.json" in keep["files"] and "features/features_1.parquet" in keep["files"]
    assert pix.verify_keep(ds) == []
    # both captures, the extract and raw are gone; the QC report stays; with the
    # default --keep-normalized the IAP1 and Parquet stay too and are in KEEP.json
    assert not tops.exists() and not book.exists()
    assert not list((ds.parent / "iex_20251128.markers").glob("*.tops.json.gz"))
    assert not (ds / "raw").exists()
    assert (ds / "normalized" / "qc_report.json").is_file()
    iap1 = sorted((ds / "normalized").glob("*.normalized.iap1"))
    assert iap1 and not list((ds / "normalized").glob("*.normalized.jsonl"))
    assert all(f"normalized/{q.name}" in keep["files"] for q in iap1)
    assert keep["kept_normalized"] is True and "raw" in keep["deleted"]
    last = rep["steps"][-1]
    assert last["kept_normalized"] is True and last["kept_normalized_gb"] >= 0
    assert last["kept_dataset_gb"] >= last["kept_normalized_gb"]
    assert (ds.parent / "iex_20251128.markers" / "_DONE_ALL").is_file()
    report = json.loads((ds / "process_report.json").read_text())
    assert report["approval"] == APPROVAL and report["free_gb_min_seen"] > 0
    # no paired Nasdaq day given: the round lots are the smallest quoted TOPS sizes
    assert report["round_lots"] == {"AAPL": 100, "QQQ": 200}
    assert report["round_lots_source"] == "tops_min_quoted_size"
    assert all("free_gb_after" in s for s in report["steps"])
    # a second run is a no-op
    rep2 = pix.process_iex_day(
        man,
        DATE,
        vendor,
        out_root,
        SYMS,
        configs_dir=CONFIGS_DIR,
        min_free_gb=0.0,
        log=lines.append,
        run=_features_fake,
    )
    assert all(s["status"].startswith("skipped") for s in rep2["steps"])


def test_delete_normalized_removes_the_iap1_and_parquet(tmp_path):
    vendor, _tops, _book = _vendor(tmp_path)
    man = _manifest(tmp_path, vendor)
    out_root = tmp_path / "iex"
    rep = pix.process_iex_day(
        man,
        DATE,
        vendor,
        out_root,
        SYMS,
        configs_dir=CONFIGS_DIR,
        partition_instruments=1,
        min_free_gb=0.0,
        keep_normalized=False,
        log=lambda _l: None,
        run=_features_fake,
    )
    ds = out_root / "iex_20251128"
    assert not list((ds / "normalized").glob("*.normalized.*"))
    assert not list((ds / "normalized").glob("*.parquet"))
    keep = json.loads((ds / pix.KEEP_NAME).read_text())
    assert "kept_normalized" not in keep and not any(k.endswith(".iap1") for k in keep["files"])
    assert rep["steps"][-1]["kept_normalized"] is False
    assert rep["steps"][-1]["kept_normalized_gb"] == 0
    # the wrapper carries the opt-out, never the default
    cmd = tmp_path / "x.cmd"
    pix.main(
        [
            "--manifest",
            "m",
            "--date",
            DATE,
            "--vendor-dir",
            "v",
            "--out-root",
            "o",
            "--symbols",
            "AAPL",
            "--delete-normalized",
            "--render-cmd",
            str(cmd),
        ]
    )
    assert "--delete-normalized" in cmd.read_bytes().decode("ascii")
    pix.main(
        [
            "--manifest",
            "m",
            "--date",
            DATE,
            "--vendor-dir",
            "v",
            "--out-root",
            "o",
            "--symbols",
            "AAPL",
            "--render-cmd",
            str(cmd),
        ]
    )
    assert "--delete-normalized" not in cmd.read_bytes().decode("ascii")


def test_divergent_tops_refuses_the_ingest_unless_allowed(tmp_path):
    vendor, _tops, _book = _vendor(tmp_path, diverge=True)
    man = _manifest(tmp_path, vendor)
    out_root = tmp_path / "iex"
    with pytest.raises(BookDivergenceError):
        pix.process_iex_day(
            man,
            DATE,
            vendor,
            out_root,
            SYMS,
            configs_dir=CONFIGS_DIR,
            min_free_gb=0.0,
            log=lambda _l: None,
            run=_features_fake,
        )
    markers = out_root / "iex_20251128.markers"
    assert (markers / "book_divergence.json").is_file()
    assert (markers / "_DONE_delete_tops").is_file() and not (markers / "_DONE_ingest").exists()
    # the TOPS capture is gone but its extract is kept for the resume
    assert list(markers.glob("*.tops.json.gz"))
    rep = pix.process_iex_day(
        man,
        DATE,
        vendor,
        out_root,
        SYMS,
        configs_dir=CONFIGS_DIR,
        min_free_gb=0.0,
        allow_book_divergence=True,
        log=lambda _l: None,
        run=_features_fake,
    )
    ing = next(s for s in rep["steps"] if s["step"] == "ingest")
    assert ing["book_verification"]["AAPL"] == {
        "status": "diverged",
        "compared": 3,
        "mismatched": 1,
    }
    assert rep["steps"][0]["status"].startswith("skipped")


def test_download_refusals_dry_run_and_one_vendor_file_at_a_time(tmp_path):
    vendor, tops, book = _vendor(tmp_path)
    man = _manifest(tmp_path, vendor)
    out_root = tmp_path / "iex"
    common = dict(configs_dir=CONFIGS_DIR, min_free_gb=0.0, log=lambda _l: None)
    # dry run: the guard and the plan, nothing written
    rep = pix.process_iex_day(man, DATE, vendor, out_root / "b", SYMS, dry_run=True, **common)
    assert rep["disk"]["ok"] and all(s.get("dry_run") for s in rep["steps"])
    assert not (out_root / "b" / "iex_20251128").exists()
    # a missing day's feeds, a missing file without --download, approval needed
    with pytest.raises(IngestError, match=r"no \['TOPS', 'DEEP\+'\]"):
        pix.process_iex_day(man, "2025-12-08", vendor, out_root / "c", SYMS, **common)
    tops_bytes = tops.read_bytes()
    tops.unlink()
    # the book capture is still on disk: the owner's one-file rule refuses the TOPS download
    with pytest.raises(IngestError, match="one vendor file at a time"):
        pix.process_iex_day(man, DATE, vendor, out_root / "d", SYMS, **common)
    book_bytes = book.read_bytes()
    book.unlink()
    with pytest.raises(IngestError, match="--download was not given"):
        pix.process_iex_day(man, DATE, vendor, out_root / "d", SYMS, **common)
    with pytest.raises(IngestError, match="--i-have-approval"):
        pix.process_iex_day(man, DATE, vendor, out_root / "d", SYMS, download=True, **common)
    # the downloads are a subprocess of tools/iex_download.py, one feed at a time
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        if "iex_download.py" in str(cmd[1]):
            calls.append(cmd)
            assert "--i-have-approval" in cmd
            feed = cmd[cmd.index("--feed") + 1]
            (vendor / (tops.name if feed == "TOPS" else book.name)).write_bytes(
                tops_bytes if feed == "TOPS" else book_bytes
            )
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        return _features_fake(cmd)

    rep = pix.process_iex_day(
        man,
        DATE,
        vendor,
        out_root / "d",
        SYMS,
        download=True,
        approval=True,
        partition_instruments=1,
        run=fake_run,
        **common,
    )
    assert [c[c.index("--feed") + 1] for c in calls] == ["TOPS", "DEEP+"]
    steps = {s["step"]: s for s in rep["steps"]}
    assert steps["download_tops"]["download"] == "downloaded"
    assert len(steps["download_tops"]["sha256"]) == 64 and steps["download_tops"]["mb_per_s"] >= 0
    assert steps["download_book"]["download"] == "downloaded"
    assert not tops.exists() and not book.exists()
    with pytest.raises(IngestError, match="--feed"):
        pix.process_iex_day(man, DATE, vendor, out_root / "e", SYMS, feed="tops", **common)


def test_render_wrapper_chains_the_days_and_registers_the_job(tmp_path):
    reg = tmp_path / "jobs.json"
    cmd = tmp_path / "run_iex_overlap.cmd"
    rc = pix.main(
        [
            "--manifest",
            "m.json",
            "--date",
            DATE,
            "--date",
            "2025-12-08",
            "--vendor-dir",
            "v",
            "--out-root",
            "o",
            "--symbols",
            "AAPL,QQQ",
            "--download",
            "--i-have-approval",
            "--peak-gb",
            "40",
            "--render-cmd",
            str(cmd),
            "--jobs-registry",
            str(reg),
            "--job-name",
            "iex_overlap",
        ]
    )
    assert rc == 0
    text = cmd.read_bytes().decode("ascii")
    assert text.count("--date 2025-") == 2 and "--date 2025-12-08" in text
    assert "--feed deep-plus" in text and "--i-have-approval" in text and "--peak-gb 40" in text
    assert "--round-lots-from" not in text
    nas = tmp_path / "dataset.json"
    nas.write_text(
        json.dumps(
            {"universe": [{"symbol": "AAPL", "lot_size": 100}, {"symbol": "QQQ", "lot_size": 40}]}
        )
    )
    assert pix.round_lots_from_dataset(nas) == {"AAPL": 100, "QQQ": 40}
    rc = pix.main(
        [
            "--manifest",
            "m.json",
            "--date",
            DATE,
            "--vendor-dir",
            "v",
            "--out-root",
            "o",
            "--symbols",
            "AAPL,QQQ",
            "--round-lots-from",
            str(nas),
            "--render-cmd",
            str(cmd),
        ]
    )
    assert rc == 0 and "--round-lots-from" in cmd.read_bytes().decode("ascii")
    assert "EXIT_2025-11-28" in text and text.rstrip().endswith("exit /b %FAILED%")
    assert "\r\n" in text
    job = json.loads(reg.read_text())["jobs"][0]
    assert job["name"] == "iex_overlap" and job["restart_safe"] is True
    assert "process-iex-da[y]" in job["process_pattern"]
    with pytest.raises(SystemExit):  # two dates without --render-cmd: an argparse error
        pix.main(
            [
                "--manifest",
                "m",
                "--date",
                DATE,
                "--date",
                "x",
                "--vendor-dir",
                "v",
                "--out-root",
                "o",
            ]
        )


# ----------------------------------------------------------- tools/iex_hist


INDEX = {
    "20251128": [
        {
            "link": "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20251128%2F20251128_IEXTP1_TOPS1.6.pcap.gz?generation=1&alt=media",
            "date": "20251128",
            "feed": "TOPS",
            "version": "1.6",
            "protocol": "IEXTP1",
            "size": "4371690941",
        },
        {
            "link": "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20251128%2F20251128_IEXTP1_DPLS1.0.pcap.gz?generation=1&alt=media",
            "date": "20251128",
            "feed": "DPLS",
            "version": "1.0",
            "protocol": "IEXTP1",
            "size": "4313169306",
        },
        {
            "link": "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20251128%2F20251128_IEXTP1_DEEP1.0.pcap.gz?generation=1&alt=media",
            "date": "20251128",
            "feed": "DEEP",
            "version": "1.0",
            "protocol": "IEXTP1",
            "size": "4596311004",
        },
    ],
    "20251127": [],  # Thanksgiving
    "20251126": [
        {
            "link": "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20251126%2F20251126_IEXTP1_TOPS1.6.pcap.gz",
            "date": "20251126",
            "feed": "TOPS",
            "version": "1.6",
            "protocol": "IEXTP1",
            "size": 10,
        },
    ],
    "20251125": [
        {
            "link": "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20251125%2F20251125_IEXTP1_TOPS1.6.pcap.gz",
            "date": "20251125",
            "feed": "TOPS",
            "version": "1.6",
            "protocol": "IEXTP1",
            "size": 10,
        },
        {
            "link": "https://www.googleapis.com/download/storage/v1/b/iex/o/data%2Ffeeds%2F20251125%2F20251125_IEXTP1_DPLS1.0.pcap.gz",
            "date": "20251125",
            "feed": "DPLS",
            "version": "1.0",
            "protocol": "IEXTP1",
            "size": 10,
        },
    ],
}


def _fake_fetch(yyyymmdd: str, timeout: float = 30.0) -> str:
    return json.dumps(INDEX.get(yyyymmdd, []))


def test_iex_hist_index_and_recent_rule(tmp_path):
    hist = _load_tool("iex_hist")
    feeds = hist.parse_index(_fake_fetch("20251128"), "20251128")
    assert set(feeds) == {"TOPS", "DEEP+", "DEEP"}
    assert feeds["DEEP+"]["file"] == "20251128_IEXTP1_DPLS1.0.pcap.gz"
    assert feeds["TOPS"]["bytes"] == 4371690941
    assert hist.parse_index("", "20251127") == {}
    with pytest.raises(ValueError, match="allowlist"):
        hist.parse_index(json.dumps([{"feed": "TOPS", "link": "https://evil/x", "size": 1}]), "x")
    # the rule: latest N with TOPS and DEEP+; 11-26 (TOPS only) and 11-27 (empty) fall out
    available = {hist.iso(k): hist.parse_index(_fake_fetch(k), k) for k in INDEX}
    assert hist.select_recent(available, 5) == ["2025-11-28", "2025-11-25"]
    assert hist.select_recent(available, 1) == ["2025-11-28"]
    out = tmp_path / "overlap.json"
    assert (
        hist.main(
            [
                "index",
                "--date",
                DATE,
                "--date",
                "2025-11-27",
                "--out",
                str(out),
                "--approval",
                APPROVAL,
            ],
            fetch=_fake_fetch,
        )
        == 0
    )
    doc = pix.load_iex_manifest(out)
    assert doc["approval"] == APPROVAL and doc["days"][1]["feeds"]["TOPS"]["bytes"] == 4371690941
    assert doc["days"][0]["feeds"] == {} and "not on IEX HIST" in doc["days"][0]["note"]
    rec = tmp_path / "recent.json"
    assert (
        hist.main(
            ["recent", "--n", "5", "--lookback", "7", "--today", "2025-11-29", "--out", str(rec)],
            fetch=_fake_fetch,
        )
        == 0
    )
    doc = pix.load_iex_manifest(rec)
    assert [d["date"] for d in doc["days"]] == ["2025-11-25", "2025-11-28"]
    assert doc["approval"] is None and "NOT approved" in doc["note"]
    assert doc["rule"]["required_feeds"] == ["TOPS", "DEEP+"] and doc["rule"]["n"] == 5


def test_iex_download_refuses_without_the_approval_sentence(tmp_path, capsys):
    dl = _load_tool("iex_download")
    vendor = tmp_path / "v"
    vendor.mkdir()
    man = _manifest(tmp_path, vendor, approval=None)
    args = ["--manifest", str(man), "--vendor-dir", str(vendor), "--date", DATE, "--feed", "TOPS"]
    assert dl.main(args) == 2  # no flag
    assert dl.main(args + ["--i-have-approval"]) == 2  # no sentence in the manifest
    assert "approval sentence" in capsys.readouterr().err
    man = _manifest(tmp_path, vendor)
    assert dl.main(args + ["--i-have-approval", "--feed", "DEEP"]) == 2  # no such feed

    class _Resp:
        def __init__(self, data: bytes):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n: int) -> bytes:
            out, self.data = self.data[:n], self.data[n:]
            return out

    def opener(url: str):
        assert url.startswith("https://www.googleapis.com/")
        return _Resp(b"x")

    assert dl.main(args + ["--i-have-approval"], opener=opener) == 0
    assert (vendor / "20251128_IEXTP1_TOPS1.6.pcap.gz").read_bytes() == b"x"

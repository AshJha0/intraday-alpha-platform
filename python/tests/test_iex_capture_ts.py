"""v1.18 REL-04: the IEX capture timestamp as ``receive_ts`` (synthetic captures only)."""

from __future__ import annotations

import json
from pathlib import Path

import iex_encoder as ix
import pytest
from conftest import CONFIGS_DIR
from iap.marketdata import process_iex as pix
from iap.marketdata.feederrors import IngestError
from iap.marketdata.iex import PROTOCOL_DEEP_PLUS
from iap.marketdata.ingest import ingest, sequence_rows
from iap.marketdata.itch50 import session_midnight_ns
from iap.research import holdout_registry as hr
from itch50_encoder import hms_ns
from test_holdout_registry import make_dataset
from test_process_iex import _features_fake, _manifest, _tops_capture

pytestmark = pytest.mark.slow

DATE = "2025-11-28"
MID = session_midnight_ns(DATE)
OPEN = hms_ns(9, 30)
NS = 1_000_000_000
LAT = 150_000  # capture - send, ns
SYMS = ["AAPL", "QQQ"]


def _ep(ts: int) -> int:
    return MID + ts


def _capture(path: Path, *, stamp: bool = True) -> int:
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS, pcapng=True, nanosecond=True)
    t = OPEN + 5 * NS
    pre = [
        ix.system_event("O", _ep(hms_ns(3))),
        ix.directory("AAPL", _ep(hms_ns(3, 0, 1))),
        ix.directory("QQQ", _ep(hms_ns(3, 0, 2)), flags=0x20),
        ix.system_event("S", _ep(hms_ns(4))),
        ix.system_event("R", _ep(OPEN)),
    ]
    w.segment(pre, send_time=_ep(OPEN), capture_ts=_ep(OPEN) + LAT)
    body = [
        ix.add_order("AAPL", _ep(t), "B", 1, 100, 1_000_000),
        ix.add_order("AAPL", _ep(t), "S", 2, 200, 1_000_100),
        ix.add_order("QQQ", _ep(t), "B", 21, 300, 5_000_000),
        ix.add_order("QQQ", _ep(t), "S", 22, 300, 5_000_100),
        ix.add_order("AAPL", _ep(t + 1), "B", 3, 50, 1_000_000),
        ix.executed("AAPL", _ep(t + 2), 2, 200, 1_000_100, 5),
        ix.executed("QQQ", _ep(t + 3), 21, 100, 5_000_000, 6),
        ix.system_event("M", _ep(hms_ns(16))),
    ]
    for i in range(0, len(body), 4):
        chunk = body[i : i + 4]
        send = _ep(t + 3 + i)
        w.segment(chunk, send_time=send, capture_ts=send + LAT if stamp else None)
    w.write(path, compress=True)
    return t


def _rows(out: Path) -> list[dict]:
    f = next((out / "raw").glob("*.jsonl"))
    return [json.loads(x) for x in f.read_text().splitlines()]


def test_sequence_rows_uses_the_capture_field_and_counts_clamps():
    # inst, ts, type(ADD=0?), side, price, qty, oid, tid, capture
    protos = [
        (0, 100, 0, 0, 10, 1, 1, 0, 150),
        (0, 200, 0, 0, 10, 1, 2, 0, 120),
        (0, 300, 0, 0, 10, 1, 3, 0, -1),
    ]
    info: dict = {}
    rows = list(sequence_rows(protos, [1], 102, info, True))
    assert [(r[3], r[4]) for r in rows] == [(100, 150), (200, 200), (300, 300)]
    assert info["capture_ts_before_exchange"] == 1 and info["capture_ts_absent"] == 1
    plain = list(sequence_rows([p[:8] for p in protos], [1], 102, {}, False))
    assert all(r[3] == r[4] for r in plain)


def test_ingest_capture_ts_writes_receive_ts_and_differs_from_the_default(tmp_path):
    pcap = tmp_path / "d.pcap.gz"
    _capture(pcap)
    cap = tmp_path / "cap"
    base = tmp_path / "base"
    m_cap = ingest(
        "iex-deep-plus", [pcap], DATE, SYMS, cap, configs_dir=CONFIGS_DIR, capture_ts=True
    )
    m_base = ingest("iex-deep-plus", [pcap], DATE, SYMS, base, configs_dir=CONFIGS_DIR)
    rows = _rows(cap)
    assert rows and all(r["receive_ts"] >= r["exchange_ts"] for r in rows)
    assert any(r["receive_ts"] > r["exchange_ts"] for r in rows)
    assert all(r["receive_ts"] == r["exchange_ts"] for r in _rows(base))
    assert m_cap["source"]["receive_ts"] == "iex_capture" and "receive_ts" not in m_base["source"]
    assert m_cap["dataset_version"] != m_base["dataset_version"]
    iex = m_cap["sessions"][DATE]["messages"]["iex"]
    assert (
        iex["feed_latency"]["n"] > 0 and "offset" in iex["capture_ts_provenance"]["offset_limits"]
    )
    assert "feed_latency" not in m_base["sessions"][DATE]["messages"]["iex"]


def test_capture_ts_refuses_non_iex_and_mixed_modes(tmp_path):
    pcap = tmp_path / "d.pcap.gz"
    _capture(pcap)
    out = tmp_path / "o"
    ingest("iex-deep-plus", [pcap], DATE, SYMS, out, configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="receive_ts source"):
        ingest("iex-deep-plus", [pcap], DATE, SYMS, out, configs_dir=CONFIGS_DIR, capture_ts=True)
    with pytest.raises(IngestError, match="IEX formats only"):
        ingest(
            "itch50", [pcap], DATE, SYMS, tmp_path / "x", configs_dir=CONFIGS_DIR, capture_ts=True
        )


def _vendor(tmp_path: Path) -> tuple[Path, Path]:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    t = _capture(vendor / "20251128_IEXTP1_DPLS1.0.pcap.gz")
    _tops_capture(vendor / "20251128_IEXTP1_TOPS1.6.pcap.gz", t)
    return vendor, _manifest(tmp_path, vendor)


def _day(man, vendor, out_root, **kw):
    return pix.process_iex_day(
        man, DATE, vendor, out_root, SYMS, configs_dir=CONFIGS_DIR, partition_instruments=1,
        min_free_gb=0.0, log=lambda _l: None, run=_features_fake, **kw,
    )  # fmt: skip


def test_process_day_capture_ts_chain_and_overwrite_refusals(tmp_path):
    vendor, man = _vendor(tmp_path)
    out_root = tmp_path / "iex_capts"
    rep = _day(man, vendor, out_root, capture_ts=True, keep_gz=True)
    ing = next(s for s in rep["steps"] if s["step"] == "ingest")
    assert rep["capture_ts"] is True and ing["feed_latency"]["n"] > 0
    ds = out_root / "iex_20251128"
    assert json.loads((ds / "dataset.json").read_text())["source"]["receive_ts"] == "iex_capture"
    # the legacy A4 root is never a capture-ts target
    with pytest.raises(IngestError, match="must not write"):
        _day(man, vendor, pix.LEGACY_IEX_ROOT, capture_ts=True, dry_run=True)
    with pytest.raises(IngestError, match="must not write"):
        _day(man, vendor, tmp_path / "data" / "real" / "iex", capture_ts=True, dry_run=True)
    # a default run does not overwrite a capture-ts dataset, nor the reverse
    with pytest.raises(IngestError, match="refusing to overwrite"):
        _day(man, vendor, out_root)
    plain = tmp_path / "iex_plain"
    _day(man, vendor, plain, keep_gz=True)
    with pytest.raises(IngestError, match="refusing to overwrite"):
        _day(man, vendor, plain, capture_ts=True)
    assert (
        json.loads((plain / "iex_20251128" / "dataset.json").read_text())["source"].get(
            "receive_ts"
        )
        is None
    )


def test_cli_flag_and_wrapper(tmp_path):
    vendor, man = _vendor(tmp_path)
    cmd = tmp_path / "w.cmd"
    rc = pix.main(
        ["--manifest", str(man), "--date", DATE, "--date", "2025-12-08", "--vendor-dir", str(vendor),
         "--out-root", str(tmp_path / "iex_capts"), "--symbols", "AAPL,QQQ", "--capture-ts",
         "--render-cmd", str(cmd)]
    )  # fmt: skip
    text = cmd.read_text()
    assert rc == 0 and text.count("--capture-ts") == 2 and "iex_capts" in text


def test_capture_dataset_does_not_bypass_the_holdout_registry(tmp_path):
    # a new content hash / version with a registered holdout session inherits the scope's state
    ds = make_dataset(tmp_path, "iex_capts_20260515", ["2026-05-15"], payload=b"capture-ts bytes")
    with pytest.raises(hr.HoldoutRefused):
        hr.guard(ds, purpose="test")
    with pytest.raises(hr.HoldoutRefused):
        hr.refuse_unit_name("iex_capts_20260515")

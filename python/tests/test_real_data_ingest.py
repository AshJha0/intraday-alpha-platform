"""``python -m iap.marketdata ingest``: layout, manifest, determinism, the
point-in-time reference data it writes, the research hook, a throughput
smoke test and the opt-in run over real sample files.

No vendor data: ITCH bytes come from ``itch50_encoder``, LOBSTER files from
``lobster_fixture``.  The last two tests are skipped unless environment
variables point at files the owner obtained (docs/REAL_DATA.md §7).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import tracemalloc
from pathlib import Path

import pytest
from conftest import CONFIGS_DIR, REPO_ROOT
from iap.core.codec import encode_jsonl, read_iap1, read_jsonl, sha256_file, write_jsonl
from iap.core.events import EventType, MarketEvent, validation_error
from iap.experiment import tracker
from iap.features.__main__ import main as features_main
from iap.features.context import build_contexts
from iap.marketdata.__main__ import main as marketdata_main
from iap.marketdata.feederrors import BookDivergenceError, IngestError
from iap.marketdata.ingest import (
    _BookCheck,
    ingest,
    load_manifest,
    real_dataset_version,
    sequence_events,
)
from iap.marketdata.itch50 import Itch50Mapper, Itch50Reader
from iap.orderbook.book import ApplyStatus, OrderBook
from iap.reference.refdata import ReferenceData
from iap.reference.secmaster import SecurityMaster, SecurityMasterError
from iap.research.__main__ import main as research_main
from itch50_encoder import Itch50Encoder, build_session, hms_ns
from lobster_fixture import write_lobster

SYMBOLS = ("AAPL", "MSFT", "QQQ")
D1, D2 = "2019-12-30", "2019-12-31"


def _day(tmp_path: Path, seed: int, name: str, compress: bool = False, **kw) -> Path:
    kw.setdefault("n_actions", 1500)
    enc, _ = build_session(seed, symbols=SYMBOLS, etp_symbols=("QQQ",), **kw)
    return enc.write(tmp_path / name, compress=compress)


def _cli(*args: str) -> int:
    return marketdata_main(["ingest", *args])


def _ingest_cli(path: Path, date: str, out: Path, *extra: str) -> int:
    return _cli(
        "--format", "itch50", "--input", str(path), "--date", date,
        "--symbols", ",".join(SYMBOLS), "--out", str(out), *extra,
    )  # fmt: skip


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_ingest_writes_the_generator_layout_and_a_manifest(tmp_path, capsys):
    src = _day(tmp_path, 7, "20191230.NASDAQ_ITCH50.gz", compress=True)
    out = tmp_path / "ds"
    assert _ingest_cli(src, D1, out) == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(_tree(out)) == {
        "dataset.json",
        "raw/eq_20191230.jsonl",
        "normalized/eq_20191230.normalized.jsonl",
        "normalized/eq_20191230.normalized.iap1",
        "normalized/events.parquet",
        "normalized/qc_report.json",
        "configs/instruments/instruments.json",
        "configs/venues/venues.json",
        "configs/execution/execution.json",
        "reference/security_master.json",
    }
    manifest = load_manifest(out)
    assert manifest["kind"] == "real" and manifest["x-version"] == 1
    assert manifest["source"] == {"format": "itch50", "venue": "XNAS", "venue_id": 101}
    assert [u["symbol"] for u in manifest["universe"]] == list(SYMBOLS)
    assert [u["instrument_id"] for u in manifest["universe"]] == [1, 2, 3]
    session = manifest["sessions"][D1]
    assert session["inputs"] == [
        {"file": src.name, "sha256": sha256_file(src), "bytes": src.stat().st_size}
    ]
    messages = session["messages"]
    reader = Itch50Reader(src)
    list(reader)
    assert messages["total"] == reader.messages_read
    assert messages["by_type"] == {k: v for k, v in sorted(reader.counts.items()) if v}
    assert messages["skipped_unknown"] == 0 and messages["filtered_other_symbols"] > 0
    assert session["raw"]["sha256"] == sha256_file(out / "raw" / "eq_20191230.jsonl")
    assert session["book_check"]["clean"] is True
    assert session["mapping"]["outside_session_dropped"] == len(SYMBOLS)
    assert manifest["dataset_version"] == real_dataset_version(out / "normalized")
    assert summary["dataset_version"] == manifest["dataset_version"]
    assert summary["events"] == session["raw"]["events"] > 1000
    # no wall clock and no absolute path in any written JSON document
    for name in ("dataset.json", "normalized/qc_report.json"):
        text = (out / name).read_text()
        assert str(tmp_path) not in text and tmp_path.as_posix() not in text
        assert "timing" not in text and b"\r" not in (out / name).read_bytes()

    raw = read_jsonl(out / "raw" / "eq_20191230.jsonl")
    normalized = read_iap1(out / "normalized" / "eq_20191230.normalized.iap1")
    assert len(raw) == len(normalized) == session["raw"]["events"]
    assert all(validation_error(ev) is None for ev in raw)
    assert {ev.venue_id for ev in raw} == {101}
    assert all(ev.receive_ts == ev.exchange_ts for ev in raw)
    assert [ev.event_id for ev in normalized] == list(range(1, len(normalized) + 1))
    totals = manifest["normalized"]["qc_totals"]
    assert totals["events_in"] == totals["events_out"] == len(raw)
    assert all(totals[k] == 0 for k in totals if k not in ("events_in", "events_out"))


def test_same_input_gives_identical_output_bytes(tmp_path, capsys):
    src = _day(tmp_path, 7, "day.itch")
    a, b = tmp_path / "a", tmp_path / "deeper" / "b"
    b.parent.mkdir()
    assert _ingest_cli(src, D1, a) == 0 and _ingest_cli(src, D1, b) == 0
    capsys.readouterr()
    tree = _tree(a)
    assert tree == _tree(b) and len(tree) == 10
    # re-ingesting the same session into the same directory changes nothing
    assert _ingest_cli(src, D1, a) == 0
    assert _tree(a) == tree
    # the compressed form of the same bytes maps to the same events
    packed = tmp_path / "day.itch.gz"
    enc, _ = build_session(7, symbols=SYMBOLS, etp_symbols=("QQQ",), n_actions=1500)
    enc.write(packed, compress=True)
    c = tmp_path / "c"
    assert _ingest_cli(packed, D1, c) == 0
    other = _tree(c)
    assert {k: v for k, v in other.items() if k != "dataset.json"} == {
        k: v for k, v in tree.items() if k != "dataset.json"
    }
    assert load_manifest(c)["dataset_version"] == load_manifest(a)["dataset_version"]


def test_output_bytes_of_a_seeded_dataset_are_pinned(tmp_path, capsys):
    """Known-answer pin (the dataset of COOKBOOK recipe 36): the hashes were
    recorded BEFORE the pass-2 / normaliser performance work, so any change
    to the output bytes — by an optimisation or by a platform — fails here."""
    out = tmp_path / "ds"
    for seed, date in ((7, D1), (8, D2)):
        src = _day(
            tmp_path, seed, f"{date}.itch.gz", compress=True, n_actions=6000, spacing_ns=10**9
        )
        assert _ingest_cli(src, date, out) == 0
    capsys.readouterr()
    manifest = load_manifest(out)
    assert manifest["dataset_version"] == (
        "70ddb405e78c2356c2a6f15a85f8dfe520cc27bf427ec2532d4257c480e8e29e"
    )
    assert manifest["sessions"][D1]["raw"]["sha256"] == (
        "104ceb7f631d8356133eb486152547d6929c8a628caadc550d2f62fed9abdab2"
    )
    assert manifest["normalized"]["files"] == {
        "eq_20191230.normalized.iap1": "a1c8a4f4b293913b19b3947a3ec32a7d235b061b90249cadf4d814bcba24611a",
        "eq_20191230.normalized.jsonl": "104ceb7f631d8356133eb486152547d6929c8a628caadc550d2f62fed9abdab2",
        "eq_20191231.normalized.iap1": "27b440e07cc1ca984fe819cefd7b50b7c548c02d4aea4c219316fbaa8c3a6f53",
        "eq_20191231.normalized.jsonl": "30933784970dd485f31a0863b533c38ee7354073946a77995c1b2d7045f5e09f",
    }


def test_dataset_version_is_content_pinned_and_disjoint_from_synthetic(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    assert _ingest_cli(_day(tmp_path, 7, "d7.itch"), D1, a) == 0
    assert _ingest_cli(_day(tmp_path, 8, "d8.itch"), D1, b) == 0
    capsys.readouterr()
    va, vb = load_manifest(a)["dataset_version"], load_manifest(b)["dataset_version"]
    assert va != vb and len(va) == 64 and int(va, 16) >= 0
    # the synthetic fingerprint of the very same normalized bytes is another value
    root = tmp_path / "as_synthetic"
    shutil.copytree(a / "normalized", root / "data" / "normalized")
    assert tracker.data_version(root) != va


def test_reference_data_of_the_dataset_loads_through_the_existing_services(tmp_path, capsys):
    out = tmp_path / "ds"
    assert _ingest_cli(_day(tmp_path, 7, "d.itch"), D1, out) == 0
    capsys.readouterr()
    refdata = ReferenceData.load(out / "configs")
    assert [i.symbol for i in refdata.instruments()] == list(SYMBOLS)
    aapl, qqq = refdata.instrument("AAPL"), refdata.instrument("QQQ")
    assert (aapl.asset_class, aapl.tick_size, aapl.lot_size, aapl.currency) == (
        "EQUITY", 0.01, 100, "USD",
    )  # fmt: skip
    assert qqq.asset_class == "ETF"  # from the directory's ETP flag
    assert refdata.venue_ids_for(1) == [101] and refdata.venue(101).venue == "XNAS"
    assert refdata.trading_days == [D1]
    open_ns, close_ns = refdata.session_bounds_ns("EQUITY", D1)
    assert close_ns - open_ns == 23_400 * 10**9
    assert refdata.session_timezone("EQUITY") == "America/New_York"
    manifest = load_manifest(out)
    per = manifest["sessions"][D1]["per_symbol"]["AAPL"]
    assert per["ref_price_source"] == "opening_cross"
    assert aapl.ref_price == per["ref_price_e4"] / 10_000 and aapl.adv == per["volume"] > 0
    contexts = build_contexts(out / "configs")
    assert contexts[1].ref_instrument_id == 3 and contexts[1].session_timezone == "America/New_York"

    master = SecurityMaster.load(out / "reference" / "security_master.json")
    rec = master.as_of("QQQ", D1)
    assert (rec.etp_flag, rec.asset_class, rec.round_lot_size, rec.source) == (
        "Y",
        "ETF",
        100,
        "itch50",
    )
    assert rec.locate == manifest_locate(tmp_path / "d.itch", "QQQ")
    with pytest.raises(SecurityMasterError):
        master.as_of("QQQ", "2019-12-27")


def manifest_locate(path: Path, symbol: str) -> int:
    reader = Itch50Reader(path, symbols=(symbol,))
    list(reader)
    return reader.directory[symbol].locate


def test_a_second_session_extends_the_dataset(tmp_path, capsys):
    out = tmp_path / "ds"
    assert _ingest_cli(_day(tmp_path, 8, "d2.itch"), D2, out) == 0
    first = load_manifest(out)["dataset_version"]
    assert _ingest_cli(_day(tmp_path, 7, "d1.itch"), D1, out) == 0  # an EARLIER date, later
    capsys.readouterr()
    manifest = load_manifest(out)
    assert list(manifest["sessions"]) == [D1, D2] and manifest["dataset_version"] != first
    assert ReferenceData.load(out / "configs").trading_days == [D1, D2]
    qc = json.loads((out / "normalized" / "qc_report.json").read_text())
    assert sorted(qc["files"]) == ["eq_20191230.jsonl", "eq_20191231.jsonl"]
    # per-instrument sequences restart every session: one counted reset per stream
    assert qc["totals"]["sequence_resets"] == len(SYMBOLS) and qc["totals"]["gaps"] == 0
    master = SecurityMaster.load(out / "reference" / "security_master.json")
    assert master.dates("AAPL") == [D1, D2]
    # order of ingestion does not matter: the same two sessions, the other way round
    other = tmp_path / "other"
    assert _ingest_cli(tmp_path / "d1.itch", D1, other) == 0
    assert _ingest_cli(tmp_path / "d2.itch", D2, other) == 0
    capsys.readouterr()
    assert _tree(other) == _tree(out)


def test_deferred_normalisation_gives_the_same_dataset(tmp_path, capsys):
    """A many-day load normalises once, at the end: `--defer-normalize` on all
    but the last date produces the bytes of the session-by-session load."""
    d1, d2 = _day(tmp_path, 7, "d1.itch"), _day(tmp_path, 8, "d2.itch")
    stepwise = tmp_path / "stepwise"
    assert _ingest_cli(d1, D1, stepwise) == 0 and _ingest_cli(d2, D2, stepwise) == 0
    deferred = tmp_path / "deferred"
    assert _ingest_cli(d1, D1, deferred, "--defer-normalize") == 0
    summary = json.loads(capsys.readouterr().out.rsplit("\n{\n", 1)[-1].join(["{\n", ""]))
    assert summary["dataset_version"] is None and summary["qc_totals"] is None
    pending = load_manifest(deferred)
    assert pending["normalized"] is None and "dataset_version" not in pending
    assert not (deferred / "normalized").exists()
    assert research_main(["run", "--no-prereg", "--alpha", "EQ03", "--dataset-dir", str(deferred)]) == 1
    assert _ingest_cli(d2, D2, deferred) == 0
    capsys.readouterr()
    assert _tree(deferred) == _tree(stepwise)


def test_ingest_refuses_inconsistent_requests(tmp_path, capsys):
    src = _day(tmp_path, 7, "d.itch")
    out = tmp_path / "ds"
    assert _ingest_cli(src, D1, out) == 0
    before = _tree(out)
    with pytest.raises(IngestError, match="one format and one universe"):
        ingest("itch50", [src], D2, ["AAPL"], out, configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="no Stock Directory"):
        ingest("itch50", [src], D1, ["AAPL", "NOPE"], tmp_path / "x1", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="YYYY-MM-DD"):
        ingest("itch50", [src], "30/12/2019", SYMBOLS, tmp_path / "x2", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="input file not found"):
        ingest("itch50", [tmp_path / "nope"], D1, SYMBOLS, tmp_path / "x3", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="--symbols is required"):
        ingest("itch50", [src], D1, None, tmp_path / "x4", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="twice"):
        ingest("itch50", [src], D1, ["AAPL", "AAPL"], tmp_path / "x5", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="exactly one --input"):
        ingest("itch50", [src, src], D1, SYMBOLS, tmp_path / "x6", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="unknown format"):
        ingest("pitch", [src], D1, SYMBOLS, tmp_path / "x7", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="execution config template not found"):
        ingest("itch50", [src], D1, SYMBOLS, tmp_path / "x8", configs_dir=tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "note.txt").write_text("mine")
    with pytest.raises(IngestError, match="not empty and is not an ingested dataset"):
        ingest("itch50", [src], D1, SYMBOLS, occupied, configs_dir=CONFIGS_DIR)
    assert _tree(out) == before  # the refused requests left the dataset alone
    # the CLI turns them into exit code 1 and one line on stderr
    capsys.readouterr()
    assert _ingest_cli(src, D1, tmp_path / "x9", "--symbols", "NOPE") == 1
    assert "error: " in capsys.readouterr().err
    assert not (tmp_path / "x9" / "dataset.json").exists()


def test_truncated_input_fails_the_ingest_with_the_offset(tmp_path, capsys):
    src = _day(tmp_path, 7, "d.itch")
    cut = tmp_path / "cut.itch"
    cut.write_bytes(src.read_bytes()[:-5])
    assert _ingest_cli(cut, D1, tmp_path / "ds") == 1
    assert "byte offset" in capsys.readouterr().err
    assert not (tmp_path / "ds").exists()  # no spool, no partial file, no empty directories


def test_limit_messages_ingests_the_head_of_the_file(tmp_path, capsys):
    src = _day(tmp_path, 7, "d.itch")
    out = tmp_path / "ds"
    assert _ingest_cli(src, D1, out, "--limit-messages", "400") == 0
    capsys.readouterr()
    session = load_manifest(out)["sessions"][D1]
    assert session["limit_messages"] == 400
    assert session["messages"]["total"] == 400 and session["messages"]["limit_reached"] is True
    full = tmp_path / "full"
    assert _ingest_cli(src, D1, full) == 0
    capsys.readouterr()
    assert session["raw"]["events"] < load_manifest(full)["sessions"][D1]["raw"]["events"]


def _penny_and_subpenny_day(tmp_path: Path) -> Path:
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "BIGCO")
    enc.stock_directory(2, hms_ns(3, 0, 1), "PENNY", round_lot_size=1)
    enc.system_event(hms_ns(9, 30), "Q")
    t = hms_ns(9, 30, 1)
    enc.add(1, t, 11, "B", 100, "BIGCO", 1_500_000)
    enc.add(1, t + 1, 12, "S", 100, "BIGCO", 1_500_100)
    enc.trade(1, t + 2, "B", 30, "BIGCO", 1_500_050, 1)  # hidden midpoint print
    enc.add(2, t + 3, 21, "B", 1000, "PENNY", 4_321)  # $0.4321: sub-penny quoting below $1
    enc.add(2, t + 4, 22, "S", 1000, "PENNY", 4_400)
    enc.executed(2, t + 5, 21, 400, 2)
    return enc.write(tmp_path / "ticks.itch")


def test_tick_size_follows_the_displayed_prices(tmp_path):
    src = _penny_and_subpenny_day(tmp_path)
    out = tmp_path / "ds"
    manifest = ingest("itch50", [src], D1, ["BIGCO", "PENNY"], out, configs_dir=CONFIGS_DIR)
    ticks = {u["symbol"]: (u["tick_size"], u["lot_size"]) for u in manifest["universe"]}
    assert ticks == {"BIGCO": (0.01, 100), "PENNY": (0.0001, 1)}
    raw = read_jsonl(out / "raw" / "eq_20191230.jsonl")
    prices = {(ev.instrument_id, ev.event_type): ev.price_ticks for ev in raw if ev.price_ticks}
    assert prices[(1, int(EventType.ADD))] == 15_001 and prices[(1, int(EventType.TRADE))] == 15_001
    assert prices[(2, int(EventType.EXECUTE))] == 4_321  # exact: one tick is 1/10000 dollar
    assert manifest["sessions"][D1]["raw"]["trade_prices_rounded_to_tick"] == 1
    assert manifest["sessions"][D1]["per_symbol"]["PENNY"]["ref_price_source"] == "first_trade"
    # forcing the cent grid on the sub-dollar name rejects its off-grid orders, counted
    forced = ingest(
        "itch50", [src], D1, ["BIGCO", "PENNY"], tmp_path / "forced",
        configs_dir=CONFIGS_DIR, tick_size="0.01",
    )  # fmt: skip
    assert forced["sessions"][D1]["per_symbol"]["PENNY"]["off_tick_orders_rejected"] == 1
    fine = ingest(
        "itch50", [src], D1, ["BIGCO", "PENNY"], tmp_path / "fine",
        configs_dir=CONFIGS_DIR, tick_size="0.0001",
    )  # fmt: skip
    assert {u["tick_size_e4"] for u in fine["universe"]} == {1}
    with pytest.raises(IngestError, match="one\n?.*tick size per instrument|differs from the"):
        ingest(
            "itch50", [src], D2, ["BIGCO", "PENNY"], tmp_path / "fine",
            configs_dir=CONFIGS_DIR, tick_size="0.01",
        )  # fmt: skip


def _stub_quote_day(tmp_path: Path) -> Path:
    """The pattern of a real Nasdaq file (2019-12-30): a stock trading at
    hundreds of dollars carries a few far-from-market BIDS priced under
    $1.00 — legally sub-penny under Rule 612 — entered before the real
    quotes arrive."""
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "BIGCO")
    enc.system_event(hms_ns(4), "S")
    t = hms_ns(8, 59)
    enc.add(1, t, 1, "B", 1, "BIGCO", 2_600)  # $0.26: under a dollar but on the cent grid
    enc.add(1, t + 1, 2, "B", 1, "BIGCO", 1)  # $0.0001
    enc.add(1, t + 2, 3, "B", 100, "BIGCO", 8_769)  # $0.8769
    enc.add(1, t + 3, 4, "B", 100, "BIGCO", 12)  # $0.0012
    enc.system_event(hms_ns(9, 30), "Q")
    t = hms_ns(9, 30, 1)
    enc.add(1, t, 11, "B", 100, "BIGCO", 2_894_300)
    enc.add(1, t + 1, 12, "S", 100, "BIGCO", 2_894_500)
    enc.cancel(1, t + 2, 3, 40)  # partial cancel of a rejected order
    enc.delete(1, t + 3, 2)  # delete of a rejected order
    enc.replace(1, t + 4, 3, 5, 100, 9_000)  # rejected order replaced ONTO the grid ($0.90)
    enc.replace(1, t + 5, 11, 13, 100, 2_894_350)  # on-grid order replaced OFF the grid
    enc.executed(1, t + 6, 13, 100, 77)  # ... and executed: the print survives, rounded
    enc.delete(1, t + 7, 4)
    return enc.write(tmp_path / "stubs.itch")


def test_sub_dollar_stub_bids_do_not_make_a_stock_sub_penny(tmp_path):
    """Regression (first real run): a handful of sub-dollar, sub-penny stub
    bids set every instrument's tick to 0.0001.  The tick is the increment
    of the price range the instrument is quoted in; orders off that grid
    are rejected and counted, never rounded."""
    out = tmp_path / "ds"
    manifest = ingest(
        "itch50", [_stub_quote_day(tmp_path)], D1, ["BIGCO"], out, configs_dir=CONFIGS_DIR
    )
    (entry,) = manifest["universe"]
    assert (entry["tick_size"], entry["tick_size_e4"]) == (0.01, 100)
    session = manifest["sessions"][D1]
    per = session["per_symbol"]["BIGCO"]
    assert per["displayed_orders_below_one_dollar"] == 5  # four stubs + the replacement at $0.90
    assert per["off_tick_orders_rejected"] == 4  # $0.0001, $0.8769, $0.0012 and the 289.435 replace
    assert per["ref_price_e4"] == 2_894_350 and per["ref_price_source"] == "first_trade"
    raw_info = session["raw"]
    assert raw_info["off_tick_orders_rejected"] == 4
    # cancel + delete + the CANCEL half of the replace of #3, the execute of #13, the delete of #4
    assert raw_info["events_on_rejected_orders"] == 5
    assert raw_info["trade_prices_rounded_to_tick"] == 1
    assert session["book_check"]["clean"] is True
    assert session["book_check"]["per_symbol"]["BIGCO"]["resting_orders_at_end"] == 3
    raw = read_jsonl(out / "raw" / "eq_20191230.jsonl")
    assert [ev.sequence for ev in raw] == list(range(1, len(raw) + 1))  # gap-free after rejections
    book_events = [
        (ev.event_type, ev.side, ev.price_ticks, ev.qty, ev.order_id)
        for ev in raw
        if ev.event_type != int(EventType.STATUS)
    ]
    add, cancel, trade = int(EventType.ADD), int(EventType.CANCEL), int(EventType.TRADE)
    assert book_events == [
        (add, 0, 26, 1, 1),  # the on-grid stub rests at 26 ticks
        (add, 0, 28_943, 100, 11),
        (add, 1, 28_945, 100, 12),
        (add, 0, 90, 100, 5),  # the replacement of a rejected order: ADD only
        (cancel, 0, 28_943, 0, 11),  # the original leaves; its off-grid replacement is rejected
        (trade, 1, 28_944, 100, 0),  # 289.435 rounds half up; no EXECUTE of a rejected order
    ]
    # the real order book accepts every emitted event and agrees with the check
    book = OrderBook(1, 101)
    assert all(book.apply(ev) == ApplyStatus.APPLIED for ev in raw)
    assert book.order_count_total() == 3 and book.best_bid() == (90, 100)
    assert (out / "configs" / "instruments" / "instruments.json").read_text().count(
        '"tick_size": 0.01'
    ) == 1


def test_a_stock_quoted_below_a_dollar_is_a_sub_penny_instrument(tmp_path):
    enc = Itch50Encoder()
    enc.stock_directory(1, hms_ns(3), "PENNY")
    enc.system_event(hms_ns(9, 30), "Q")
    t = hms_ns(9, 30, 1)
    enc.add(1, t, 1, "S", 100, "PENNY", 10_200)  # one offer above a dollar
    enc.add(1, t + 1, 2, "B", 100, "PENNY", 9_911)
    enc.add(1, t + 2, 3, "S", 100, "PENNY", 9_950)
    manifest = ingest(
        "itch50",
        [enc.write(tmp_path / "p.itch")],
        D1,
        ["PENNY"],
        tmp_path / "ds",
        configs_dir=CONFIGS_DIR,
    )
    assert manifest["universe"][0]["tick_size_e4"] == 1
    assert manifest["sessions"][D1]["raw"]["off_tick_orders_rejected"] == 0


def _reference_book_report(events) -> dict:
    """What the real ``OrderBook`` says about a stream (the check's oracle)."""
    book = OrderBook(1, 101)
    dropped = sum(book.apply(ev) != ApplyStatus.APPLIED for ev in events)
    counters = book.counters()
    return {
        "dropped": dropped,
        "events_applied": counters["events_applied"],
        "drop_counters": {k: v for k, v in counters.items() if v and k != "events_applied"},
        "resting_orders_at_end": book.order_count_total(),
        "crossed_at_end": book.is_crossed(),
    }


def _check_report(events) -> dict:
    check = _BookCheck(1)
    for ev in events:
        check.apply(0, ev.event_type, ev.side, ev.price_ticks, ev.qty, ev.order_id)
    report = check.report(("X",))
    return {
        "dropped": report["dropped"],
        "crossing_adds": report["crossing_adds"],
        **report["per_symbol"]["X"],
    }


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_book_check_agrees_with_the_real_order_book_on_clean_streams(tmp_path, seed):
    enc, _ = build_session(seed, symbols=("AAPL",), n_actions=2500)
    path = enc.write(tmp_path / "d.itch")
    protos = list(Itch50Mapper(D1, ("AAPL",)).events(Itch50Reader(path, symbols=("AAPL",))))
    events = list(sequence_events(protos, [100], 101))
    ours = _check_report(events)
    assert ours.pop("crossing_adds") == 0
    assert ours == _reference_book_report(events) and ours["dropped"] == 0


def test_book_check_agrees_with_the_real_order_book_on_inconsistent_streams():
    add, modify, cancel, execute, status = (
        int(EventType.ADD), int(EventType.MODIFY), int(EventType.CANCEL),
        int(EventType.EXECUTE), int(EventType.STATUS),
    )  # fmt: skip
    rows = [
        (add, 0, 100, 50, 1),
        (add, 0, 100, 50, 1),  # duplicate id
        (add, 1, 105, 30, 2),
        (modify, 0, 100, 20, 1),
        (modify, 0, 101, 10, 1),  # price mismatch
        (modify, 0, 100, 60, 1),  # increase
        (execute, 0, 100, 500, 1),  # more than rests: removes the order
        (execute, 0, 100, 10, 1),  # unknown now
        (cancel, 0, 0, 0, 9),  # unknown
        (modify, 0, 0, 5, 9),  # unknown
        (add, 0, 0, 10, 3),  # non-positive price
        (cancel, 1, 105, 0, 2),
        (status, 0, 0, 3, 0),
        (add, 0, 110, 10, 4),  # crossing is allowed outside continuous trading
        (add, 1, 108, 10, 5),
    ]
    events = [
        MarketEvent(i + 1, 1, 101, 1000 + i, 1000 + i, i + 1, et, side, price, qty, oid, 0)
        for i, (et, side, price, qty, oid) in enumerate(rows)
    ]
    ours = _check_report(events)
    assert ours.pop("crossing_adds") == 0
    reference = _reference_book_report(events)
    assert ours == reference
    assert ours["drop_counters"] == {
        "invalid_payload_dropped": 1, "modify_price_mismatch": 1, "unknown_order_events": 4,
    }  # fmt: skip
    assert ours["crossed_at_end"] is True and ours["resting_orders_at_end"] == 2


def test_book_check_counts_a_crossing_displayed_add_in_continuous_trading():
    check = _BookCheck(1)
    add = int(EventType.ADD)
    check.apply(0, add, 1, 105, 10, 1)
    check.apply(0, add, 0, 104, 10, 2)
    assert check.crossing_adds == 0
    check.apply(0, add, 0, 105, 10, 3)  # locks / crosses the offer
    check.apply(0, int(EventType.CANCEL), 0, 0, 0, 3)
    check.apply(0, int(EventType.STATUS), 0, 0, 2, 0)
    check.apply(0, add, 0, 106, 10, 4)  # halted: rests crossed, not counted as an add
    check.apply(0, int(EventType.STATUS), 0, 0, 1, 0)  # trading resumes on a crossed book
    report = check.report(("X",))
    assert (report["crossing_adds"], report["crossed_on_resume"], report["clean"]) == (1, 1, False)


def test_raw_file_bytes_equal_the_canonical_codec(tmp_path):
    """The batched writer of pass 2 produces exactly ``write_jsonl``'s bytes."""
    out = tmp_path / "ds"
    src = _day(tmp_path, 7, "d.itch")
    ingest("itch50", [src], D1, SYMBOLS, out, configs_dir=CONFIGS_DIR)
    raw = out / "raw" / "eq_20191230.jsonl"
    again = tmp_path / "again.jsonl"
    write_jsonl(again, read_jsonl(raw))
    assert again.read_bytes() == raw.read_bytes()
    mapper = Itch50Mapper(D1, SYMBOLS)
    protos = mapper.events(Itch50Reader(src, symbols=SYMBOLS))
    assert encode_jsonl(sequence_events(protos, [100, 100, 100], 101)) == raw.read_bytes()


def test_corporate_actions_table_is_validated_and_copied(tmp_path):
    src = _day(tmp_path, 7, "d.itch")
    table = tmp_path / "ca.csv"
    table.write_text(
        "ex_date,symbol,action,ratio_new,ratio_old,cash_amount,new_symbol\n"
        "2020-08-31,AAPL,SPLIT,4,1,,\n"
    )
    out = tmp_path / "ds"
    manifest = ingest(
        "itch50", [src], D1, SYMBOLS, out, configs_dir=CONFIGS_DIR, corporate_actions=table
    )
    copied = out / "reference" / "corporate_actions.csv"
    assert copied.read_bytes() == table.read_bytes()
    assert manifest["corporate_actions"] == {
        "file": "reference/corporate_actions.csv",
        "sha256": sha256_file(copied),
        "actions": 1,
    }
    table.write_text("nonsense\n")
    with pytest.raises(ValueError, match="header must be exactly"):
        ingest("itch50", [src], D1, SYMBOLS, out, configs_dir=CONFIGS_DIR, corporate_actions=table)


def test_lobster_ingest_verifies_the_book_and_fails_on_a_wrong_orderbook_file(tmp_path, capsys):
    date = "2012-06-21"
    files = tmp_path / "lobster"
    files.mkdir()
    aapl, aapl_book, _ = write_lobster(files, "AAPL", date, 1, n_messages=400)
    msft, _, _ = write_lobster(files, "MSFT", date, 2, n_messages=400)
    out = tmp_path / "ds"
    args = ["--format", "lobster", "--input", str(msft), str(aapl), "--date", date]
    assert _cli(*args, "--out", str(out)) == 0  # symbols and orderbook files from the names
    summary = json.loads(capsys.readouterr().out)
    assert summary["book_verification"] == {"AAPL": "match", "MSFT": "match"}
    manifest = load_manifest(out)
    session = manifest["sessions"][date]
    assert manifest["source"]["format"] == "lobster" and session["symbols"] == ["AAPL", "MSFT"]
    assert [i["file"] for i in session["inputs"]] == [
        msft.name, aapl.name, msft.name.replace("message", "orderbook"), aapl_book.name,
    ]  # fmt: skip
    assert session["book_verification"]["AAPL"] == {
        "status": "match", "levels": 5, "rows_verified": 400,
    }  # fmt: skip
    assert session["book_check"]["clean"] is True
    assert session["messages"]["total"] == 800 and session["mapping"]["seeded_levels"] == 20
    assert (
        SecurityMaster.load(out / "reference" / "security_master.json").as_of("MSFT", date).source
        == "lobster"
    )
    tree = _tree(out)
    again = tmp_path / "again"
    assert _cli(*args, "--out", str(again)) == 0
    assert _tree(again) == tree  # byte-deterministic

    rows = aapl_book.read_text().splitlines()
    fields = rows[250].split(",")
    fields[1] = str(int(fields[1]) + 100)
    rows[250] = ",".join(fields)
    aapl_book.write_text("\n".join(rows) + "\n")
    capsys.readouterr()
    bad = tmp_path / "bad"
    assert _cli(*args, "--out", str(bad)) == 3
    err = capsys.readouterr().err
    assert "AAPL: reconstructed book diverges" in err and '"message_index": 250' in err
    assert not (bad / "dataset.json").exists()
    with pytest.raises(BookDivergenceError) as exc:
        ingest("lobster", [msft, aapl], date, None, tmp_path / "bad2", configs_dir=CONFIGS_DIR)
    assert exc.value.report["first_divergence"]["side"] == "ask"
    assert _cli(*args, "--out", str(bad), "--allow-book-divergence") == 0
    capsys.readouterr()
    recorded = load_manifest(bad)["sessions"][date]["book_verification"]
    assert recorded["AAPL"]["status"] == "diverged" and recorded["MSFT"]["status"] == "match"
    assert recorded["AAPL"]["rows_verified"] == 250


def test_research_runs_on_an_ingested_dataset_by_path(tmp_path, capsys):
    """ingest -> features -> ``python -m iap.research run --dataset-dir``: the
    spec and every ledger entry carry the REAL dataset's version, and the
    checkout's own ledger and experiments folder are not touched."""
    out = tmp_path / "ds"
    for seed, date in ((7, D1), (8, D2)):
        src = _day(tmp_path, seed, f"{date}.itch", n_actions=6000, spacing_ns=10**9)
        assert _ingest_cli(src, date, out) == 0
    assert (
        features_main(
            [
                "--data-dir", str(out / "normalized"),
                "--out-dir", str(out / "features"),
                "--configs", str(out / "configs"),
                "--registry-out", str(out / "reference" / "feature_registry.json"),
            ]
        )
        == 0
    )  # fmt: skip
    repo_ledger = REPO_ROOT / "research" / "experiments.json"
    ledger_before = repo_ledger.read_bytes()
    experiments_before = sorted(p.name for p in (REPO_ROOT / "research" / "experiments").iterdir())
    capsys.readouterr()
    assert research_main(["run", "--no-prereg", "--alpha", "EQ03", "--dataset-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    version = load_manifest(out)["dataset_version"]
    assert f"dataset_version    {version}" in printed
    assert repo_ledger.read_bytes() == ledger_before
    assert sorted(p.name for p in (REPO_ROOT / "research" / "experiments").iterdir()) == (
        experiments_before
    )
    (spec_path,) = (out / "research" / "experiments").glob("*/spec.json")
    spec = json.loads(spec_path.read_text())
    summary = json.loads((out / "features" / "features_summary.json").read_text())
    assert spec["dataset_version"] == version != tracker.data_version()
    assert spec["feature_version"] == summary["registry_hash"]
    ledger = json.loads((out / "research" / "experiments.json").read_text())
    assert [d["dataset_version"] for d in ledger["datasets"]] == [version]
    assert research_main(["run", "--no-prereg", "--alpha", "EQ03", "--dataset-dir", str(tmp_path / "nope")]) == 1
    assert "needs dataset.json" in capsys.readouterr().err


def _bulk_file(path: Path, n_messages: int) -> int:
    """A large valid file built without the simulator: add / execute / delete
    cycles over one wanted symbol and three others."""
    enc = Itch50Encoder()
    symbols = ("AAPL", "MSFT", "AMZN", "GOOG")
    enc.system_event(hms_ns(3), "O")
    for i, s in enumerate(symbols):
        enc.stock_directory(i + 1, hms_ns(3, 0, 1), s)
    enc.system_event(hms_ns(9, 30), "Q")
    ts = hms_ns(9, 30)
    ref = 0
    while enc.messages < n_messages - 1:
        for i, s in enumerate(symbols):
            ref += 1
            ts += 1000
            side = "B" if ref % 2 else "S"
            price = 1_500_000 + (-100 if side == "B" else 100) * (1 + ref % 5)
            enc.add(i + 1, ts, ref, side, 200, s, price)
            enc.cancel(i + 1, ts + 1, ref, 100)
            enc.delete(i + 1, ts + 2, ref)
    enc.system_event(hms_ns(16), "M")
    enc.write(path)
    return enc.messages


def test_parser_throughput_smoke_and_bounded_memory(tmp_path):
    """Generous lower bounds only (CI runners vary); the measured rates are
    printed with ``-s`` / ``-rP`` and quoted in docs/REAL_DATA.md."""
    path = tmp_path / "bulk.itch"
    n = _bulk_file(path, 300_000)
    size = path.stat().st_size

    t0 = time.perf_counter()
    reader = Itch50Reader(path, symbols=("AAPL",))
    yielded = sum(1 for _ in reader)
    filtered_rate = n / (time.perf_counter() - t0)
    assert reader.messages_read == n and reader.filtered == n - yielded
    t0 = time.perf_counter()
    assert sum(1 for _ in Itch50Reader(path)) == n
    decode_rate = n / (time.perf_counter() - t0)
    t0 = time.perf_counter()
    mapper = Itch50Mapper(D1, ("AAPL",))
    events = sum(1 for _ in mapper.events(Itch50Reader(path, symbols=("AAPL",))))
    mapped_rate = n / (time.perf_counter() - t0)
    print(
        f"\nITCH 5.0 parser throughput over {n:,} messages ({size / 1e6:.1f} MB): "
        f"filtered to 1 of 4 symbols {filtered_rate:,.0f} msg/s; decode all {decode_rate:,.0f} "
        f"msg/s; filter + map to canonical events {mapped_rate:,.0f} msg/s ({events:,} events)"
    )
    assert filtered_rate > 20_000 and decode_rate > 10_000 and mapped_rate > 10_000
    assert mapper.live_orders == 0  # state is the live orders only

    # bounded memory: streaming the whole file keeps a small, size-independent peak
    tracemalloc.start()
    try:
        mapper = Itch50Mapper(D1, ("AAPL",))
        peak_orders = 0
        for i, _ in enumerate(
            mapper.events(Itch50Reader(path, symbols=("AAPL",), limit_messages=120_000))
        ):
            if i % 1000 == 0:
                peak_orders = max(peak_orders, mapper.live_orders)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"peak traced memory while streaming {size / 1e6:.1f} MB: {peak / 1e6:.2f} MB")
    assert peak < 6_000_000 < size
    assert peak_orders <= 1


@pytest.mark.skipif(
    not os.environ.get("IAP_REAL_ITCH50_FILE"),
    reason="set IAP_REAL_ITCH50_FILE (+ IAP_REAL_ITCH50_DATE, optional IAP_REAL_ITCH50_SYMBOLS, "
    "IAP_REAL_LIMIT_MESSAGES) to a TotalView-ITCH 5.0 file you obtained (docs/REAL_DATA.md)",
)
def test_real_itch50_sample_ingests_with_a_clean_book(tmp_path):
    src = Path(os.environ["IAP_REAL_ITCH50_FILE"])
    date = os.environ["IAP_REAL_ITCH50_DATE"]
    symbols = os.environ.get("IAP_REAL_ITCH50_SYMBOLS", "AAPL,MSFT").split(",")
    limit = os.environ.get("IAP_REAL_LIMIT_MESSAGES")
    manifest = ingest(
        "itch50", [src], date, symbols, tmp_path / "ds",
        configs_dir=CONFIGS_DIR, limit_messages=int(limit) if limit else None,
    )  # fmt: skip
    session = manifest["sessions"][date]
    print(json.dumps({k: session[k] for k in ("messages", "mapping", "book_check")}, indent=1))
    check = session["book_check"]
    assert check["dropped"] == 0 and check["crossing_adds"] == 0, check
    assert session["mapping"]["unknown_order_refs"] == 0
    assert session["mapping"]["duplicate_order_refs"] == 0
    totals = manifest["normalized"]["qc_totals"]
    assert totals["invalid"] == 0 and totals["gaps"] == 0 and totals["duplicates"] == 0
    assert totals["events_out"] == session["raw"]["events"] > 0


@pytest.mark.skipif(
    not os.environ.get("IAP_REAL_LOBSTER_MESSAGE_FILE"),
    reason="set IAP_REAL_LOBSTER_MESSAGE_FILE (+ IAP_REAL_LOBSTER_DATE; the orderbook file must "
    "sit beside it) to a LOBSTER sample you obtained (docs/REAL_DATA.md)",
)
def test_real_lobster_sample_reconstructs_the_vendor_book(tmp_path):
    msg = Path(os.environ["IAP_REAL_LOBSTER_MESSAGE_FILE"])
    date = os.environ["IAP_REAL_LOBSTER_DATE"]
    manifest = ingest("lobster", [msg], date, None, tmp_path / "ds", configs_dir=CONFIGS_DIR)
    session = manifest["sessions"][date]
    print(
        json.dumps({k: session[k] for k in ("messages", "mapping", "book_verification")}, indent=1)
    )
    (report,) = session["book_verification"].values()
    assert report["status"] == "match" and report["rows_verified"] == session["messages"]["total"]
    assert session["book_check"]["dropped"] == 0


def test_merging_single_day_datasets_equals_ingesting_them_together(tmp_path, capsys):
    """``python -m iap.marketdata merge``: two one-session datasets merge into
    the dataset that ingesting both sessions into one directory produces."""
    d1, d2 = _day(tmp_path, 7, "d1.itch"), _day(tmp_path, 8, "d2.itch")
    assert _ingest_cli(d1, D1, tmp_path / "a") == 0
    assert _ingest_cli(d2, D2, tmp_path / "b") == 0
    assert _ingest_cli(d1, D1, tmp_path / "seq") == 0
    assert _ingest_cli(d2, D2, tmp_path / "seq") == 0
    merged = tmp_path / "merged"
    args = ["merge", str(tmp_path / "b"), str(tmp_path / "a"), "--out", str(merged)]
    assert marketdata_main(args) == 0
    capsys.readouterr()
    skip = {"dataset.json", "normalized/qc_report.json", "normalized/events.parquet"}
    seq = {k: v for k, v in _tree(tmp_path / "seq").items() if k not in skip}
    got = {k: v for k, v in _tree(merged).items() if k not in skip}
    assert got == seq
    m, s = load_manifest(merged), load_manifest(tmp_path / "seq")
    assert m["dataset_version"] == s["dataset_version"]
    assert m["sessions"] == s["sessions"]
    assert ReferenceData.load(merged / "configs").trading_days == [D1, D2]
    # refusals: overlapping sessions, an occupied output
    assert marketdata_main(["merge", str(tmp_path / "a"), str(tmp_path / "seq"), "--out",
                            str(tmp_path / "x")]) == 1  # fmt: skip
    assert marketdata_main(args) == 1
    assert "session 2019-12-30 is in more than one input" in capsys.readouterr().err


def test_batch_and_real_power_run_on_an_ingested_dataset(tmp_path, capsys):
    """v1.6.0: the 24-alpha batch (run_all.py --dataset-dir) and the real-data
    power study run end to end on an ingested dataset, with the ETF read from
    the dataset (QQQ is id 3 here, not the synthetic 11)."""
    import importlib.util

    from iap.alpha import configure_universe, universe_ids
    from iap.research import power_real

    out = tmp_path / "ds"
    for seed, date in ((7, D1), (8, D2)):
        src = _day(tmp_path, seed, f"{date}.itch", n_actions=6000, spacing_ns=10**9)
        assert _ingest_cli(src, date, out) == 0
    assert (
        features_main(
            [
                "--data-dir", str(out / "normalized"),
                "--out-dir", str(out / "features"),
                "--configs", str(out / "configs"),
                "--registry-out", str(out / "reference" / "feature_registry.json"),
            ]
        )
        == 0
    )  # fmt: skip
    spec = importlib.util.spec_from_file_location(
        "run_all", REPO_ROOT / "research" / "alpha_reports" / "run_all.py"
    )
    run_all = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_all)
    repo_ledger = REPO_ROOT / "research" / "experiments.json"
    before = repo_ledger.read_bytes()
    reports = tmp_path / "reports"
    try:
        assert run_all.main(["--dataset-dir", str(out), "--out-dir", str(reports)]) == 0
        assert universe_ids("etf") == (3,) and universe_ids("constituents") == (1, 2)
    finally:
        configure_universe(None)
    assert repo_ledger.read_bytes() == before
    assert (reports / "REPORT.md").is_file() and (reports / "EQ10.json").is_file()
    ledger = json.loads((reports / "experiments.json").read_text())
    assert [d["dataset_version"] for d in ledger["datasets"]] == [
        load_manifest(out)["dataset_version"]
    ]
    with pytest.raises(SystemExit, match="--out-dir"):
        run_all.main(["--dataset-dir", str(out)])
    try:
        doc = power_real.run_real_power_study(
            out, levels=(0.0, 0.2), break_levels=(0.2,), n_seeds=2, sessions=[2], gate_looks=100
        )
    finally:
        configure_universe(None)
    capsys.readouterr()
    assert doc["dataset_version"] == load_manifest(out)["dataset_version"]
    scenarios = {c["scenario"] for c in doc["cells"]}
    assert scenarios == {"shifted:stable", "real:stable", "shifted:break"}
    assert all(c["n_runs"] == (1 if c["scenario"].startswith("real") else 2) for c in doc["cells"])
    assert "Planted-signal power on real data" in power_real.render_markdown(doc)

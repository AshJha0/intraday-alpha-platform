"""DBN MBO (Databento XNAS.ITCH) reader, v1.15: synthetic DBN files built
in-test with the official ``databento-dbn`` codec, and the cross-check that
an equivalent ITCH 5.0 and DBN event sequence normalise identically.
"""

from __future__ import annotations

import datetime as dt
import json
import types

import pytest
from conftest import CONFIGS_DIR
from iap.core.events import EventType
from iap.marketdata.dbn_mbo import (
    DbnMboReader,
    instrument_ids,
    price_e4,
    session_boundaries,
)
from iap.marketdata.feederrors import IngestError
from iap.marketdata.ingest import ingest
from iap.marketdata.itch50 import (
    CrossTrade,
    Itch50Mapper,
    Itch50Reader,
    OrderDelete,
    OrderReplace,
    session_midnight_ns,
)
from itch50_encoder import Itch50Encoder, hms_ns

dbn = pytest.importorskip("databento_dbn")

DATE = "2025-01-15"
MID = session_midnight_ns(DATE)
OPEN = hms_ns(9, 30)
CLOSE = hms_ns(16)
AAPL, QQQ, MSFT = 7, 9, 11


class DbnWriter:
    """A tiny DBN MBO file image: metadata + records, encoded by the codec."""

    def __init__(self, date=DATE, dataset="XNAS.ITCH", mapping=None):
        day = dt.date.fromisoformat(date)
        mapping = mapping if mapping is not None else {"AAPL": AAPL, "QQQ": QQQ, "MSFT": MSFT}
        mappings = [
            types.SimpleNamespace(
                raw_symbol=sym,
                intervals=[
                    types.SimpleNamespace(
                        start_date=day, end_date=day + dt.timedelta(days=1), symbol=str(iid)
                    )
                ],
            )
            for sym, iid in mapping.items()
        ]
        self.metadata = dbn.Metadata(
            dataset=dataset,
            start=MID,
            end=MID + 86_400 * 10**9,
            stype_in=dbn.SType.RAW_SYMBOL,
            stype_out=dbn.SType.INSTRUMENT_ID,
            schema=dbn.Schema.MBO,
            symbols=list(mapping),
            mappings=mappings,
        )
        self.records: list[bytes] = []
        self.seq = 0

    def mbo(self, iid, ts, action, side, oid=0, size=0, price_e4=0, *, price_e9=None):
        self.seq += 1
        msg = dbn.MBOMsg(
            publisher_id=2,
            instrument_id=iid,
            ts_event=MID + ts,
            order_id=oid,
            price=price_e9 if price_e9 is not None else price_e4 * 100_000,
            size=size,
            action=getattr(dbn.Action, action),
            side=getattr(dbn.Side, side),
            ts_recv=MID + ts + 1,
            flags=0,
            sequence=self.seq,
        )
        self.records.append(bytes(msg))

    def write(self, path):
        path.write_bytes(self.metadata.encode() + b"".join(self.records))
        return path


def _itch_day() -> Itch50Encoder:
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.stock_directory(2, hms_ns(3, 0, 2), "QQQ", etp="Y")
    enc.system_event(hms_ns(4), "S")
    return enc


def _equivalent_pair(tmp_path):
    """The same session as ITCH 5.0 and as DBN MBO (no match numbers: DBN has none)."""
    enc, w = _itch_day(), DbnWriter()
    t = OPEN + 1_000
    # pre-open book building (AUCTION)
    enc.add(1, hms_ns(8), 11, "B", 300, "AAPL", 1_500_000)
    w.mbo(AAPL, hms_ns(8), "ADD", "BID", 11, 300, 1_500_000)
    w.mbo(MSFT, hms_ns(8, 0, 1), "ADD", "BID", 99, 100, 4_000_000)  # outside the universe
    enc.add(2, hms_ns(8, 1), 21, "S", 500, "QQQ", 5_000_000)
    w.mbo(QQQ, hms_ns(8, 1), "ADD", "ASK", 21, 500, 5_000_000)
    enc.system_event(OPEN, "Q")
    enc.add(1, t, 12, "S", 200, "AAPL", 1_500_100)
    w.mbo(AAPL, t, "ADD", "ASK", 12, 200, 1_500_100)
    # a displayed execution: ITCH E == DBN trade + fill
    enc.executed(1, t + 1, 11, 100, 0)
    w.mbo(AAPL, t + 1, "TRADE", "ASK", 0, 100, 1_500_000)
    w.mbo(AAPL, t + 1, "FILL", "BID", 11, 100, 1_500_000)
    # partial cancel, full cancel (ITCH X, D == DBN C)
    enc.cancel(1, t + 2, 11, 50)
    w.mbo(AAPL, t + 2, "CANCEL", "BID", 11, 50, 1_500_000)
    enc.delete(1, t + 3, 12)
    w.mbo(AAPL, t + 3, "CANCEL", "ASK", 12, 200, 1_500_100)
    # a non-displayed print: ITCH P (resting side B) == DBN unpaired T (aggressor A)
    enc.trade(1, t + 4, "B", 10, "AAPL", 1_500_050, 0)
    w.mbo(AAPL, t + 4, "TRADE", "ASK", 0, 10, 1_500_050)
    # execution at a price other than the order's: ITCH C == DBN T + F
    enc.executed_with_price(1, t + 5, 11, 50, 0, 1_500_100)
    w.mbo(AAPL, t + 5, "TRADE", "ASK", 0, 50, 1_500_100)
    w.mbo(AAPL, t + 5, "FILL", "BID", 11, 50, 1_500_100)
    enc.executed(2, t + 6, 21, 500, 0)
    w.mbo(QQQ, t + 6, "TRADE", "BID", 0, 500, 5_000_000)
    w.mbo(QQQ, t + 6, "FILL", "ASK", 21, 500, 5_000_000)
    enc.add(1, t + 7, 13, "B", 100, "AAPL", 1_499_900)
    w.mbo(AAPL, t + 7, "ADD", "BID", 13, 100, 1_499_900)
    # after the close: dropped on both paths
    enc.system_event(CLOSE, "M")
    enc.add(1, CLOSE + 1, 14, "B", 100, "AAPL", 1_499_800)
    w.mbo(AAPL, CLOSE + 1, "ADD", "BID", 14, 100, 1_499_800)
    return enc.write(tmp_path / "day.itch"), w.write(tmp_path / "day.dbn")


def _map_itch(path, symbols=("AAPL", "QQQ")):
    mapper = Itch50Mapper(DATE, symbols)
    return list(mapper.events(Itch50Reader(path, symbols=symbols))), mapper


def _map_dbn(path, symbols=("AAPL", "QQQ"), **kw):
    mapper = Itch50Mapper(DATE, symbols)
    reader = DbnMboReader(path, DATE, symbols, mapper=mapper, **kw)
    return list(mapper.events(reader)), mapper, reader


# ----------------------------------------------------------- cross-check


def test_equivalent_itch_and_dbn_sequences_map_to_identical_events(tmp_path):
    itch, dbn_file = _equivalent_pair(tmp_path)
    p_itch, m_itch = _map_itch(itch)
    p_dbn, m_dbn, reader = _map_dbn(dbn_file)
    assert p_dbn == p_itch
    assert m_dbn.counters == m_itch.counters
    assert m_dbn.counters["outside_session_dropped"] == 1
    assert reader.filtered == 1
    assert reader.counters["trades_paired_with_fill"] == 3
    assert reader.counters["hidden_trades"] == 1
    assert m_dbn.live_orders == m_itch.live_orders == 2


def test_equivalent_sequences_ingest_to_identical_raw_and_normalized_data(tmp_path):
    itch, dbn_file = _equivalent_pair(tmp_path)
    syms = ["AAPL", "QQQ"]
    a = ingest("itch50", [itch], DATE, syms, tmp_path / "a", configs_dir=CONFIGS_DIR)
    b = ingest("dbn-mbo", [dbn_file], DATE, syms, tmp_path / "b", configs_dir=CONFIGS_DIR)
    stem = "raw/eq_20250115.jsonl"
    assert (tmp_path / "a" / stem).read_bytes() == (tmp_path / "b" / stem).read_bytes()
    assert a["universe"] == b["universe"]
    assert b["universe"][1]["asset_class"] == "ETF"
    sa, sb = a["sessions"][DATE], b["sessions"][DATE]
    assert sa["per_symbol"] == sb["per_symbol"]
    assert sa["raw"]["sha256"] == sb["raw"]["sha256"]
    assert sa["book_check"] == sb["book_check"]
    assert sb["format"] == "dbn-mbo" and b["source"]["format"] == "dbn-mbo"
    assert sb["messages"]["dbn"]["dataset"] == "XNAS.ITCH"
    assert a["normalized"]["qc_totals"] == b["normalized"]["qc_totals"]
    na = sorted(p.name for p in (tmp_path / "a" / "normalized").rglob("*") if p.is_file())
    nb = sorted(p.name for p in (tmp_path / "b" / "normalized").rglob("*") if p.is_file())
    assert na == nb


def test_defer_normalize_and_cli(tmp_path, capsys):
    from iap.marketdata.__main__ import main

    _, dbn_file = _equivalent_pair(tmp_path)
    out = tmp_path / "ds"
    rc = main(
        ["ingest", "--format", "dbn-mbo", "--input", str(dbn_file), "--date", DATE,
         "--symbols", "AAPL,QQQ", "--out", str(out), "--configs-dir", str(CONFIGS_DIR),
         "--quiet", "--defer-normalize"]
    )  # fmt: skip
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["qc_totals"] is None and summary["symbols"] == ["AAPL", "QQQ"]
    manifest = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    (inp,) = manifest["sessions"][DATE]["inputs"]
    assert inp["file"] == "day.dbn" and len(inp["sha256"]) == 64


# ------------------------------------------------------------ translation


def test_price_conversion_rounds_half_to_even():
    assert price_e4(150_000_000_000) == (1_500_000, False)
    assert price_e4(150_000_050_000) == (1_500_000, True)  # half: even stays
    assert price_e4(150_000_150_000) == (1_500_002, True)  # half: odd rounds up
    assert price_e4(150_000_060_000) == (1_500_001, True)
    assert price_e4((1 << 63) - 1) == (0, False)
    assert price_e4(-5) == (0, False)


def test_session_boundaries_follow_the_early_close_calendar():
    assert session_boundaries("2025-01-15")[2] == (hms_ns(16), "M")
    assert session_boundaries("2025-11-28")[2] == (hms_ns(13), "M")
    assert session_boundaries("2025-11-28")[3] == (hms_ns(17), "E")


def test_instrument_ids_use_the_mapping_valid_on_the_session_date():
    day = dt.date.fromisoformat(DATE)
    md = types.SimpleNamespace(
        mappings={
            "AAPL": [
                {"start_date": day - dt.timedelta(days=5), "end_date": day, "symbol": "1"},
                {"start_date": day, "end_date": day + dt.timedelta(days=1), "symbol": "2"},
            ],
            "ZZZ": [{"start_date": day, "end_date": day, "symbol": "x"}],
        }
    )
    assert instrument_ids(md, ["AAPL", "ZZZ", "NOPE"], DATE) == {2: "AAPL"}


def test_modify_clear_unpaired_fill_and_cross(tmp_path):
    w = DbnWriter()
    t = OPEN + 10
    w.mbo(AAPL, OPEN - 5, "ADD", "BID", 1, 100, 1_500_000)
    w.mbo(AAPL, t, "ADD", "ASK", 2, 100, 1_500_200)
    w.mbo(AAPL, t + 1, "MODIFY", "BID", 1, 80, 1_500_100)
    w.mbo(AAPL, t + 2, "FILL", "ASK", 2, 40, 1_500_200)  # no trade before it
    w.mbo(AAPL, t + 3, "TRADE", "NONE", 0, 1_000, 1_500_150)  # auction print
    w.mbo(AAPL, t + 4, "NONE", "NONE")
    w.mbo(AAPL, t + 5, "CLEAR", "NONE")
    w.mbo(AAPL, t + 6, "ADD", "BID", 3, 100, 1_500_000, price_e9=150_000_050_000)
    path = w.write(tmp_path / "x.dbn")
    protos, mapper, reader = _map_dbn(path, ("AAPL",))
    assert reader.counts["M"] == 1 and reader.counts["R"] == 1 and reader.counts["N"] == 1
    assert reader.counters["fills_unpaired"] == 1
    assert reader.counters["cross_trades"] == 1
    assert reader.counters["clear_deletes"] == 2
    assert reader.counters["prices_rounded_to_e4"] == 1
    cross = mapper.crosses["AAPL"]
    assert [(c["cross_type"], c["shares"], c["price_e4"]) for c in cross] == [
        ("O", 1_000, 1_500_150)
    ]
    assert mapper.live_orders == 1 and mapper._orders[3][2] == 1_500_000
    kinds = [p[2] for p in protos]
    assert kinds.count(int(EventType.EXECUTE)) == 1


def test_translation_messages_are_itch_tuples(tmp_path):
    w = DbnWriter()
    w.mbo(AAPL, OPEN + 1, "ADD", "BID", 1, 100, 1_500_000)
    w.mbo(AAPL, OPEN + 2, "MODIFY", "BID", 1, 50, 1_500_000)
    w.mbo(AAPL, OPEN + 3, "TRADE", "NONE", 0, 10, 1_500_000)
    w.mbo(AAPL, CLOSE + 5, "TRADE", "NONE", 0, 10, 1_500_000)
    path = w.write(tmp_path / "x.dbn")
    msgs = list(DbnMboReader(path, DATE, ["AAPL"]))
    assert any(type(m) is OrderReplace and m.orig_ref == m.new_ref == 1 for m in msgs)
    crosses = [m for m in msgs if type(m) is CrossTrade]
    assert [c.cross_type for c in crosses] == ["O", "C"]
    assert not any(type(m) is OrderDelete for m in msgs)


def test_records_outside_the_new_york_day_are_dropped_and_limit_is_honoured(tmp_path):
    w = DbnWriter()
    w.mbo(AAPL, -1_000, "ADD", "BID", 1, 100, 1_500_000)  # previous evening
    for i in range(5):
        w.mbo(AAPL, OPEN + i, "ADD", "BID", 10 + i, 100, 1_500_000)
    path = w.write(tmp_path / "x.dbn")
    _, mapper, reader = _map_dbn(path, ("AAPL",), limit_messages=3)
    assert reader.counters["outside_session_date"] == 1
    assert reader.limit_reached and reader.messages_read == 3
    assert mapper.live_orders == 2


def test_refusals(tmp_path):
    other = DbnWriter(dataset="GLBX.MDP3").write(tmp_path / "glbx.dbn")
    with pytest.raises(IngestError, match="dataset"):
        list(DbnMboReader(other, DATE, ["AAPL"]))
    w = DbnWriter(mapping={"MSFT": MSFT})
    w.mbo(MSFT, OPEN, "ADD", "BID", 1, 100, 4_000_000)
    with pytest.raises(IngestError, match="no symbology mapping"):
        ingest("dbn-mbo", [w.write(tmp_path / "m.dbn")], DATE, ["AAPL"], tmp_path / "o",
               configs_dir=CONFIGS_DIR)  # fmt: skip
    _, d = _equivalent_pair(tmp_path)
    with pytest.raises(IngestError, match="exactly one"):
        ingest("dbn-mbo", [d, d], DATE, ["AAPL"], tmp_path / "p", configs_dir=CONFIGS_DIR)
    with pytest.raises(IngestError, match="orderbook"):
        ingest("dbn-mbo", [d], DATE, ["AAPL"], tmp_path / "q", configs_dir=CONFIGS_DIR,
               orderbooks=[d])  # fmt: skip


def test_zstd_compressed_file(tmp_path):
    zstd = pytest.importorskip("zstandard")
    itch, dbn_file = _equivalent_pair(tmp_path)
    zst = tmp_path / "day.dbn.zst"
    zst.write_bytes(zstd.ZstdCompressor().compress(dbn_file.read_bytes()))
    assert _map_dbn(zst)[0] == _map_itch(itch)[0]

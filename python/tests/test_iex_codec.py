"""IEX HIST codec (v1.17, A1): pcap / IEX-TP framing, the three message
protocols, the DEEP+ and DEEP translations into the ITCH message model, the
TOPS oracle and the ingest wiring.  Synthetic bytes only (``iex_encoder``)."""

from __future__ import annotations

import io
import json

import pytest
from conftest import CONFIGS_DIR
from iap.core.events import EventType
from iap.marketdata.feederrors import BookDivergenceError, IngestError
from iap.marketdata.iex import (
    PROTOCOL_DEEP,
    PROTOCOL_DEEP_PLUS,
    PROTOCOL_TOPS,
    VENUE_ID,
    IexAddOrder,
    IexPriceLevel,
    IexQuote,
    IexReader,
    TopsQuotes,
    UnknownMessage,
    decode_message,
    pcap_packets,
    tp_segment,
)
from iap.marketdata.ingest import ingest
from iap.marketdata.itch50 import Itch50Mapper, Itch50Reader, session_midnight_ns
from itch50_encoder import Itch50Encoder, hms_ns

import iex_encoder as ix

DATE = "2025-01-15"
MID = session_midnight_ns(DATE)
OPEN = hms_ns(9, 30)
CLOSE = hms_ns(16)
NS = 1_000_000_000


def _ep(ts_since_midnight: int) -> int:
    return MID + ts_since_midnight


# -------------------------------------------------------------- framing


def test_pcap_and_tp_framing_microsecond_nanosecond_vlan_and_heartbeats(tmp_path):
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS)
    w.segment([ix.system_event("O", _ep(hms_ns(3)))], send_time=_ep(hms_ns(3)))
    w.heartbeat(_ep(hms_ns(3, 0, 1)))
    w.segment([ix.system_event("S", _ep(hms_ns(4)))], send_time=_ep(hms_ns(4)), vlan=True)
    data = w.bytes()
    packets = list(pcap_packets(io.BytesIO(data)))
    assert len(packets) == 3 and packets[1][0] == (_ep(hms_ns(3, 0, 1)) // 1000) * 1000
    segs = [tp_segment(p) for _, p in packets]
    assert [len(s.messages) for s in segs] == [1, 0, 1]
    assert segs[0].protocol == PROTOCOL_DEEP_PLUS and segs[0].first_sequence == 1
    assert segs[2].first_sequence == 2 and segs[2].session == 7
    # nanosecond pcap keeps the full capture stamp
    n = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS, nanosecond=True)
    n.segment([ix.system_event("O", _ep(hms_ns(3)) + 123)], send_time=_ep(hms_ns(3)) + 123)
    assert list(pcap_packets(io.BytesIO(n.bytes())))[0][0] == _ep(hms_ns(3)) + 123
    # gzip by content, pcapng refused, garbage refused
    gz = w.write(tmp_path / "day.pcap.gz", compress=True)
    r = IexReader(gz, DATE, ["AAPL"])
    assert [type(m).__name__ for m in r] == ["SystemEvent", "SystemEvent"]
    assert r.counters["segments"] == 3 and r.counters["heartbeats"] == 1 and r.pcap.vlan == 1
    with pytest.raises(IngestError, match="pcapng"):
        list(pcap_packets(io.BytesIO(b"\x0a\x0d\x0d\x0a" + b"\x00" * 40)))
    with pytest.raises(IngestError, match="not a pcap"):
        list(pcap_packets(io.BytesIO(b"\x00" * 40)))


def test_spec_examples_decode_byte_for_byte():
    # DEEP+ Add Order example (spec, page 21): ZIEXT, buy, id 429974, 100 shares, $99.05
    add = bytes.fromhex(
        "61 38 b2 8f a5 a0 ab 86 6d 14 5a 49 45 58 54 20 20 20 96 8f 06 00 00 00 00 00 "
        "64 00 00 00 24 1d 0f 00 00 00 00 00"
    )
    m = decode_message(PROTOCOL_DEEP_PLUS, add)
    assert m == IexAddOrder(0, 1471980724912754610, "ZIEXT", 429974, 100, 990500)
    # the same bytes are not an order message on the DEEP (price level) protocol
    assert type(decode_message(PROTOCOL_DEEP, add)) is UnknownMessage
    # DEEP Price Level Update example fields: size e4 25 00 00 (9,700), price $99.05
    plu = ix.price_level("B", "ZIEXT", 1471980724912754610, 9700, 990500)
    assert plu[18:22] == bytes.fromhex("e4 25 00 00") and plu[22:30] == bytes.fromhex(
        "24 1d 0f 00 00 00 00 00"
    )
    assert decode_message(PROTOCOL_DEEP, plu) == IexPriceLevel(
        0, 1, 1471980724912754610, "ZIEXT", 9700, 990500
    )
    q = ix.quote("ZIEXT", 5, 100, 990000, 991000, 200)
    assert decode_message(PROTOCOL_TOPS, q) == IexQuote(0, 5, "ZIEXT", 100, 990000, 991000, 200)
    assert type(decode_message(PROTOCOL_DEEP, q)) is UnknownMessage
    # a short block and an unknown type are counted, never raised
    assert decode_message(PROTOCOL_DEEP_PLUS, add[:20]) == UnknownMessage("a", 20)
    assert decode_message(PROTOCOL_DEEP_PLUS, b"Z" + b"\x00" * 30).type_code == "Z"


# ------------------------------------------------------- DEEP+ vs ITCH


def _itch_day() -> Itch50Encoder:
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.stock_directory(2, hms_ns(3, 0, 2), "QQQ", etp="Y")
    enc.system_event(hms_ns(4), "S")
    return enc


def _equivalent_pair(tmp_path):
    """The same session as ITCH 5.0 and as IEX DEEP+ (match numbers = IEX trade ids)."""
    enc = _itch_day()
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS)
    msgs: list[bytes] = [
        ix.system_event("O", _ep(hms_ns(3))),
        ix.directory("AAPL", _ep(hms_ns(3, 0, 1))),
        ix.directory("QQQ", _ep(hms_ns(3, 0, 2)), flags=0x20),
        ix.directory("ZTST", _ep(hms_ns(3, 0, 3)), flags=0x80),  # a test security, skipped
        ix.system_event("S", _ep(hms_ns(4))),
    ]
    t = OPEN + 1_000
    enc.add(1, hms_ns(8), 11, "B", 300, "AAPL", 1_500_000)
    msgs.append(ix.add_order("AAPL", _ep(hms_ns(8)), "B", 11, 300, 1_500_000))
    msgs.append(ix.add_order("MSFT", _ep(hms_ns(8, 0, 1)), "B", 99, 100, 4_000_000))  # outside
    enc.add(2, hms_ns(8, 1), 21, "S", 500, "QQQ", 5_000_000)
    msgs.append(ix.add_order("QQQ", _ep(hms_ns(8, 1)), "S", 21, 500, 5_000_000))
    enc.system_event(OPEN, "Q")
    msgs.append(ix.system_event("R", _ep(OPEN)))
    enc.add(1, t, 12, "S", 200, "AAPL", 1_500_100)
    msgs.append(ix.add_order("AAPL", _ep(t), "S", 12, 200, 1_500_100))
    # a displayed execution: ITCH E == DEEP+ L at the resting price
    enc.executed(1, t + 1, 11, 100, 501)
    msgs.append(ix.executed("AAPL", _ep(t + 1), 11, 100, 1_500_000, 501))
    # size-down in place (priority kept): ITCH X == DEEP+ M maintain-priority
    enc.cancel(1, t + 2, 11, 50)
    msgs.append(ix.modify_order("AAPL", _ep(t + 2), 11, 150, 1_500_000, maintain_priority=True))
    enc.delete(1, t + 3, 12)
    msgs.append(ix.delete_order("AAPL", _ep(t + 3), 12))
    # a non-displayed print: ITCH P (resting side B) == DEEP+ T
    enc.trade(1, t + 4, "B", 10, "AAPL", 1_500_050, 502)
    msgs.append(ix.trade("AAPL", _ep(t + 4), 10, 1_500_050, 502))
    # execution at another price: ITCH C printable == DEEP+ L with another price
    enc.executed_with_price(1, t + 5, 11, 50, 503, 1_500_100)
    msgs.append(ix.executed("AAPL", _ep(t + 5), 11, 50, 1_500_100, 503))
    enc.executed(2, t + 6, 21, 500, 504)
    msgs.append(ix.executed("QQQ", _ep(t + 6), 21, 500, 5_000_000, 504))
    # a price change: ITCH U (same id) == DEEP+ M with a new price
    enc.replace(1, t + 7, 11, 11, 100, 1_499_900)
    msgs.append(ix.modify_order("AAPL", _ep(t + 7), 11, 100, 1_499_900))
    # after the close: dropped on both paths
    enc.system_event(CLOSE, "M")
    msgs.append(ix.system_event("M", _ep(CLOSE)))
    enc.add(1, CLOSE + 1, 14, "B", 100, "AAPL", 1_499_800)
    msgs.append(ix.add_order("AAPL", _ep(CLOSE + 1), "B", 14, 100, 1_499_800))
    for i in range(0, len(msgs), 3):
        w.segment(msgs[i : i + 3], send_time=_ep(hms_ns(3)) + i)
    return enc.write(tmp_path / "day.itch"), w.write(tmp_path / "day.pcap")


def _map_itch(path, symbols=("AAPL", "QQQ")):
    mapper = Itch50Mapper(DATE, symbols)
    return list(mapper.events(Itch50Reader(path, symbols=symbols))), mapper


def _map_iex(path, symbols=("AAPL", "QQQ"), **kw):
    mapper = Itch50Mapper(DATE, symbols)
    reader = IexReader(path, DATE, symbols, mapper=mapper, **kw)
    return list(mapper.events(reader)), mapper, reader


def test_equivalent_itch_and_deep_plus_sequences_map_to_identical_events(tmp_path):
    itch, pcap = _equivalent_pair(tmp_path)
    p_itch, m_itch = _map_itch(itch)
    p_iex, m_iex, reader = _map_iex(pcap)
    assert p_iex == p_itch and len(p_iex) > 10
    assert m_iex.counters == m_itch.counters
    assert reader.protocol == PROTOCOL_DEEP_PLUS and reader.level_feed is False
    assert reader.filtered == 2 and reader.counters["test_securities_skipped"] == 0  # MSFT, ZTST: outside the universe
    assert reader.counters["modify_in_place"] == 1 and reader.counters["modify_replace"] == 1
    assert reader.counters["executed_at_other_price"] == 1
    assert reader.counters["hidden_trades"] == 1 and reader.counters["outside_session_date"] == 0
    assert {
        EventType.ADD,
        EventType.EXECUTE,
        EventType.TRADE,
        EventType.CANCEL,
        EventType.MODIFY,
    } <= {e[2] for e in p_iex}
    assert m_iex.directory["QQQ"].etp_flag == "Y"


def test_deep_plus_clear_book_halts_breaks_and_crosses(tmp_path):
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS)
    t = OPEN + 5 * NS
    w.segment(
        [
            ix.system_event("O", _ep(hms_ns(3))),
            ix.system_event("S", _ep(hms_ns(4))),
            ix.system_event("R", _ep(OPEN)),
            ix.add_order("AAPL", _ep(t), "B", 1, 100, 1_000_000),  # no D: synthesised
            ix.add_order("AAPL", _ep(t), "S", 2, 100, 1_000_100),
            ix.trading_status("AAPL", _ep(t + 1), "H", "T1  "),
            ix.operational_halt("AAPL", _ep(t + 2), "O"),
            ix.trading_status("AAPL", _ep(t + 3), "T"),
            ix.operational_halt("AAPL", _ep(t + 4), "N"),
            ix.trade("AAPL", _ep(t + 5), 300, 1_000_050, 9, flags=0x08),  # single-price cross
            ix.trade("AAPL", _ep(t + 6), 7, 1_000_050, 10, flags=0x60),  # extended hours, odd lot
            ix.trade_break("AAPL", _ep(t + 7), 7, 1_000_050, 10),
            ix.official_price("AAPL", _ep(t + 8), "Q", 1_000_000),
            ix.security_event("AAPL", _ep(t + 8), "O"),
            ix.clear_book("AAPL", _ep(t + 9)),
            ix.add_order("AAPL", _ep(MID + 2 * 86_400 * NS), "B", 3, 1, 1),  # another day
        ],
        send_time=_ep(t),
    )
    events, mapper, reader = _map_iex(w.write(tmp_path / "x.pcap"), symbols=("AAPL",))
    c = reader.counters
    assert c["directory_synthesised"] == 1 and mapper.directory["AAPL"].round_lot_size == 100
    assert c["cross_trades"] == 1 and mapper.counters["cross_trades"] == 1
    assert mapper.crosses["AAPL"][0]["cross_type"] == "O"  # within a minute of the open
    assert c["extended_hours_trades"] == 1 and c["odd_lot_trades"] == 1
    assert mapper.broken == [10]
    assert c["clears"] == 1 and c["clear_deletes"] == 2 and mapper.live_orders == 0
    assert c["outside_session_date"] == 1
    assert reader.counts["X"] == 1 and reader.counts["E"] == 1
    statuses = [e[5] for e in events if e[2] == EventType.STATUS]
    assert 2 in statuses and statuses[-1] == 1  # HALT seen, TRADING restored


# ------------------------------------------------------------ DEEP levels


def test_deep_level_feed_models_levels_as_synthetic_orders_and_pairs_trades(tmp_path):
    w = ix.IexPcapWriter(PROTOCOL_DEEP)
    t = OPEN + 5 * NS
    w.segment(
        [
            ix.system_event("O", _ep(hms_ns(3))),
            ix.directory("AAPL", _ep(hms_ns(3, 0, 1))),
            ix.system_event("S", _ep(hms_ns(4))),
            ix.system_event("R", _ep(OPEN)),
            ix.price_level("B", "AAPL", _ep(t), 300, 1_000_000),  # level add
            ix.price_level("S", "AAPL", _ep(t), 200, 1_000_100),
            ix.price_level("B", "AAPL", _ep(t + 1), 500, 1_000_000),  # +200 joins the back
            # an atomic event: a trade report, then the level decrement that it explains
            ix.trade("AAPL", _ep(t + 2), 100, 1_000_000, 77),
            ix.price_level("B", "AAPL", _ep(t + 2), 400, 1_000_000, complete=False),
            ix.price_level("S", "AAPL", _ep(t + 2), 200, 1_000_100, complete=True),
            # a decrement with no trade: a cancel from the back
            ix.price_level("B", "AAPL", _ep(t + 3), 150, 1_000_000),
            # a trade report nothing accounts for: a non-displayed print
            ix.trade("AAPL", _ep(t + 4), 30, 1_000_050, 78),
            ix.price_level("S", "AAPL", _ep(t + 4), 200, 1_000_100),
            # level removed
            ix.price_level("B", "AAPL", _ep(t + 5), 0, 1_000_000),
        ],
        send_time=_ep(t),
    )
    events, mapper, reader = _map_iex(w.write(tmp_path / "deep.pcap"), symbols=("AAPL",))
    assert reader.level_feed is True and reader.protocol == PROTOCOL_DEEP
    c = reader.counters
    assert c["level_adds"] == 2 and c["level_increments"] == 1
    assert c["level_executed_qty"] == 100 and c["trades_matched_to_level"] == 1
    assert c["level_cancelled_qty"] == 250 + 150 and c["level_deletes"] == 1
    assert c["trades_unmatched_to_level"] == 1 and c["hidden_trades"] == 1
    kinds = [(e[1] - MID, e[2], e[5]) for e in events if e[2] != EventType.STATUS]
    ex = [k for k in kinds if k[1] == EventType.EXECUTE]
    assert ex == [(t + 2, EventType.EXECUTE, 100)]  # the FRONT order executed
    trades = [e for e in events if e[2] == EventType.TRADE]
    assert [(e[7], e[5]) for e in trades] == [(77, 100), (78, 30)]
    assert mapper.live_orders == 1 and mapper.counters["unknown_order_refs"] == 0
    # the book the mapper rebuilt from synthetic orders is the level feed's book
    assert mapper._levels[0].totals[0] == {} and mapper._levels[0].totals[1] == {1_000_100: 200}


# ------------------------------------------------------------ TOPS oracle


def _tops(tmp_path, quotes, name="tops.pcap"):
    w = ix.IexPcapWriter(PROTOCOL_TOPS)
    w.segment(
        [ix.quote("AAPL", _ep(ts), bs, b, a, asz) for ts, bs, b, a, asz in quotes],
        send_time=_ep(OPEN),
    )
    return w.write(tmp_path / name)


def _deep_plus_session(tmp_path, name="dp.pcap"):
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS)
    t = OPEN + 5 * NS
    w.segment(
        [
            ix.system_event("O", _ep(hms_ns(3))),
            ix.directory("AAPL", _ep(hms_ns(3, 0, 1))),
            ix.system_event("S", _ep(hms_ns(4))),
            ix.system_event("R", _ep(OPEN)),
            ix.add_order("AAPL", _ep(t), "B", 1, 100, 1_000_000),
            ix.add_order("AAPL", _ep(t), "S", 2, 200, 1_000_100),
            ix.add_order("AAPL", _ep(t + 1), "B", 3, 50, 1_000_000),  # bid size 150
            ix.executed("AAPL", _ep(t + 2), 2, 200, 1_000_100, 5),  # ask gone
        ],
        send_time=_ep(t),
    )
    return w.write(tmp_path / name), t


def test_tops_oracle_matches_the_mapped_book_and_reports_divergence(tmp_path):
    dp, t = _deep_plus_session(tmp_path)
    good = _tops(
        tmp_path,
        [
            (t, 100, 1_000_000, 1_000_100, 200),
            (t + 1, 150, 1_000_000, 1_000_100, 200),
            (t + 2, 150, 1_000_000, 0, 0),
        ],
    )
    quotes = TopsQuotes.load(good, ["AAPL"], MID)
    assert quotes.quotes == 3
    _, _, reader = _map_iex(dp, symbols=("AAPL",), tops=quotes)
    rep = reader.oracle.report()["AAPL"]
    assert rep["status"] == "match" and rep["compared"] == 3 and rep["mismatched"] == 0
    bad = _tops(
        tmp_path,
        [(t, 100, 1_000_000, 1_000_100, 200), (t + 1, 100, 1_000_000, 1_000_100, 200)],
        "bad.pcap",
    )
    _, _, reader = _map_iex(dp, symbols=("AAPL",), tops=TopsQuotes.load(bad, ["AAPL"], MID))
    rep = reader.oracle.report()["AAPL"]
    assert rep["status"] == "diverged" and rep["mismatched"] == 1
    assert (
        rep["first_divergence"]["book"]["bid_size"] == 150
        and rep["first_divergence"]["tops"]["bid_size"] == 100
    )
    assert rep["bbo_changes_without_tops_quote"] == 1
    with pytest.raises(IngestError, match="not TOPS"):
        TopsQuotes.load(dp, ["AAPL"], MID)


# ---------------------------------------------------------------- ingest


def test_ingest_deep_plus_equals_itch_and_records_the_venue_and_oracle(tmp_path):
    itch, pcap = _equivalent_pair(tmp_path)
    syms = ["AAPL", "QQQ"]
    a = ingest("itch50", [itch], DATE, syms, tmp_path / "a", configs_dir=CONFIGS_DIR)
    b = ingest("iex-deep-plus", [pcap], DATE, syms, tmp_path / "b", configs_dir=CONFIGS_DIR)
    stem = "raw/eq_20250115.jsonl"
    ra = (tmp_path / "a" / stem).read_text()
    rb = (tmp_path / "b" / stem).read_text()
    assert ra.replace('"venue_id":101', '"venue_id":102') == rb
    assert b["source"] == {"format": "iex-deep-plus", "venue": "IEXG", "venue_id": VENUE_ID}
    sb = b["sessions"][DATE]
    assert (
        sb["messages"]["iex"]["protocol"] == "DEEP+"
        and sb["messages"]["iex"]["level_feed"] is False
    )
    assert sb["book_verification"] is None
    assert a["sessions"][DATE]["per_symbol"] == sb["per_symbol"]
    venues = json.loads((tmp_path / "b" / "configs" / "venues" / "venues.json").read_text())
    assert venues["venues"][0]["venue"] == "IEXG" and venues["venues"][0]["venue_id"] == 102
    with pytest.raises(IngestError, match="--tops applies"):
        ingest("itch50", [itch], DATE, syms, tmp_path / "c", configs_dir=CONFIGS_DIR, tops=pcap)


def test_ingest_with_tops_oracle_refuses_a_divergent_decode_unless_allowed(tmp_path):
    dp, t = _deep_plus_session(tmp_path)
    good = _tops(
        tmp_path,
        [
            (t, 100, 1_000_000, 1_000_100, 200),
            (t + 1, 150, 1_000_000, 1_000_100, 200),
            (t + 2, 150, 1_000_000, 0, 0),
        ],
    )
    m = ingest(
        "iex-deep-plus", [dp], DATE, ["AAPL"], tmp_path / "ok", configs_dir=CONFIGS_DIR, tops=good
    )
    assert m["sessions"][DATE]["book_verification"]["AAPL"]["status"] == "match"
    bad = _tops(
        tmp_path,
        [(t, 100, 1_000_000, 1_000_100, 200), (t + 1, 100, 1_000_000, 1_000_100, 200)],
        "bad.pcap",
    )
    with pytest.raises(BookDivergenceError):
        ingest(
            "iex-deep-plus",
            [dp],
            DATE,
            ["AAPL"],
            tmp_path / "bad",
            configs_dir=CONFIGS_DIR,
            tops=bad,
        )
    m = ingest(
        "iex-deep-plus",
        [dp],
        DATE,
        ["AAPL"],
        tmp_path / "bad2",
        configs_dir=CONFIGS_DIR,
        tops=bad,
        allow_book_divergence=True,
    )
    assert m["sessions"][DATE]["book_verification"]["AAPL"]["status"] == "diverged"


def test_refusals_limit_and_protocol_checks(tmp_path):
    dp, t = _deep_plus_session(tmp_path)
    tops = _tops(tmp_path, [(t, 1, 1, 2, 1)])
    with pytest.raises(IngestError, match="not DEEP or DEEP"):
        list(IexReader(tops, DATE, ["AAPL"]))
    with pytest.raises(IngestError, match="not the format asked for"):
        list(IexReader(dp, DATE, ["AAPL"], level_feed=True))
    r = IexReader(dp, DATE, ["AAPL"], limit_messages=5)
    assert len(list(r)) >= 1 and r.limit_reached and r.messages_read == 5
    # a DEEP segment inside a DEEP+ capture is another protocol: counted, skipped
    w = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS)
    w.segment([ix.system_event("O", _ep(hms_ns(3)))], send_time=_ep(hms_ns(3)))
    w.segment(
        [ix.price_level("B", "AAPL", _ep(OPEN), 1, 1)], send_time=_ep(OPEN), protocol=PROTOCOL_DEEP
    )
    w.segment([ix.system_event("S", _ep(hms_ns(4)))], send_time=_ep(hms_ns(4)))
    w.seq += 5  # a sequence gap
    w.segment([ix.system_event("R", _ep(OPEN))], send_time=_ep(OPEN))
    r = IexReader(w.write(tmp_path / "mix.pcap"), DATE, ["AAPL"])
    out = list(r)
    assert (
        len(out) == 3
        and r.counters["other_protocol_segments"] == 1
        and r.counters["sequence_gaps"] == 1
    )
    with pytest.raises(IngestError, match="no IEX-TP"):
        e = ix.IexPcapWriter(PROTOCOL_DEEP_PLUS)
        e.packet(b"\x00" * 60, _ep(OPEN))
        list(IexReader(e.write(tmp_path / "empty.pcap"), DATE, ["AAPL"]))

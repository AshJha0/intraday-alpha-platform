"""ITCH 5.0 -> canonical events: every mapping decision of docs/REAL_DATA.md §4,
and book-reconstruction invariants through the real ``iap.orderbook`` book.
"""

from __future__ import annotations

import pytest
from iap.core.events import EventType, SessionStatus, Side, validation_error
from iap.marketdata.feederrors import FeedFormatError
from iap.marketdata.ingest import sequence_events
from iap.marketdata.itch50 import (
    SYNTHETIC_TRADE_ID_BASE,
    Itch50Mapper,
    Itch50Reader,
    session_midnight_ns,
)
from iap.orderbook.book import ApplyStatus, OrderBook
from itch50_encoder import Itch50Encoder, build_session, hms_ns

DATE = "2019-12-30"
MID = session_midnight_ns(DATE)
ADD, MODIFY, CANCEL, EXECUTE, TRADE, STATUS = (
    int(EventType.ADD),
    int(EventType.MODIFY),
    int(EventType.CANCEL),
    int(EventType.EXECUTE),
    int(EventType.TRADE),
    int(EventType.STATUS),
)
BID, ASK = int(Side.BID), int(Side.ASK)
TRADING, HALT, AUCTION, CLOSE = (int(s) for s in SessionStatus)
OPEN = hms_ns(9, 30)


def _open_day(symbols=("AAPL",)) -> Itch50Encoder:
    """Directory + start of market hours: the regular session is open.

    Every symbol's stream therefore starts with two STATUS events — AUCTION
    at the start of system hours, TRADING at the start of market hours.
    """
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    for i, s in enumerate(symbols):
        enc.stock_directory(i + 1, hms_ns(3, 0, 1), s)
    enc.system_event(hms_ns(4), "S")
    enc.system_event(OPEN, "Q")
    return enc


def _map(tmp_path, enc, symbols=("AAPL",), date=DATE, **kw):
    path = enc.write(tmp_path / "day.itch")
    mapper = Itch50Mapper(date, symbols, **kw)
    return list(mapper.events(Itch50Reader(path, symbols=symbols))), mapper


def _body(protos):
    """Proto events without instrument index and timestamp."""
    return [p[2:] for p in protos]


def test_session_midnight_is_new_york_local_time_with_dst():
    # 2019-12-30 is EST (UTC-5), 2019-07-01 is EDT (UTC-4)
    assert session_midnight_ns("2019-12-30") == 1_577_682_000 * 10**9
    assert session_midnight_ns("2019-07-01") == 1_561_953_600 * 10**9


def test_add_execute_cancel_delete_map_to_canonical_events(tmp_path):
    enc = _open_day()
    t = OPEN + 1_000
    enc.add(1, t, 11, "B", 300, "AAPL", 1_500_000)
    enc.add(1, t + 1, 12, "S", 200, "AAPL", 1_500_100, mpid="MMKR")
    enc.executed(1, t + 2, 11, 100, 501)  # partial
    enc.cancel(1, t + 3, 11, 50)  # partial cancel keeps the order
    enc.executed(1, t + 4, 11, 150, 502)  # fills the rest
    enc.cancel(1, t + 5, 12, 200)  # a cancel of everything removes the order
    enc.add(1, t + 6, 13, "S", 100, "AAPL", 1_500_200)
    enc.delete(1, t + 7, 13)
    protos, mapper = _map(tmp_path, enc)
    assert protos[:2] == [
        (0, MID + hms_ns(4), STATUS, 0, 0, AUCTION, 0, 0),
        (0, MID + OPEN, STATUS, 0, 0, TRADING, 0, 0),
    ]
    assert _body(protos[2:]) == [
        (ADD, BID, 1_500_000, 300, 11, 0),
        (ADD, ASK, 1_500_100, 200, 12, 0),
        (EXECUTE, BID, 1_500_000, 100, 11, 501),
        (TRADE, ASK, 1_500_000, 100, 0, 501),  # the aggressor sold into the bid
        (MODIFY, BID, 1_500_000, 150, 11, 0),  # remaining quantity, same price
        (EXECUTE, BID, 1_500_000, 150, 11, 502),
        (TRADE, ASK, 1_500_000, 150, 0, 502),
        (CANCEL, ASK, 1_500_100, 0, 12, 0),
        (ADD, ASK, 1_500_200, 100, 13, 0),
        (CANCEL, ASK, 1_500_200, 0, 13, 0),
    ]
    assert [p[1] for p in protos[2:4]] == [MID + OPEN + 1_000, MID + OPEN + 1_001]
    assert mapper.live_orders == 0
    assert not any(v for k, v in mapper.counters.items())


def test_executed_with_price_prints_at_the_execution_price_unless_non_printable(tmp_path):
    enc = _open_day()
    enc.add(1, OPEN + 1, 11, "S", 300, "AAPL", 1_500_100)
    enc.executed_with_price(1, OPEN + 2, 11, 100, 601, 1_500_050, "Y")
    enc.executed_with_price(1, OPEN + 3, 11, 100, 602, 1_500_000, "N")
    protos, mapper = _map(tmp_path, enc)
    assert _body(protos[3:]) == [
        (EXECUTE, ASK, 1_500_100, 100, 11, 601),  # the book event keeps the resting price
        (TRADE, BID, 1_500_050, 100, 0, 601),
        (EXECUTE, ASK, 1_500_100, 100, 11, 602),  # no TRADE: it is inside a cross print
    ]
    assert mapper.counters["nonprintable_executions"] == 1


def test_replace_is_cancel_plus_add_and_loses_queue_priority(tmp_path):
    enc = _open_day()
    enc.add(1, OPEN + 1, 11, "B", 100, "AAPL", 1_500_000)
    enc.add(1, OPEN + 2, 12, "B", 100, "AAPL", 1_500_000)
    enc.replace(1, OPEN + 3, 11, 13, 200, 1_500_000)  # same price, more size
    protos, _ = _map(tmp_path, enc)
    assert _body(protos[4:]) == [
        (CANCEL, BID, 1_500_000, 0, 11, 0),
        (ADD, BID, 1_500_000, 200, 13, 0),
    ]
    book = OrderBook(1, 101)
    for ev in sequence_events(protos, [100], 101):
        assert book.apply(ev) == ApplyStatus.APPLIED
    # FIFO at the level: the untouched order is now ahead of the replacement
    assert book.resting_orders(BID) == [(12, BID, 15_000, 100), (13, BID, 15_000, 200)]


def test_partial_cancel_keeps_queue_priority(tmp_path):
    enc = _open_day()
    enc.add(1, OPEN + 1, 11, "B", 300, "AAPL", 1_500_000)
    enc.add(1, OPEN + 2, 12, "B", 100, "AAPL", 1_500_000)
    enc.cancel(1, OPEN + 3, 11, 100)
    protos, _ = _map(tmp_path, enc)
    book = OrderBook(1, 101)
    for ev in sequence_events(protos, [100], 101):
        book.apply(ev)
    assert book.resting_orders(BID) == [(11, BID, 15_000, 200), (12, BID, 15_000, 100)]


def test_hidden_execution_is_a_trade_that_does_not_touch_the_book(tmp_path):
    enc = _open_day()
    enc.add(1, OPEN + 1, 11, "B", 100, "AAPL", 1_500_000)
    enc.add(1, OPEN + 2, 12, "S", 100, "AAPL", 1_500_100)
    enc.trade(1, OPEN + 3, "B", 70, "AAPL", 1_500_050, 701)  # midpoint, non-displayed
    protos, mapper = _map(tmp_path, enc)
    assert protos[-1][2:] == (TRADE, ASK, 1_500_050, 70, 0, 701)
    assert mapper.counters["hidden_trades"] == 1
    counters: dict = {}
    events = list(sequence_events(protos, [100], 101, counters))
    assert events[-1].price_ticks == 15_001  # 150.005 rounds half up to a whole cent tick
    assert counters == {"events": 5, "trade_prices_rounded_to_tick": 1}
    book = OrderBook(1, 101)
    for ev in events:
        assert book.apply(ev) == ApplyStatus.APPLIED
    assert book.best_bid() == (15_000, 100) and book.best_ask() == (15_001, 100)
    assert book.trade_flow == -70


def test_hidden_trade_side_uses_the_indicator_before_2014_and_the_tick_rule_after(tmp_path):
    def day() -> Itch50Encoder:
        enc = _open_day()
        enc.add(1, OPEN + 1, 11, "S", 500, "AAPL", 1_500_100)
        enc.executed(1, OPEN + 2, 11, 100, 1)  # print at 150.01, buyer-initiated
        enc.trade(1, OPEN + 3, "B", 10, "AAPL", 1_500_200, 2)  # uptick
        enc.trade(1, OPEN + 4, "B", 10, "AAPL", 1_500_200, 3)  # zero tick: carries the sign
        enc.trade(1, OPEN + 5, "B", 10, "AAPL", 1_500_000, 4)  # downtick
        enc.trade(1, OPEN + 6, "S", 10, "AAPL", 1_500_000, 5)
        return enc

    modern, mapper = _map(tmp_path, day(), date="2019-12-30")
    assert [p[3] for p in modern if p[2] == TRADE] == [BID, BID, BID, ASK, ASK]
    assert mapper.counters["hidden_trades_signed_by_tick_rule"] == 4
    legacy, mapper = _map(tmp_path, day(), date="2013-06-03")
    # indicator = side of the resting hidden order; the aggressor is the other side
    assert [p[3] for p in legacy if p[2] == TRADE] == [BID, ASK, ASK, ASK, BID]
    assert mapper.counters["hidden_trades_signed_by_tick_rule"] == 0


def test_session_phases_map_to_status_and_after_hours_flow_is_dropped(tmp_path):
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.trading_action(1, hms_ns(3, 0, 2), "AAPL", "T")
    enc.system_event(hms_ns(4), "S")
    enc.add(1, hms_ns(8), 11, "B", 100, "AAPL", 1_500_000)  # pre-market: book builds
    enc.system_event(OPEN, "Q")
    enc.add(1, OPEN + 1, 12, "S", 100, "AAPL", 1_500_100)
    enc.system_event(hms_ns(16), "M")
    enc.add(1, hms_ns(16, 0, 1), 13, "B", 100, "AAPL", 1_499_900)
    enc.delete(1, hms_ns(16, 0, 2), 11)
    enc.system_event(hms_ns(20), "E")
    enc.system_event(hms_ns(20, 5), "C")
    protos, mapper = _map(tmp_path, enc)
    assert [(p[1] - MID, *p[2:]) for p in protos] == [
        (hms_ns(3, 0, 2), STATUS, 0, 0, AUCTION, 0, 0),
        (hms_ns(8), ADD, BID, 1_500_000, 100, 11, 0),
        (OPEN, STATUS, 0, 0, TRADING, 0, 0),
        (OPEN + 1, ADD, ASK, 1_500_100, 100, 12, 0),
        (hms_ns(16), STATUS, 0, 0, CLOSE, 0, 0),
    ]
    assert mapper.counters["outside_session_dropped"] == 2
    assert [e["event_code"] for e in mapper.system_events] == ["O", "S", "Q", "M", "E", "C"]

    extended, mapper = _map(tmp_path, enc, extended_hours=True)
    assert [(p[1] - MID, p[2], p[5]) for p in extended if p[2] == STATUS] == [
        (hms_ns(3, 0, 2), STATUS, AUCTION),
        (hms_ns(4), STATUS, TRADING),
        (hms_ns(20), STATUS, CLOSE),
    ]
    assert (
        sum(p[2] == ADD for p in extended) == 3 and mapper.counters["outside_session_dropped"] == 0
    )


def test_trading_actions_map_to_halt_auction_trading(tmp_path):
    enc = _open_day(("AAPL", "MSFT"))
    enc.add(1, OPEN + 1, 11, "B", 100, "AAPL", 1_500_000)
    enc.trading_action(1, OPEN + 2, "AAPL", "H", "LUDP")
    enc.trading_action(1, OPEN + 3, "AAPL", "Q")
    enc.add(1, OPEN + 4, 12, "S", 100, "AAPL", 1_499_900)  # crosses: allowed while quoting
    enc.executed_with_price(1, OPEN + 5, 11, 100, 801, 1_499_950, "N")  # the reopening cross
    enc.executed_with_price(1, OPEN + 5, 12, 100, 801, 1_499_950, "N")
    enc.cross(1, OPEN + 5, 100, "AAPL", 1_499_950, 801, "H")
    enc.trading_action(1, OPEN + 6, "AAPL", "T")
    enc.trading_action(1, OPEN + 7, "AAPL", "P")
    enc.trading_action(1, OPEN + 8, "AAPL", "T")
    enc.trading_action(1, OPEN + 9, "AAPL", "T")  # no change: nothing emitted
    enc.trading_action(1, OPEN + 10, "AAPL", "?")
    protos, mapper = _map(tmp_path, enc, symbols=("AAPL", "MSFT"))
    aapl = [p for p in protos if p[0] == 0]
    assert [p[5] for p in aapl if p[2] == STATUS] == [
        AUCTION, TRADING, HALT, AUCTION, TRADING, HALT, TRADING,
    ]  # fmt: skip
    assert mapper.counters["unknown_trading_states"] == 1
    assert mapper.counters["cross_trades"] == 1 and mapper.counters["nonprintable_executions"] == 2
    assert not any(p[2] == TRADE for p in protos)  # a cross print is not signed order flow
    assert mapper.crosses["AAPL"] == [
        {
            "cross_type": "H",
            "price_e4": 1_499_950,
            "shares": 100,
            "exchange_ts": MID + OPEN + 5,
            "match_number": 801,
        }
    ]
    # MSFT only ever saw the start of market hours
    assert [p[5] for p in protos if p[0] == 1] == [AUCTION, TRADING]
    # through the real book: the crossing add rests during the quote-only phase,
    # the cross executions uncross it, and the book is empty and uncrossed after
    book = OrderBook(1, 101)
    crossed_while_quoting = False
    for ev in sequence_events(aapl, [50], 101):
        assert book.apply(ev) == ApplyStatus.APPLIED
        crossed_while_quoting |= book.is_crossed()
        if book.status == TRADING:
            assert not book.is_crossed()
    assert crossed_while_quoting and book.order_count_total() == 0


def test_broken_trades_reg_sho_and_participant_messages_are_counted_not_mapped(tmp_path):
    enc = _open_day()
    enc.reg_sho(1, OPEN + 1, "AAPL", "1")
    enc.market_participant(1, OPEN + 2, "AAPL")
    enc.broken(1, OPEN + 3, 4242)
    protos, mapper = _map(tmp_path, enc)
    assert [p[2] for p in protos] == [STATUS, STATUS]
    assert mapper.counters["reg_sho_messages"] == 1
    assert mapper.counters["market_participant_messages"] == 1
    assert mapper.counters["broken_trades"] == 1 and mapper.broken == [4242]


def test_inconsistent_references_are_counted_never_guessed(tmp_path):
    enc = _open_day()
    enc.add(1, OPEN + 1, 11, "B", 100, "AAPL", 1_500_000)
    enc.add(1, OPEN + 2, 11, "B", 100, "AAPL", 1_500_000)  # duplicate reference
    enc.add(1, OPEN + 3, 12, "B", 0, "AAPL", 1_500_000)  # zero shares
    enc.add(1, OPEN + 4, 13, "X", 100, "AAPL", 1_500_000)  # bad side
    enc.executed(1, OPEN + 5, 99, 100, 1)  # unknown
    enc.cancel(1, OPEN + 6, 99, 100)
    enc.delete(1, OPEN + 7, 99)
    enc.replace(1, OPEN + 8, 99, 100, 100, 1_500_000)
    enc.executed(1, OPEN + 9, 11, 500, 0)  # more than rests, and match number 0
    protos, mapper = _map(tmp_path, enc)
    assert _body(protos[2:]) == [
        (ADD, BID, 1_500_000, 100, 11, 0),
        (EXECUTE, BID, 1_500_000, 100, 11, 0),
        (TRADE, ASK, 1_500_000, 100, 0, SYNTHETIC_TRADE_ID_BASE + 1),
    ]
    c = mapper.counters
    assert (c["duplicate_order_refs"], c["invalid_orders"], c["unknown_order_refs"]) == (1, 2, 4)
    assert c["overfilled_executions"] == 1 and c["synthetic_trade_ids"] == 1


def test_symbols_outside_the_universe_are_ignored_by_the_mapper(tmp_path):
    enc, _ = build_session(11, symbols=("AAPL",), n_actions=300)
    path = enc.write(tmp_path / "day.itch")
    filtered = list(Itch50Mapper(DATE, ("AAPL",)).events(Itch50Reader(path, symbols=("AAPL",))))
    unfiltered = list(Itch50Mapper(DATE, ("AAPL",)).events(Itch50Reader(path)))
    assert filtered == unfiltered and len(filtered) > 100


def test_timestamp_outside_the_day_is_a_format_error(tmp_path):
    enc = _open_day()
    enc.add(1, 86_400 * 10**9, 11, "B", 100, "AAPL", 1_500_000)
    with pytest.raises(FeedFormatError, match="not inside one day"):
        _map(tmp_path, enc)


@pytest.mark.parametrize("seed", [1, 2, 3, 20260930, 0xDEADBEEF])
def test_book_reconstruction_invariants_on_seeded_sessions(tmp_path, seed):
    """Property-style (SplitMix64 scenarios): a valid stream never crosses the
    book in continuous trading, never leaves a non-positive quantity, never
    reuses an order id, and ends on exactly the builder's book — per level,
    in FIFO order — with every event applied by the real order book."""
    symbols = ("AAPL", "MSFT")
    enc, expected = build_session(seed, symbols=symbols, n_actions=2500)
    path = enc.write(tmp_path / "day.itch", compress=seed % 2 == 0)
    mapper = Itch50Mapper(DATE, symbols)
    protos = list(mapper.events(Itch50Reader(path, symbols=symbols)))
    books = [OrderBook(i + 1, 101) for i in range(len(symbols))]
    seen_ids: list[set[int]] = [set(), set()]
    last_seq = [0, 0]
    for ev in sequence_events(protos, [100, 100], 101):
        assert validation_error(ev) is None
        i = ev.instrument_id - 1
        assert ev.sequence == last_seq[i] + 1  # gap-free per instrument
        last_seq[i] = ev.sequence
        book = books[i]
        if ev.event_type == ADD:
            assert ev.order_id not in seen_ids[i]
            seen_ids[i].add(ev.order_id)
        assert book.apply(ev) == ApplyStatus.APPLIED
        if book.status == TRADING:
            assert not book.is_crossed()
    for symbol, book in zip(symbols, books, strict=True):
        counters = book.counters()
        assert {k: v for k, v in counters.items() if v and k != "events_applied"} == {}
        resting = book.resting_orders()
        assert all(qty > 0 for _, _, _, qty in resting)
        for side_code, side in (("B", BID), ("S", ASK)):
            want = expected["books"][symbol][side_code]
            got: dict[int, list[tuple[int, int]]] = {}
            for oid, _, price, qty in book.resting_orders(side):
                got.setdefault(price * 100, []).append((oid, qty))
            assert got == want
    assert mapper.counters["unknown_order_refs"] == 0
    assert mapper.counters["outside_session_dropped"] == len(symbols)  # one after-hours add each

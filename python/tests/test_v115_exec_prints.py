"""v1.15: ITCH ``C`` (executed-with-price) handling — the printed execution
price rides on the EXECUTE (opt-in), non-printable cross executions never
fill a resting child, and a hidden ``P`` print right after a non-printable
``C`` at the same timestamp is classified as hidden."""

from __future__ import annotations

from iap.core.events import EventType, Side
from iap.execution import ExecutionSimulator, OrderType
from iap.marketdata.itch50 import Itch50Mapper, Itch50Reader
from itch50_encoder import Itch50Encoder, hms_ns
from test_execution_rules import LAT, T0, Feeder, child, make_config, seed_book

DATE = "2019-12-30"
OPEN = hms_ns(9, 30)
EXECUTE, TRADE, MODIFY, CANCEL = (
    int(EventType.EXECUTE),
    int(EventType.TRADE),
    int(EventType.MODIFY),
    int(EventType.CANCEL),
)
BID, ASK = int(Side.BID), int(Side.ASK)


def _day() -> Itch50Encoder:
    enc = Itch50Encoder()
    enc.system_event(hms_ns(3), "O")
    enc.stock_directory(1, hms_ns(3, 0, 1), "AAPL")
    enc.system_event(hms_ns(4), "S")
    enc.system_event(OPEN, "Q")
    enc.add(1, OPEN + 1, 11, "S", 300, "AAPL", 1_500_100)
    enc.executed_with_price(1, OPEN + 2, 11, 100, 601, 1_500_050, "Y")
    # a non-printable C and a hidden P at the SAME timestamp
    enc.executed_with_price(1, OPEN + 3, 11, 100, 602, 1_500_000, "N")
    enc.trade(1, OPEN + 3, "B", 70, "AAPL", 1_500_050, 701)
    enc.executed_with_price(1, OPEN + 4, 11, 100, 603, 1_500_000, "N")
    return enc


def _map(tmp_path, **kw):
    path = _day().write(tmp_path / "day.itch")
    m = Itch50Mapper(DATE, ("AAPL",), **kw)
    return [p[2:] for p in m.events(Itch50Reader(path, symbols=("AAPL",)))][3:], m


def test_default_mapping_unchanged(tmp_path):
    body, m = _map(tmp_path)
    assert body[0] == (EXECUTE, ASK, 1_500_100, 100, 11, 601)  # resting price
    assert body[2] == (EXECUTE, ASK, 1_500_100, 100, 11, 602)  # non-printable kept as EXECUTE
    assert m.counters["nonprintable_executions"] == 2


def test_exec_prints_carries_printed_price_and_reduces_on_nonprintable(tmp_path):
    body, m = _map(tmp_path, exec_prints=True)
    assert body[0] == (EXECUTE, ASK, 1_500_050, 100, 11, 601)  # printed price
    assert body[1] == (TRADE, BID, 1_500_050, 100, 0, 601)
    # non-printable: a size reduction, never an EXECUTE
    assert body[2] == (MODIFY, ASK, 1_500_100, 100, 11, 0)
    assert body[3][0] == TRADE and body[3][-1] == 701  # the hidden P
    assert body[4] == (CANCEL, ASK, 1_500_100, 0, 11, 0)  # emptied the order
    assert EXECUTE not in [b[0] for b in body[2:]]
    assert m.counters["nonprintable_executions"] == 2


def _rest_bid(sim, f):
    seed_book(sim, f)
    oid = sim.submit(child(0, OrderType.LIMIT, 100, 50, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert sim.orders[oid].ahead_qty == 300
    return oid


def test_hidden_print_after_nonprintable_execute_with_other_trade_id():
    """A non-printable EXECUTE (match 602) then a P print (match 701) at the
    same ts: the TRADE is NOT the EXECUTE's print -> hidden (opt-in)."""
    sim = ExecutionSimulator(make_config(), hidden_prints=True)
    f = Feeder()
    oid = _rest_bid(sim, f)
    t = T0 + 2_000_000
    sim.on_event(f.ev(t, EventType.EXECUTE, 0, 100, 100, 11, 602))
    sim.on_event(f.ev(t, EventType.TRADE, 1, 100, 230, 0, 701))
    assert sim.hidden_print_trades == 1
    assert sim.orders[oid].ahead_qty == 0
    assert [(x.qty, x.price_ticks) for x in sim.fills] == [(30, 100)]


def test_matching_trade_id_is_the_executes_own_print():
    sim = ExecutionSimulator(make_config(), hidden_prints=True)
    f = Feeder()
    _rest_bid(sim, f)
    t = T0 + 2_000_000
    sim.on_event(f.ev(t, EventType.EXECUTE, 0, 100, 100, 11, 601))
    sim.on_event(f.ev(t, EventType.TRADE, 1, 100, 100, 0, 601))
    assert sim.hidden_print_trades == 0


def _price_improved(on: bool):
    sim = ExecutionSimulator(make_config(), exec_print_price=on)
    f = Feeder()
    oid = _rest_bid(sim, f)
    # resting order 11 (bid 100) executes 330 at a printed 99: queue + us
    sim.on_event(f.ev(T0 + 2_000_000, EventType.EXECUTE, 0, 99, 300, 11, 9))
    sim.on_event(f.add(T0 + 2_000_001, 0, 100, 300, 13))
    sim.on_event(f.ev(T0 + 2_000_002, EventType.EXECUTE, 0, 98, 100, 13, 10))
    return sim, oid


def test_exec_print_price_is_opt_in():
    off, _ = _price_improved(False)
    on, _ = _price_improved(True)
    assert off.fills and all(x.price_ticks == 100 for x in off.fills)
    assert on.fills and all(x.price_ticks == 98 for x in on.fills)
    assert [x.qty for x in on.fills] == [x.qty for x in off.fills]

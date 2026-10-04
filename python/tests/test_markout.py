"""Markout analysis (iap.tca.markout): definitions, edge cases, aggregation,
passive-order statistics and the golden tests/golden/expected_markout.json."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.tca.fills import MAKER, TAKER, MarketTimeline
from iap.tca.markout import (
    DEFAULT_HORIZONS_NS,
    MarkoutFill,
    PassiveOrder,
    build_gated_timeline,
    fill_measures,
    markout_mid,
    markout_report,
    passive_order_stats,
    queue_bucket,
    reference_mid,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import make_golden_markout as golden_tool  # noqa: E402

T0 = 1_000_000_000_000
SEC = 1_000_000_000


def flat(n=400, bid=99.99, ask=100.01) -> MarketTimeline:
    tl = MarketTimeline()
    for k in range(n):
        tl.append(T0 + k * SEC, bid, ask, 100, 100)
    return tl


def ramp(n=400, step=0.01) -> MarketTimeline:
    """Mid rises ``step`` per second from 100.00; spread 2 cents."""
    tl = MarketTimeline()
    for k in range(n):
        m = 100.0 + k * step
        tl.append(T0 + k * SEC, m - 0.01, m + 0.01, 100, 100)
    return tl


def test_default_horizons_are_the_documented_six():
    assert list(DEFAULT_HORIZONS_NS) == ["100ms", "1s", "5s", "30s", "60s", "5min"]
    assert DEFAULT_HORIZONS_NS["100ms"] == 100_000_000
    assert DEFAULT_HORIZONS_NS["5min"] == 300 * SEC


def test_markout_mid_is_the_state_at_or_before_the_horizon():
    tl = ramp()
    assert markout_mid(tl, T0, 5 * SEC) == pytest.approx(100.05)
    # between two states: the earlier one (no interpolation, no look-ahead)
    assert markout_mid(tl, T0, 5 * SEC + SEC // 2) == pytest.approx(100.05)
    assert markout_mid(tl, T0, 0) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        markout_mid(tl, T0, -1)


def test_signs_buy_and_sell_taker_on_a_rising_mid():
    tl = ramp()
    buy = MarkoutFill(T0, 100.01, 10, 0, TAKER)
    x = fill_measures(buy, tl, 5 * SEC)
    assert x["markout"] == pytest.approx(0.04)  # bought, the mid went up: good
    assert x["effective_half_spread"] == pytest.approx(0.01)  # paid the half-spread
    assert x["realised_half_spread"] == pytest.approx(-0.04)
    assert x["price_impact"] == pytest.approx(0.05)
    sell = MarkoutFill(T0, 99.99, 10, 1, TAKER)
    y = fill_measures(sell, tl, 5 * SEC)
    assert y["markout"] == pytest.approx(-0.06)  # sold, the mid went up: bad
    assert y["effective_half_spread"] == pytest.approx(0.01)
    assert y["price_impact"] == pytest.approx(-0.05)


def test_maker_fill_uses_the_pre_event_state_and_shows_adverse_selection():
    tl = MarketTimeline()
    tl.append(T0, 99.98, 100.02, 100, 100)
    tl.append(T0 + 1000, 99.90, 100.02, 100, 100)  # the trade-through that hit us
    for k in range(1, 20):
        tl.append(T0 + k * SEC, 99.90, 99.94, 100, 100)  # mid fell to 99.92
    f = MarkoutFill(T0 + 1000, 99.98, 100, 0, MAKER)
    assert reference_mid(tl, f.ts, MAKER) == pytest.approx(100.0)  # strictly before
    assert reference_mid(tl, f.ts, TAKER) == pytest.approx(99.96)
    x = fill_measures(f, tl, 5 * SEC)
    assert x["effective_half_spread"] == pytest.approx(-0.02)  # earned 2 cents
    assert x["price_impact"] == pytest.approx(-0.08)  # the mid moved against us
    assert x["realised_half_spread"] == pytest.approx(0.06)  # net: lost 6 cents
    assert x["markout"] == pytest.approx(-0.06)
    rep = markout_report([f, f, f], tl, min_fills=2)
    adv = rep["adverse_selection"]["5s"]
    assert adv["n"] == 3
    assert adv["bps"] == pytest.approx(1e4 * 0.08 / 99.98)
    assert adv["ccy"] == pytest.approx(3 * 0.08 * 100)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("liq", [MAKER, TAKER])
def test_effective_equals_realised_plus_impact_exactly(side, liq):
    tl = ramp(step=-0.013)
    f = MarkoutFill(T0 + 7 * SEC + 1, 99.5, 30, side, liq)
    for h in DEFAULT_HORIZONS_NS.values():
        x = fill_measures(f, tl, h)
        assert x is not None
        assert x["effective_half_spread"] == pytest.approx(
            x["realised_half_spread"] + x["price_impact"], abs=1e-12
        )
        assert x["markout"] == -x["realised_half_spread"]


def test_undefined_at_session_end_is_null_never_zero():
    tl = flat(61)
    end = tl.last_ts
    assert markout_mid(tl, end, 0) is not None
    assert markout_mid(tl, end, 1) is None  # one nanosecond past the data
    assert markout_mid(tl, end - 5 * SEC, 5 * SEC) is not None
    assert markout_mid(tl, end - 5 * SEC, 30 * SEC) is None
    f = MarkoutFill(end - 5 * SEC, 100.01, 10, 0, TAKER)
    rep = markout_report([f] * 6, tl)
    assert rep["all"]["horizons"]["5s"]["n"] == 6
    assert rep["all"]["horizons"]["30s"] == {
        "n": 0,
        **{
            f"{m}_{k}": None
            for m in ("markout", "effective_half_spread", "realised_half_spread", "price_impact")
            for k in ("bps", "se_bps", "ccy")
        },
    }


def test_undefined_before_the_first_quote():
    tl = flat()
    assert markout_mid(tl, T0 - 10 * SEC, SEC) is None
    f = MarkoutFill(T0 - SEC, 100.0, 10, 0, TAKER)
    assert fill_measures(f, tl, 5 * SEC) is None  # a mid exists at +5 s, no reference mid
    assert reference_mid(tl, f.ts, TAKER) is None


def test_undefined_when_a_gate_starts_inside_the_window_or_after_the_last_quote():
    tl = MarketTimeline()
    for k in range(0, 10):
        tl.append(T0 + k * SEC, 99.99, 100.01, 100, 100)
    for k in range(30, 60):  # no state during the 20 s gap
        tl.append(T0 + k * SEC, 99.99, 100.01, 100, 100)
    tl.add_halt(T0 + 10 * SEC)
    assert markout_mid(tl, T0 + 2 * SEC, 5 * SEC) is not None  # ends before the gate
    assert markout_mid(tl, T0 + 2 * SEC, 8 * SEC) is None  # gate at the horizon
    assert markout_mid(tl, T0 + 2 * SEC, 40 * SEC) is None  # gate inside the window
    # a fill inside the gap: its 5 s horizon would use the pre-gate quote
    assert markout_mid(tl, T0 + 15 * SEC, 5 * SEC) is None
    # ... but once quotes are back the window is clean again
    assert markout_mid(tl, T0 + 31 * SEC, 5 * SEC) is not None
    assert markout_mid(tl, T0 + 15 * SEC, 20 * SEC) is not None


def test_cells_counts_standard_errors_and_the_too_few_rule():
    tl = ramp()
    fills = [
        MarkoutFill(T0 + k * SEC, 100.0 + k * 0.01 + 0.01, 10 * (k + 1), 0, TAKER, 1, "IS")
        for k in range(6)
    ] + [MarkoutFill(T0 + 3 * SEC, 100.02, 10, 1, TAKER, 2, "TWAP")]
    rep = markout_report(fills, tl, min_fills=5, bucket_ns=4 * SEC, session_start_ts=T0)
    cell = rep["all"]["horizons"]["1s"]
    assert cell["n"] == 7
    vals = [1e4 * fill_measures(f, tl, SEC)["markout"] / f.price for f in fills]
    mean = sum(vals) / 7
    se = math.sqrt(sum((v - mean) ** 2 for v in vals) / 6 / 7)
    assert cell["markout_bps"] == pytest.approx(mean)
    assert cell["markout_se_bps"] == pytest.approx(se)
    assert cell["markout_ccy"] == pytest.approx(
        sum(fill_measures(f, tl, SEC)["markout"] * f.qty for f in fills)
    )
    assert cell["effective_half_spread_bps"] == pytest.approx(
        cell["realised_half_spread_bps"] + cell["price_impact_bps"]
    )
    assert rep["all"]["n_fills"] == 7 and rep["all"]["qty"] == 220
    assert list(rep["by_liquidity"]) == ["TAKER"]
    assert list(rep["by_venue"]) == ["1", "2"]
    assert list(rep["by_algo"]) == ["IS", "TWAP"]
    assert list(rep["by_side"]) == ["BUY", "SELL"]
    assert list(rep["by_time_bucket"]) == ["0", "1"]
    assert rep["by_time_bucket"]["0"]["n_fills"] == 5
    # one fill on venue 2: counted, every statistic null
    lone = rep["by_venue"]["2"]["horizons"]["1s"]
    assert lone["n"] == 1 and lone["markout_bps"] is None and lone["markout_ccy"] is None
    # no MAKER fills: adverse selection is null with n 0
    assert rep["adverse_selection"]["1s"] == {"n": 0, "bps": None, "se_bps": None, "ccy": None}
    with pytest.raises(ValueError):
        markout_report(fills, tl, min_fills=1)
    with pytest.raises(ValueError):
        markout_report(fills, tl, bucket_ns=0)


def test_currency_uses_the_quantity_unit():
    tl = ramp()
    eq = MarkoutFill(T0, 100.01, 10, 0, TAKER)
    fx = MarkoutFill(T0, 100.01, 10, 0, TAKER, qty_unit=1000.0)
    a = markout_report([eq] * 5, tl)["all"]["horizons"]["5s"]["markout_ccy"]
    b = markout_report([fx] * 5, tl)["all"]["horizons"]["5s"]["markout_ccy"]
    assert b == pytest.approx(1000.0 * a)


def test_fill_and_order_records_are_validated():
    for bad in (
        {"liquidity": "X"},
        {"side": 2},
        {"qty": 0},
        {"price": 0.0},
    ):
        kw = {"ts": T0, "price": 1.0, "qty": 1, "side": 0, "liquidity": TAKER}
        kw.update(bad)
        with pytest.raises(ValueError):
            MarkoutFill(**kw)
    with pytest.raises(ValueError):
        PassiveOrder(100, 101, T0, T0, T0, 0)
    with pytest.raises(ValueError):
        PassiveOrder(100, 50, T0, None, None, 0)
    with pytest.raises(ValueError):
        PassiveOrder(100, 0, T0, T0, None, 0)


def test_passive_order_fill_rate_time_to_fill_and_queue_buckets():
    orders = [
        PassiveOrder(100, 100, T0, T0 + 2 * SEC, T0 + 4 * SEC, 0),
        PassiveOrder(100, 100, T0, T0 + 4 * SEC, T0 + 4 * SEC, 0),
        PassiveOrder(100, 50, T0, T0 + 6 * SEC, T0 + 6 * SEC, 0),
        PassiveOrder(200, 0, T0, None, None, 5000),
        PassiveOrder(200, 0, T0, None, None, 5000),
        PassiveOrder(200, 200, T0, T0 + 30 * SEC, T0 + 30 * SEC, 5000),
        PassiveOrder(100, 0, T0, None, None, 400),
    ]
    rep = passive_order_stats(orders, min_orders=3)
    a = rep["all"]
    assert a["n_orders"] == 7 and a["posted_qty"] == 1000 and a["filled_qty"] == 450
    assert a["fill_rate_qty"] == pytest.approx(0.45)
    assert a["fill_rate_orders"] == pytest.approx(4 / 7)
    assert a["full_fill_rate"] == pytest.approx(3 / 7)
    assert a["time_to_first_fill_ns"]["n"] == 4
    assert a["time_to_first_fill_ns"]["mean"] == pytest.approx(10.5 * SEC)
    assert a["time_to_full_fill_ns"]["n"] == 3
    assert list(rep["by_queue_ahead_at_entry"]) == ["0", "1-500", ">2000"]
    front = rep["by_queue_ahead_at_entry"]["0"]
    back = rep["by_queue_ahead_at_entry"][">2000"]
    assert front["fill_rate_qty"] == pytest.approx(250 / 300)
    assert back["fill_rate_qty"] == pytest.approx(200 / 600)
    assert front["time_to_first_fill_ns"]["mean"] == pytest.approx(4 * SEC)
    # one order in the bucket: counted, rates null
    lone = rep["by_queue_ahead_at_entry"]["1-500"]
    assert lone["n_orders"] == 1 and lone["fill_rate_qty"] is None
    assert back["time_to_first_fill_ns"] == {"n": 1, "mean": None, "se": None}
    assert [queue_bucket(q, (0, 500, 2000)) for q in (0, 1, 500, 501, 2000, 2001)] == [
        "0",
        "1-500",
        "1-500",
        "501-2000",
        "501-2000",
        ">2000",
    ]
    with pytest.raises(ValueError):
        passive_order_stats(orders, queue_edges=(1, 5))
    with pytest.raises(ValueError):
        passive_order_stats(orders, min_orders=1)


def _ev(seq, ts, etype, side=0, px=0, qty=0, oid=0, venue=1) -> MarketEvent:
    return MarketEvent(
        event_id=seq,
        instrument_id=7,
        venue_id=venue,
        exchange_ts=ts,
        receive_ts=ts,
        sequence=seq,
        event_type=int(etype),
        side=side,
        price_ticks=px,
        qty=qty,
        order_id=oid,
        trade_id=0,
    )


def test_gated_timeline_records_halts_auctions_and_quote_gaps():
    evs = [
        _ev(1, T0, EventType.ADD, 0, 100, 10, 1),
        _ev(2, T0 + 1, EventType.ADD, 1, 102, 10, 2),
        _ev(3, T0 + SEC, EventType.HEARTBEAT),
        _ev(4, T0 + 2 * SEC, EventType.STATUS, qty=int(SessionStatus.HALT)),
        _ev(5, T0 + 3 * SEC, EventType.HEARTBEAT),  # halted: no state
        _ev(6, T0 + 4 * SEC, EventType.STATUS, qty=int(SessionStatus.AUCTION)),  # still gated
        _ev(7, T0 + 5 * SEC, EventType.STATUS, qty=int(SessionStatus.TRADING)),
        _ev(8, T0 + 6 * SEC, EventType.CANCEL, oid=2),  # the ask disappears
        _ev(9, T0 + 7 * SEC, EventType.HEARTBEAT),
        _ev(10, T0 + 8 * SEC, EventType.ADD, 1, 104, 10, 3),
        _ev(11, T0 + 20 * SEC, EventType.HEARTBEAT),
    ]
    tl = build_gated_timeline(evs, 7, 0.01)
    assert tl.halts == [T0 + 2 * SEC, T0 + 6 * SEC]  # one gate per closure, one per gap
    assert tl.ts == [T0 + 1, T0 + SEC, T0 + 5 * SEC, T0 + 8 * SEC, T0 + 20 * SEC]
    assert markout_mid(tl, T0 + 1, SEC) == pytest.approx(1.01)
    assert markout_mid(tl, T0 + 1, 5 * SEC) is None  # halt inside
    assert markout_mid(tl, T0 + 5 * SEC, 2 * SEC) is None  # quote gap inside
    assert markout_mid(tl, T0 + 8 * SEC, 5 * SEC) == pytest.approx(1.02)
    with pytest.raises(ValueError):
        build_gated_timeline(evs, 7, 0.0)


# ----------------------------------------------------------------- golden --


@pytest.fixture(scope="module")
def golden(golden_dir):
    with open(golden_dir / "expected_markout.json", encoding="utf-8") as f:
        return json.load(f)


def _close(got, want, tol, path="$"):
    if isinstance(want, dict):
        assert isinstance(got, dict) and list(got) == list(want), path
        for k in want:
            _close(got[k], want[k], tol, f"{path}.{k}")
    elif isinstance(want, list):
        assert len(got) == len(want), path
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            _close(g, w, tol, f"{path}[{i}]")
    elif want is None:
        assert got is None, path
    elif isinstance(want, float):
        assert got is not None and abs(got - want) <= tol, (path, got, want)
    else:
        assert got == want, (path, got, want)


def test_golden_markout_report_matches(golden):
    assert golden["x-version"] == 1 and golden["tolerance"] == 1e-9
    got = json.loads(json.dumps(golden_tool.expected(golden)))
    _close(got, golden["expected"], golden["tolerance"])


def test_golden_markout_file_is_the_generator_output(golden_dir):
    want = (golden_dir / "expected_markout.json").read_bytes()
    assert (json.dumps(golden_tool.document(), indent=1) + "\n").encode("utf-8") == want


def test_golden_markout_covers_the_edge_cases(golden):
    per_fill = golden["expected"]["per_fill"]
    fills = golden["fills"]
    assert len(fills) == 40 and len(per_fill) == 40
    before_quote, at_end, before_halt, in_gap = per_fill[-4:]
    assert all(v is None for v in before_quote.values())  # no reference mid
    assert all(v is None for v in at_end.values())  # timeline ends
    assert before_halt["1s"] is not None and before_halt["5s"] is None
    assert all(v is None for v in in_gap.values())  # quote predates the gap
    rep = golden["expected"]["report"]
    assert set(rep["by_liquidity"]) == {"MAKER", "TAKER"}
    assert rep["all"]["horizons"]["5min"]["n"] < rep["all"]["horizons"]["100ms"]["n"]
    nulls = [c["horizons"]["60s"]["markout_bps"] is None for c in rep["by_time_bucket"].values()]
    assert any(nulls) and not all(nulls)
    assert rep["all"]["horizons"]["5min"]["markout_bps"] is None  # every window hits a gate
    assert rep["adverse_selection"]["1s"]["bps"] is not None
    assert len(golden["expected"]["passive"]["by_queue_ahead_at_entry"]) >= 3

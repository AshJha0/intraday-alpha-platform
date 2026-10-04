"""Execution policies: NATIVE / AGGRESSIVE / PASSIVE (iap.execution.passive).

State machine (POST -> REST -> REPRICE / CROSS), the never-cross posting
rule, fee signs, quantity conservation and the policy golden
(tests/golden/expected_replay_fills_passive.json). Mirrors
cpp/tests/test_replay_fills_passive.cpp and the Java
ReplayFillsPassiveGoldenTest / PassivePolicyTest.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from iap.core.codec import read_jsonl
from iap.core.events import EventType
from iap.execution import (
    AlgoType,
    CancelReason,
    ExecPolicy,
    ExecutionReplay,
    Liquidity,
    OrderState,
    OrderType,
    ParentOrder,
    PassiveParams,
    max_behind_qty,
    patience_ns,
    post_price,
)
from iap.orderbook.book import ConsolidatedBook
from test_exec_algos import SEC, T0, _ev, algo_config

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import make_golden_replay_passive as golden_tool  # noqa: E402

# ------------------------------------------------------------ pure rules --


def test_post_price_joins_the_near_touch():
    assert post_price(0, (100, 500), (101, 500), 3) == 100
    assert post_price(1, (100, 500), (101, 500), 3) == 101
    assert post_price(0, (100, 500), (102, 500), 3) == 100  # spread 2 < 3


def test_post_price_improves_one_tick_when_the_spread_allows():
    assert post_price(0, (100, 500), (103, 500), 3) == 101
    assert post_price(1, (100, 500), (103, 500), 3) == 102
    assert post_price(0, (100, 500), (110, 500), 3) == 101  # one tick, never more
    assert post_price(0, (100, 500), (110, 500), 0) == 100  # improvement disabled
    assert post_price(0, (100, 500), (102, 500), 2) == 101  # at the mid, not through


def test_post_price_never_reaches_the_opposite_touch():
    for bid in range(95, 106):
        for ask in range(95, 106):
            for improve in (0, 1, 2, 3):
                buy = post_price(0, (bid, 10), (ask, 10), improve)
                sell = post_price(1, (bid, 10), (ask, 10), improve)
                assert buy is not None and buy < ask, (bid, ask, improve)
                assert sell is not None and sell > bid, (bid, ask, improve)
                if ask > bid:  # an uncrossed book is never posted behind
                    assert buy >= bid and sell <= ask


def test_post_price_needs_a_near_quote_and_tolerates_a_missing_far_one():
    assert post_price(0, None, (101, 5), 3) is None
    assert post_price(1, (100, 5), None, 3) is None
    assert post_price(0, (100, 5), None, 3) == 100
    assert post_price(1, None, (101, 5), 3) == 101
    assert post_price(0, (1, 5), (1, 5), 3) is None  # would post at 0


def test_patience_is_set_by_urgency_and_the_is_risk_aversion():
    p = PassiveParams(max_rest_ns=30 * SEC)
    assert patience_ns(p, 0.0, False, 1.0) == 30 * SEC
    assert patience_ns(p, 0.5, False, 1.0) == 15 * SEC
    assert patience_ns(p, 1.0, False, 1.0) == 0
    assert patience_ns(p, 7.0, False, 1.0) == 0  # clamped
    assert patience_ns(p, -3.0, False, 1.0) == 30 * SEC  # clamped
    assert patience_ns(p, 0.2, True, 1.0) == int(math.floor(30e9 * 0.8 * math.exp(-1.0)))
    assert patience_ns(p, 0.2, True, 0.0) == 24 * SEC
    assert patience_ns(p, 0.2, True, 3.0) < patience_ns(p, 0.2, True, 1.0)
    assert max_behind_qty(PassiveParams(max_behind_fraction=0.1), 405) == 40


def test_passive_params_are_validated():
    for bad in (
        {"max_rest_ns": -1},
        {"end_margin_ns": -1},
        {"max_reprices": -1},
        {"max_behind_fraction": 1.5},
        {"improve_min_spread_ticks": -1},
    ):
        with pytest.raises(ValueError):
            PassiveParams(**bad)


# --------------------------------------------------------- replay-driven --


def parent(algo: AlgoType, qty: int, slices: int, **kw) -> ParentOrder:
    base = {
        "parent_id": 1,
        "instrument_id": 7,
        "venue_id": 1,
        "side": 0,
        "qty": qty,
        "algo": algo,
        "start_ts": T0,
        "end_ts": T0 + 100 * SEC,
        "slices": slices,
    }
    base.update(kw)
    return ParentOrder(**base)


def stream(bid=100, ask=101, hit_bid_every=0, hit_qty=50, bid_move_at=None, depth=100_000):
    """Venue-1 book ``bid`` x ``ask`` and one event per half second for 200 s.

    ``hit_bid_every`` > 0 adds a marketable ASK ADD of ``hit_qty`` at the bid
    every that many seconds (the replayed book matches it against the bid:
    a trade the simulator's rule 4 tracks). ``bid_move_at`` (seconds) adds a
    better bid one tick up at that time.
    """
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid=0):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, bid, depth, 1)
    push(T0 - SEC + 1, EventType.ADD, 1, ask, depth, 2)
    for i in range(200):
        ts = T0 + i * SEC
        push(ts, EventType.TRADE, 0, bid, 40, 0, 1000 + i)
        if hit_bid_every and i % hit_bid_every == hit_bid_every - 1:
            push(ts + SEC // 4, EventType.ADD, 1, bid, hit_qty, 5000 + i)
        if bid_move_at is not None and i == bid_move_at:
            push(ts + SEC // 4, EventType.ADD, 0, bid + 1, 500, 9000)
        push(ts + SEC // 2, EventType.HEARTBEAT, 0, 0, 0, 0)
    return evs


def run(p: ParentOrder, events):
    replay = ExecutionReplay(algo_config(), [p])
    return replay.run(events), replay


def test_native_is_the_default_and_names_the_existing_behaviour():
    assert ParentOrder().policy == ExecPolicy.NATIVE
    evs = stream(hit_bid_every=3)
    a, _ = run(parent(AlgoType.TWAP, 400, 4), evs)
    b, _ = run(parent(AlgoType.TWAP, 400, 4, policy=ExecPolicy.NATIVE, urgency=0.9), evs)
    assert [f.to_dict() for f in a.fills] == [f.to_dict() for f in b.fills]
    assert a.passive == {} and b.passive == {}
    assert all(o.type == OrderType.LIMIT for o in a.children[1])


def test_aggressive_sends_market_children_for_every_algo():
    for algo in (AlgoType.TWAP, AlgoType.VWAP, AlgoType.IS):
        res, _ = run(parent(algo, 400, 4, policy=ExecPolicy.AGGRESSIVE), stream())
        assert all(o.type == OrderType.MARKET for o in res.children[1])
        assert res.parents[1].filled_qty == 400
        for f in res.fills:
            assert f.liquidity == Liquidity.TAKER
            assert f.price_ticks == 101  # pays the spread
            assert f.fee == 0.003 * f.qty and f.fee > 0.0  # taker fee: a cost
            assert f.impact_cost > 0.0


def test_passive_rest_extension_then_cross_when_the_queue_never_clears():
    # Spread of one tick: post AT the bid behind 100,000 displayed; the tape
    # never reaches us. Patience 10 s (urgency 0.5 of 20 s), one reprice.
    params = PassiveParams(max_rest_ns=20 * SEC)
    p = parent(AlgoType.TWAP, 400, 4, policy=ExecPolicy.PASSIVE, urgency=0.5, passive=params)
    res, _ = run(p, stream())
    kids = res.children[1]
    posted = [o for o in kids if o.type == OrderType.LIMIT]
    crossed = [o for o in kids if o.type == OrderType.MARKET]
    assert len(posted) == 4 and len(crossed) == 4
    for o in posted:
        assert o.limit_ticks == 100  # the near touch, not through 101
        assert o.entry_ahead_qty == 100_000
        assert o.state == OrderState.CANCELLED and o.cancel_reason == CancelReason.USER
        assert o.remaining == 100
    # POST at the slice, extension at +10 s (price unchanged: queue position
    # kept), cancel at +20 s, MARKET once the cancel has taken effect.
    for i, o in enumerate(crossed):
        due = T0 + i * 25 * SEC
        assert due + 20 * SEC <= o.decision_ts <= due + 21 * SEC
    assert res.passive[1].to_dict() == {
        "posts": 4,
        "reprices": 0,
        "rest_extensions": 4,
        "crosses_timeout": 4,
        "crosses_behind": 0,
        "crosses_immediate": 0,
    }
    assert res.parents[1].filled_qty == 400
    assert all(f.liquidity == Liquidity.TAKER and f.price_ticks == 101 for f in res.fills)


def test_passive_posts_inside_a_wide_spread_and_earns_the_rebate():
    # Spread 4 ticks: post one tick inside (101), nothing ahead of us; every
    # marketable sell trades through 101 and fills us there.
    p = parent(AlgoType.TWAP, 400, 4, policy=ExecPolicy.PASSIVE, urgency=0.0)
    res, _ = run(p, stream(bid=100, ask=104, hit_bid_every=2, hit_qty=60))
    kids = res.children[1]
    assert [o.type for o in kids] == [OrderType.LIMIT] * 4
    assert all(o.limit_ticks == 101 and o.entry_ahead_qty == 0 for o in kids)
    assert res.parents[1].filled_qty == 400
    for f in res.fills:
        assert f.liquidity == Liquidity.MAKER
        assert f.price_ticks == 101  # our limit, never better
        assert f.qty <= 60  # bounded by the volume that traded (rule 4)
        assert f.fee == -0.002 * f.qty and f.fee < 0.0  # maker rebate: negative
        assert f.impact_cost == 0.0
    assert res.parents[1].rebates == pytest.approx(0.002 * 400)
    assert res.parents[1].fees == 0.0
    assert res.passive[1].crosses_timeout == res.passive[1].crosses_behind == 0


def test_passive_reprices_when_the_touch_moves_away():
    # A better bid appears 5 s after the first post: at the 10 s deadline the
    # post price is 101, not our 100 -> cancel, re-post at 101.
    params = PassiveParams(max_rest_ns=20 * SEC)
    p = parent(AlgoType.TWAP, 100, 1, policy=ExecPolicy.PASSIVE, urgency=0.5, passive=params)
    res, _ = run(p, stream(bid=100, ask=102, bid_move_at=5))
    kids = res.children[1]
    assert [o.type for o in kids] == [OrderType.LIMIT, OrderType.LIMIT, OrderType.MARKET]
    assert [o.limit_ticks for o in kids[:2]] == [100, 101]
    assert kids[0].cancel_reason == CancelReason.USER
    assert kids[1].decision_ts > kids[0].decision_ts + 10 * SEC  # after the cancel landed
    assert kids[1].qty == 100  # the cancelled remainder, not more
    s = res.passive[1]
    assert (s.posts, s.reprices, s.rest_extensions, s.crosses_timeout) == (2, 1, 0, 1)
    assert res.parents[1].filled_qty == 100


def test_passive_crosses_as_soon_as_the_schedule_is_behind():
    # Patience 60 s exceeds the 25 s slice interval and nothing fills: when
    # slice 1 comes due the backlog is slice 0 (100 > floor(0.1 * 400)).
    params = PassiveParams(max_rest_ns=60 * SEC)
    p = parent(AlgoType.TWAP, 400, 4, policy=ExecPolicy.PASSIVE, urgency=0.0, passive=params)
    res, _ = run(p, stream())
    kids = res.children[1]
    first_cross = next(o for o in kids if o.type == OrderType.MARKET)
    assert T0 + 25 * SEC <= first_cross.decision_ts < T0 + 26 * SEC
    assert res.passive[1].crosses_behind >= 3
    assert res.passive[1].rest_extensions == 0
    # A tolerance of the whole order never declares the schedule behind.
    lax = replace(params, max_behind_fraction=1.0)
    res2, _ = run(replace(p, passive=lax), stream())
    assert res2.passive[1].crosses_behind == 0


def test_passive_pov_posts_the_deficit_and_crosses_when_behind():
    p = parent(
        AlgoType.POV,
        500,
        1,
        participation=0.10,
        max_child_qty=25,
        policy=ExecPolicy.PASSIVE,
        urgency=0.0,
    )
    res, _ = run(p, stream())
    s = res.passive[1]
    assert s.posts > 0 and s.crosses_behind > 0
    # Never ahead of the participation target, never more than the parent.
    assert res.parents[1].filled_qty <= 400
    assert all(o.qty <= 25 for o in res.children[1])


def test_urgency_one_and_the_end_margin_cross_immediately():
    p = parent(AlgoType.TWAP, 400, 4, policy=ExecPolicy.PASSIVE, urgency=1.0)
    res, _ = run(p, stream())
    assert all(o.type == OrderType.MARKET for o in res.children[1])
    assert res.passive[1].crosses_immediate == 4 and res.passive[1].posts == 0
    # A margin as long as the window leaves no time to rest at all.
    params = PassiveParams(end_margin_ns=100 * SEC)
    q = parent(AlgoType.TWAP, 400, 4, policy=ExecPolicy.PASSIVE, urgency=0.0, passive=params)
    res2, _ = run(q, stream())
    assert all(o.type == OrderType.MARKET for o in res2.children[1])


def test_a_fill_while_the_cancel_is_in_flight_is_not_sent_again():
    # The golden scenario has exactly this: order 6 (IS slice 0, 304 posted)
    # fills 300 passively, is cancelled with 4 left, and only 4 are crossed.
    res, _ = golden_tool.run_scenario(golden_tool.GOLDEN.parent)
    kids = {o.order_id: o for o in res.children[2]}
    assert kids[6].qty == 304 and kids[6].remaining == 4
    assert kids[6].cancel_reason == CancelReason.USER
    assert kids[9].type == OrderType.MARKET and kids[9].qty == 4


@pytest.mark.parametrize("urgency", [0.0, 0.3, 0.7, 1.0])
@pytest.mark.parametrize("fraction", [0.0, 0.1, 1.0])
@pytest.mark.parametrize("reprices", [0, 1, 3])
def test_quantity_is_conserved_and_fills_stay_in_the_window(urgency, fraction, reprices):
    params = PassiveParams(
        max_rest_ns=40 * SEC, max_reprices=reprices, max_behind_fraction=fraction
    )
    parents = [
        parent(a, 300, 3, parent_id=i + 1, side=i % 2, policy=ExecPolicy.PASSIVE,
               urgency=urgency, passive=params, participation=0.2)
        for i, a in enumerate((AlgoType.TWAP, AlgoType.VWAP, AlgoType.IS, AlgoType.POV))
    ]  # fmt: skip
    replay = ExecutionReplay(algo_config(), parents)
    res = replay.run(stream(bid=100, ask=103, hit_bid_every=3, bid_move_at=7))
    for p in parents:
        rep = res.parents[p.parent_id]
        assert 0 <= rep.filled_qty <= p.qty
        assert rep.filled_qty + rep.unfilled_qty == p.qty
        assert abs(rep.total_cost - (rep.fees - rep.rebates + rep.impact)) <= 1e-12
    for f in res.fills:
        p = parents[f.parent_id - 1]
        assert p.start_ts <= f.ts <= p.end_ts
        assert (f.fee < 0.0) == (f.liquidity == Liquidity.MAKER)
    for o in replay.simulator.orders.values():
        assert o.is_terminal  # nothing is left working after the stream


def test_posted_limits_never_reach_the_opposite_touch_on_the_golden_stream(golden_dir):
    events = read_jsonl(golden_dir / "events_eq_mbo.jsonl")
    book = ConsolidatedBook(1)
    touch_at: dict[int, list[tuple]] = {}
    for ev in events:
        book.apply(ev)
        vb = book.books.get(1)
        touch_at.setdefault(ev.exchange_ts, []).append(
            (None, None) if vb is None else (vb.best_bid(), vb.best_ask())
        )
    res, _ = golden_tool.run_scenario(golden_dir)
    checked = 0
    for kids in res.children.values():
        for o in kids:
            if o.type != OrderType.LIMIT:
                continue
            ok = False
            for bid, ask in touch_at[o.decision_ts]:
                opp = ask if o.side == 0 else bid
                if opp is None or (
                    o.limit_ticks < opp[0] if o.side == 0 else o.limit_ticks > opp[0]
                ):
                    ok = True
            assert ok, o
            checked += 1
    assert checked >= 20


# ----------------------------------------------------------------- golden --


@pytest.fixture(scope="module")
def golden(golden_dir):
    with open(golden_dir / "expected_replay_fills_passive.json", encoding="utf-8") as f:
        return json.load(f)


def test_policy_golden_is_reproduced_exactly(golden, golden_dir):
    assert golden["x-version"] == 1
    res, t0 = golden_tool.run_scenario(golden_dir)
    assert golden["t0"] == t0 and golden["events_processed"] == res.events_processed
    assert len(res.fills) == len(golden["fills"])
    for got, want in zip(res.fills, golden["fills"], strict=True):
        row = got.to_dict()
        for key in ("fill_id", "order_id", "parent_id", "venue_id", "side", "price_ticks", "qty"):
            assert row[key] == want[key], (want["fill_id"], key)
        assert row["ts"] == want["ts"] and row["liquidity"] == want["liquidity"]
        assert abs(row["fee"] - want["fee"]) <= 1e-9
        assert abs(row["impact_cost"] - want["impact_cost"]) <= 1e-9
    for pid, want in golden["parents"].items():
        got = res.parents[int(pid)].to_dict()
        for key, value in want.items():
            assert got[key] == pytest.approx(value, abs=1e-9), (pid, key)
    assert {str(k): v.to_dict() for k, v in res.passive.items()} == golden["passive"]


def test_policy_golden_file_is_the_generator_output_byte_for_byte(golden_dir):
    res, t0 = golden_tool.run_scenario(golden_dir)
    want = (golden_dir / "expected_replay_fills_passive.json").read_bytes()
    assert golden_tool.render(res, t0).encode("utf-8") == want


def test_policy_golden_covers_every_transition(golden):
    total = dict.fromkeys(next(iter(golden["passive"].values())), 0)
    for row in golden["passive"].values():
        for key, value in row.items():
            total[key] += value
    for key in ("posts", "reprices", "rest_extensions", "crosses_timeout", "crosses_behind"):
        assert total[key] > 0, key
    liq = {(f["parent_id"], f["liquidity"]) for f in golden["fills"]}
    assert {(1, "MAKER"), (1, "TAKER"), (2, "MAKER"), (2, "TAKER"), (4, "TAKER")} <= liq
    assert all(p["unfilled_qty"] == 0 for p in golden["parents"].values())

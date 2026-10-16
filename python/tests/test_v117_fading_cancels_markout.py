"""v1.17 WS-B items B4 (quote fading), B5 (queue-aware cancels, X-4 split)
and B6 (markout decomposition, X-8 hazard clock).

Synthetic books only. The default-off paths are pinned by the execution
goldens (``test_execution_golden``, ``test_passive_policy``); here a
PASSIVE parent without the opt-ins is also shown byte-identical on the
local stream with and without a signal attached.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from iap.core.events import EventType
from iap.execution import (
    AlgoType,
    ArraySignalSource,
    ChildOrder,
    ExecPolicy,
    ExecutionReplay,
    FadeParams,
    Fill,
    FillHazardModel,
    Liquidity,
    OrderType,
    ParentOrder,
    PassiveParams,
    PullRecord,
    QueueCancelParams,
    pull_outcomes,
    pull_rows,
    pull_summary,
)
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.execution.fading import CANCEL_RULES, book_pressure, fade_rule, queue_cancel_rule
from iap.execution.fill_hazard import IEX_TAI_OFFSET_S, hazard_features, time_of_day
from iap.research import execution_benchmark as bench
from iap.tca.fills import MarketTimeline
from iap.tca.markout_decomposition import (
    FILL_TYPES,
    cluster_mean,
    decompose,
    fill_rows,
    fill_type,
    markout_decomposition,
)
from test_exec_algos import SEC, T0, _ev, algo_config
from test_fill_hazard_x4 import planted_samples

REPO = Path(__file__).resolve().parents[2]
INS = 7
#: decision + risk + wire (ExecConfig defaults) + venue 1 mean (algo_config), no jitter
LATENCY = 50_000 + 50_000 + 100_000 + 100_000


# ------------------------------------------------------------- streams --


def book(hits, *, bid_top=200, ask_top=200, seconds=60, extra=()):
    """A venue-1 book: bid 100 x ``bid_top`` (order 100), bid 99 x 3 x 500
    (orders 101-103), deeper bids and asks 100,000 deep, ask 101 x
    ``ask_top``; a 40-lot TRADE every second (POV volume, and a visit).
    ``hits`` = timestamps of an EXECUTE of 500 against the next order at
    99: the market trades THROUGH a buy resting at 100 with more quantity
    than the 200 displayed ahead of it, so the resting child fills
    (simulator rule 4; a crossing ADD is not applied by the replayed book, so
    an EXECUTE is the way a synthetic stream fills a resting order).
    ``extra`` = ``(ts, etype, side, px, qty, oid)`` events appended as given.
    Sorted by time, stable."""
    rows = []

    def push(ts, etype, side, px, qty, oid, tid=0):
        rows.append((ts, len(rows), etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, 100, bid_top, 100)
    for j in range(3):
        push(T0 - SEC + 1 + j, EventType.ADD, 0, 99, 500, 101 + j)
    for k in range(2, 30):
        push(T0 - SEC + 10 + k, EventType.ADD, 0, 100 - k, 100_000, 110 + k)
    push(T0 - SEC + 40, EventType.ADD, 1, 101, ask_top, 200)
    for k in range(1, 30):
        push(T0 - SEC + 40 + k, EventType.ADD, 1, 101 + k, 100_000, 200 + k)
    for i in range(seconds):
        push(T0 + i * SEC, EventType.TRADE, 0, 100, 40, 0, 1000 + i)
    for n, ts in enumerate(hits):
        push(ts, EventType.EXECUTE, 0, 99, 500, 101 + n, 2000 + n)
    for ts, et, side, px, q, oid in extra:
        push(ts, et, side, px, q, oid)
    rows.sort(key=lambda r: (r[0], r[1]))  # sequence numbers follow event time
    return [
        _ev(i + 1, ts, et, side, px, q, oid, tid)
        for i, (ts, _, et, side, px, q, oid, tid) in enumerate(rows)
    ]


def parent(**kw) -> ParentOrder:
    base = {
        "parent_id": 1,
        "instrument_id": INS,
        "venue_id": 1,
        "side": 0,
        "qty": 100,
        "algo": AlgoType.TWAP,
        "start_ts": T0,
        "end_ts": T0 + 30 * SEC,
        "slices": 1,
        "max_child_qty": 100,
        "policy": ExecPolicy.PASSIVE,
        "urgency": 0.0,  # patience 30 s: the child rests for the whole window
    }
    base.update(kw)
    return ParentOrder(**base)


def run(parents, events):
    return ExecutionReplay(algo_config(), list(parents)).run(events)


def signal(*readings):
    return ArraySignalSource({INS: [(ts, z, z * 1e-4) for ts, z in readings]})


ADVERSE_AT_5S = signal((T0 + 5 * SEC, 3.0))  # a buy: z > 0 = the price is going up... adverse


# ------------------------------------------------------- rule functions --


def test_rule_functions_are_plain_and_one_sided():
    assert book_pressure(100, 900) == pytest.approx(0.9)
    assert book_pressure(0, 0) == 0.5
    # a buy (side 0): z > 0 is adverse; the signal leg fires before the pressure leg
    assert fade_rule(0, 2.0, 1.0, 0.95, 0.8) == "fade:signal"
    assert fade_rule(0, -2.0, 1.0, 0.95, 0.8) == "fade:pressure"
    assert fade_rule(0, -2.0, 1.0, 0.7, 0.8) == ""
    assert fade_rule(1, -2.0, 1.0, None, 0.0) == "fade:signal"
    assert fade_rule(0, 5.0, 0.0, None, 0.0) == ""  # both legs off
    assert fade_rule(0, None, 1.0, None, 0.8) == ""
    assert queue_cancel_rule(1500, 1000, 0.9, 0.5) == "queue:ahead"
    assert queue_cancel_rule(500, 1000, 0.1, 0.5) == "queue:p_fill"
    assert queue_cancel_rule(500, 0, None, 0.5) == ""
    assert queue_cancel_rule(500, 1000, 0.6, 0.5) == ""
    assert set(CANCEL_RULES) == {"fade:signal", "fade:pressure", "queue:ahead", "queue:p_fill"}


def test_params_are_validated():
    with pytest.raises(ValueError):
        FadeParams(threshold_z=0.0, pressure_threshold=0.0)
    with pytest.raises(ValueError):
        FadeParams(pressure_threshold=0.4)
    with pytest.raises(ValueError):
        FadeParams(action="wait")
    with pytest.raises(ValueError):
        QueueCancelParams()
    with pytest.raises(ValueError):
        QueueCancelParams(min_p_fill=0.5)  # needs a hazard
    with pytest.raises(ValueError):
        QueueCancelParams(max_ahead_qty=10, action="hold")
    evs = book([])
    # the rules act on posted children: NATIVE without alpha urgency is refused
    with pytest.raises(ValueError):
        run([parent(policy=ExecPolicy.NATIVE, fade=FadeParams(pressure_threshold=0.8))], evs)
    # the signal leg needs a source
    with pytest.raises(ValueError):
        run([parent(fade=FadeParams(threshold_z=1.0))], evs)
    assert not ParentOrder().pulls_resting


# ------------------------------------------------------------- default off --


def test_passive_parent_without_opt_ins_is_unchanged_with_a_signal_attached():
    evs = book([T0 + 3 * SEC + SEC // 4])
    a = run([parent()], evs)
    b = run([parent(signal_source=ADVERSE_AT_5S)], evs)
    assert [f.to_dict() for f in a.fills] == [f.to_dict() for f in b.fills]
    assert a.pulls == [] and b.pulls == []
    assert a.parents[1].diagnostics()["pulls"] == {}
    assert all(o.cancel_rule == "" and o.replaces_order_id == 0 for o in a.children[1])
    assert a.parents[1].filled_qty == 100 and a.fills[0].liquidity == Liquidity.MAKER


# ------------------------------------------------------------------- B4 --


def test_fade_pulls_through_the_cancel_latency_holds_then_reposts_when_clear():
    """The signal turns adverse at 5 s: the resting bid is pulled (the cancel
    takes effect one latency later), the remainder is held off the book
    (a hit at 6 s finds nothing of ours), the signal clears at 8 s and the
    quantity is re-posted; the re-post fills at 10 s and is typed
    cancelled_replaced."""
    evs = book([T0 + 6 * SEC + SEC // 4, T0 + 10 * SEC + SEC // 4])
    src = signal((T0 + 5 * SEC, 3.0), (T0 + 8 * SEC, 0.0))
    fp = FadeParams(threshold_z=1.0, max_age_ns=0)
    res = run([parent(signal_source=src, fade=fp)], evs)
    pulls = res.pulls
    assert len(pulls) == 1
    rec = pulls[0]
    assert rec.rule == "fade:signal" and rec.decision_ts == T0 + 5 * SEC
    assert rec.effect_ts == rec.decision_ts + LATENCY  # rule 7: the cancel travelled
    assert rec.outcome == "pulled" and rec.pulled_qty == 100 and rec.filled_in_flight == 0
    assert rec.signal_z == 3.0 and rec.ahead_qty == 200
    kids = res.children[1]
    assert kids[0].cancel_rule == "fade:signal" and kids[0].cancel_rule_ts == T0 + 5 * SEC
    assert kids[0].state.name == "CANCELLED" and kids[0].cancel_reason.name == "USER"
    # nothing of ours filled at the 6 s hit (held off the book)
    assert all(f.ts > T0 + 8 * SEC for f in res.fills)
    # the re-post replaces the pulled child, is a LIMIT, and its fill is typed
    assert len(kids) == 2 and kids[1].replaces_order_id == kids[0].order_id
    assert kids[1].type == OrderType.LIMIT and kids[1].decision_ts == T0 + 8 * SEC
    assert res.parents[1].filled_qty == 100
    orders = {o.order_id: o for o in kids}
    assert {fill_type(f, orders[f.order_id]) for f in res.fills} == {"cancelled_replaced"}
    d = res.parents[1].diagnostics()
    assert d["pulls"] == {"fade:signal": 1} and d["pulled_qty"] == {"fade:signal": 100}
    assert d["pulls_lost_race"] == 0 and d["held_unsent_qty"] == 0


def test_fade_cancel_can_lose_the_race_and_the_fill_is_typed_faded():
    """A marketable sell lands 1 µs after the fade decision, before the
    cancel can arrive: the child fills in full (simulator rule 7 — the
    cancel is a no-op on a terminal order), the record says lost_race and
    the fill is a *faded* fill."""
    evs = book([T0 + 5 * SEC + 1_000])
    res = run([parent(signal_source=ADVERSE_AT_5S, fade=FadeParams(max_age_ns=0))], evs)
    assert len(res.pulls) == 1
    rec = res.pulls[0]
    assert rec.outcome == "lost_race" and rec.pulled_qty == 0 and rec.filled_in_flight == 100
    assert rec.effect_ts > rec.decision_ts
    assert res.parents[1].filled_qty == 100 and res.parents[1].pulls_lost_race == 1
    o = res.children[1][0]
    assert o.state.name == "FILLED" and o.cancel_rule == "fade:signal"
    assert fill_type(res.fills[0], o) == "faded"
    assert res.fills[0].ts == T0 + 5 * SEC + 1_000 and res.fills[0].liquidity == Liquidity.MAKER


def test_held_quantity_crosses_at_the_end_margin_and_counts_as_committed():
    """The signal never clears: the held remainder is crossed at
    ``end_ts - end_margin_ns`` (the PASSIVE rule for a remainder), so a
    fade never leaves quantity silently unsent inside the window."""
    evs = book([])
    res = run(
        [
            parent(
                signal_source=ADVERSE_AT_5S,
                fade=FadeParams(max_age_ns=0),
                end_ts=T0 + 20 * SEC,
                # events are 1 s apart here: a cross inside the last second would
                # expire before the event that activates it (rule 9: expiries first)
                passive=PassiveParams(end_margin_ns=2 * SEC),
            )
        ],
        evs,
    )
    assert res.pulls[0].outcome == "pulled"
    kids = res.children[1]
    assert len(kids) == 2 and kids[1].type == OrderType.MARKET
    assert kids[1].decision_ts == T0 + 20 * SEC - 2 * SEC  # the end margin
    assert kids[1].replaces_order_id == kids[0].order_id
    assert res.parents[1].filled_qty == 100 and res.fills[0].liquidity == Liquidity.TAKER
    assert fill_type(res.fills[0], kids[1]) == "aggressive"
    assert res.parents[1].held_unsent_qty == 0


def test_fade_actions_repost_and_cross_and_max_fades():
    evs = book([])
    src = ADVERSE_AT_5S
    cross = run([parent(signal_source=src, fade=FadeParams(action="cross", max_age_ns=0))], evs)
    k = cross.children[1]
    assert len(k) == 2 and k[1].type == OrderType.MARKET
    assert k[1].decision_ts == T0 + 6 * SEC  # the first visit after the cancel resolved
    repost = run(
        [parent(signal_source=src, fade=FadeParams(action="repost", max_fades=2, max_age_ns=0))],
        evs,
    )
    k = repost.children[1]
    # re-posted at once, faded again at the next visit, then (max_fades) rests
    rules = [o.cancel_rule for o in k]
    assert rules[:2] == ["fade:signal", "fade:signal"] and rules[-1] == ""
    assert len(repost.pulls) == 2 and all(o.type == OrderType.LIMIT for o in k)
    assert repost.parents[1].diagnostics()["pulls"] == {"fade:signal": 2}


def test_pressure_fade_needs_no_signal_and_reads_the_venue_touch():
    """Ask side nine times the bid side: pressure 0.9 >= 0.8 pulls at once;
    a balanced book never does."""
    heavy = book([], ask_top=1800)
    fp = FadeParams(threshold_z=0.0, pressure_threshold=0.8)
    res = run([parent(fade=fp)], heavy)
    assert res.pulls and res.pulls[0].rule == "fade:pressure"
    assert res.pulls[0].pressure == pytest.approx(0.9)
    assert res.children[1][0].cancel_rule == "fade:pressure"
    calm = run([parent(fade=fp)], book([]))
    assert calm.pulls == []


def test_pov_parent_counts_held_qty_as_committed():
    """A held fade must not make the POV deficit re-issue the same quantity."""
    evs = book([], seconds=40)
    p = parent(
        algo=AlgoType.POV,
        participation=0.5,
        qty=100,
        max_child_qty=20,
        signal_source=ADVERSE_AT_5S,
        fade=FadeParams(max_age_ns=0),
        end_ts=T0 + 30 * SEC,
    )
    res = run([p], evs)
    sent = sum(o.qty for o in res.children[1] if o.replaces_order_id == 0)
    assert sent <= 100  # fresh children never exceed the parent qty
    assert res.parents[1].filled_qty + res.parents[1].unfilled_qty == 100


# ------------------------------------------------------------------- B5 --


def test_queue_ahead_cancel_crosses_the_hopeless_queue():
    evs = book([T0 + 3 * SEC + SEC // 4], bid_top=5_000)
    qp = QueueCancelParams(max_ahead_qty=1_000)
    res = run([parent(queue_cancel=qp)], evs)
    rec = res.pulls[0]
    assert rec.rule == "queue:ahead" and rec.ahead_qty == 5_000 and rec.outcome == "pulled"
    k = res.children[1]
    assert k[0].cancel_rule == "queue:ahead" and k[1].type == OrderType.MARKET
    assert k[1].replaces_order_id == k[0].order_id
    assert res.parents[1].filled_qty == 100 and res.fills[0].liquidity == Liquidity.TAKER
    # the same book with a short queue: no pull, the hit fills us passively
    calm = run([parent(queue_cancel=qp)], book([T0 + 3 * SEC + SEC // 4]))
    assert calm.pulls == [] and calm.fills[0].liquidity == Liquidity.MAKER
    # repost action: cancel then post again (a LIMIT), not a cross
    rp = run([parent(queue_cancel=replace(qp, action="repost"))], evs)
    assert rp.children[1][1].type == OrderType.LIMIT


def test_queue_p_fill_cancel_uses_the_hazard_on_the_models_clock():
    model = FillHazardModel(clock="America/New_York").fit(planted_samples(n=4000))
    qp = QueueCancelParams(
        min_p_fill=0.5, horizon_ns=10 * SEC, hazard=model, depletion_per_s=lambda side, q: 0.0
    )
    evs = book([], bid_top=5_000)
    res = run([parent(queue_cancel=qp)], evs)
    rec = res.pulls[0]
    assert rec.rule == "queue:p_fill" and 0.0 <= rec.p_fill < 0.5
    # the probability the replay used is the model's on its own clock
    x = model.features(queue_ahead=5_000, spread_ticks=1, imbalance=5_000 / 5_200, ts_ns=T0)
    assert rec.p_fill == pytest.approx(float(model.p_fill_within(x, 10 * SEC)))
    # a thin queue has a high probability: no cancel
    assert run([parent(queue_cancel=qp)], book([], bid_top=10)).pulls == []
    # a grace period delays the rule
    late = run([parent(queue_cancel=replace(qp, min_rest_ns=4 * SEC))], evs)
    assert late.pulls[0].decision_ts >= T0 + 4 * SEC


def test_calibration_depletion_split_is_opt_in_and_attributes_the_emptying_event():
    """One touch level emptied by an EXECUTE (filled ahead), one by a CANCEL
    (left ahead): the split counts them apart; the default document has no
    split keys."""
    evs = [
        _ev(1, T0, EventType.ADD, 1, 105, 100, 900),  # a standing ask
        _ev(2, T0 + 1, EventType.ADD, 0, 100, 100, 1),
        _ev(3, T0 + SEC, EventType.EXECUTE, 0, 100, 100, 1),  # bid 100 traded through
        _ev(4, T0 + 2 * SEC, EventType.ADD, 0, 100, 100, 2),
        _ev(5, T0 + 4 * SEC, EventType.CANCEL, 0, 100, 100, 2),  # bid 100 cancelled away
        _ev(6, T0 + 5 * SEC, EventType.ADD, 0, 100, 100, 3),
        _ev(7, T0 + 9 * SEC, EventType.HEARTBEAT, 0, 0, 0, 0),
    ]
    plain = estimate_calibration(evs, {INS: 0.01}, queue_edges=(0, 500))
    cell = plain["queue_depletion"]["bid"]["1-500"]
    assert cell["depletions"] == 2 and "exec_depletions" not in cell
    split = estimate_calibration(evs, {INS: 0.01}, queue_edges=(0, 500), depletion_split=True)
    cell = split["queue_depletion"]["bid"]["1-500"]
    assert cell["depletions"] == 2
    assert cell["exec_depletions"] == 1 and cell["cancel_depletions"] == 1
    assert cell["exec_share"] == pytest.approx(0.5)
    # exposure: 1 s (exec episode) + 2 s (cancel episode) + 4 s censored = 7 s
    assert cell["exposure_s"] == pytest.approx(7.0)
    assert cell["exec_hazard_per_s"] == pytest.approx(1 / 7)
    assert cell["cancel_hazard_per_s"] == pytest.approx(1 / 7)
    assert cell["hazard_per_s"] == pytest.approx(2 / 7)
    cal = ExecCalibration.from_dict(split)
    assert cal.queue_depletion_hazard(0, 10) == pytest.approx(2 / 7)
    assert cal.queue_exec_depletion_hazard(0, 10) == pytest.approx(1 / 7)  # X-4: the fill rate
    assert cal.queue_cancel_depletion_hazard(0, 10) == pytest.approx(1 / 7)
    assert ExecCalibration.from_dict(plain).queue_exec_depletion_hazard(0, 10) is None
    # every pre-existing key of the default document is unchanged by the split
    for k in plain:
        if k != "queue_depletion":
            assert split[k] == plain[k]


# ------------------------------------------------------------------- B6 --


def _tl(states):
    tl = MarketTimeline()
    for ts, bid, ask in states:
        tl.append(ts, bid, ask, 100, 100)
    return tl


def test_pull_outcomes_split_saved_from_missed_with_the_touch_through_proxy():
    """Two pulled buys at 100.00: after the first the ask falls to 100 and the
    mid ends at 99.5 (the fill would have been adverse: saved 50 bps); after
    the second the ask falls to 100 and the mid ends at 100.5 (a favourable
    fill forgone: missed 50 bps); a third pull never sees the ask reach 100
    (would not have filled); a fourth is still in flight (ignored)."""
    tl = _tl(
        [
            (T0, 100.0, 100.01),
            (T0 + 1 * SEC, 99.99, 100.0),  # ask reaches 100: pull 1 would have filled
            (T0 + 2 * SEC, 99.0, 100.0),  # 1 s after pull 1: mid 99.5
            (T0 + 11 * SEC, 99.99, 100.0),  # pull 2 would have filled
            (T0 + 12 * SEC, 100.0, 101.0),  # mid 100.5
            (T0 + 21 * SEC, 100.0, 100.01),  # pull 3: ask never reaches 100
            (T0 + 23 * SEC, 100.0, 100.01),
        ]
    )

    def rec(oid, effect, outcome="pulled", pulled=100, rule="fade:signal"):
        return PullRecord(
            1,
            oid,
            0,
            10_000,
            100,
            effect - 1000,
            rule,
            effect_ts=effect,
            pulled_qty=pulled,
            outcome=outcome,
        )

    pulls = [
        rec(1, T0 + 1 * SEC),
        rec(2, T0 + 11 * SEC),
        rec(3, T0 + 21 * SEC),
        PullRecord(1, 4, 0, 10_000, 100, T0 + 22 * SEC, "fade:signal"),  # unresolved
        rec(5, T0 + 1 * SEC, outcome="lost_race", pulled=0),
        rec(6, T0 + 1 * SEC, rule="queue:ahead"),  # not a fade: excluded by default
    ]
    out = pull_outcomes(pulls, tl, 0.01, horizons_ns={"1s": SEC})
    assert out["n_pulls"] == 3 and out["pulled_qty"] == 300 and out["lost_race"] == 1
    h = out["horizons"]["1s"]
    assert h["n"] == 3 and h["would_fill"] == 2 and h["would_fill_qty"] == 200
    assert h["saved"]["n"] == 1 and h["saved"]["bps_mean"] == pytest.approx(50.0)
    assert h["saved"]["ccy"] == pytest.approx(0.5 * 100)
    assert h["missed"]["n"] == 1 and h["missed"]["bps_mean"] == pytest.approx(50.0)
    assert h["missed"]["ccy"] == pytest.approx(0.5 * 100)
    assert h["net_ccy"] == pytest.approx(0.0) and h["net_bps"] == pytest.approx(0.0)
    assert "counterfactual" in out["label"]
    # the queue rule is summarised when asked for, and rows pool across calls
    q = pull_outcomes(pulls, tl, 0.01, horizons_ns={"1s": SEC}, rules=("queue:ahead",))
    assert q["n_pulls"] == 1 and q["horizons"]["1s"]["saved"]["n"] == 1
    rows = pull_rows(pulls, tl, 0.01, horizons_ns={"1s": SEC}, cluster="d1")
    rows += pull_rows(pulls, tl, 0.01, horizons_ns={"1s": SEC}, cluster="d2")
    pooled = pull_summary(rows, ["1s"])
    assert pooled["n_pulls"] == 6 and pooled["horizons"]["1s"]["saved"]["ccy"] == pytest.approx(100)


def _fill(fid, oid, ts, price_ticks, liq, side=0, qty=100):
    return Fill(fid, oid, 1, INS, 1, side, price_ticks, qty, ts, liq, 0.0, 0.0)


def test_fill_type_and_regime_follow_the_child_provenance():
    plain = ChildOrder(order_id=1)
    faded = ChildOrder(order_id=2, cancel_rule="fade:signal", cancel_rule_ts=T0 + 5)
    repl = ChildOrder(order_id=3, replaces_order_id=1, urgency_regime="agree")
    both = ChildOrder(order_id=4, replaces_order_id=1, cancel_rule="queue:ahead", cancel_rule_ts=T0)
    assert fill_type(_fill(1, 1, T0, 10_000, Liquidity.MAKER), plain) == "passive"
    assert fill_type(_fill(1, 1, T0, 10_000, Liquidity.TAKER), plain) == "aggressive"
    assert fill_type(_fill(1, 2, T0 + 5, 10_000, Liquidity.MAKER), faded) == "faded"
    assert fill_type(_fill(1, 2, T0 + 4, 10_000, Liquidity.MAKER), faded) == "passive"  # before
    assert fill_type(_fill(1, 3, T0, 10_000, Liquidity.MAKER), repl) == "cancelled_replaced"
    assert fill_type(_fill(1, 4, T0 + 1, 10_000, Liquidity.MAKER), both) == "faded"  # lost race
    assert fill_type(_fill(1, 3, T0, 10_000, Liquidity.TAKER), repl) == "aggressive"
    assert set(FILL_TYPES) == {"passive", "aggressive", "faded", "cancelled_replaced", "dark"}


def test_cluster_mean_is_the_cr1_day_cluster_statistic():
    # two clusters with residual sums -2 and +2 over 4 values
    s = cluster_mean([1.0, 2.0, 3.0, 4.0], ["a", "a", "b", "b"])
    assert s["mean"] == 2.5 and s["n_clusters"] == 2 and s["df"] == 1
    # G/(G-1) * sum(sum_g resid)^2 / N^2 = 2 * (4 + 4) / 16 = 1 -> se 1
    assert s["se"] == pytest.approx(1.0) and s["t"] == pytest.approx(2.5)
    assert s["ci95"][0] < 2.5 < s["ci95"][1] and 0 < s["p_student_t"] < 1
    one = cluster_mean([1.0, 2.0], ["a", "a"])
    assert one["mean"] == 1.5 and one["se"] is None and one["n_clusters"] == 1
    assert cluster_mean([], [])["mean"] is None
    with pytest.raises(ValueError):
        cluster_mean([1.0], [])


def test_markout_decomposition_by_type_and_regime_with_day_clusters():
    """Two days of hand-built fills: passive buys at the bid whose mid then
    falls (adverse), aggressive buys at the ask whose mid then rises, one
    faded fill; the SEs cluster on the day, so a type seen on one day only
    has no SE."""
    day = 86_400 * SEC
    states = []
    for d in (0, 1):
        t = T0 + d * day
        states += [
            (t - SEC, 100.0, 100.02),  # the pre-fill state a maker fill is measured against
            (t, 100.0, 100.02),
            (t + SEC, 99.9, 99.92),
            (t + 10 * SEC, 100.2, 100.22),
        ]
    tl = _tl(states)
    orders = {
        1: ChildOrder(order_id=1, urgency_regime="agree"),
        2: ChildOrder(order_id=2),  # plain regime
        3: ChildOrder(order_id=3, cancel_rule="fade:signal", cancel_rule_ts=T0),
    }
    fills = []
    for d in (0, 1):
        t = T0 + d * day
        fills.append(_fill(1 + 3 * d, 1, t, 10_000, Liquidity.MAKER))  # passive buy at 100.00
        fills.append(_fill(2 + 3 * d, 2, t, 10_002, Liquidity.TAKER))  # aggressive buy at 100.02
    fills.append(_fill(9, 3, T0, 10_000, Liquidity.MAKER))  # faded, day 0 only
    hz = {"1s": SEC, "10s": 10 * SEC}
    rows = fill_rows(fills, orders, tl, 0.01, horizons_ns=hz)
    assert {r["cluster"] for r in rows} == {str(T0 // day), str((T0 + day) // day)}
    d = decompose(rows, ["1s", "10s"])
    assert d["all"]["n_fills"] == 5 and set(d["by_fill_type"]) == {"passive", "aggressive", "faded"}
    assert d["fill_type_share_of_qty"]["passive"] == pytest.approx(0.4)
    passive = d["by_fill_type"]["passive"]["horizons"]
    # bought at 100.00, mid 99.91 one second later: -9 bps on both days, SE 0
    assert passive["1s"]["markout"]["mean"] == pytest.approx(-9.0)
    assert passive["1s"]["markout"]["n_clusters"] == 2 and passive["1s"]["markout"]["se"] == 0.0
    # price impact of a maker fill vs the pre-fill mid 100.01: -10 bps (adverse selection +10)
    assert passive["1s"]["price_impact"]["mean"] == pytest.approx(-10.0, abs=1e-6)
    aggressive = d["by_fill_type"]["aggressive"]["horizons"]["10s"]["markout"]
    assert aggressive["mean"] == pytest.approx(1e4 * (100.21 - 100.02) / 100.02)
    faded = d["by_fill_type"]["faded"]["horizons"]["1s"]["markout"]
    assert faded["n"] == 1 and faded["n_clusters"] == 1 and faded["se"] is None
    assert set(d["by_regime"]) == {"agree", "plain"}
    assert "passive|agree" in d["by_fill_type_and_regime"]
    assert "aggressive|plain" in d["by_fill_type_and_regime"]
    one = markout_decomposition(fills, orders, tl, 0.01, horizons_ns=hz)
    assert one["all"]["horizons"]["1s"]["markout"]["n_clusters"] == 2
    assert "clustered by day" in d["inference"]


def test_hazard_clock_is_exchange_local_and_named_in_the_result(tmp_path):
    # 2019-01-15 14:30:00 UTC = 09:30 New York (EST)
    ts = 1_547_562_600 * SEC
    assert time_of_day(ts) == pytest.approx(14.5 / 24)
    assert time_of_day(ts, "America/New_York") == pytest.approx(9.5 / 24)
    # 2019-07-15 13:30:00 UTC = 09:30 New York (EDT): local time is DST-stable
    ts_summer = 1_563_197_400 * SEC
    assert time_of_day(ts_summer) == pytest.approx(13.5 / 24)
    assert time_of_day(ts_summer, "America/New_York") == pytest.approx(9.5 / 24)
    # an IEX capture stamp is TAI, 37 s ahead of UTC
    assert time_of_day(ts + IEX_TAI_OFFSET_S * SEC, "America/New_York", IEX_TAI_OFFSET_S) == (
        pytest.approx(9.5 / 24)
    )
    x = hazard_features(queue_ahead=1, spread_ticks=1, ts_ns=ts, clock="America/New_York")
    assert x["tod"] == pytest.approx(9.5 / 24)
    with pytest.raises(ValueError):
        FillHazardModel(clock="Mars/Olympus")
    s = planted_samples(n=3000)
    utc = FillHazardModel().fit(s)
    ny = FillHazardModel(clock="America/New_York", capture_offset_s=IEX_TAI_OFFSET_S).fit(s)
    assert utc.to_dict()["clock"] == "utc" and utc.to_dict()["capture_offset_s"] == 0
    assert ny.to_dict()["clock"] == "America/New_York"
    assert ny.to_dict()["capture_offset_s"] == 37
    assert ny.features(queue_ahead=1, spread_ticks=1, ts_ns=ts + 37 * SEC)["tod"] == (
        pytest.approx(9.5 / 24)
    )
    ny.save(tmp_path / "h.json")
    back = FillHazardModel.load(tmp_path / "h.json")
    assert back.clock == "America/New_York" and back.capture_offset_s == 37
    assert back.to_json() == ny.to_json()
    # a v1.12 document without the keys is UTC
    doc = json.loads(utc.to_json())
    del doc["clock"], doc["capture_offset_s"]
    assert FillHazardModel.from_dict(doc).clock == "utc"


# ------------------------------------------------------------ benchmark --


@pytest.fixture(scope="module")
def dataset(tmp_path_factory, golden_dir) -> Path:
    root = tmp_path_factory.mktemp("bench_b456")
    norm = root / "normalized"
    norm.mkdir()
    for day in ("20190101", "20190102"):
        shutil.copy(golden_dir / "events_eq_mbo.jsonl", norm / f"eq_{day}.normalized.jsonl")
    return root


def _run(dataset, out, *more):
    return bench.main(
        [
            "--data-dir",
            str(dataset),
            "--config-dir",
            str(REPO / "configs"),
            "--out",
            str(out),
            "--sizes-pct",
            "0.001",
            "--algos",
            "TWAP,POV",
            "--window-min",
            "1.5",
            "--warmup-min",
            "0.5",
            "--no-timestamp",
            *more,
        ]
    )


def test_driver_runs_the_pull_variants_and_reports_fades_and_markouts(dataset, tmp_path):
    out = tmp_path / "out"
    rc = _run(
        dataset,
        out,
        "--policies",
        "passive,passive+fade,passive+qcancel,passive+fade_qcancel",
        "--baseline-policy",
        "passive",
        "--fade-pressure",
        "0.55",
        "--qcancel-max-ahead",
        "1",
        "--markout-horizons",
        "1s,5s",
    )
    assert rc == 0
    doc = json.loads((out / "benchmark_bench_b4560.json").read_text())
    assert doc["pull_rules"]["fade"]["pressure_threshold"] == 0.55
    assert doc["pull_rules"]["queue_cancel"]["max_ahead_qty"] == 1
    assert doc["pull_rules"]["queue_cancel"]["depletion_rate"].startswith("none")
    assert doc["markout_horizons"] == ["1s", "5s"] and doc["fill_types"] == list(FILL_TYPES)
    keys = set(doc["paired_vs_baseline"])
    assert "passive+fade-passive|TWAP|0.001" in keys
    assert "passive+qcancel-passive|POV|0.001" in keys
    assert "passive+fade_qcancel-passive|TWAP|0.001" in keys
    # the queue rule at 1 share ahead pulls on the golden stream and crosses
    qc = doc["cells"]["passive+qcancel|TWAP|0.001"]
    assert qc["pulls"]["children"].get("queue:ahead", 0) > 0
    assert qc["fill_type_share_of_filled"].get("aggressive", 0) > 0
    assert sum(qc["fill_type_share_of_filled"].values()) == pytest.approx(1.0)
    assert doc["cells"]["passive|TWAP|0.001"]["pulls"]["children"] == {}
    fades = doc["fades_saved_vs_fills_missed"]
    assert "passive+qcancel" in fades and fades["passive+qcancel"]["queue"]["n_pulls"] > 0
    q1 = fades["passive+qcancel"]["queue"]["horizons"]["1s"]
    assert set(q1) >= {"n", "would_fill", "saved", "missed", "net_bps"}
    mkd = doc["markout_decomposition"]
    assert set(mkd) == set(doc["grid"]["policies"])
    cell = mkd["passive"]["all"]["horizons"]["1s"]["markout"]
    assert cell["n"] > 0 and cell["n_clusters"] == 2 and cell["se"] is not None
    assert set(mkd["passive+qcancel"]["by_fill_type"]) <= set(FILL_TYPES)
    md = (out / "BENCHMARK_bench_b4560.md").read_text(encoding="utf-8")
    assert "Fades saved vs fills missed" in md and "Markout by fill type" in md
    # the plain passive cells are byte-identical to a run without the variants
    out2 = tmp_path / "out2"
    assert (
        _run(
            dataset,
            out2,
            "--policies",
            "passive",
            "--baseline-policy",
            "passive",
            "--markout-horizons",
            "1s,5s",
        )
        == 0
    )
    doc2 = json.loads((out2 / "benchmark_bench_b4560.json").read_text())
    for k in doc2["cells"]:
        a, b = doc["cells"][k], doc2["cells"][k]
        assert a["costs_bps"] == b["costs_bps"] and a["fill_rate"] == b["fill_rate"]
    assert doc2["markout_decomposition"]["passive"] == mkd["passive"]
    assert doc2["fades_saved_vs_fills_missed"] == {}
    draft = out2 / "prereg_DRAFT.json"
    assert not draft.exists()


def test_driver_refuses_misuse_of_the_pull_variants(dataset, tmp_path):
    with pytest.raises(SystemExit):
        _run(dataset, tmp_path / "a", "--policies", "native,native+fade", "--fade-pressure", "0.6")
    with pytest.raises(SystemExit):
        _run(dataset, tmp_path / "b", "--policies", "passive,passive+fade")  # no knob
    with pytest.raises(SystemExit):
        _run(
            dataset,
            tmp_path / "c",
            "--policies",
            "passive,passive+fade",
            "--fade-threshold-z",
            "1.0",  # the signal leg without --signal
        )
    with pytest.raises(SystemExit):
        _run(dataset, tmp_path / "d", "--policies", "passive", "--markout-horizons", "7s")

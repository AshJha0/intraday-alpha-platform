"""v2.0 stage 2 D4: latency honesty (D-06) and dated fees (D-07).

The replay's market-data delay is never silently zero (its default is the
DECLARED value of ``configs/reference/venue_latency.json`` and the result
records which value and which ``receive_ts`` relationship applied); every
execution number of the replay and of the engine's execution model can be
reported at the plan 5.9 grid (0.5 s, 1 s, 2 s, 5 s); every fee comes from a
dated schedule and an undated one cannot be used.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import REPO_ROOT
from iap.core.events import EventType
from iap.execution import latency as lat
from iap.execution.child_exec import ChildExecParams
from iap.execution.fees import (
    FEE_SCHEDULES_PATH,
    UndatedFeeError,
    load_fee_schedule,
    load_fee_schedules,
    require_as_of,
)
from iap.execution.realism import load_fee_tiers
from iap.execution.replay import ExecutionReplay
from iap.execution.sim import (
    COLUMNS,
    LATENCY_NS,
    LATENCY_STEPS_NS,
    FeeSchedule,
    Market,
    Quote,
    SimError,
    expected_cost_bps,
    expected_cost_sensitivity,
    simulate,
    simulate_columns,
    simulate_sensitivity,
)
from iap.execution.venues_model import load_venue_model_config
from test_rel04_child_execution import Feeder, _beats, _book, _parent, make_config

T0 = 1_700_000_000_000_000_000
MS = 1_000_000
STEPS = ("0.5s", "1s", "2s", "5s")


# ------------------------------------------------------------ declared feed delay


def test_declared_feed_delay_is_positive_and_states_receive_ts_per_route():
    fd = lat.declared_feed_delay()
    assert fd.ns > 0 and fd.label == "assumed" and "venue_latency.json" in fd.source
    doc = json.loads(lat.VENUE_LATENCY_PATH.read_text(encoding="utf-8"))
    routes = doc["feed_delay"]["routes"]
    assert {"itch50", "iex-deep-plus", "iex-deep", "iex-capture", "synthetic"} <= set(routes)
    for name in routes:
        row = lat.declared_feed_delay(name)
        assert row.ns > 0 and "receive_ts" in row.receive_ts and row.route == name
    # the exchange-stamp routes say receive_ts == exchange_ts; the capture route says it differs
    assert "receive_ts == exchange_ts" in lat.declared_feed_delay("itch50").receive_ts
    assert "capture host" in lat.declared_feed_delay("iex-capture").receive_ts
    assert "no absolute feed latency" in lat.declared_feed_delay("iex-capture").receive_ts
    with pytest.raises(KeyError):
        lat.declared_feed_delay("not-a-route")


def test_a_zero_or_missing_declared_default_is_refused(tmp_path):
    doc = json.loads(lat.VENUE_LATENCY_PATH.read_text(encoding="utf-8"))
    for bad in (0, -5, None, 1.5):
        d = json.loads(json.dumps(doc))
        d["feed_delay"]["default_ns"] = bad
        p = tmp_path / "vl.json"
        p.write_text(json.dumps(d))
        with pytest.raises(ValueError, match="default_ns|silent zero"):
            lat.declared_feed_delay(path=p)
    d = json.loads(json.dumps(doc))
    del d["feed_delay"]
    p = tmp_path / "vl2.json"
    p.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="feed_delay"):
        lat.declared_feed_delay(path=p)


def test_engine_latency_constant_equals_the_declared_table():
    doc = json.loads(lat.VENUE_LATENCY_PATH.read_text(encoding="utf-8"))
    assert LATENCY_NS == doc["engine_decision_to_arrival_ns"] == 500_000_000
    assert list(LATENCY_STEPS_NS) == doc["sensitivity_steps_ns"] == list(lat.SENSITIVITY_STEPS_NS)
    assert [lat.step_label(s) for s in LATENCY_STEPS_NS] == list(STEPS)


# ------------------------------------------------------------ the replay default


def test_replay_default_delay_is_the_declared_value_not_zero_and_is_recorded():
    f = Feeder()
    ev = _book(f) + _beats(f, 0, 5)
    declared = lat.declared_feed_delay().ns
    res = ExecutionReplay(make_config(), [_parent(ChildExecParams())]).run(ev)
    assert res.latency["md_delay_ns"] == declared > 0
    assert res.latency["source"].startswith("declared")
    assert "receive_ts" in res.latency and res.latency["label"] == "assumed"
    child = res.children[1][0]
    assert child.decision_ts == T0 + 10 + declared  # seen at the event, decided after the delay
    # an explicit zero is allowed, and recorded as an explicit optimistic assumption
    zero = ExecutionReplay(make_config(), [_parent(ChildExecParams())], md_delay_ns=0).run(ev)
    assert zero.latency["md_delay_ns"] == 0 and zero.latency["source"].startswith("explicit")
    assert "optimistic" in zero.latency["source"]
    assert zero.children[1][0].decision_ts == T0 + 10
    # a route picks its own statement
    r = ExecutionReplay(
        make_config(), [_parent(ChildExecParams())], md_delay_ns=0, md_route="iex-capture"
    ).run(ev)
    assert r.latency["route"] == "iex-capture" and "capture host" in r.latency["receive_ts"]
    with pytest.raises(ValueError):
        ExecutionReplay(make_config(), [_parent(ChildExecParams())], md_delay_ns=-1)


def test_parents_without_child_exec_are_unchanged_by_the_default_delay():
    """The goldens have no child_exec parent: the declared default moves nothing there."""
    f = Feeder()
    ev = _book(f) + [f.ev(T0 + 11, EventType.TRADE, 1, 101, 1000, 0)] + _beats(f, 5, 10)
    a = ExecutionReplay(make_config(), [_parent(None)]).run(ev)
    b = ExecutionReplay(make_config(), [_parent(None)], md_delay_ns=0).run(ev)
    assert a.fills == b.fills and a.parents[1] == b.parents[1]
    assert a.latency["md_delay_ns"] > 0 and b.latency["md_delay_ns"] == 0


def test_replay_sensitivity_reports_every_parent_at_the_four_steps():
    f = Feeder()
    ev = _book(f) + [f.ev(T0 + 11, EventType.TRADE, 1, 101, 1000, 0)] + _beats(f, 5, 10)

    def build(ns):
        return ExecutionReplay(make_config(), [_parent(ChildExecParams())], md_delay_ns=ns)

    sens = lat.replay_sensitivity(build, lambda: list(ev))
    assert sens["base_ns"] == lat.declared_feed_delay().ns
    assert set(sens["steps"]) == set(STEPS) and sens["steps_ns"]["5s"] == 5_000_000_000
    for rec in (sens["base"], *sens["steps"].values()):
        assert set(rec["parents"]) == {"1"}
        row = rec["parents"]["1"]
        assert {"filled_qty", "avg_price", "total_cost", "fees", "rebates", "impact"} <= set(row)
    table = lat.sensitivity_table(sens, "filled_qty")
    assert set(table["1"]) == {"base", *STEPS}
    # at the declared delay the child arrives inside the stream and fills; at 5 s it is
    # decided after the last event of this short stream, so nothing fills: the number moves
    assert table["1"]["base"] > 0 and table["1"]["5s"] == 0
    assert "not measured" in sens["statement"]


# ------------------------------------------------------------ the engine's execution model


def _quote(ts, mid=100.0, spread_bps=5.0, size=5000):
    s = mid * spread_bps / 1e4
    return Quote(ts=ts, bid=mid - s / 2, ask=mid + s / 2, bid_size=size, ask_size=size)


def test_simulate_sensitivity_carries_the_four_steps_beside_the_base_fill():
    def market_at(ns):
        # a quote timeline: the mid drifts 1 bp per second of latency
        mid = 100.0 * (1 + 1e-4 * ns / 1e9)
        return Market("AAA", _quote(T0 + ns, mid), 200_000, False)

    sens = simulate_sensitivity(
        ts_decision=T0, name="AAA", qty=1000, mid_decision=100.0, market_at=market_at, market=None
    )
    assert sens["base_ns"] == LATENCY_NS and set(sens["steps"]) == set(STEPS)
    base = sens["base"]
    assert base.filled == 1000 and base.arrival_ts == T0 + LATENCY_NS
    assert sens["steps"]["0.5s"].fill.price == base.fill.price  # the base IS the 0.5 s step
    prices = [sens["steps"][k].fill.price for k in STEPS]
    assert prices == sorted(prices) and prices[-1] > prices[0]  # later arrival, worse buy price
    assert sens["steps"]["5s"].arrival_ts == T0 + 5_000_000_000
    # without a timeline the one quote is met at every step: the stale rule refuses late arrivals
    one = Market("AAA", _quote(T0 - 1_000_000_000), 200_000, False)  # 1 s old at decision
    sens1 = simulate_sensitivity(
        ts_decision=T0, name="AAA", qty=1000, mid_decision=100.0, market=one
    )
    assert sens1["steps"]["0.5s"].filled == 1000
    assert sens1["steps"]["5s"].filled == 0 and "stale" in sens1["steps"]["5s"].reason


def test_expected_cost_sensitivity_and_the_hurdle_share_one_implementation():
    q = _quote(T0 + LATENCY_NS)
    sens = expected_cost_sensitivity(1000, lambda ns: q, 200_000)
    assert sens["base"] == expected_cost_bps(1000, q, 200_000)
    assert set(sens["steps"]) == set(STEPS)
    assert all(v == sens["base"] for v in sens["steps"].values())  # same quote at every step


# ------------------------------------------------------------ dated fees


def test_an_undated_fee_cannot_be_used():
    with pytest.raises(SimError, match="as_of"):
        FeeSchedule(per_share=0.001)
    with pytest.raises(SimError, match="ISO date"):
        FeeSchedule(per_share=0.001, dated="yesterday")
    with pytest.raises(SimError):
        FeeSchedule(per_share=0.001, dated="2026-13-01")
    ok = FeeSchedule(per_share=0.001, dated="2026-10-25")
    assert ok.fee(100, 50.0) == pytest.approx(0.1)
    with pytest.raises(UndatedFeeError):
        require_as_of({}, "x")
    with pytest.raises(UndatedFeeError):
        require_as_of({"as_of": 20261025}, "x")


def test_fee_schedule_table_is_dated_labelled_and_sourced(tmp_path):
    table = load_fee_schedules()
    assert "engine_default" in table
    for name, row in table.items():
        assert row["as_of"] and row["label"] in ("quoted", "indicative", "assumed"), name
        assert row["source"].strip(), name
    default = FeeSchedule.default()
    assert default.per_share == 0.0005 and default.dated == table["engine_default"]["as_of"]
    assert default.label == "assumed"  # a planning value, never presented as quoted
    # a schedule without a date, label or source is refused at load
    doc = json.loads(FEE_SCHEDULES_PATH.read_text(encoding="utf-8"))
    for strip in ("as_of", "label", "source"):
        d = json.loads(json.dumps(doc))
        del d["schedules"]["engine_default"][strip]
        p = tmp_path / f"fees_{strip}.json"
        p.write_text(json.dumps(d))
        with pytest.raises(UndatedFeeError):
            load_fee_schedule("engine_default", path=p)
    with pytest.raises(KeyError):
        load_fee_schedule("no_such_schedule")


def test_the_three_cost_columns_read_their_fees_from_the_table():
    """Default fees of simulate / simulate_columns / expected_cost_bps are the engine_default row."""
    mkt = Market("AAA", _quote(T0 + LATENCY_NS), 200_000, False)
    cols = simulate_columns(ts_decision=T0, name="AAA", qty=1000, mid_decision=100.0, market=mkt)
    table = load_fee_schedule("engine_default")
    for name, out in cols.items():
        assert name in COLUMNS and out.fill is not None
        assert out.fill.fee == pytest.approx(
            1000 * table["per_share"] + 1000 * out.fill.price * table["notional_rate"]
        )
    explicit = simulate(
        ts_decision=T0,
        name="AAA",
        qty=1000,
        mid_decision=100.0,
        market=mkt,
        fees=FeeSchedule.from_table("engine_default"),
    )
    assert explicit.fill.fee == cols["conservative"].fill.fee
    # another schedule changes the fee, nothing else
    other = replace(FeeSchedule.default(), per_share=0.001)
    o = simulate(ts_decision=T0, name="AAA", qty=1000, mid_decision=100.0, market=mkt, fees=other)
    assert (
        o.fill.fee == pytest.approx(2 * explicit.fill.fee) and o.fill.price == explicit.fill.price
    )


def test_exchange_fee_tiers_and_venue_model_carry_as_of_dates(tmp_path):
    tiers_path = REPO_ROOT / "configs" / "reference" / "fee_tiers.json"
    doc = json.loads(tiers_path.read_text(encoding="utf-8"))
    assert require_as_of(doc, "fee_tiers") == doc["as_of"] and doc["label"] == "indicative"
    assert load_fee_tiers()["top"]["taker_fee_per_share"] == 0.003
    undated = {k: v for k, v in doc.items() if k != "as_of"}
    p = tmp_path / "fee_tiers.json"
    p.write_text(json.dumps(undated))
    with pytest.raises(UndatedFeeError):
        load_fee_tiers(p)
    vm_path = REPO_ROOT / "research" / "execution" / "venue_model.json"
    vm = load_venue_model_config(vm_path)
    assert vm["as_of"] and vm["label"] == "assumed"
    undated_vm = {k: v for k, v in vm.items() if k != "as_of"}
    p2 = tmp_path / "venue_model.json"
    p2.write_text(json.dumps(undated_vm))
    with pytest.raises(UndatedFeeError):
        load_venue_model_config(p2)
    # the fee_tiers rows are mirrored, dated, in the schedule table
    sched = load_fee_schedules()
    for tier, name in (
        ("top", "xnas_access_fee_cap"),
        ("mid", "xnas_mid_tier"),
        ("base", "xnas_base_tier"),
    ):
        assert sched[name]["taker_fee_per_share"] == doc["tiers"][tier]["taker_fee_per_share"]
        assert sched[name]["maker_rebate_per_share"] == doc["tiers"][tier]["maker_rebate_per_share"]

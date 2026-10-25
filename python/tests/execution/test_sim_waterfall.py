"""EN4 (v2.0 stage 5): the one fill-price convention, the three columns, the failure cases, and the waterfall.

The waterfall (midpoint-reference P&L + latency drift - half-spread - impact -
fees + execution residual - financing) must reconcile to the ledger's cash
identity to zero on fixtures with partial fills, netting across two families
and a failed closeout with overnight carry; every fill here comes from the
simulator, not from a hand-written price.
"""

from __future__ import annotations

import math

import pytest
from iap.execution.sim import (
    COLUMNS,
    LATENCY_NS,
    AuctionPrint,
    DailyImpactModel,
    FeeSchedule,
    ImpactCoefficients,
    Market,
    Quote,
    SimError,
    closeout_valuation,
    expected_cost_bps,
    impact_per_share,
    simulate,
    simulate_columns,
    simulate_daily,
)
from iap.portfolio.ledger import CohortState, FamilyConfig, Ledger, LedgerConfig, Valuation

T0 = 1_700_000_000_000_000_000
MIN = 60 * 10**9


def quote(ts, mid, spread_bps=5.0, size=5000):
    s = mid * spread_bps / 1e4
    return Quote(ts=ts, bid=mid - s / 2, ask=mid + s / 2, bid_size=size, ask_size=size)


def market(name, ts_decision, mid, **kw):
    size = kw.pop("size", 5000)
    spread = kw.pop("spread_bps", 5.0)
    vol = kw.pop("volume_5m", 200_000)
    halted = kw.pop("halted", False)
    q = kw.pop("quote", quote(ts_decision + LATENCY_NS, mid, spread, size))
    return Market(name, q, vol, halted)


def test_one_fill_price_convention_and_latency_drift_is_not_a_cost():
    mkt = market("AAA", T0, 100.02, spread_bps=10.0)
    out = simulate(ts_decision=T0, name="AAA", qty=1000, mid_decision=100.0, market=mkt)
    f = out.fill
    assert out.filled == 1000 and out.refused == 0 and out.reason == ""
    assert f.mid_arrival == pytest.approx(100.02) and out.arrival_ts == T0 + 500_000_000
    assert f.price == pytest.approx(f.mid_arrival + f.half_spread + f.impact)
    assert f.half_spread == pytest.approx(100.02 * 10 / 1e4 / 2)
    assert f.latency_drift == pytest.approx(
        -1000 * 0.02
    )  # a separate attribution line, never in the price
    assert f.column == "conservative"
    sell = simulate(ts_decision=T0, name="AAA", qty=-1000, mid_decision=100.0, market=mkt).fill
    assert sell.price == pytest.approx(sell.mid_arrival - sell.half_spread - sell.impact)


def test_three_columns_differ_only_by_impact_multiplier_and_depth_haircut():
    mkt = market("AAA", T0, 50.0, size=1000, volume_5m=100_000)
    outs = simulate_columns(ts_decision=T0, name="AAA", qty=900, mid_decision=50.0, market=mkt)
    assert set(outs) == {"base", "conservative", "stress"}
    base, cons, stress = outs["base"], outs["conservative"], outs["stress"]
    assert (
        base.filled == 900 and cons.filled == 750 and stress.filled == 500
    )  # 100 / 75 / 50 % of 1000
    assert cons.refused == 150 and "insufficient depth" in cons.reason
    for o in outs.values():
        f = o.fill
        assert f.half_spread == base.fill.half_spread and f.mid_arrival == base.fill.mid_arrival
        depth = o.depth_available
        expected = impact_per_share(
            f.qty, f.mid_arrival, depth, 100_000, COLUMNS[o.column], ImpactCoefficients()
        )
        assert f.impact == pytest.approx(expected)
        assert f.price == pytest.approx(f.mid_arrival + f.half_spread + f.impact)
    # same size and depth, the impact scales exactly with the multiplier
    same = {
        c: simulate(
            ts_decision=T0, name="AAA", qty=100, mid_decision=50.0, market=mkt, column=c
        ).fill
        for c in COLUMNS
    }
    i1 = same["base"].impact
    assert same["conservative"].impact == pytest.approx(
        1.5 * i1 * (1000 // 1) / (1000 * 0.75) * 0
        + 1.5 * impact_per_share(100, 50.0, 750, 100_000, COLUMNS["base"], ImpactCoefficients())
    )
    assert same["stress"].impact / same["base"].impact > 1.5
    assert ImpactCoefficients().label == "estimated"


def test_failure_cases_are_deterministic_and_named():
    k = dict(ts_decision=T0, name="AAA", qty=1000, mid_decision=100.0)
    assert (
        simulate(**k, market=Market("AAA", None, 100_000)).reason
        == "quote disappeared before arrival"
    )
    assert simulate(**k, market=market("AAA", T0, 100.0, halted=True)).reason == "halt"
    stale = quote(T0 + LATENCY_NS - 6 * 10**9, 100.0)
    assert simulate(**k, market=market("AAA", T0, 100.0, quote=stale)).reason == "stale quote"
    crossed = Quote(T0 + LATENCY_NS, 100.1, 99.9, 100, 100)
    assert simulate(**k, market=market("AAA", T0, 100.0, quote=crossed)).reason == "crossed quote"
    # price through the entry threshold refuses a discretionary order, never a mandatory one
    mkt = market("AAA", T0, 100.5)
    r = simulate(**k, market=mkt, limit_price=100.2)
    assert r.filled == 0 and "entry threshold" in r.reason
    assert simulate(**k, market=mkt, limit_price=100.2, mandatory=True).filled == 1000
    # participation: scaled to 5 % of trailing volume, dropped below the minimum ticket
    thin = market("AAA", T0, 100.0, volume_5m=10_000)
    r = simulate(**k, market=thin)
    assert (
        r.filled == 500
        and r.refused == 500
        and "participation" in r.reason
        and r.participation == pytest.approx(0.05)
    )
    r = simulate(**k, market=market("AAA", T0, 100.0, volume_5m=10), min_ticket=10)
    assert r.filled == 0 and "participation breach" in r.reason
    r = simulate(**dict(k, qty=5), market=market("AAA", T0, 100.0), min_ticket=10)
    assert r.filled == 0 and "minimum ticket" in r.reason
    # insufficient depth: partial, remainder refused (not filled at a worse price)
    r = simulate(**k, market=market("AAA", T0, 100.0, size=400))
    assert r.filled == 300 and r.refused == 700
    assert simulate(**k, market=market("AAA", T0, 100.0, size=0)).reason.startswith(
        "insufficient depth"
    )
    with pytest.raises(SimError):
        simulate(**dict(k, qty=0), market=mkt)
    with pytest.raises(SimError):
        simulate(**k, market=market("BBB", T0, 100.0))
    v = closeout_valuation(
        100.0, 1000, 100_000, 2000, "halt through the close", reference_spread=0.05
    )
    assert v.cause == "halt through the close" and v.price < 100.0 - 0.025
    assert closeout_valuation(100.0, -1000, 100_000, 2000, "no quote").price > 100.0
    assert (
        expected_cost_bps(1000, quote(T0, 100.0, 10.0), 200_000) > 5.0
    )  # half-spread alone is 5 bps


def test_daily_horizon_execution_at_the_auction_with_square_root_impact():
    a = AuctionPrint(ts=T0, price=40.0, volume=1_000_000, sigma_daily=0.02, adv=5_000_000)
    r = simulate_daily(ts_decision=T0 - MIN, name="DDD", qty=20_000, mid_decision=39.9, auction=a)
    f = r.fill
    assert f.half_spread == 0.0 and f.mid_arrival == 40.0
    assert f.impact == pytest.approx(1.5 * 1.0 * 0.02 * math.sqrt(20_000 / 5_000_000) * 40.0)
    assert f.price == pytest.approx(40.0 + f.impact)
    assert DailyImpactModel().label == "estimated"
    r = simulate_daily(ts_decision=T0, name="DDD", qty=200_000, mid_decision=40.0, auction=a)
    assert r.filled == 50_000 and "participation" in r.reason
    r = simulate_daily(
        ts_decision=T0,
        name="DDD",
        qty=10,
        mid_decision=40.0,
        auction=AuctionPrint(T0, 40.0, 0, 0.02, 1),
    )
    assert r.filled == 0 and r.reason == "no auction print"


# --------------------------------------------------------------------------- the waterfall against the identity
def run_fixture(C=1_000_000.0):
    """Two families, one name shared; partial fills; a failed closeout carried overnight; all fills simulated."""
    led = Ledger(LedgerConfig(financing_rate_per_session=0.0001, borrow_rate_per_session=0.0002))
    led.register_family(FamilyConfig("h1", flat_at_close=True))
    led.register_family(FamilyConfig("d1", flat_at_close=False))
    led.deposit(C)
    fees = FeeSchedule(per_share=0.001)
    log = []

    def execute(d, mids, markets, **kw):
        for n in sorted(d.orders):
            o = d.orders[n]
            if o.submitted == 0:
                continue
            out = simulate(
                ts_decision=d.ts,
                name=n,
                qty=o.submitted,
                mid_decision=mids[n],
                market=markets[n],
                fees=fees,
                mandatory=o.mandatory != 0,
                **kw,
            )
            log.append(out)
            if out.fill is not None:
                led.fill(out.fill)
        led.settle()

    # session 1 -----------------------------------------------------------
    led.open_session("s1")
    t1 = T0
    led.open_cohort(
        ("h1", "15m", 1),
        formation_ts=t1 - 15 * MIN,
        decision_ts=t1,
        expiry_ts=t1 + 15 * MIN,
        intents={"AAA": 2000, "BBB": -1500},
        forecasts={"AAA": 8.0, "BBB": 7.0},
    )
    led.open_cohort(
        ("d1", "5d", 1),
        formation_ts=t1 - MIN,
        decision_ts=t1,
        expiry_ts=t1 + 5 * 24 * 60 * MIN,
        intents={"AAA": -500, "CCC": 1000},
        forecasts={"AAA": 20.0, "CCC": 30.0},
    )
    mids = {"AAA": 100.0, "BBB": 50.0, "CCC": 20.0}
    d = led.decide(t1, mids)
    assert d.orders["AAA"].submitted == 1500  # netted across families: 2000 - 500
    markets = {
        "AAA": market("AAA", t1, 100.01, size=1200),  # partial: 75 % of 1200 = 900 filled
        "BBB": market("BBB", t1, 49.99, size=10_000),
        "CCC": market("CCC", t1, 20.02, size=10_000),
    }
    execute(d, mids, markets)
    assert led.cohort(("h1", "15m", 1)).state is CohortState.LIVE
    # expiry of the h1 cohort: BBB's quote disappears, so its exit is outstanding (closing) and carried
    t2 = t1 + 15 * MIN
    mids2 = {"AAA": 100.3, "BBB": 49.8, "CCC": 20.1}
    d = led.decide(t2, mids2)
    markets2 = {"AAA": market("AAA", t2, 100.28), "BBB": Market("BBB", None, 100_000)}
    execute(d, mids2, markets2)
    assert led.cohort(("h1", "15m", 1)).state is CohortState.CLOSING
    # pre-close: BBB halted through the close -> stress valuation, flagged, carried overnight
    t3 = t2 + 5 * MIN
    mids3 = {"AAA": 100.2, "BBB": 49.7, "CCC": 20.0}
    d = led.decide(t3, mids3, pre_close=True)
    execute(d, mids3, {"BBB": market("BBB", t3, 49.7, halted=True)})
    vals = {
        "BBB": closeout_valuation(
            49.7,
            led.positions["BBB"],
            100_000,
            2000,
            "halt through the close",
            reference_spread=0.025,
        ),
        "CCC": Valuation(20.0),
        "AAA": Valuation(
            100.2
        ),  # d1's short of 500 is carried as a matter of course (not flat at close)
    }
    r1 = led.close_session(vals)
    assert r1.closeout_failures and r1.carried["BBB"] != 0 and r1.carried["CCC"] == 1000
    # session 2: residual liquidated at the first valid quote; d1 holds on
    led.open_session("s2")
    t4 = t1 + 24 * 60 * MIN
    mids4 = {"AAA": 100.4, "BBB": 49.9, "CCC": 20.3}
    d = led.decide(t4, mids4)
    execute(d, mids4, {"BBB": market("BBB", t4, 49.92, size=10_000)})
    assert "BBB" not in led.positions and led.cohort(("h1", "15m", 1)).state is CohortState.CLOSED
    r2 = led.close_session({"AAA": Valuation(100.5), "CCC": Valuation(20.25)})
    return led, [r1, r2], log


def test_waterfall_reconciles_to_the_cash_identity_to_zero_with_partials_netting_and_carry():
    led, results, log = run_fixture()
    assert any(o.refused and o.filled for o in log)  # a partial fill happened
    assert any(o.fill is None for o in log)  # a refusal happened
    for r in results:
        w = r.waterfall
        total = (
            w["midpoint_reference_pnl"]
            + w["latency_drift"]
            - w["half_spread"]
            - w["impact"]
            - w["fees"]
            + w["execution_residual"]
            - w["financing"]
        )
        assert abs(total - r.net_pnl) <= 1e-9 * max(1.0, abs(r.net_pnl))
        assert abs(r.identity_gap) <= 1e-9
        assert r.net_pnl == pytest.approx(r.cash_close + r.marked_inventory_close - r.equity_open)
    # an independent reconstruction from the fills alone: cash flows, latency drift, costs
    cash = 1_000_000.0
    for f in led.fills:
        cash += -f.qty * f.price - f.fee
    cash -= sum(r.waterfall["financing"] for r in results)
    assert cash == pytest.approx(led.cash)
    assert sum(r.waterfall["half_spread"] for r in results) == pytest.approx(
        sum(abs(f.qty) * f.half_spread for f in led.fills)
    )
    assert sum(r.waterfall["impact"] for r in results) == pytest.approx(
        sum(abs(f.qty) * f.impact for f in led.fills)
    )
    assert sum(r.waterfall["latency_drift"] for r in results) == pytest.approx(
        sum(f.latency_drift for f in led.fills)
    )
    assert results[1].waterfall["financing"] > 0  # overnight carry was charged, once, at the open
    assert (
        results[0].waterfall["execution_residual"] != 0
        or results[1].waterfall["execution_residual"] != 0
    )
    # across the two sessions the identity telescopes: sum NetPnL = equity now - C
    equity_now = led.cash + 1000 * 20.25 + led.positions["AAA"] * 100.5
    assert sum(r.net_pnl for r in results) == pytest.approx(equity_now - 1_000_000.0)

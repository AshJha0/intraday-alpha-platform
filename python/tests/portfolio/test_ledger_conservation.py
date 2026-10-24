"""EN2 (v2.0 stage 5): conservation invariants of the cohort and executable ledgers on synthetic fixtures.

Fixtures: partial fills, netting across two families in one name, a failed
closeout with overnight carry, and an independent cash/position/cost
reconstruction (E2's acceptance) that matches the ledger's NetPnL and its
waterfall to zero.  Every fill follows the one fill-price convention
P = M_arrival + side * (half_spread + impact).
"""

from __future__ import annotations

import math

import pytest
from iap.core.rng import SplitMix64
from iap.portfolio.ledger import (
    CohortState,
    FamilyConfig,
    Fill,
    Ledger,
    LedgerConfig,
    LedgerError,
    TradeDecision,
    Valuation,
    distribute,
)

T0 = 1_700_000_000_000_000_000
MIN = 60 * 10**9
H1, D1 = "h1", "d1_reversal"


def mkfill(ts, name, qty, mid_decision, mid_arrival=None, hs=0.005, imp=0.002, fee=None):
    ma = mid_decision if mid_arrival is None else mid_arrival
    side = 1 if qty > 0 else -1
    return Fill(
        ts=ts,
        name=name,
        qty=qty,
        price=ma + side * (hs + imp),
        fee=0.0005 * abs(qty) if fee is None else fee,
        half_spread=hs,
        impact=imp,
        mid_decision=mid_decision,
        mid_arrival=ma,
    )


def new_ledger(**cfg) -> Ledger:
    led = Ledger(LedgerConfig(**cfg))
    led.register_family(FamilyConfig(H1, flat_at_close=True))
    led.register_family(FamilyConfig(D1, flat_at_close=False))
    led.deposit(1_000_000.0)
    return led


def reconstruct(led: Ledger) -> list[dict[str, float]]:
    """E2: cash, positions and costs rebuilt from the event log alone, one row per session."""
    cash = 0.0
    pos: dict[str, int] = {}
    val: dict[str, float] = {}
    rows = []
    cur = None
    for e in led.events:
        k = e["event"]
        if k == "deposit":
            cash += e["amount"]
        elif k == "corporate_action" and e["applied"]:
            pos[e["name"]] = pos[e["name"]] * e["num"] // e["den"]
            val[e["name"]] = val[e["name"]] * e["den"] / e["num"]
        elif k == "session_open":
            q_open = {n: q for n, q in pos.items() if q}
            cur = {
                "equity_open": cash + sum(q * val[n] for n, q in q_open.items()),
                "q_open": q_open,
                "v_open": dict(val),
                "fills": [],
                "financing": 0.0,
            }
        elif k == "financing":
            cash -= e["amount"]
            cur["financing"] += e["amount"]
        elif k == "fill":
            cash += -e["qty"] * e["price"] - e["fee"]
            pos[e["name"]] = pos.get(e["name"], 0) + e["qty"]
            cur["fills"].append(e)
        elif k == "session_close":
            res = next(r for r in led.sessions if r.session_id == e["session"])
            v_close = e["valuations"]
            val.update(v_close)
            inv = sum(q * v_close[n] for n, q in pos.items() if q)
            net = cash + inv - cur["equity_open"]
            fills = cur["fills"]
            # midpoint-reference + execution residual, from positions, valuations and decision mids only:
            # sum_n (q_close V_close - q_open V_open) - sum_f q_f M_decision
            ref = (
                inv
                - sum(q * cur["v_open"][n] for n, q in cur["q_open"].items())
                - sum(f["qty"] * f["mid_decision"] for f in fills)
            )
            rows.append(
                {
                    "session": e["session"],
                    "net_pnl": net,
                    "reference_pnl": ref,
                    "latency_drift": sum(
                        -f["qty"] * (f["mid_arrival"] - f["mid_decision"]) for f in fills
                    ),
                    "half_spread": sum(abs(f["qty"]) * f["half_spread"] for f in fills),
                    "impact": sum(abs(f["qty"]) * f["impact"] for f in fills),
                    "fees": sum(f["fee"] for f in fills),
                    "financing": cur["financing"],
                    "ledger_net": res.net_pnl,
                }
            )
    return rows


def close(led: Ledger, valuations: dict[str, Valuation]):
    return led.close_session(valuations)


def assert_reconciles(led: Ledger) -> None:
    for row, res in zip(reconstruct(led), led.sessions, strict=True):
        w = res.waterfall
        assert math.isclose(row["net_pnl"], res.net_pnl, abs_tol=1e-9), (row, res)
        assert math.isclose(
            row["reference_pnl"],
            w["midpoint_reference_pnl"] + w["execution_residual"],
            abs_tol=1e-9,
        )
        for k in ("latency_drift", "half_spread", "impact", "fees", "financing"):
            assert math.isclose(row[k], w[k], abs_tol=1e-9), k
        assert abs(res.identity_gap) <= 1e-9
    total_cohort = sum(c.signal_pnl + c.execution_residual for c in led.cohorts.values())
    total_sessions = sum(
        r.waterfall["midpoint_reference_pnl"] + r.waterfall["execution_residual"]
        for r in led.sessions
    )
    assert math.isclose(total_cohort, total_sessions, abs_tol=1e-9)


# --------------------------------------------------------------------------- allocation rule
def test_distribute_mandatory_first_then_largest_remainder_ties_to_lower_key():
    # mandatory in expiry order, capped at their intents
    assert distribute(50, [((2, "b"), 40), ((1, "a"), 30)], []) == [20, 30]
    # pro rata by largest remainder: 10 shares over caps 7, 7, 7 (floor 3 each, remainder 1/3 each -> tie to lower key)
    assert distribute(10, [], [((3,), 7), ((1,), 7), ((2,), 7)]) == [3, 4, 3]
    # remainders decide before keys
    assert distribute(7, [], [((1,), 10), ((2,), 25)]) == [2, 5]
    # enough for everyone
    assert distribute(100, [((1,), 10)], [((1,), 20), ((2,), 30)]) == [10, 20, 30]
    assert distribute(0, [((1,), 10)], [((1,), 20)]) == [0, 0]
    with pytest.raises(LedgerError):
        distribute(-1, [], [])


# --------------------------------------------------------------------------- fixtures
def test_partial_fill_creates_no_fictitious_position_and_cancels_the_discretionary_remainder():
    led = new_ledger()
    led.open_session("S1")
    led.open_cohort(
        (H1, "15m", 1),
        formation_ts=T0 - 15 * MIN,
        decision_ts=T0,
        expiry_ts=T0 + 15 * MIN,
        intents={"AAA": 300, "BBB": -200},
        forecasts={"AAA": 5.0, "BBB": -4.0},
        decisions={"AAA": TradeDecision(1.2, 5.0, 1.0, 1.0)},
    )
    d = led.decide(T0, {"AAA": 100.0, "BBB": 50.0})
    assert d.orders["AAA"].submitted == 300 and d.orders["BBB"].submitted == -200
    led.fill(mkfill(T0, "AAA", 120, 100.0, 100.02))  # 120 of 300
    # BBB: no fill at all
    led.settle()
    c = led.cohort((H1, "15m", 1))
    assert c.state is CohortState.LIVE and c.allocated == {"AAA": 120}
    assert c.cancelled == {"AAA": 180, "BBB": -200} and c.outstanding == {}
    assert led.positions == {"AAA": 120}
    qs = led._quantity_states
    assert (
        qs["intended"] == 100
        and qs["submitted"] == 100
        and qs["filled"] == 120
        and qs["cancelled"] == -20
    )
    # expiry: full exit filled
    led.decide(T0 + 15 * MIN, {"AAA": 101.0})
    led.fill(mkfill(T0 + 15 * MIN, "AAA", -120, 101.0, 100.99))
    led.settle()
    assert c.state is CohortState.CLOSED and led.positions == {}
    assert math.isclose(c.signal_pnl, 120 * (101.0 - 100.0))
    res = close(led, {})
    assert res.closeout_failures == [] and res.carried == {}
    w = res.waterfall
    # latency drift: bought into a rising mid (-120 * 0.02), sold into a falling mid (+120 * -0.01 -> -1.2)
    assert math.isclose(w["latency_drift"], -120 * 0.02 - 120 * 0.01)
    assert math.isclose(w["half_spread"], 240 * 0.005) and math.isclose(w["impact"], 240 * 0.002)
    assert math.isclose(w["fees"], 240 * 0.0005) and w["financing"] == 0.0
    assert math.isclose(res.net_pnl, 120.0 - 3.6 - 1.2 - 0.48 - 0.12)
    assert_reconciles(led)
    rows = led.research_rows()
    assert {r["name"]: r["cushion_bps"] for r in rows if r["cohort_id"] == 1}[
        "AAA"
    ] == pytest.approx(3.0)


def test_netting_across_two_families_in_one_name_charges_costs_once():
    led = new_ledger()
    led.open_session("S1")
    # H1 cohort 1 long 100 AAA, filled fully at t0
    led.open_cohort(
        (H1, "15m", 1),
        formation_ts=T0,
        decision_ts=T0,
        expiry_ts=T0 + 15 * MIN,
        intents={"AAA": 100},
        forecasts={"AAA": 3.0},
    )
    led.decide(T0, {"AAA": 100.0})
    led.fill(mkfill(T0, "AAA", 100, 100.0))
    led.settle()
    # at t0+15: H1 exits -100 (mandatory) while a daily family enters +60 and another H1 cohort enters +15
    t1 = T0 + 15 * MIN
    led.open_cohort(
        (D1, "5d", 1),
        formation_ts=t1,
        decision_ts=t1,
        expiry_ts=t1 + 5 * 390 * MIN,
        intents={"AAA": 60},
        forecasts={"AAA": 8.0},
    )
    led.open_cohort(
        (H1, "15m", 2),
        formation_ts=t1,
        decision_ts=t1,
        expiry_ts=t1 + 15 * MIN,
        intents={"AAA": 15},
        forecasts={"AAA": 2.0},
    )
    d = led.decide(t1, {"AAA": 102.0})
    o = d.orders["AAA"]
    assert o.intended == -25 and o.mandatory == -100 and o.submitted == -25
    led.fill(mkfill(t1, "AAA", -25, 102.0, 101.98))  # the one external order
    led.settle()
    assert o.crossed == 75 and o.filled == -25 and o.outstanding == 0 and o.cancelled == 0
    h1a, dd, h1b = led.cohort((H1, "15m", 1)), led.cohort((D1, "5d", 1)), led.cohort((H1, "15m", 2))
    assert h1a.state is CohortState.CLOSED and h1a.allocated == {}
    assert dd.allocated == {"AAA": 60} and h1b.allocated == {"AAA": 15}
    assert (
        led.positions == {"AAA": 75} and len(led.fills) == 2
    )  # one fee-bearing fill per net order
    assert math.isclose(h1a.signal_pnl, 100 * 2.0)
    # H1 cohort 2 exits at t2; the daily cohort carries overnight (no flag: not a flat-at-close family)
    t2 = t1 + 15 * MIN
    led.decide(t2, {"AAA": 101.0})
    led.fill(mkfill(t2, "AAA", -15, 101.0))
    led.settle()
    assert h1b.state is CohortState.CLOSED and math.isclose(h1b.signal_pnl, 15 * (101.0 - 102.0))
    res = close(led, {"AAA": Valuation(101.5)})
    assert res.carried == {"AAA": 60} and res.closeout_failures == []
    assert math.isclose(dd.signal_pnl, 60 * (101.5 - 102.0))
    assert math.isclose(
        res.waterfall["fees"], (100 + 25 + 15) * 0.0005
    )  # net executed quantity only
    assert_reconciles(led)
    # a second session: the carried inventory opens Equity_open at V_close and is financed if a rate is declared
    eq = led.open_session("S2")
    assert math.isclose(eq, led.cash + 60 * 101.5)
    led.decide(T0 + 390 * MIN, {"AAA": 103.0})
    led.settle()
    res2 = close(led, {"AAA": Valuation(103.0)})
    assert math.isclose(res2.net_pnl, 60 * (103.0 - 101.5)) and math.isclose(
        res2.waterfall["overnight_pnl"], 60 * 1.5
    )
    assert_reconciles(led)


def test_failed_closeout_carries_residual_with_flag_and_charges_the_loss_once():
    led = new_ledger(financing_rate_per_session=0.0001, borrow_rate_per_session=0.0002)
    led.open_session("S1")
    led.open_cohort(
        (H1, "30m", 7),
        formation_ts=T0,
        decision_ts=T0,
        expiry_ts=T0 + 30 * MIN,
        intents={"AAA": -200},
        forecasts={"AAA": -6.0},
    )
    led.decide(T0, {"AAA": 50.0})
    led.fill(mkfill(T0, "AAA", -200, 50.0))
    led.settle()
    t1 = T0 + 30 * MIN
    led.decide(t1, {"AAA": 49.0})
    led.fill(mkfill(t1, "AAA", 150, 49.0, 49.01))  # the expiry exit is only partly filled
    led.settle()
    c = led.cohort((H1, "30m", 7))
    assert (
        c.state is CohortState.CLOSING
        and c.allocated == {"AAA": -50}
        and c.outstanding == {"AAA": 50}
    )
    assert math.isclose(
        c.signal_pnl, -200 * (49.0 - 50.0)
    )  # attribution closed at the scheduled expiry
    # re-sent at the next decision time, but the name is halted through the close: no fill, stress valuation
    t2 = t1 + 5 * MIN
    d = led.decide(t2, {"AAA": 49.5}, pre_close=True)
    assert d.orders["AAA"].mandatory == 50 and d.orders["AAA"].intents[0].kind == "resend"
    led.settle()
    with pytest.raises(LedgerError, match="without a closeout_failure cause"):
        close(led, {"AAA": Valuation(49.8)})
    res1 = close(led, {"AAA": Valuation(49.8, cause="halt through the close")})
    assert (
        res1.carried == {"AAA": -50}
        and res1.closing_cohorts == 1
        and res1.closing_gross_shares == 50
    )
    assert res1.closeout_failures[0]["cause"] == "halt through the close"
    # execution residual so far: the residual short valued from 49.0 (expiry mid) to 49.8 (stress valuation)
    assert math.isclose(c.execution_residual, -50 * (49.8 - 49.0))
    assert math.isclose(res1.waterfall["execution_residual"], -40.0)
    # next session: Equity_open carries the residual at 49.8; financing and borrow are charged on the carried notional
    eq = led.open_session("S2")
    assert math.isclose(eq, led._cash_open - 50 * 49.8)
    fin = 50 * 49.8 * (0.0001 + 0.0002)
    assert math.isclose(led.sessions[-1].waterfall["financing"], 0.0)
    t3 = T0 + 390 * MIN
    d = led.decide(t3, {"AAA": 50.3})
    assert (
        d.orders["AAA"].mandatory == 50
    )  # liquidated by a mandatory order at the first valid quote
    led.fill(mkfill(t3, "AAA", 50, 50.3, 50.31))
    led.settle()
    assert c.state is CohortState.CLOSED and led.positions == {}
    res2 = close(led, {})
    assert math.isclose(res2.waterfall["financing"], fin)
    assert math.isclose(res2.waterfall["overnight_pnl"], -50 * (50.3 - 49.8))
    # the loss on the residual is charged once: S1 took 49.0 -> 49.8, S2 takes 49.8 -> 50.3
    assert math.isclose(c.execution_residual, -50 * (50.3 - 49.0))
    assert math.isclose(res2.waterfall["execution_residual"], -50 * (50.3 - 49.8))
    assert_reconciles(led)


def test_allocation_rule_on_a_shared_fill_expiring_first_then_pro_rata_with_ties():
    led = new_ledger()
    led.register_family(FamilyConfig("h2", flat_at_close=True))
    led.open_session("S1")
    # two long cohorts to expire at different times
    led.open_cohort(
        (H1, "15m", 1),
        formation_ts=T0,
        decision_ts=T0,
        expiry_ts=T0 + 15 * MIN,
        intents={"AAA": 100},
        forecasts={"AAA": 1.0},
    )
    led.open_cohort(
        (H1, "30m", 1),
        formation_ts=T0,
        decision_ts=T0,
        expiry_ts=T0 + 30 * MIN,
        intents={"AAA": 100},
        forecasts={"AAA": 1.0},
    )
    led.decide(T0, {"AAA": 10.0})
    led.fill(mkfill(T0, "AAA", 200, 10.0))
    led.settle()
    led.request_liquidation((H1, "30m", 1), "sector tolerance breached")
    t1 = T0 + 15 * MIN
    # both exit (-100 each: one scheduled, one risk liquidation); three cohorts enter short: -7, -7 (tie) and -7 (other family)
    led.open_cohort(
        (H1, "15m", 3),
        formation_ts=t1,
        decision_ts=t1,
        expiry_ts=t1 + 15 * MIN,
        intents={"AAA": -7},
        forecasts={"AAA": -1.0},
    )
    led.open_cohort(
        (H1, "15m", 2),
        formation_ts=t1,
        decision_ts=t1,
        expiry_ts=t1 + 15 * MIN,
        intents={"AAA": -7},
        forecasts={"AAA": -1.0},
    )
    led.open_cohort(
        ("h2", "15m", 2),
        formation_ts=t1,
        decision_ts=t1,
        expiry_ts=t1 + 15 * MIN,
        intents={"AAA": -7},
        forecasts={"AAA": -1.0},
    )
    d = led.decide(t1, {"AAA": 11.0})
    assert d.orders["AAA"].intended == -221 and d.orders["AAA"].mandatory == -200
    led.fill(mkfill(t1, "AAA", -160, 11.0))  # partial: 160 of 221
    led.settle()
    # expiring first in expiry order: the 15m cohort (expiry t1) before the 30m cohort (expiry t1+15, liquidated)
    assert (
        led.cohort((H1, "15m", 1)).allocated == {}
        and led.cohort((H1, "15m", 1)).state is CohortState.CLOSED
    )
    c30 = led.cohort((H1, "30m", 1))
    assert (
        c30.allocated == {"AAA": 40}
        and c30.outstanding == {"AAA": -40}
        and c30.state is CohortState.CLOSING
    )
    assert c30.early_exit_reason == "sector tolerance breached" and c30.attribution_closed_ts == t1
    # entering cohorts receive nothing from an external fill that mandatory exits exhausted
    for key in ((H1, "15m", 3), (H1, "15m", 2), ("h2", "15m", 2)):
        c = led.cohort(key)
        assert c.state is CohortState.UNFILLED and c.cancelled == {"AAA": -7}
    assert led.positions == {"AAA": 40}
    # now a pure entry round with a partial fill: 10 shares over -7/-7/-7 -> 4/3/3 by lower cohort id, then family id
    t2 = t1 + 5 * MIN
    led.open_cohort(
        (H1, "15m", 5),
        formation_ts=t2,
        decision_ts=t2,
        expiry_ts=t2 + 15 * MIN,
        intents={"BBB": -7},
        forecasts={"BBB": -1.0},
    )
    led.open_cohort(
        (H1, "15m", 4),
        formation_ts=t2,
        decision_ts=t2,
        expiry_ts=t2 + 15 * MIN,
        intents={"BBB": -7},
        forecasts={"BBB": -1.0},
    )
    led.open_cohort(
        ("h2", "15m", 4),
        formation_ts=t2,
        decision_ts=t2,
        expiry_ts=t2 + 15 * MIN,
        intents={"BBB": -7},
        forecasts={"BBB": -1.0},
    )
    d = led.decide(
        t2, {"AAA": 11.0, "BBB": 20.0}, withhold=["AAA"]
    )  # AAA: only the mandatory re-send goes
    assert d.orders["AAA"].submitted == -40 and d.orders["AAA"].intents[0].kind == "resend"
    led.fill(mkfill(t2, "BBB", -10, 20.0))
    led.fill(mkfill(t2, "AAA", -40, 11.0))
    led.settle()
    assert led.cohort((H1, "15m", 4)).allocated == {"BBB": -4}
    assert led.cohort(("h2", "15m", 4)).allocated == {"BBB": -3}
    assert led.cohort((H1, "15m", 5)).allocated == {"BBB": -3}
    assert c30.state is CohortState.CLOSED and led.positions == {"BBB": -10}
    # pre-close liquidation of a flat-at-close family's live cohorts, fully filled
    t3 = t2 + 5 * MIN
    d = led.decide(t3, {"BBB": 19.0}, pre_close=True)
    assert d.orders["BBB"].mandatory == 10 and {i.kind for i in d.orders["BBB"].intents} == {
        "liquidation"
    }
    led.fill(mkfill(t3, "BBB", 10, 19.0))
    led.settle()
    for key in ((H1, "15m", 4), (H1, "15m", 5), ("h2", "15m", 4)):
        c = led.cohort(key)
        assert c.state is CohortState.CLOSED and c.early_exit_reason == "pre-close liquidation"
    close(led, {})
    assert_reconciles(led)


def test_refusals_and_rule_violations_are_refused_not_hidden():
    led = new_ledger(gross_cap=5_000.0)
    with pytest.raises(LedgerError, match="open a session"):
        led.decide(T0, {})
    led.open_session("S1")
    led.refuse_cohort(
        (H1, "15m", 1), decision_ts=T0, expiry_ts=T0 + MIN, reason="fewer than 10 names per leg"
    )
    assert led.cohort((H1, "15m", 1)).state is CohortState.REFUSED
    with pytest.raises(LedgerError, match="not registered"):
        led.open_cohort(
            ("nobody", "1d", 1),
            formation_ts=T0,
            decision_ts=T0,
            expiry_ts=T0 + MIN,
            intents={"A": 1},
            forecasts={"A": 1.0},
        )
    with pytest.raises(LedgerError, match="no finite forecast"):
        led.open_cohort(
            (H1, "15m", 2),
            formation_ts=T0,
            decision_ts=T0,
            expiry_ts=T0 + MIN,
            intents={"A": 1},
            forecasts={},
        )
    with pytest.raises(LedgerError, match="formation_ts <= decision_ts < expiry_ts"):
        led.open_cohort(
            (H1, "15m", 2),
            formation_ts=T0,
            decision_ts=T0,
            expiry_ts=T0,
            intents={"A": 1},
            forecasts={"A": 1.0},
        )
    led.open_cohort(
        (H1, "15m", 2),
        formation_ts=T0,
        decision_ts=T0,
        expiry_ts=T0 + 15 * MIN,
        intents={"AAA": 100},
        forecasts={"AAA": 1.0},
    )
    with pytest.raises(LedgerError, match="needs mids"):
        led.decide(T0, {})
    d = led.decide(T0, {"AAA": 100.0})
    with pytest.raises(LedgerError, match="not settled"):
        led.decide(T0 + MIN, {"AAA": 100.0})
    with pytest.raises(LedgerError, match="fill-price convention"):
        Fill(T0, "AAA", 10, 100.5, 0.0, 0.01, 0.0, 100.0, 100.0)
    with pytest.raises(LedgerError, match="not a partial fill"):
        led.fill(mkfill(T0, "AAA", -10, 100.0))
    with pytest.raises(LedgerError, match="not a partial fill"):
        led.fill(mkfill(T0, "AAA", 101, 100.0))
    with pytest.raises(LedgerError, match="no submitted net order"):
        led.fill(mkfill(T0, "ZZZ", 1, 100.0))
    with pytest.raises(LedgerError, match="settle the open decision"):
        led.close_session({})
    led.fill(
        mkfill(T0, "AAA", 60, 100.0)
    )  # 6,000 notional > the 5,000 cap: the ledger refuses at settle
    with pytest.raises(LedgerError, match="exceeds the cap"):
        led.settle()
    assert d.settled is False


def test_corporate_action_on_carried_inventory_preserves_notional_and_refuses_fractions():
    led = new_ledger()
    led.open_session("S1")
    led.open_cohort(
        (D1, "5d", 1),
        formation_ts=T0,
        decision_ts=T0,
        expiry_ts=T0 + 5 * 390 * MIN,
        intents={"AAA": 90},
        forecasts={"AAA": 2.0},
    )
    led.decide(T0, {"AAA": 30.0})
    led.fill(mkfill(T0, "AAA", 90, 30.0))
    led.settle()
    close(led, {"AAA": Valuation(31.0)})
    with pytest.raises(LedgerError, match="fractional shares"):
        led.apply_corporate_action("AAA", 4, 90 * 4 + 1)
    led.apply_corporate_action("AAA", 3, 1)  # a 3-for-1 split overnight
    assert led.positions == {"AAA": 270} and led.cohort((D1, "5d", 1)).allocated == {"AAA": 270}
    eq = led.open_session("S2")
    assert math.isclose(eq, led.cash + 270 * 31.0 / 3)
    led.decide(T0 + 390 * MIN, {"AAA": 10.5})
    led.settle()
    res = close(led, {"AAA": Valuation(10.5)})
    assert math.isclose(res.net_pnl, 270 * (10.5 - 31.0 / 3))
    assert [e for e in led.events if e["event"] == "valuation_failure"][0]["name"] == "AAA"
    assert_reconciles(led)


def test_randomised_sessions_hold_every_invariant_and_reconcile_to_zero():
    """Many families, names, partial fills, crosses, carries, liquidations: invariants and E2 at every step."""
    rng = SplitMix64(20261024)
    led = new_ledger(financing_rate_per_session=0.00005, borrow_rate_per_session=0.0001)
    led.register_family(FamilyConfig("h2", flat_at_close=True))
    names = ["N0", "N1", "N2", "N3"]
    mids = {n: 50.0 + 10 * i for i, n in enumerate(names)}
    cohort_no = 0
    for s in range(4):
        led.open_session(f"S{s}")
        base = T0 + s * 390 * MIN
        for step in range(8):
            ts = base + step * 5 * MIN
            for n in names:
                mids[n] = round(mids[n] * (1 + 0.004 * rng.normal()), 2)
            if step < 6:
                for fam, hz, exp in ((H1, "15m", 15), ("h2", "30m", 30), (D1, "3d", 3 * 390)):
                    if rng.uniform() < 0.7:
                        cohort_no += 1
                        picks = [n for n in names if rng.uniform() < 0.6] or [names[rng.below(4)]]
                        intents = {
                            n: (rng.randint(1, 40) * (1 if rng.uniform() < 0.5 else -1))
                            for n in picks
                        }
                        led.open_cohort(
                            (fam, hz, cohort_no),
                            formation_ts=ts,
                            decision_ts=ts,
                            expiry_ts=ts + exp * MIN,
                            intents=intents,
                            forecasts=dict.fromkeys(picks, 1.0),
                        )
            live = [k for k, c in led.cohorts.items() if c.state is CohortState.LIVE]
            if live and rng.uniform() < 0.1:
                led.request_liquidation(live[rng.below(len(live))], "halt")
            withhold = [n for n in names if rng.uniform() < 0.15]
            d = led.decide(ts, dict(mids), withhold=withhold, pre_close=(step == 7))
            for n, o in d.orders.items():
                if o.submitted == 0:
                    continue
                u = rng.uniform()
                frac = 0.0 if u < 0.15 else (1.0 if u < 0.6 else rng.uniform())
                q = int(round(abs(o.submitted) * frac)) * (1 if o.submitted > 0 else -1)
                if q:
                    led.fill(
                        mkfill(
                            ts,
                            n,
                            q,
                            mids[n],
                            round(mids[n] * (1 + 0.0005 * rng.normal()), 4),
                            hs=0.01,
                            imp=0.003,
                        )
                    )
            led.settle()
            # invariants after every decision
            for n in names:
                assert led.positions.get(n, 0) == sum(
                    c.allocated.get(n, 0) for c in led.cohorts.values()
                )
            for c in led.cohorts.values():
                assert not (c.state is CohortState.LIVE and c.expiry_ts <= ts)
        held = {n for c in led.cohorts.values() for n, q in c.allocated.items() if q}
        vals = {n: Valuation(mids[n], cause="depth exhausted") for n in held}
        res = close(led, vals)
        assert abs(res.identity_gap) <= 1e-9
        assert res.closing_cohorts == sum(
            c.state is CohortState.CLOSING for c in led.cohorts.values()
        )
    assert_reconciles(led)
    assert len(led.fills) > 40 and any(e["event"] == "cross" for e in led.events)
    assert (
        any(r.closeout_failures for r in led.sessions)
        and sum(r.waterfall["financing"] for r in led.sessions) > 0
    )
    # cohort-level quantity reconciliation from the event log: every submission is allocated (fill or cross),
    # outstanding (mandatory remainder, re-sent) or cancelled (discretionary remainder); allocations sum to the position
    allocs: dict[tuple, int] = {}
    for e in led.events:
        if e["event"] == "allocation":
            allocs[(e["cohort"], e["name"])] = allocs.get((e["cohort"], e["name"]), 0) + e["qty"]
    for key, c in led.cohorts.items():
        if c.state is CohortState.REFUSED:
            continue
        for n in set(c.submitted) | set(c.intents):
            a = allocs.get((key, n), 0)
            assert c.submitted.get(n, 0) == a + c.outstanding.get(n, 0) + c.cancelled.get(n, 0), (
                key,
                n,
            )
            assert a == c.allocated.get(n, 0), (key, n)
            if n in c.withheld:
                assert c.withheld[n] == c.intents[n]  # a withheld entry is intended, not submitted

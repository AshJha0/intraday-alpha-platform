"""Golden test: pinned fill sets vs expected Perold decomposition (1e-9)."""

from __future__ import annotations

import json

import pytest

from iap.tca.tca import perold_decomposition


@pytest.fixture(scope="module")
def golden(golden_dir):
    with open(golden_dir / "expected_tca.json") as f:
        return json.load(f)


def test_golden_tca_cases_match(golden):
    tol = golden["tolerance"]
    assert tol == 1e-9
    assert len(golden["cases"]) >= 4
    for case in golden["cases"]:
        c = case["inputs"]
        got = perold_decomposition(
            c["side_sign"], c["qty_target"],
            [tuple(f) for f in c["fills"]],
            c["decision_mid"], c["arrival_mid"], c["end_mid"])
        exp = case["expected"]
        assert set(got) == set(exp), case["inputs"]["name"]
        for key, want in exp.items():
            assert abs(got[key] - want) <= tol, \
                f"{c['name']}: {key} {got[key]} != {want}"


def test_golden_tca_internal_identity(golden):
    """The stored expected values themselves satisfy the IS identity."""
    for case in golden["cases"]:
        e = case["expected"]
        assert abs(e["delay_cost"] + e["trading_cost"]
                   + e["opportunity_cost"] - e["total_is"]) <= 1e-9
        assert abs(e["delay_bps"] + e["trading_bps"]
                   + e["opportunity_bps"] - e["total_is_bps"]) <= 1e-9


def test_golden_tca_covers_key_scenarios(golden):
    names = {c["inputs"]["name"] for c in golden["cases"]}
    assert {"buy_full_fill", "sell_partial_fill", "buy_unfilled",
            "fx_sell_full"} <= names
    # the unfilled case is pure opportunity cost
    unfilled = next(c["expected"] for c in golden["cases"]
                    if c["inputs"]["name"] == "buy_unfilled")
    assert unfilled["delay_cost"] == 0.0
    assert unfilled["trading_cost"] == 0.0
    assert unfilled["total_is"] == unfilled["opportunity_cost"]


def _build(case):
    from iap.tca.fills import MarketTimeline
    tl = MarketTimeline()
    for ts, bid, ask, bsz, asz in case["states"]:
        tl.append_state_pinned(ts, bid, ask, bsz, asz)
    for h in case["halts"]:
        tl.add_halt(h)
    return tl


def _order(case, tl):
    from iap.tca.fills import ParentOrder, stamp_fill
    o = case["order"]
    order = ParentOrder(order_id=1, instrument_id=1, side=o["side"],
                        qty_target=o["qty_target"], decision_ts=o["decision_ts"],
                        arrival_ts=o["arrival_ts"], end_ts=o["end_ts"])
    for ts, price, qty, liq in o["fills"]:
        order.fills.append(stamp_fill(tl, ts, price, qty, o["side"], liq))
    return order


def test_golden_timeline_cases_match(golden):
    """v2 timeline cases: builder rule, fill stamping, markout 'defined'
    rule, window validation and the spread/impact split (1e-9)."""
    from iap.tca.tca import (adverse_selection_with_counts, order_tca,
                             spread_and_impact_cost)
    assert golden["x-version"] == 2
    cases = golden["timeline_cases"]
    assert {c["name"] for c in cases} >= {
        "buy_at_session_end", "passive_fill_pre_event_mid",
        "locked_kept_crossed_skipped", "fill_outside_window_rejected",
        "end_beyond_timeline_rejected", "halt_inside_markout_window"}
    tol = golden["tolerance"]
    for c in cases:
        tl = _build(c)
        exp = c["expected"]
        assert len(tl) == exp["n_states"], c["name"]
        assert tl.crossed_states_skipped == exp["crossed_states_skipped"]
        order = _order(c, tl)
        if "expect_error" in c:
            with pytest.raises(ValueError, match=c["expect_error"]):
                order_tca(order, tl)
            continue
        for f, (mid, hs) in zip(order.fills, exp["fill_ref"]):
            assert abs(f.mid_at_fill - mid) <= tol, c["name"]
            assert abs(f.half_spread_at_fill - hs) <= tol, c["name"]
        split = spread_and_impact_cost(order)
        for key in ("spread_cost", "impact_cost", "exec_cost_vs_mid"):
            assert abs(split[key] - exp[key]) <= tol, (c["name"], key)
        markouts, n = adverse_selection_with_counts(order, tl)
        assert n == exp["adverse_selection_n"], c["name"]
        for delta, want in exp["adverse_selection_bps"].items():
            got = markouts[delta]
            if want is None:
                assert got is None, (c["name"], delta)
            else:
                assert abs(got - want) <= tol, (c["name"], delta)
        rec = order_tca(order, tl)
        assert abs(rec["perold"]["total_is"] - exp["total_is"]) <= tol
        assert abs(rec["end_mid"] - exp["end_mid"]) <= tol
        assert rec["adverse_selection_n"] == exp["adverse_selection_n"]

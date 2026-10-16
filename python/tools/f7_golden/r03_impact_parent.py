"""F7 rule 3: simulator rule 6b, impact on the parent (``ExecConfig.impact_model
= "parent"``: incremental temporary leg on the parent's cumulative taker
quantity with ``impact_exponent``, permanent leg on the instrument's
cumulative signed taker flow; ``parent_id == 0`` children are their own
parent)."""

from __future__ import annotations

from iap.execution import AlgoType, ExecPolicy, ParentOrder

from . import SLICE_INSTRUMENT, SLICE_VENUE, base_document
from ._replay import (
    GOLDEN_EVENTS,
    SEC,
    SLICE_EVENTS,
    SLICE_INPUTS,
    SLICE_T0,
    golden_config,
    golden_t0,
    run_scenario,
    scenario,
    slice_config,
)

NAME = "expected_f7_impact_parent.json"


def golden_parents(t0: int) -> list[ParentOrder]:
    return [
        ParentOrder(
            parent_id=1,
            instrument_id=1,
            venue_id=1,
            side=1,
            qty=600,
            algo=AlgoType.IS,
            start_ts=t0 + 120 * SEC,
            end_ts=t0 + 720 * SEC,
            slices=3,
            risk_aversion=1.0,
        ),
        ParentOrder(
            parent_id=2,
            instrument_id=1,
            venue_id=1,
            side=0,
            qty=5000,
            algo=AlgoType.POV,
            start_ts=t0 + 60 * SEC,
            end_ts=t0 + 900 * SEC,
            participation=0.2,
            max_child_qty=100,
        ),
        ParentOrder(
            parent_id=3,
            instrument_id=1,
            venue_id=1,
            side=0,
            qty=900,
            algo=AlgoType.TWAP,
            start_ts=t0 + 200 * SEC,
            end_ts=t0 + 800 * SEC,
            slices=3,
            policy=ExecPolicy.AGGRESSIVE,
            max_child_qty=250,
        ),
        ParentOrder(
            parent_id=0,
            instrument_id=1,
            venue_id=1,
            side=1,
            qty=400,
            algo=AlgoType.IS,
            start_ts=t0 + 300 * SEC,
            end_ts=t0 + 760 * SEC,
            slices=2,
            risk_aversion=0.5,
            max_child_qty=150,
        ),
    ]


def slice_parents() -> list[ParentOrder]:
    t0 = SLICE_T0
    return [
        ParentOrder(
            parent_id=1,
            instrument_id=SLICE_INSTRUMENT,
            venue_id=SLICE_VENUE,
            side=0,
            qty=20000,
            algo=AlgoType.POV,
            start_ts=t0 + 5 * SEC,
            end_ts=t0 + 100 * SEC,
            participation=0.15,
            max_child_qty=500,
        ),
        ParentOrder(
            parent_id=2,
            instrument_id=SLICE_INSTRUMENT,
            venue_id=SLICE_VENUE,
            side=1,
            qty=12000,
            algo=AlgoType.IS,
            start_ts=t0 + 10 * SEC,
            end_ts=t0 + 90 * SEC,
            slices=6,
            risk_aversion=1.5,
            max_child_qty=1000,
        ),
        ParentOrder(
            parent_id=3,
            instrument_id=SLICE_INSTRUMENT,
            venue_id=SLICE_VENUE,
            side=0,
            qty=6000,
            algo=AlgoType.VWAP,
            start_ts=t0 + 20 * SEC,
            end_ts=t0 + 110 * SEC,
            slices=4,
            policy=ExecPolicy.AGGRESSIVE,
            max_child_qty=700,
        ),
        ParentOrder(
            parent_id=0,
            instrument_id=SLICE_INSTRUMENT,
            venue_id=SLICE_VENUE,
            side=1,
            qty=3000,
            algo=AlgoType.TWAP,
            start_ts=t0 + 30 * SEC,
            end_ts=t0 + 95 * SEC,
            slices=3,
            policy=ExecPolicy.AGGRESSIVE,
        ),
    ]


def scenarios() -> list[dict]:
    t0 = golden_t0()
    return [
        scenario(
            "golden stream, linear parent adder, no permanent leg",
            golden_config(impact_model="parent", impact_exponent=1.0),
            GOLDEN_EVENTS,
            golden_parents(t0),
        ),
        scenario(
            "golden stream, square-root parent adder + permanent leg",
            golden_config(
                impact_model="parent", impact_exponent=0.5, permanent_impact_bps_per_pct_adv=0.5
            ),
            GOLDEN_EVENTS,
            golden_parents(t0),
        ),
        scenario(
            "golden stream, exponent 0.7 + permanent 1.25, control: rule 6 child adder",
            golden_config(impact_model="child"),
            GOLDEN_EVENTS,
            golden_parents(t0),
        ),
        scenario(
            "real slice, square-root parent adder + permanent leg 0.3",
            slice_config(
                impact_model="parent", impact_exponent=0.5, permanent_impact_bps_per_pct_adv=0.3
            ),
            SLICE_EVENTS,
            slice_parents(),
            digest=True,
        ),
        scenario(
            "real slice, exponent 0.7 parent adder, no permanent leg",
            slice_config(impact_model="parent", impact_exponent=0.7),
            SLICE_EVENTS,
            slice_parents(),
            digest=True,
        ),
    ]


def document() -> dict:
    doc = base_document(
        "F7 rule 3 golden: simulator rule 6b, parent impact (API_TRADING.md section 2.13 "
        "P0-1; iap.execution.simulator._parent_impact is the reference). Each scenario "
        "replays `events` (golden:events_eq_mbo.jsonl = tests/golden/events_eq_mbo.jsonl; "
        "slice = the real slice, see `slice`) through ExecutionReplay(config, parents) and "
        "records every fill ([fill_id, order_id, parent_id, instrument_id, venue_id, side, "
        "price_ticks, qty, ts, liquidity, fee, impact_cost]), every child ([order_id, "
        "parent_id, venue_id, side, type, limit_ticks, qty, decision_ts, expire_ts, state, "
        "remaining, cancel_reason]) and the per-parent report (keyed by parent_id as a "
        "string). The third scenario is the rule-6 control under the same parents.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r03_impact_parent --force",
    )
    scen = scenarios()
    doc["inputs"] = {"scenarios": scen, "slice": SLICE_INPUTS}
    doc["expected"] = {"scenarios": [run_scenario(s, with_passive=False) for s in scen]}
    return doc

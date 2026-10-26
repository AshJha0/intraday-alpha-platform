"""F7 rule 4: ``ParentOrder.pov_exact`` (target ``floor(p / (1 - p) * V)``, the
whole deficit covered at once in children of at most ``max_child_qty``) under
the NATIVE, AGGRESSIVE and PASSIVE policies, with the plain POV control."""

from __future__ import annotations

from iap.execution import AlgoType, ExecPolicy, ParentOrder, PassiveParams

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

NAME = "expected_f7_pov_exact.json"


def golden_parents(t0: int) -> list[ParentOrder]:
    w = dict(
        instrument_id=1,
        venue_id=1,
        algo=AlgoType.POV,
        start_ts=t0 + 30 * SEC,
        end_ts=t0 + 900 * SEC,
    )
    return [
        ParentOrder(
            parent_id=1, side=0, qty=8000, participation=0.2, max_child_qty=50, pov_exact=True, **w
        ),
        ParentOrder(
            parent_id=2, side=1, qty=8000, participation=0.2, max_child_qty=50, pov_exact=False, **w
        ),
        ParentOrder(
            parent_id=3,
            side=0,
            qty=6000,
            participation=0.3,
            max_child_qty=120,
            pov_exact=True,
            policy=ExecPolicy.PASSIVE,
            urgency=0.4,
            passive=PassiveParams(max_rest_ns=20 * SEC, max_reprices=2, improve_min_spread_ticks=2),
            **w,
        ),
        ParentOrder(
            parent_id=4,
            side=1,
            qty=6000,
            participation=0.3,
            max_child_qty=120,
            pov_exact=False,
            policy=ExecPolicy.PASSIVE,
            urgency=0.4,
            passive=PassiveParams(max_rest_ns=20 * SEC, max_reprices=2, improve_min_spread_ticks=2),
            **w,
        ),
        ParentOrder(
            parent_id=5,
            side=1,
            qty=3000,
            participation=0.1,
            max_child_qty=75,
            pov_exact=True,
            policy=ExecPolicy.AGGRESSIVE,
            **w,
        ),
        ParentOrder(
            parent_id=6,
            side=0,
            qty=2500,
            participation=0.45,
            max_child_qty=1000,
            pov_exact=True,
            **w,
        ),
    ]


def slice_parents() -> list[ParentOrder]:
    t0 = SLICE_T0
    w = dict(
        instrument_id=SLICE_INSTRUMENT,
        venue_id=SLICE_VENUE,
        algo=AlgoType.POV,
        start_ts=t0 + 5 * SEC,
        end_ts=t0 + 100 * SEC,
    )
    return [
        ParentOrder(
            parent_id=1,
            side=0,
            qty=20000,
            participation=0.05,
            max_child_qty=300,
            pov_exact=True,
            **w,
        ),
        ParentOrder(
            parent_id=2,
            side=0,
            qty=20000,
            participation=0.05,
            max_child_qty=300,
            pov_exact=False,
            **w,
        ),
        ParentOrder(
            parent_id=3,
            side=1,
            qty=15000,
            participation=0.04,
            max_child_qty=400,
            pov_exact=True,
            policy=ExecPolicy.PASSIVE,
            urgency=0.3,
            passive=PassiveParams(max_rest_ns=5 * SEC, max_reprices=1, improve_min_spread_ticks=2),
            **w,
        ),
        ParentOrder(
            parent_id=4,
            side=1,
            qty=20000,
            participation=0.05,
            max_child_qty=250,
            pov_exact=True,
            policy=ExecPolicy.AGGRESSIVE,
            **w,
        ),
    ]


def scenarios() -> list[dict]:
    t0 = golden_t0()
    return [
        scenario(
            "golden stream, pov_exact vs plain POV under NATIVE / PASSIVE / AGGRESSIVE",
            golden_config(),
            GOLDEN_EVENTS,
            golden_parents(t0),
        ),
        scenario(
            "real slice, pov_exact vs plain POV",
            slice_config(),
            SLICE_EVENTS,
            slice_parents(),
            digest=True,
        ),
    ]


def document() -> dict:
    doc = base_document(
        "F7 rule 4 golden: ParentOrder.pov_exact (docs/API_REFERENCE.md section 2.13 P0-5; "
        "iap.execution.replay._pov_target and the deficit loops of _schedule / "
        "_schedule_passive are the reference). Same scenario / row shapes as "
        "expected_f7_impact_parent.json plus `passive`: the PassiveStats of every PASSIVE "
        "parent (keyed by parent_id as a string).",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r04_pov_exact --force",
    )
    scen = scenarios()
    doc["inputs"] = {"scenarios": scen, "slice": SLICE_INPUTS}
    doc["expected"] = {"scenarios": [run_scenario(s, with_passive=True) for s in scen]}
    return doc

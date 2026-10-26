"""F7 rule 8: B4 quote fading (``iap.execution.fading``: ``book_pressure``,
``fade_rule`` and the counterfactual ``would_have_filled`` / ``pull_rows`` /
``pull_summary`` / ``pull_outcomes``)."""

from __future__ import annotations

from iap.execution.fading import (
    PullRecord,
    book_pressure,
    fade_rule,
    pull_outcomes,
    pull_rows,
    pull_summary,
    would_have_filled,
)
from iap.execution.signal import ArraySignalSource
from iap.tca.fills import MarketTimeline

from . import SEC, SLICE_INSTRUMENT, SLICE_PROVENANCE, SLICE_TICK, base_document
from ._slice import READINGS_RULE, TIMELINE_RULE, readings, samples, timeline

NAME = "expected_f7_fading.json"
T0 = 1_787_578_200_000_000_000

PRESSURE_CASES = [[100, 100], [0, 0], [300, 100], [100, 300], [0, 50], [50, 0], [1, 999999], [7, 3]]

FADE_CASES = [
    {"side": side, "z": z, "threshold_z": thr, "pressure": pr, "pressure_threshold": pthr}
    for side in (0, 1)
    for z in (None, 1.5, -1.5, 1.0, -1.0, 0.2)
    for thr in (0.0, 1.0)
    for pr in (None, 0.8, 0.5, 0.79999)
    for pthr in (0.0, 0.8)
]

HORIZONS = {"100ms": 100_000_000, "1s": SEC, "5s": 5 * SEC}


def fixed_timeline() -> dict:
    """A 200 s BBO walk with one halt at 120 s: states [ts, bid, ask, bid_sz, ask_sz]."""
    states = []
    bid = 100.00
    ask = 100.02
    for k in range(201):
        if k % 7 == 3:
            bid, ask = bid + 0.01, ask + 0.01
        if k % 11 == 5:
            bid, ask = bid - 0.02, ask - 0.02
        if k % 13 == 0:
            ask = ask + 0.01
        if k % 13 == 6:
            ask = ask - 0.01
        states.append(
            [T0 + k * SEC, round(bid, 2), round(ask, 2), 100 * (1 + k % 5), 100 * (1 + (k * 3) % 7)]
        )
    return {"states": states, "halts": [T0 + 120 * SEC]}


def pull_row(**kw) -> dict:
    r = PullRecord(**kw)
    return r.to_dict()


def fixed_pulls() -> list[dict]:
    rows = []
    specs = [
        (0, 10000, 100, 5, "fade:signal", "pulled", 100, 0),
        (1, 10002, 200, 12, "fade:pressure", "pulled", 200, 0),
        (0, 9999, 300, 20, "fade:signal", "partial", 120, 180),
        (1, 10003, 50, 33, "queue:ahead", "pulled", 50, 0),
        (0, 10001, 400, 47, "fade:pressure", "lost_race", 0, 400),
        (1, 10001, 150, 60, "fade:signal", "pulled", 150, 0),
        (0, 9998, 100, 118, "fade:signal", "pulled", 100, 0),  # halt inside the 5 s window
        (1, 10004, 100, 196, "fade:pressure", "pulled", 100, 0),  # past the timeline end at 5 s
        (0, 10000, 100, 150, "fade:signal", "", 0, 0),  # unresolved: skipped
        (0, 10000, 100, 160, "queue:p_fill", "pulled", 100, 0),
    ]
    for i, (side, px, qty, sec, rule, outcome, pulled, filled) in enumerate(specs, start=1):
        rows.append(
            pull_row(
                parent_id=1 + i % 2,
                order_id=100 + i,
                side=side,
                price_ticks=px,
                qty_at_decision=qty,
                decision_ts=T0 + sec * SEC - 400_000,
                rule=rule,
                ahead_qty=25 * i,
                signal_z=0.1 * i,
                pressure=0.5 + 0.03 * i,
                p_fill=-1.0 if i % 3 else 0.2,
                effect_ts=T0 + sec * SEC + 1_000 if outcome else 0,
                pulled_qty=pulled,
                filled_in_flight=filled,
                outcome=outcome,
            )
        )
    return rows


def pulls_from_rows(rows: list[dict]) -> list[PullRecord]:
    return [PullRecord(**r) for r in rows]


def build_timeline(d: dict) -> MarketTimeline:
    tl = MarketTimeline()
    for ts, bid, ask, bsz, asz in d["states"]:
        tl.append_state_pinned(ts, bid, ask, bsz, asz)
    for h in d["halts"]:
        tl.add_halt(h)
    return tl


WHF_CASES = [
    {"side": 0, "price": 100.02, "start_ts": T0, "end_ts": T0 + 10 * SEC},
    {"side": 0, "price": 99.0, "start_ts": T0, "end_ts": T0 + 200 * SEC},
    {"side": 1, "price": 100.0, "start_ts": T0, "end_ts": T0 + 10 * SEC},
    {"side": 1, "price": 101.0, "start_ts": T0, "end_ts": T0 + 200 * SEC},
    {"side": 0, "price": 100.02, "start_ts": T0 + 300 * SEC, "end_ts": T0 + 400 * SEC},
    {"side": 1, "price": 100.0, "start_ts": T0 + 5 * SEC, "end_ts": T0 + 5 * SEC},
]


def expected_fixed(tl_doc, pulls_doc) -> dict:
    tl = build_timeline(tl_doc)
    pulls = pulls_from_rows(pulls_doc)
    rows_default = pull_rows(pulls, tl, 0.01, horizons_ns=HORIZONS, qty_unit=1.0, cluster="s1")
    rows_queue = pull_rows(
        pulls,
        tl,
        0.01,
        horizons_ns=HORIZONS,
        qty_unit=100.0,
        rules=("queue:ahead", "queue:p_fill"),
        cluster="",
    )
    return {
        "pressure": [book_pressure(a, b) for a, b in PRESSURE_CASES],
        "fade": [
            fade_rule(c["side"], c["z"], c["threshold_z"], c["pressure"], c["pressure_threshold"])
            for c in FADE_CASES
        ],
        "would_have_filled": [
            would_have_filled(tl, c["side"], c["price"], c["start_ts"], c["end_ts"])
            for c in WHF_CASES
        ],
        "rows": rows_default,
        "summary": pull_summary(rows_default, list(HORIZONS), lost_race=1),
        "rows_queue": rows_queue,
        "summary_queue": pull_summary(
            rows_queue, ["1s", "5s"], lost_race=0, rules=("queue:ahead", "queue:p_fill")
        ),
        "outcomes": pull_outcomes(pulls, tl, 0.01, horizons_ns=HORIZONS),
        "summary_empty": pull_summary([], None),
    }


def slice_block() -> tuple[dict, dict]:
    tl = timeline()
    src = ArraySignalSource({SLICE_INSTRUMENT: readings()})
    rules = []
    pulls = []
    for i, s in enumerate(samples()):
        r = src.at(SLICE_INSTRUMENT, s["ts"])
        z = None if r is None else r.z
        for side in (0, 1):
            own, opp = (s["bid_sz"], s["ask_sz"]) if side == 0 else (s["ask_sz"], s["bid_sz"])
            pr = book_pressure(own, opp)
            rules.append(
                {
                    "k": s["k"],
                    "side": side,
                    "pressure": pr,
                    "z": z,
                    "rule_signal": fade_rule(side, z, 1.0, pr, 0.0),
                    "rule_pressure": fade_rule(side, None, 0.0, pr, 0.6),
                    "rule_both": fade_rule(side, z, 0.5, pr, 0.75),
                }
            )
        side = i % 2
        px = s["bid"] if side == 0 else s["ask"]
        rule = ("fade:signal", "fade:pressure", "queue:ahead")[i % 3]
        outcome = ("pulled", "partial", "pulled", "lost_race")[i % 4]
        pulled = {"pulled": 100, "partial": 60, "lost_race": 0}[outcome]
        pulls.append(
            pull_row(
                parent_id=1,
                order_id=1000 + i,
                side=side,
                price_ticks=px,
                qty_at_decision=100,
                decision_ts=s["ts"],
                rule=rule,
                ahead_qty=s["bid_sz"] if side == 0 else s["ask_sz"],
                signal_z=0.0 if z is None else z,
                pressure=book_pressure(s["bid_sz"], s["ask_sz"])
                if side == 0
                else book_pressure(s["ask_sz"], s["bid_sz"]),
                p_fill=-1.0,
                effect_ts=s["ts"] + 1_000_000,
                pulled_qty=pulled,
                filled_in_flight=100 - pulled,
                outcome=outcome,
            )
        )
    records = pulls_from_rows(pulls)
    hz = {"100ms": 100_000_000, "1s": SEC, "5s": 5 * SEC, "30s": 30 * SEC}
    rows = pull_rows(records, tl, SLICE_TICK, horizons_ns=hz, cluster="2019-01-30")
    inputs = dict(
        SLICE_PROVENANCE,
        readings_rule=READINGS_RULE,
        timeline_rule=TIMELINE_RULE,
        rules_rule=(
            "per sample and side: pressure = book_pressure(own L1 size, opposite L1 size); z = the "
            "reading at the sample's exchange_ts; rule_signal = fade_rule(side, z, 1.0, pressure, "
            "0.0); rule_pressure = fade_rule(side, None, 0.0, pressure, 0.6); rule_both = "
            "fade_rule(side, z, 0.5, pressure, 0.75)"
        ),
        pulls=pulls,
        horizons=hz,
        pull_rows_args={"tick_size": SLICE_TICK, "qty_unit": 1.0, "cluster": "2019-01-30"},
    )
    return inputs, {
        "rules": rules,
        "rows": rows,
        "summary": pull_summary(
            rows, list(hz), lost_race=sum(1 for p in pulls if p["outcome"] == "lost_race")
        ),
        "outcomes": pull_outcomes(records, tl, SLICE_TICK, horizons_ns=hz),
    }


def document() -> dict:
    doc = base_document(
        "F7 rule 8 golden: B4 quote fading (iap.execution.fading; docs/API_REFERENCE.md section "
        "2.16): book_pressure cases [own, opp]; fade_rule cases; would_have_filled cases on "
        "the fixed timeline (states [ts, bid, ask, bid_sz, ask_sz], halts); pull_rows / "
        "pull_summary / pull_outcomes over PullRecord rows (PullRecord.to_dict field "
        "names). `rows` = pull_rows(pulls, timeline, 0.01, horizons, qty_unit 1, "
        "cluster 's1'); `summary` = pull_summary(rows, horizons, lost_race=1); `rows_queue` / "
        "`summary_queue` = the queue rules at qty_unit 100, horizons 1s and 5s; `outcomes` = "
        "pull_outcomes(pulls, timeline, 0.01, horizons).",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r08_fading --force",
    )
    tl_doc = fixed_timeline()
    pulls_doc = fixed_pulls()
    sl_inputs, sl = slice_block()
    doc["inputs"] = {
        "pressure": PRESSURE_CASES,
        "fade": FADE_CASES,
        "timeline": tl_doc,
        "would_have_filled": WHF_CASES,
        "pulls": pulls_doc,
        "horizons": HORIZONS,
        "slice": sl_inputs,
    }
    doc["expected"] = dict(expected_fixed(tl_doc, pulls_doc), slice=sl)
    return doc

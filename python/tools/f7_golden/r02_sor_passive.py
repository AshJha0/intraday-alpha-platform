"""F7 rule 2: ``CostAwareRouter`` passive routing with fill probability
(``passive_costs`` / ``route_passive`` with the model's ``p_fill_touch`` and
with ``hazard_fill_prob`` over the X-4 depletion read), plus
``aggressive_costs`` / ``route_aggressive`` and ``allocate_passive``."""

from __future__ import annotations

from iap.execution.config import SorOptions
from iap.execution.sor_v2 import RouterOptions

from . import (
    SLICE_INSTRUMENT,
    SLICE_PROVENANCE,
    SLICE_TICK,
    base_document,
    build_book,
    event_from_row,
    is_sample,
    load_slice,
)
from ._sor import (
    LEDGER_FILLS,
    T0,
    TICK,
    calibration_docs,
    fill_prob_from,
    model_row,
    options_from_row,
    options_row,
    router,
    slice_models,
    sor_options_row,
    three_venue_books,
    venue_models,
)

NAME = "expected_f7_sor_passive.json"
SEC = 1_000_000_000

BASE = RouterOptions()
PEN = RouterOptions(latency_penalty_bps_per_ms=0.5, miss_cost_bps=1.25, tox_horizon="100ms")
ALLOC = RouterOptions(max_toxicity_bps=1.0, tox_scale_bps=0.5, lot=100)
ALLOC_1 = RouterOptions(tox_scale_bps=2.0, lot=1)


def _q(kind, side, ts, options, candidates=None, fill_prob=None, qty=0, tick=TICK):
    return {
        "kind": kind,
        "side": side,
        "ts": ts,
        "qty": qty,
        "tick": tick,
        "candidates": candidates,
        "fill_prob": fill_prob,
        "options": options_row(options),
    }


def _hz(source, horizon_s):
    return {"source": source, "horizon_s": horizon_s}


def scenarios() -> list[dict]:
    q = T0 + 10 * SEC
    base = three_venue_books()
    queries = [
        _q("passive", 0, q, BASE),
        _q("passive", 1, q, BASE),
        _q("passive", 0, q, PEN),
        _q("passive", 1, q, PEN),
        _q("passive", 0, q, BASE, fill_prob=_hz("exec", 1.0)),
        _q("passive", 1, q, BASE, fill_prob=_hz("exec", 1.0)),
        _q("passive", 0, q, BASE, fill_prob=_hz("pooled", 0.5)),
        _q("passive", 1, q, PEN, fill_prob=_hz("cancel", 2.0)),
        _q("passive", 0, q, BASE, candidates=[2, 3], fill_prob=_hz("exec", 1.0)),
        _q("aggressive", 0, q, BASE),
        _q("aggressive", 1, q, BASE),
        _q("aggressive", 0, q, PEN),
        _q("aggressive", 1, q, PEN, candidates=[1, 2]),
        _q("allocate", 0, q, ALLOC, qty=1050),
        _q("allocate", 1, q, ALLOC, qty=1050),
        _q("allocate", 0, q, ALLOC_1, qty=1001),
        _q("allocate", 1, q, ALLOC_1, qty=7),
        _q("allocate", 0, q, ALLOC, qty=0),  # error
    ]
    halt = three_venue_books(halt_v3=True)
    stale = three_venue_books(gap_v2=True)
    return [
        {
            "label": "three venues",
            "venues": [model_row(m) for _, m in sorted(venue_models().items())],
            "sor_options": sor_options_row(SorOptions()),
            "ledger_fills": LEDGER_FILLS,
            "events": base.rows,
            "queries": queries,
        },
        {
            "label": "venue 3 halted, no ledger fills",
            "venues": [model_row(m) for _, m in sorted(venue_models().items())],
            "sor_options": sor_options_row(SorOptions()),
            "ledger_fills": [],
            "events": halt.rows,
            "queries": [
                _q("passive", 0, q, BASE),
                _q("aggressive", 1, q, BASE),
                _q("allocate", 0, q, ALLOC, qty=500),
            ],
        },
        {
            "label": "venue 2 stale, latency cap 120us",
            "venues": [model_row(m) for _, m in sorted(venue_models().items())],
            "sor_options": sor_options_row(SorOptions(max_venue_latency_ns=120_000)),
            "ledger_fills": LEDGER_FILLS,
            "events": stale.rows,
            "queries": [
                _q("passive", 0, q, BASE, fill_prob=_hz("exec", 1.0)),
                _q("aggressive", 0, q, BASE),
                _q("allocate", 1, q, ALLOC_1, qty=333),
            ],
        },
    ]


def _costs(d):
    return {str(k): v for k, v in sorted(d.items())}


def run_queries(r, book, queries, docs) -> list:
    out = []
    for qd in queries:
        opts = options_from_row(qd["options"])
        r.options = opts
        fp = None
        if qd["fill_prob"] is not None:
            fp = fill_prob_from(docs, qd["fill_prob"]["source"], qd["fill_prob"]["horizon_s"])
        try:
            if qd["kind"] == "passive":
                costs = r.passive_costs(
                    book, qd["side"], qd["ts"], qd["tick"], qd["candidates"], fp
                )
                route = r.route_passive(
                    book, qd["side"], qd["ts"], qd["tick"], qd["candidates"], fp
                )
                out.append({"costs": _costs(costs), "route": route})
            elif qd["kind"] == "aggressive":
                costs = r.aggressive_costs(book, qd["side"], qd["ts"], qd["tick"], qd["candidates"])
                route = r.route_aggressive(book, qd["side"], qd["ts"], qd["tick"], qd["candidates"])
                out.append({"costs": _costs(costs), "route": route})
            else:
                alloc = r.allocate_passive(book, qd["side"], qd["qty"], qd["candidates"])
                out.append({"allocation": _costs(alloc)})
        except ValueError as exc:
            out.append({"error": str(exc)})
    return out


def expected_fixed(scen, docs) -> list:
    from iap.execution.types import VenueSpec
    from iap.execution.venues_model import FeeTier, VenueModel

    out = []
    for s in scen:
        models = {}
        for row in s["venues"]:
            spec = VenueSpec(**row["spec"])
            models[spec.venue_id] = VenueModel(
                spec,
                p_fill_touch=row["p_fill_touch"],
                toxicity_bps=row["toxicity_bps"],
                latency_ns=row["latency_ns"],
                tiers=tuple(FeeTier(*t) for t in row["tiers"]),
            )
        book = build_book(7, [event_from_row(r) for r in s["events"]])
        r = router(models, RouterOptions(), SorOptions(**s["sor_options"]), s["ledger_fills"])
        out.append(run_queries(r, book, s["queries"], docs))
    return out


def slice_block(docs) -> tuple[dict, list]:
    events = load_slice()
    models = slice_models()
    r = router(models, RouterOptions(), SorOptions(), [])
    book = build_book(SLICE_INSTRUMENT, [])
    rows = []
    for k, ev in enumerate(events, start=1):
        book.apply(ev)
        if not is_sample(k):
            continue
        t = ev.exchange_ts
        q = [
            _q("passive", 0, t, BASE, tick=SLICE_TICK),
            _q("passive", 1, t, BASE, tick=SLICE_TICK),
            _q("passive", 0, t, PEN, fill_prob=_hz("exec", 1.0), tick=SLICE_TICK),
            _q("passive", 1, t, BASE, fill_prob=_hz("exec", 5.0), tick=SLICE_TICK),
            _q("aggressive", 0, t, BASE, tick=SLICE_TICK),
            _q("aggressive", 1, t, PEN, tick=SLICE_TICK),
            _q("allocate", 0, t, ALLOC, qty=1050, tick=SLICE_TICK),
            _q("allocate", 1, t, ALLOC_1, qty=777, tick=SLICE_TICK),
        ]
        rows.append({"k": k, "ts": t, "results": run_queries(r, book, q, docs)})
    inputs = dict(
        SLICE_PROVENANCE,
        venues=[model_row(m) for m in models.values()],
        sor_options=sor_options_row(SorOptions()),
        queries=(
            "at every sample, in order: passive buy / sell (model p_fill), passive buy with "
            "latency penalty 0.5 bps/ms + miss 1.25 bps + 100ms toxicity and the exec hazard "
            "fill_prob at 1 s, passive sell with the exec hazard at 5 s, aggressive buy, "
            "aggressive sell (penalty options), allocate buy 1050 (lot 100, tox cap 1.0, "
            "scale 0.5), allocate sell 777 (lot 1, scale 2.0); ts = the sampled event's "
            'exchange_ts; the hazard tables are `calibration_docs["101"]`'
        ),
    )
    return inputs, rows


def document() -> dict:
    doc = base_document(
        "F7 rule 2 golden: iap.execution.sor_v2.CostAwareRouter passive routing with fill "
        "probability (docs/API_REFERENCE.md section 2.9 / 2.13 P0-4): passive_costs / "
        "route_passive with the model's p_fill_touch or with "
        "hazard_fill_prob(rate, horizon_s) where rate(vid, side, q) reads the per-venue "
        "calibration document (`calibration_docs`, keyed by venue id; `source` exec = "
        "queue_exec_depletion_hazard (X-4), pooled = queue_depletion_hazard, cancel = "
        "queue_cancel_depletion_hazard; a venue without a document or an empty cell is "
        "None and skipped); aggressive_costs / route_aggressive; allocate_passive. "
        "Costs are keyed by venue id as strings; route 0 = NO_ROUTE; {error} = ValueError. "
        "Books and ledger fills as in expected_f7_sor_sweep.json.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r02_sor_passive --force",
    )
    docs = calibration_docs()
    scen = scenarios()
    sl_inputs, sl_rows = slice_block(docs)
    doc["inputs"] = {"calibration_docs": docs, "fixed": scen, "slice": sl_inputs}
    doc["expected"] = {"fixed": expected_fixed(scen, docs), "slice": sl_rows}
    return doc

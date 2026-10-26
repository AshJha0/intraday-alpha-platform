"""F7 rule 1: ``CostAwareRouter.plan_sweep`` (all-in and price ranking, both
staggers, limit, candidate subset, eligibility: HALT, stale, latency cap)."""

from __future__ import annotations

from iap.execution.config import SorOptions
from iap.execution.sor_v2 import RouterOptions

from . import (
    SAMPLE_EVERY,
    SLICE_INSTRUMENT,
    SLICE_PROVENANCE,
    SLICE_TICK,
    SLICE_VENUE,
    base_document,
    build_book,
    is_sample,
    load_slice,
)
from ._sor import (
    LEDGER_FILLS,
    T0,
    TICK,
    model_row,
    options_from_row,
    options_row,
    router,
    slice_models,
    sor_options_row,
    three_venue_books,
    venue_models,
)

NAME = "expected_f7_sor_sweep.json"
SEC = 1_000_000_000


def _children(ch):
    return [[c.venue_id, c.price_ticks, c.qty, c.send_offset_ns, c.arrival_offset_ns] for c in ch]


def _scenario(label, models, events, sor_options, queries):
    return {
        "label": label,
        "venues": [model_row(m) for _, m in sorted(models.items())],
        "sor_options": sor_options_row(sor_options),
        "ledger_fills": LEDGER_FILLS,
        "events": events,
        "queries": queries,
    }


def _query(side, qty, ts, options, limit_ticks=None, candidates=None, tick=TICK):
    return {
        "side": side,
        "qty": qty,
        "ts": ts,
        "limit_ticks": limit_ticks,
        "candidates": candidates,
        "tick": tick,
        "options": options_row(options),
    }


PRICE = RouterOptions()
PRICE_NONE = RouterOptions(stagger="none")
ALL_IN = RouterOptions(sweep_rank="all_in")
ALL_IN_3 = RouterOptions(sweep_rank="all_in", sweep_levels=2)


def scenarios() -> list[dict]:
    q = T0 + 10 * SEC
    base = three_venue_books()
    normal = [
        _query(0, 900, q, PRICE),
        _query(0, 900, q, ALL_IN),
        _query(0, 2500, q, PRICE_NONE),
        _query(0, 2500, q, ALL_IN),
        _query(1, 1200, q, PRICE),
        _query(1, 1200, q, ALL_IN),
        _query(0, 700, q, PRICE, limit_ticks=10002),
        _query(1, 2000, q, ALL_IN, limit_ticks=9998),
        _query(0, 1500, q, ALL_IN_3),
        _query(0, 1500, q, PRICE, candidates=[3, 1]),
        _query(1, 400, q, ALL_IN, candidates=[2]),
        _query(0, 100000, q, PRICE),  # more than the displayed depth
        _query(0, 0, q, PRICE),  # error: qty must be > 0
        _query(0, 100, q, ALL_IN, tick=None),  # error: all_in needs tick
    ]
    big = three_venue_books(halt_v3=True)
    stale = three_venue_books(gap_v2=True)
    return [
        _scenario("three venues", venue_models(), base.rows, SorOptions(), normal),
        _scenario(
            "venue 1 dear taker (fee outweighs a tick under all_in)",
            venue_models(all_in_fees=True),
            base.rows,
            SorOptions(),
            [_query(0, 900, q, PRICE), _query(0, 900, q, ALL_IN), _query(1, 900, q, ALL_IN)],
        ),
        _scenario(
            "venue 3 halted",
            venue_models(),
            big.rows,
            SorOptions(),
            [_query(0, 900, q, PRICE), _query(0, 900, q, ALL_IN)],
        ),
        _scenario(
            "venue 2 stale (sequence gap)",
            venue_models(),
            stale.rows,
            SorOptions(),
            [_query(0, 900, q, PRICE), _query(1, 900, q, ALL_IN)],
        ),
        _scenario(
            "latency cap excludes venue 2",
            venue_models(),
            base.rows,
            SorOptions(max_venue_latency_ns=200_000),
            [_query(0, 900, q, PRICE), _query(0, 900, q, ALL_IN)],
        ),
    ]


def run_queries(r, book, queries) -> list:
    from . import event_from_row  # noqa: F401 (documentation of the input form)

    out = []
    for qd in queries:
        opts = options_from_row(qd["options"])
        r.options = opts
        try:
            ch = r.plan_sweep(
                book,
                qd["side"],
                qd["qty"],
                qd["ts"],
                limit_ticks=qd["limit_ticks"],
                candidates=qd["candidates"],
                tick=qd["tick"],
            )
        except ValueError as exc:
            out.append({"error": str(exc)})
            continue
        out.append({"children": _children(ch)})
    return out


def expected_fixed(scen: list[dict]) -> list:
    from iap.execution.types import VenueSpec
    from iap.execution.venues_model import FeeTier, VenueModel

    from . import event_from_row

    out = []
    for s in scen:
        models = {}
        for row in s["venues"]:
            sp = row["spec"]
            spec = VenueSpec(**sp)
            models[spec.venue_id] = VenueModel(
                spec,
                p_fill_touch=row["p_fill_touch"],
                toxicity_bps=row["toxicity_bps"],
                latency_ns=row["latency_ns"],
                tiers=tuple(FeeTier(*t) for t in row["tiers"]),
            )
        book = build_book(7, [event_from_row(r) for r in s["events"]])
        r = router(models, RouterOptions(), SorOptions(**s["sor_options"]), s["ledger_fills"])
        out.append(run_queries(r, book, s["queries"]))
    return out


def slice_block() -> tuple[dict, list]:
    events = load_slice()
    models = slice_models()
    r = router(models, RouterOptions(), SorOptions(), [])
    book = build_book(SLICE_INSTRUMENT, [])
    rows = []
    for k, ev in enumerate(events, start=1):
        book.apply(ev)
        if not is_sample(k):
            continue
        vb = book.books[SLICE_VENUE]
        ba = vb.best_ask()
        bb = vb.best_bid()
        q = [
            _query(0, 5000, ev.exchange_ts, PRICE, tick=SLICE_TICK),
            _query(1, 5000, ev.exchange_ts, ALL_IN, tick=SLICE_TICK),
            _query(
                0,
                3000,
                ev.exchange_ts,
                ALL_IN,
                limit_ticks=None if ba is None else ba[0] + 2,
                tick=SLICE_TICK,
            ),
            _query(
                1,
                800,
                ev.exchange_ts,
                PRICE_NONE,
                limit_ticks=None if bb is None else bb[0],
                tick=SLICE_TICK,
            ),
        ]
        rows.append({"k": k, "ts": ev.exchange_ts, "results": run_queries(r, book, q)})
    inputs = dict(
        SLICE_PROVENANCE,
        venues=[model_row(m) for m in models.values()],
        sor_options=sor_options_row(SorOptions()),
        queries=(
            "at every sample, in order: plan_sweep(buy 5000, price rank), (sell 5000, all_in), "
            "(buy 3000, all_in, limit best_ask + 2), (sell 800, price rank, stagger none, "
            "limit best_bid); ts = the sampled event's exchange_ts; tick = the slice tick"
        ),
    )
    assert len(rows) == len([1 for k in range(1, len(events) + 1) if is_sample(k)])
    assert SAMPLE_EVERY == 500
    return inputs, rows


def document() -> dict:
    doc = base_document(
        "F7 rule 1 golden: iap.execution.sor_v2.CostAwareRouter.plan_sweep (docs/API_REFERENCE.md "
        "section 2.9 / 2.13 P0-4) on fixed three-venue books (events = MarketEvent rows "
        "[event_id, instrument_id, venue_id, exchange_ts, receive_ts, sequence, event_type, "
        "side, price_ticks, qty, order_id, trade_id] applied to a ConsolidatedBook of "
        "instrument 7) and on the sampled real slice. Each query is "
        "plan_sweep(book, side, qty, ts, limit_ticks=, candidates=, tick=) under its own "
        "RouterOptions; the ledger fills are recorded before any query (tiered fees). "
        "expected children rows: [venue_id, price_ticks, qty, send_offset_ns, "
        "arrival_offset_ns]; an {error} entry means the call raised ValueError.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r01_sor_sweep --force",
    )
    scen = scenarios()
    sl_inputs, sl_rows = slice_block()
    doc["inputs"] = {"fixed": scen, "slice": sl_inputs}
    doc["expected"] = {"fixed": expected_fixed(scen), "slice": sl_rows}
    return doc

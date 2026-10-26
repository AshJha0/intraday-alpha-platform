"""F7 part 2, rule 11: the B10 dark / midpoint venue model (``iap.execution.dark``:
``sigmoid``, ``dark_features``, ``DarkFillModel.p_fill`` / the fitted document
read, ``midpoint_ticks``, the stateless ``dark_pretrade_check`` the replay
evaluates, ``DarkFirstParams``), the dark-first replay rule
(``ExecutionReplay._dark_try`` / ``_work_dark``: eligibility, the seeded
draw, the lit-tape-driven fill at the midpoint, expiry to the lit book) and
the ``dark`` fill type of the B6 decomposition.

Every dark fill is MODELLED, not measured (the B10 label); this golden pins
the model's arithmetic and the replay rule, not a market."""

from __future__ import annotations

from iap.execution import AlgoType, ExecPolicy, ParentOrder, PassiveParams, VenueSpec
from iap.execution.dark import (
    DARK_CONTROL_GOLDENS,
    DarkControls,
    DarkFillModel,
    DarkFirstParams,
    dark_features,
    dark_pretrade_check,
    midpoint_ticks,
    sigmoid,
)
from iap.execution.types import ChildOrder, Fill, Liquidity, OrderType
from iap.tca.markout_decomposition import fill_type

from . import REPO, SEC, SLICE_INSTRUMENT, SLICE_VENUE, base_document
from ._replay import (
    GOLDEN_EVENTS,
    SLICE_EVENTS,
    SLICE_INPUTS,
    SLICE_T0,
    _report_row,
    _stats_row,
    config_from_row,
    config_row,
    events_for,
    golden_config,
    golden_t0,
    parent_from_row,
    parent_row,
    slice_config,
)

NAME = "expected_f7_dark_venue.json"
DARK_VID = 190
MULTI7_MODEL = REPO / "research" / "execution" / "dark" / "dark_fill_model_multi7.json"


def model_doc(m: DarkFillModel) -> dict:
    return m.to_dict()


def multi7_model() -> DarkFillModel:
    return DarkFillModel.load(MULTI7_MODEL)


MODELS: dict[str, DarkFillModel] = {}


def models() -> dict[str, DarkFillModel]:
    if not MODELS:
        MODELS.update(
            {
                "multi7": multi7_model(),
                "prior": DarkFillModel.prior(30 * SEC),
                "sure": DarkFillModel(coef=(50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC),
                "never": DarkFillModel(coef=(-50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC),
                "volume": DarkFillModel(coef=(-3.0, 1.5, 0.0, 0.0), horizon_ns=60 * SEC),
                "mixed": DarkFillModel(coef=(-1.2, 0.7, -0.3, -0.9), horizon_ns=45 * SEC),
            }
        )
    return MODELS


# ------------------------------------------------------------ scalar cases --

SIGMOID_CASES = [0.0, 1.0, -1.0, 30.0, -30.0, 745.0, -745.0, 1e-300, -1e-300, 0.5, -37.5, 12.25]
FEATURE_CASES = [
    [lv, sp, q, ah]
    for lv in (0, 1, 99, 100, 1000, 12345, 2_000_000)
    for sp in (0, 1, 2, 5)
    for q in (1, 100, 300, 3000)
    for ah in (0, 50, 2000)
][::5]
FEATURE_ERRORS = [[-1, 1, 100, 0], [0, -1, 100, 0], [0, 1, 0, 0], [0, 1, 100, -1]]
MIDPOINT_CASES = [
    [b, a, s]
    for b in (9999, 10000, 16319, 1)
    for a in (10000, 10001, 10003, 16320, 16325)
    for s in (0, 1)
]
CONTROL_CASES = [
    {
        "controls": dict(
            DarkControls(**kw).to_dict(),
        ),
        "venue_type": vt,
        "qty": qty,
        "trailing_lit_volume": lit,
        "dark_notional_so_far": so_far,
        "child_notional": child,
    }
    for kw, vt, qty, lit, so_far, child, _ in DARK_CONTROL_GOLDENS
] + [
    {
        "controls": DarkControls(max_participation=0.1, participation_window_ns=5 * SEC).to_dict(),
        "venue_type": "midpoint",
        "qty": 10,
        "trailing_lit_volume": 100,
        "dark_notional_so_far": 0.0,
        "child_notional": 0.0,
    },
    {
        "controls": DarkControls(max_participation=0.1).to_dict(),
        "venue_type": "dark",
        "qty": 10,
        "trailing_lit_volume": 99,
        "dark_notional_so_far": 0.0,
        "child_notional": 0.0,
    },
    {
        "controls": DarkControls(max_dark_notional=1.0).to_dict(),
        "venue_type": "dark",
        "qty": 1,
        "trailing_lit_volume": 0,
        "dark_notional_so_far": 0.5,
        "child_notional": 0.5,
    },
    {
        "controls": DarkControls(max_dark_notional=1.0).to_dict(),
        "venue_type": "dark",
        "qty": 1,
        "trailing_lit_volume": 0,
        "dark_notional_so_far": 0.5,
        "child_notional": 0.5000000000000001,
    },
    {
        "controls": DarkControls().to_dict(),
        "venue_type": "lit",
        "qty": 0,
        "trailing_lit_volume": 0,
        "dark_notional_so_far": 0.0,
        "child_notional": 0.0,
    },
]


def controls_from_row(r: dict) -> DarkControls:
    return DarkControls(
        max_participation=r["max_participation"],
        participation_window_ns=r["participation_window_ns"],
        allowed_venue_types=tuple(r["allowed_venue_types"]),
        max_dark_notional=r["max_dark_notional"],
    )


def expected_scalar() -> dict:
    out: dict = {"sigmoid": [sigmoid(x) for x in SIGMOID_CASES]}
    feats = []
    for lv, sp, q, ah in FEATURE_CASES:
        feats.append(dark_features(lv, sp, q, ah))
    out["features"] = feats
    errs = []
    for lv, sp, q, ah in FEATURE_ERRORS:
        try:
            dark_features(lv, sp, q, ah)
            errs.append(None)
        except ValueError as exc:
            errs.append(str(exc))
    out["feature_errors"] = errs
    pf = {}
    for name, m in models().items():
        pf[name] = [m.p_fill(lv, sp, q, ah) for lv, sp, q, ah in FEATURE_CASES]
    out["p_fill"] = pf
    out["midpoint"] = [midpoint_ticks(b, a, s) for b, a, s in MIDPOINT_CASES]
    ctl = []
    for c in CONTROL_CASES:
        try:
            ctl.append(
                dark_pretrade_check(
                    controls_from_row(c["controls"]),
                    c["venue_type"],
                    c["qty"],
                    c["trailing_lit_volume"],
                    c["dark_notional_so_far"],
                    c["child_notional"],
                )
            )
        except ValueError as exc:
            ctl.append({"error": str(exc)})
    out["controls"] = ctl
    # the fill type of a modelled dark fill (B6), tested before the liquidity split
    o = ChildOrder(type=OrderType.LIMIT)
    o.order_id = 1
    o.replaces_order_id = 7
    o.cancel_rule = "fade:signal"
    o.cancel_rule_ts = 0
    f_dark = Fill(
        1,
        1,
        1,
        1,
        DARK_VID,
        0,
        10000,
        100,
        10,
        Liquidity.MAKER,
        0.1,
        0.0,
        provenance="dark_midpoint",
    )
    f_lit = Fill(
        2, 1, 1, 1, 1, 0, 10000, 100, 10, Liquidity.MAKER, 0.1, 0.0, provenance="exec_print"
    )
    f_tak = Fill(
        3,
        1,
        1,
        1,
        DARK_VID,
        0,
        10000,
        100,
        10,
        Liquidity.TAKER,
        0.1,
        0.0,
        provenance="dark_midpoint",
    )
    out["fill_type"] = [fill_type(f_dark, o), fill_type(f_lit, o), fill_type(f_tak, o)]
    return out


# ------------------------------------------------------------ replay rows --


def dark_first_row(d: DarkFirstParams, model_name: str) -> dict:
    return {
        "model": model_name,
        "venue_id": d.venue_id,
        "controls": d.controls.to_dict(),
        "max_urgency": d.max_urgency,
        "regimes": list(d.regimes),
        "horizon_ns": d.horizon_ns,
    }


def parent_row_v2(p: ParentOrder, model_name: str) -> dict:
    r = parent_row(p)
    r["dark_first"] = None if p.dark_first is None else dark_first_row(p.dark_first, model_name)
    return r


def parent_from_row_v2(r: dict, docs: dict) -> ParentOrder:
    kw = dict(r)
    d = kw.pop("dark_first")
    p = parent_from_row(kw)
    if d is None:
        return p
    from dataclasses import replace

    return replace(
        p,
        dark_first=DarkFirstParams(
            model=DarkFillModel.from_dict(docs[d["model"]]),
            venue_id=d["venue_id"],
            controls=controls_from_row(d["controls"]),
            max_urgency=d["max_urgency"],
            regimes=tuple(d["regimes"]),
            horizon_ns=d["horizon_ns"],
        ),
    )


def config_row_v2(cfg) -> dict:
    r = config_row(cfg)
    for v in r["venues"]:
        v["venue_type"] = cfg.venues[v["venue_id"]].venue_type
    return r


def config_from_row_v2(row: dict):
    from dataclasses import replace

    cfg = config_from_row(
        {
            **row,
            "venues": [{k: v for k, v in r.items() if k != "venue_type"} for r in row["venues"]],
        }
    )
    venues = {
        vid: replace(
            spec, venue_type=next(r["venue_type"] for r in row["venues"] if r["venue_id"] == vid)
        )
        for vid, spec in cfg.venues.items()
    }
    return replace(cfg, venues=venues)


def _fill_row(f: Fill) -> list:
    d = f.to_dict()
    return [
        d["fill_id"],
        d["order_id"],
        d["parent_id"],
        d["instrument_id"],
        d["venue_id"],
        d["side"],
        d["price_ticks"],
        d["qty"],
        d["ts"],
        d["liquidity"],
        d["fee"],
        d["impact_cost"],
        f.provenance == "dark_midpoint",
    ]


def _child_row(o: ChildOrder) -> list:
    return [
        o.order_id,
        o.parent_id,
        o.venue_id,
        o.side,
        int(o.type),
        o.limit_ticks,
        o.qty,
        o.decision_ts,
        o.expire_ts,
        int(o.state),
        o.remaining,
        int(o.cancel_reason),
        o.venue_type,
        o.dark_outcome,
        o.dark_u,
        o.dark_p_fill,
    ]


def _report_row_v2(r) -> dict:
    d = _report_row(r)
    d.update(
        {
            "dark_children": r.dark_children,
            "dark_posted_qty": r.dark_posted_qty,
            "dark_filled_qty": r.dark_filled_qty,
            "dark_expired_qty": r.dark_expired_qty,
            "dark_unsent_qty": r.dark_unsent_qty,
            "dark_notional": r.dark_notional,
            "dark_rejected": dict(sorted(r.dark_rejected.items())),
        }
    )
    return d


def run_scenario(s: dict, docs: dict) -> dict:
    from iap.contracts.versions import canonical_json
    from iap.core.codec import sha256_bytes
    from iap.execution import ExecutionReplay

    cfg = config_from_row_v2(s["config"])
    parents = [parent_from_row_v2(r, docs) for r in s["parents"]]
    res = ExecutionReplay(cfg, parents).run(events_for(s["events"]))
    out = {
        "events_processed": res.events_processed,
        "sor_no_route": res.sor_no_route,
        "fills": [_fill_row(f) for f in res.fills],
        "parents": {str(k): _report_row_v2(r) for k, r in sorted(res.parents.items())},
        "children": [_child_row(o) for pid in sorted(res.children) for o in res.children[pid]],
        "passive": {str(k): _stats_row(v) for k, v in sorted(res.passive.items())},
    }
    if s["digest"]:
        for key in ("fills", "children"):
            rows = out.pop(key)
            out[key + "_count"] = len(rows)
            out[key + "_sha256"] = sha256_bytes(canonical_json(rows).encode("ascii"))
    return out


def dark_venue(venue_type: str = "dark", fee: float = 0.001) -> VenueSpec:
    return VenueSpec(
        venue_id=DARK_VID, name="DARK1", taker_fee_per_share=fee, venue_type=venue_type
    )


def with_dark(cfg, venue_type: str = "dark"):
    from dataclasses import replace

    return replace(cfg, venues={**cfg.venues, DARK_VID: dark_venue(venue_type)})


def golden_parents(
    t0: int, model: str, controls: DarkControls | None = None, horizon_ns: int = 0
) -> list[ParentOrder]:
    def dfp(**kw):
        return DarkFirstParams(
            model=models()[model], controls=controls or DarkControls(), horizon_ns=horizon_ns, **kw
        )

    return [
        ParentOrder(
            parent_id=1,
            instrument_id=1,
            venue_id=0,
            side=0,
            qty=1200,
            algo=AlgoType.TWAP,
            start_ts=t0 + 30 * SEC,
            end_ts=t0 + 700 * SEC,
            slices=6,
            max_child_qty=150,
            dark_first=dfp(),
        ),
        ParentOrder(
            parent_id=2,
            instrument_id=1,
            venue_id=1,
            side=1,
            qty=900,
            algo=AlgoType.VWAP,
            start_ts=t0 + 60 * SEC,
            end_ts=t0 + 760 * SEC,
            slices=4,
            max_child_qty=200,
            policy=ExecPolicy.PASSIVE,
            urgency=0.4,
            passive=PassiveParams(max_rest_ns=20 * SEC, max_reprices=1),
            dark_first=dfp(),
        ),
        ParentOrder(
            parent_id=3,
            instrument_id=1,
            venue_id=1,
            side=0,
            qty=5000,
            algo=AlgoType.POV,
            start_ts=t0 + 40 * SEC,
            end_ts=t0 + 900 * SEC,
            participation=0.2,
            max_child_qty=120,
            pov_exact=True,
            dark_first=dfp(),
        ),
        ParentOrder(
            parent_id=4,
            instrument_id=1,
            venue_id=1,
            side=1,
            qty=600,
            algo=AlgoType.IS,
            start_ts=t0 + 120 * SEC,
            end_ts=t0 + 720 * SEC,
            slices=3,
            policy=ExecPolicy.AGGRESSIVE,
            urgency=0.9,
            dark_first=dfp(),
        ),
        ParentOrder(
            parent_id=5,
            instrument_id=1,
            venue_id=1,
            side=0,
            qty=400,
            algo=AlgoType.TWAP,
            start_ts=t0 + 200 * SEC,
            end_ts=t0 + 500 * SEC,
            slices=2,
            max_child_qty=100,
            dark_first=dfp(max_urgency=0.2),
        ),
        ParentOrder(
            parent_id=6,
            instrument_id=1,
            venue_id=1,
            side=1,
            qty=300,
            algo=AlgoType.TWAP,
            start_ts=t0 + 300 * SEC,
            end_ts=t0 + 820 * SEC,
            slices=3,
        ),
    ]


def slice_parents(model: str, controls: DarkControls | None = None) -> list[ParentOrder]:
    t0 = SLICE_T0

    def dfp(**kw):
        return DarkFirstParams(model=models()[model], controls=controls or DarkControls(), **kw)

    w = dict(instrument_id=SLICE_INSTRUMENT, venue_id=SLICE_VENUE)
    return [
        ParentOrder(
            parent_id=1,
            side=0,
            qty=12000,
            algo=AlgoType.TWAP,
            start_ts=t0 + 5 * SEC,
            end_ts=t0 + 100 * SEC,
            slices=8,
            max_child_qty=500,
            dark_first=dfp(),
            **w,
        ),
        ParentOrder(
            parent_id=2,
            side=1,
            qty=9000,
            algo=AlgoType.VWAP,
            start_ts=t0 + 10 * SEC,
            end_ts=t0 + 110 * SEC,
            slices=5,
            max_child_qty=600,
            policy=ExecPolicy.PASSIVE,
            urgency=0.3,
            passive=PassiveParams(max_rest_ns=5 * SEC, max_reprices=1, improve_min_spread_ticks=2),
            dark_first=dfp(horizon_ns=8 * SEC),
            **w,
        ),
        ParentOrder(
            parent_id=3,
            side=0,
            qty=20000,
            algo=AlgoType.POV,
            start_ts=t0 + 5 * SEC,
            end_ts=t0 + 100 * SEC,
            participation=0.05,
            max_child_qty=300,
            pov_exact=True,
            dark_first=dfp(horizon_ns=10 * SEC),
            **w,
        ),
        ParentOrder(
            parent_id=4,
            side=1,
            qty=6000,
            algo=AlgoType.IS,
            start_ts=t0 + 20 * SEC,
            end_ts=t0 + 90 * SEC,
            slices=4,
            policy=ExecPolicy.AGGRESSIVE,
            urgency=0.5,
            max_child_qty=1500,
            dark_first=dfp(horizon_ns=15 * SEC),
            **w,
        ),
    ]


def scenario(label, cfg, events, parents, model, digest=False) -> dict:
    return {
        "label": label,
        "config": config_row_v2(cfg),
        "events": events,
        "parents": [parent_row_v2(p, model) for p in parents],
        "digest": digest,
    }


def scenarios() -> list[dict]:
    t0 = golden_t0()
    return [
        scenario(
            "golden stream + dark venue 190, multi7 fit",
            with_dark(golden_config()),
            GOLDEN_EVENTS,
            golden_parents(t0, "multi7"),
            "multi7",
        ),
        scenario(
            "golden stream, prior model",
            with_dark(golden_config()),
            GOLDEN_EVENTS,
            golden_parents(t0, "prior"),
            "prior",
        ),
        scenario(
            "golden stream, sure model (every post fills at the next event)",
            with_dark(golden_config()),
            GOLDEN_EVENTS,
            golden_parents(t0, "sure"),
            "sure",
        ),
        scenario(
            "golden stream, never model (every post expires to the lit book)",
            with_dark(golden_config()),
            GOLDEN_EVENTS,
            golden_parents(t0, "never", horizon_ns=15 * SEC),
            "never",
        ),
        scenario(
            "golden stream, midpoint venue with controls: only 'dark' allowed, participation 0.1 / 30 s, notional 60000",
            with_dark(golden_config(), "midpoint"),
            GOLDEN_EVENTS,
            golden_parents(
                t0,
                "sure",
                DarkControls(
                    max_participation=0.1,
                    participation_window_ns=30 * SEC,
                    allowed_venue_types=("dark",),
                    max_dark_notional=60_000.0,
                ),
            ),
            "sure",
        ),
        scenario(
            "golden stream, controls participation 0.5 / 60 s + notional 40000",
            with_dark(golden_config()),
            GOLDEN_EVENTS,
            golden_parents(
                t0,
                "mixed",
                DarkControls(
                    max_participation=0.5,
                    participation_window_ns=60 * SEC,
                    max_dark_notional=40_000.0,
                ),
            ),
            "mixed",
        ),
        scenario(
            "real slice + DARK1, multi7 fit",
            with_dark(slice_config()),
            SLICE_EVENTS,
            slice_parents("multi7"),
            "multi7",
            digest=True,
        ),
        scenario(
            "real slice + DARK1, mixed model, participation 0.1 / 30 s",
            with_dark(slice_config()),
            SLICE_EVENTS,
            slice_parents(
                "mixed", DarkControls(max_participation=0.1, participation_window_ns=30 * SEC)
            ),
            "mixed",
            digest=True,
        ),
    ]


def document() -> dict:
    doc = base_document(
        "F7 part 2 golden, rule 11: the B10 dark / midpoint venue model (docs/API_REFERENCE.md section "
        "2.17; iap.execution.dark and the dark-first rule of iap.execution.replay are the "
        "reference; every dark fill is MODELLED, not measured). scalar: sigmoid, "
        "dark_features ([lit_volume, spread_ticks, qty, queue_ahead]), p_fill per model, "
        "midpoint_ticks ([bid, ask, side]), the stateless dark_pretrade_check (the Rust risk "
        "engine owns the controls, F8; this is the replay-path mirror) and the B6 fill type of "
        "a dark_midpoint fill. scenarios: ExecutionReplay with dark_first parents over the "
        "golden stream and the real slice; fills rows = the 12 pinned fields + "
        "is_dark (provenance == dark_midpoint); children rows = the 12 pinned fields + "
        "venue_type, dark_outcome, dark_u, dark_p_fill; parent reports carry the dark_* "
        "accounting. `models` holds every DarkFillModel document the parents name.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r11_dark_venue --force",
    )
    docs = {k: model_doc(m) for k, m in models().items()}
    scen = scenarios()
    doc["inputs"] = {
        "models": docs,
        "sigmoid": SIGMOID_CASES,
        "features": FEATURE_CASES,
        "feature_errors": FEATURE_ERRORS,
        "midpoint": MIDPOINT_CASES,
        "controls": CONTROL_CASES,
        "scenarios": scen,
        "slice": SLICE_INPUTS,
    }
    doc["expected"] = dict(expected_scalar(), scenarios=[run_scenario(s, docs) for s in scen])
    return doc

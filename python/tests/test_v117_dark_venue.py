"""v1.17 WS-B item B10: dark / midpoint venue model (modelled, not measured).

Synthetic books only. A config without a dark venue and a parent without
``dark_first`` replay byte-identically (the execution goldens); here the
lit stream is also shown identical with a dark venue merely PRESENT in the
config (it is never a router candidate).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from iap.core.events import EventType
from iap.execution import (
    AlgoType,
    ArraySignalSource,
    ChildOrder,
    ExecPolicy,
    ExecutionReplay,
    Fill,
    Liquidity,
    ParentOrder,
    VenueSpec,
)
from iap.execution.dark import (
    DARK_CONTROL_CODES,
    DARK_CONTROL_GOLDENS,
    DARK_ID_BASE,
    MODELLED_LABEL,
    VENUE_TYPES,
    DarkControls,
    DarkFillModel,
    DarkFirstParams,
    calibrate_dark_model,
    dark_features,
    dark_pretrade_check,
    fit_logistic,
    hidden_print_samples,
    midpoint_ticks,
    sigmoid,
)
from iap.execution.sor_v2 import CostAwareRouter
from iap.execution.types import FILL_PROVENANCES
from iap.execution.urgency import AlphaUrgencyPolicy
from iap.execution.venues_model import VenueModel
from iap.orderbook.book import ConsolidatedBook
from iap.research import execution_benchmark as bench
from iap.tca.fills import MarketTimeline
from iap.tca.markout_decomposition import FILL_TYPES, decompose, fill_rows, fill_type
from test_exec_algos import SEC, T0, _ev, algo_config

INS = 7
DARK_VID = 90


def dark_config(venue_type: str = "dark"):
    cfg = algo_config()
    dark = VenueSpec(
        venue_id=DARK_VID, name="DRK", taker_fee_per_share=0.001, venue_type=venue_type
    )
    return replace(cfg, venues={**cfg.venues, DARK_VID: dark})


def lit_stream(n_s: int = 200, exec_qty: int = 40):
    """A two-sided venue-1 book (bid 9_999 / ask 10_001, 1,000 x 1,000) and
    one 40-lot EXECUTE against the ask (with its TRADE print) every second."""
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid=0):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, 9_999, 100, 1)
    push(T0 - SEC, EventType.ADD, 1, 10_001, 1_000, 2)
    for k in range(n_s):
        t = T0 + k * SEC
        # a buyer lifts exec_qty at the ask ...
        push(t, EventType.ADD, 1, 10_001, exec_qty, 100 + k)
        push(t + 1, EventType.EXECUTE, 1, 10_001, exec_qty, 100 + k, 1_000 + k)
        push(t + 1, EventType.TRADE, 0, 10_001, exec_qty, 0, 1_000 + k)
        # ... and a seller hits 200 at the bid (through the 100 resting ahead of us)
        push(t + 2, EventType.ADD, 0, 9_999, 200, 10_000 + k)
        push(t + 3, EventType.EXECUTE, 0, 9_999, 200, 10_000 + k, 2_000 + k)
        push(t + 3, EventType.TRADE, 1, 9_999, 200, 0, 2_000 + k)
    return evs


def parent(**kw) -> ParentOrder:
    base = dict(
        parent_id=1,
        instrument_id=INS,
        venue_id=0,
        side=0,
        qty=400,
        algo=AlgoType.TWAP,
        start_ts=T0,
        end_ts=T0 + 100 * SEC,
        slices=4,
        max_child_qty=1_000,
    )
    base.update(kw)
    return ParentOrder(**base)


def digest(res) -> str:
    rows = [f.to_dict() for f in res.fills]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------ VenueSpec --


def test_venue_type_defaults_lit_and_validates():
    v = VenueSpec(venue_id=1)
    assert v.venue_type == "lit" and v.is_lit
    assert VENUE_TYPES == ("lit", "dark", "midpoint")
    for vt in ("dark", "midpoint"):
        assert not VenueSpec(venue_id=2, venue_type=vt).is_lit
    with pytest.raises(ValueError):
        VenueSpec(venue_id=3, venue_type="grey")
    with pytest.raises(ValueError):
        VenueSpec(venue_id=4, venue_type="dark", is_fx=True)


def test_load_venues_reads_venue_type(tmp_path):
    from iap.execution.config import load_venues

    doc = {
        "venues": [
            {"venue": "L", "venue_id": 1, "latency": {"mean_ns": 0, "jitter_ns": 0}},
            {
                "venue": "D",
                "venue_id": 2,
                "venue_type": "midpoint",
                "taker_fee_per_share": 0.001,
                "latency": {"mean_ns": 0, "jitter_ns": 0},
            },
        ]
    }
    (tmp_path / "v.json").write_text(json.dumps(doc), encoding="utf-8")
    vs = load_venues(tmp_path / "v.json")
    assert vs[1].venue_type == "lit" and vs[2].venue_type == "midpoint"


def test_dark_venue_in_config_leaves_the_lit_replay_byte_identical():
    evs = lit_stream()
    for pol in (ExecPolicy.NATIVE, ExecPolicy.PASSIVE, ExecPolicy.AGGRESSIVE):
        p = parent(policy=pol)
        a = ExecutionReplay(algo_config(), [p]).run(evs)
        b = ExecutionReplay(dark_config(), [p]).run(evs)
        assert a.fills and digest(a) == digest(b)
        assert all(f.venue_id != DARK_VID for f in b.fills)


def test_cost_aware_router_never_quotes_a_dark_venue():
    cfg = dark_config()
    models = {
        v: VenueModel(spec=s, p_fill_touch=0.5, toxicity_bps={"1s": 0.0}, latency_ns=0)
        for v, s in cfg.venues.items()
    }
    book = ConsolidatedBook(INS)
    for ev in lit_stream(2):
        book.apply(ev)
    r = CostAwareRouter(models)
    assert DARK_VID not in r.aggressive_costs(book, 0, T0, 0.01)
    assert DARK_VID not in r.passive_costs(book, 0, T0, 0.01)


# ------------------------------------------------------------ the model --


def test_sigmoid_and_features_are_plain_and_monotone():
    assert sigmoid(0.0) == 0.5 and 0.0 <= sigmoid(-800.0) < 1e-10 and sigmoid(800.0) == 1.0
    x = dark_features(1_000, 2, 300, 100)
    assert x[0] == 1.0 and x[2] == 2.0
    assert x[1] == pytest.approx(__import__("math").log1p(10.0))
    assert x[3] == pytest.approx(__import__("math").log1p(4.0))
    with pytest.raises(ValueError):
        dark_features(-1, 1, 100)
    m = DarkFillModel.prior()
    assert m.label == MODELLED_LABEL and not m.fitted
    assert m.p_fill(1_000, 1, 100) == pytest.approx(0.5, abs=0.02)
    assert m.p_fill(10_000, 1, 100) > m.p_fill(1_000, 1, 100) > m.p_fill(0, 1, 100)
    assert m.p_fill(1_000, 5, 100) < m.p_fill(1_000, 1, 100)
    assert m.p_fill(1_000, 1, 1_000) < m.p_fill(1_000, 1, 100)
    assert m.p_fill(1_000, 1, 100, queue_ahead=900) == m.p_fill(1_000, 1, 1_000)


def test_model_document_round_trips_and_carries_the_label(tmp_path):
    m = replace(DarkFillModel.prior(), fitted=True, n_samples=10, sessions=("x",))
    d = m.to_dict()
    assert d["label"] == MODELLED_LABEL and "hidden" in d["proxy"]
    m.save(tmp_path / "m.json")
    back = DarkFillModel.load(tmp_path / "m.json")
    assert back == m
    d["label"] = "measured"
    with pytest.raises(ValueError):
        DarkFillModel.from_dict(d)


def test_fit_logistic_recovers_a_planted_model():
    from iap.core.rng import SplitMix64

    rng = SplitMix64(11)
    true = [-1.0, 0.9, -0.3, -0.4]
    xs, ys = [], []
    for _ in range(6_000):
        v = int(rng.uniform() * 5_000)
        sp = 1 + int(rng.uniform() * 4)
        q = 100 * (1 + int(rng.uniform() * 10))
        x = dark_features(v, sp, q)
        z = sum(c * xi for c, xi in zip(true, x, strict=True))
        xs.append(x)
        ys.append(1 if rng.uniform() < sigmoid(z) else 0)
    coef, ll, it = fit_logistic(xs, ys)
    assert it < 50 and ll < 0.0
    for c, t in zip(coef, true, strict=True):
        assert abs(c - t) < 0.2


def hidden_fixture():
    """A session where hidden inside-spread prints follow displayed volume:
    every second one 50-lot EXECUTE; every 2 s a hidden midpoint print of
    30 shares (so 60 shares per 4 s window)."""
    evs = []
    seq = 0

    def push(ts, etype, side, px, qty, oid, tid=0):
        nonlocal seq
        seq += 1
        evs.append(_ev(seq, ts, etype, side, px, qty, oid, tid))

    push(T0 - SEC, EventType.ADD, 0, 9_998, 1_000, 1)
    push(T0 - SEC, EventType.ADD, 1, 10_002, 1_000, 2)
    for k in range(120):
        t = T0 + k * SEC
        push(t, EventType.ADD, 1, 10_002, 50, 100 + k)
        push(t + 1, EventType.EXECUTE, 1, 10_002, 50, 100 + k, 1_000 + k)
        push(t + 1, EventType.TRADE, 0, 10_002, 50, 0, 1_000 + k)  # the print: not hidden
        if k % 2 == 0:
            push(t + 500_000_000, EventType.TRADE, 0, 10_000, 30, 0, 5_000 + k)  # hidden, inside
        if k % 10 == 0:
            push(t + 600_000_000, EventType.TRADE, 0, 10_002, 10, 0, 6_000 + k)  # hidden, AT ask
    return evs


def test_hidden_print_samples_use_the_proxy_definition():
    smp = hidden_print_samples(
        hidden_fixture(), horizon_ns=4 * SEC, step_ns=4 * SEC, probe_sizes=(30, 60, 100)
    )
    assert smp.instruments == [INS]
    assert smp.n_hidden_prints == 60 + 12  # inside-spread + at-the-touch hidden prints
    assert smp.n_hidden_inside_spread == 60
    assert smp.n_probes > 20 and len(smp.ys) == 3 * smp.n_probes
    # per window: 60 hidden shares => labels 1, 1, 0 for sizes 30, 60, 100 (full windows)
    by_size = {30: set(), 60: set(), 100: set()}
    for x, y in zip(smp.xs, smp.ys, strict=True):
        q = round((__import__("math").expm1(x[3])) * 100)
        by_size[q].add(y)
    assert by_size[100] == {0}
    assert 1 in by_size[30] and 1 in by_size[60]
    # the lit-volume feature is the PRIOR window's displayed volume (200 shares per 4 s)
    assert max(x[1] for x in smp.xs) == pytest.approx(__import__("math").log1p(2.0))
    assert all(x[2] == 4.0 for x in smp.xs)


def test_calibrate_dark_model_on_the_fixture_is_labelled_and_sane():
    m = calibrate_dark_model(
        [("fix", hidden_fixture())],
        horizon_ns=4 * SEC,
        step_ns=4 * SEC,
        probe_sizes=(30, 60, 100),
        source="fixture",
    )
    assert m.fitted and m.label == MODELLED_LABEL and m.sessions == ("fix",)
    assert m.n_samples == 3 * (m.n_samples // 3) and 0 < m.n_positive < m.n_samples
    assert m.coef[3] < 0.0  # a larger order is less likely to fill
    assert m.p_fill(200, 4, 30) > 0.5 > m.p_fill(200, 4, 100)
    with pytest.raises(ValueError):
        calibrate_dark_model(
            [("fix", hidden_fixture())], horizon_ns=4 * SEC, step_ns=4 * SEC, probe_sizes=(1,)
        )


def test_dark_cli_writes_a_labelled_document(tmp_path):
    from iap.core.codec import write_jsonl
    from iap.execution.dark import main

    (tmp_path / "normalized").mkdir()
    write_jsonl(tmp_path / "normalized" / "eq_20190101.normalized.jsonl", hidden_fixture())
    out = tmp_path / "dark.json"
    rc = main(
        [
            "--data-dir",
            str(tmp_path),
            "--out",
            str(out),
            "--horizon-s",
            "4",
            "--step-s",
            "4",
            "--probe-sizes",
            "30,60,100",
        ]
    )
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["label"] == MODELLED_LABEL and doc["fitted"] and doc["sessions"] == ["20190101"]
    assert "fingerprint" in doc and doc["proxy"].startswith("Nasdaq ITCH non-displayed prints")


# ------------------------------------------------------------- controls --


def test_dark_controls_golden_vectors_for_f8():
    assert DARK_CONTROL_CODES == ("venue_type", "participation", "notional")
    for kw, vt, qty, lit, so_far, child, want in DARK_CONTROL_GOLDENS:
        assert dark_pretrade_check(DarkControls(**kw), vt, qty, lit, so_far, child) == want
    with pytest.raises(ValueError):
        dark_pretrade_check(DarkControls(), "dark", 0, 0, 0.0, 0.0)
    with pytest.raises(ValueError):
        DarkControls(max_participation=-0.1)
    with pytest.raises(ValueError):
        DarkControls(allowed_venue_types=("grey",))
    assert DarkControls().to_dict()["allowed_venue_types"] == ["dark", "midpoint"]


def test_midpoint_rounds_against_us():
    assert midpoint_ticks(9_999, 10_001, 0) == 10_000 == midpoint_ticks(9_999, 10_001, 1)
    assert midpoint_ticks(9_999, 10_002, 0) == 10_001  # buy rounds up
    assert midpoint_ticks(9_999, 10_002, 1) == 10_000  # sell rounds down


# --------------------------------------------------------------- replay --


def dark_first(**kw) -> DarkFirstParams:
    base = dict(model=DarkFillModel.prior(20 * SEC), controls=DarkControls())
    base.update(kw)
    return DarkFirstParams(**base)


def test_dark_first_fills_at_the_midpoint_and_is_modelled():
    evs = lit_stream()
    # a model that fills at once: p = 1 for anything
    sure = DarkFillModel(coef=(50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC)
    p = parent(dark_first=dark_first(model=sure))
    res = ExecutionReplay(dark_config(), [p]).run(evs)
    rep = res.parents[1]
    assert rep.filled_qty == 400 and rep.dark_children == 4 and rep.dark_filled_qty == 400
    assert rep.dark_expired_qty == 0 and rep.dark_rejected == {}
    for f in res.fills:
        assert f.venue_id == DARK_VID and f.price_ticks == 10_000  # the midpoint
        assert f.provenance == "dark_midpoint" and f.liquidity == Liquidity.MAKER
        assert f.fee == pytest.approx(0.001 * f.qty) and f.impact_cost == 0.0
        assert f.fill_id >= DARK_ID_BASE and f.order_id >= DARK_ID_BASE
    kids = res.children[1]
    assert all(k.venue_type == "dark" and k.dark_outcome == "filled" for k in kids)
    assert all(0.0 <= k.dark_u < 1.0 and k.dark_p_fill == 1.0 for k in kids)
    assert rep.diagnostics()["dark_fills_label"] == MODELLED_LABEL
    assert rep.dark_notional == pytest.approx(400 * 10_000 * 0.01)
    assert "dark_midpoint" in FILL_PROVENANCES


def test_dark_first_expiry_sends_the_remainder_to_the_lit_book():
    evs = lit_stream()
    never = DarkFillModel(coef=(-50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC)
    p = parent(dark_first=dark_first(model=never), policy=ExecPolicy.AGGRESSIVE)
    res = ExecutionReplay(dark_config(), [p]).run(evs)
    rep = res.parents[1]
    assert rep.dark_children == 4 and rep.dark_filled_qty == 0 and rep.dark_expired_qty == 400
    assert rep.filled_qty == 400  # the remainder crossed on the lit venue
    lit = [f for f in res.fills if f.venue_id != DARK_VID]
    assert len(lit) == 4 and all(f.liquidity == Liquidity.TAKER for f in lit)
    kids = res.children[1]
    dark = [k for k in kids if k.venue_type == "dark"]
    assert len(dark) == 4 and all(k.dark_outcome == "expired" for k in dark)
    # the remainder was issued at the first event on / after the deadline (20 s later)
    for f, d in zip(sorted(lit, key=lambda f: f.ts), dark, strict=True):
        assert f.ts >= d.decision_ts + 20 * SEC
    # a lit child replaces nothing (a fresh step), and expired at its deadline
    assert all(k.replaces_order_id == 0 for k in kids)


def test_dark_first_is_driven_by_lit_volume_and_the_seeded_draw():
    evs = lit_stream(exec_qty=40)
    cfg = dark_config()
    # p rises with lit volume only; same seed => same draws => same fills
    m = DarkFillModel(coef=(-3.0, 1.5, 0.0, 0.0), horizon_ns=60 * SEC)
    p = parent(dark_first=dark_first(model=m), slices=1, qty=300)
    a = ExecutionReplay(cfg, [p]).run(evs)
    b = ExecutionReplay(cfg, [p]).run(evs)
    assert digest(a) == digest(b)
    k = a.children[1][0]
    assert k.venue_type == "dark" and k.dark_outcome == "filled"
    f = a.fills[0]
    # fill at the first event where p(lit volume since the post) >= u
    execs = [e for e in evs if e.event_type == EventType.EXECUTE and T0 <= e.exchange_ts <= f.ts]
    vol = sum(e.qty for e in execs)
    before = vol - execs[-1].qty
    assert m.p_fill(vol, 2, 300) >= k.dark_u > m.p_fill(before, 2, 300)
    # another seed => another draw => (generally) another fill time
    c = ExecutionReplay(replace(cfg, seed=99), [p]).run(evs)
    assert c.children[1][0].dark_u != k.dark_u


def test_dark_controls_reject_and_fall_back_to_lit():
    evs = lit_stream()
    sure = DarkFillModel(coef=(50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC)
    # participation: 100-share steps vs 1 % of a trailing 30 s volume (30 * 240 = 7,200)
    ctl = DarkControls(max_participation=0.01, participation_window_ns=30 * SEC)
    p = parent(dark_first=dark_first(model=sure, controls=ctl))
    rep = ExecutionReplay(dark_config(), [p]).run(evs).parents[1]
    assert rep.dark_rejected == {"participation": 4} and rep.dark_children == 0
    assert rep.filled_qty == 400
    # notional: 2 steps of 100 x 100.00 fit under 25,000, the third does not
    ctl = DarkControls(max_dark_notional=25_000.0)
    p = parent(dark_first=dark_first(model=sure, controls=ctl))
    rep = ExecutionReplay(dark_config(), [p]).run(evs).parents[1]
    assert rep.dark_children == 2 and rep.dark_rejected == {"notional": 2}
    # venue type: a midpoint venue when only "dark" is allowed
    ctl = DarkControls(allowed_venue_types=("dark",))
    p = parent(dark_first=dark_first(model=sure, controls=ctl))
    rep = ExecutionReplay(dark_config("midpoint"), [p]).run(evs).parents[1]
    assert rep.dark_rejected == {"venue_type": 4}
    # a high-urgency parent never goes dark
    p = parent(dark_first=dark_first(model=sure), urgency=0.9)
    rep = ExecutionReplay(dark_config(), [p]).run(evs).parents[1]
    assert rep.dark_children == 0 and rep.dark_rejected == {}
    # no dark venue in the config: a ValueError at construction
    with pytest.raises(ValueError):
        ExecutionReplay(algo_config(), [parent(dark_first=dark_first(model=sure))])


def test_dark_first_follows_the_b2_regime_for_alpha_urgency_parents():
    evs = lit_stream()
    sure = DarkFillModel(coef=(50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC)
    # a buy: z < 0 (price expected to fall) agrees with waiting -> dark; z > 0 is adverse
    src = ArraySignalSource(
        {INS: [(T0 + k * SEC, -2.0 if k < 50 else 2.0, 1e-4) for k in range(-1, 100)]}
    )
    p = parent(
        dark_first=dark_first(model=sure),
        signal_source=src,
        urgency_policy="alpha",
        alpha_urgency=AlphaUrgencyPolicy(threshold_z=1.0),
    )
    res = ExecutionReplay(dark_config(), [p]).run(evs)
    rep = res.parents[1]
    assert rep.dark_children == 2 and rep.dark_filled_qty == 200
    kids = res.children[1]
    assert [k.urgency_regime for k in kids] == ["agree", "agree", "adverse", "adverse"]
    assert rep.urgency_children == {"agree": 2, "adverse": 2}


def test_dark_fills_are_their_own_type_in_the_b6_decomposition():
    evs = lit_stream()
    sure = DarkFillModel(coef=(50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC)
    p = parent(dark_first=dark_first(model=sure))
    res = ExecutionReplay(dark_config(), [p]).run(evs)
    orders = {o.order_id: o for o in res.children[1]}
    assert "dark" in FILL_TYPES
    assert all(fill_type(f, orders[f.order_id]) == "dark" for f in res.fills)
    tl = MarketTimeline()
    for k in range(0, 200, 1):
        tl.append(T0 + k * SEC, 99.99, 100.01, 1_000, 1_000)
    rows = fill_rows(res.fills, orders, tl, 0.01, horizons_ns={"1s": SEC}, cluster="d1")
    d = decompose(rows, ["1s"])
    assert set(d["by_fill_type"]) == {"dark"} and d["fill_type_share_of_qty"] == {"dark": 1.0}
    cell = d["by_fill_type"]["dark"]["horizons"]["1s"]
    assert cell["markout"]["n"] == 4 and cell["price_impact"]["mean"] == pytest.approx(0.0)
    # by hand: a fill built without provenance is typed by liquidity as before
    plain = Fill(1, 1, 1, INS, 1, 0, 10_000, 1, T0, Liquidity.MAKER, 0.0, 0.0)
    assert fill_type(plain, ChildOrder(order_id=1)) == "passive"


# ------------------------------------------------------------ benchmark --


def test_benchmark_dark_variant_runs_and_is_labelled(tmp_path):
    from pathlib import Path

    from iap.core.codec import write_jsonl

    repo = Path(__file__).resolve().parents[2]
    data = tmp_path / "ds"
    (data / "normalized").mkdir(parents=True)
    write_jsonl(data / "normalized" / "eq_20190101.normalized.jsonl", lit_stream(2_400))
    lit = json.loads((repo / "configs" / "venues" / "venues.json").read_text(encoding="utf-8"))
    both = {
        "venues": [
            *lit["venues"],
            {
                "venue": "DRK",
                "venue_id": DARK_VID,
                "venue_type": "dark",
                "taker_fee_per_share": 0.001,
                "latency": {"mean_ns": 0, "jitter_ns": 0},
            },
        ]
    }
    (tmp_path / "venues_dark.json").write_text(json.dumps(both), encoding="utf-8")
    sure = DarkFillModel(coef=(50.0, 0.0, 0.0, 0.0), horizon_ns=20 * SEC)
    sure.save(tmp_path / "dark_model.json")
    common = dict(
        config_dir=repo / "configs",
        algos=("TWAP",),
        sizes_pct=(0.01,),
        window_min=5.0,
        warmup_min=1.0,
        slices=2,
        timestamp=False,
        venues=tmp_path / "venues_dark.json",
        dark_model=tmp_path / "dark_model.json",
        dark_max_participation=0.0,
    )
    doc = bench.run_benchmark(
        data, policies=("native", "native+dark"), baseline_policy="native", **common
    )
    assert doc["dark"]["used"] and doc["dark"]["label"] == MODELLED_LABEL
    assert doc["dark"]["venue_id"] == DARK_VID and doc["dark"]["model_doc"]["fitted"] is False
    tot = doc["dark"]["totals"]["native+dark"]
    assert tot["dark_filled_qty"] > 0 and tot["label"] == MODELLED_LABEL
    assert "native+dark-native|TWAP|0.01" in doc["paired_vs_baseline"]
    assert "dark" in doc["markout_decomposition"]["native+dark"]["by_fill_type"]
    md = bench.render(doc)
    assert "modelled, not measured" in md and "Dark venue" in md
    # the same grid twice is byte-identical
    doc2 = bench.run_benchmark(
        data, policies=("native", "native+dark"), baseline_policy="native", **common
    )
    assert json.dumps(doc, sort_keys=True) == json.dumps(doc2, sort_keys=True)
    # without a dark venue the variant refuses
    with pytest.raises(SystemExit):
        bench.run_benchmark(
            data,
            policies=("native", "native+dark"),
            baseline_policy="native",
            **{**common, "venues": None},
        )
    # the lit baseline is unchanged by the dark venue's presence
    doc3 = bench.run_benchmark(data, policies=("native",), baseline_policy="native", **common)
    assert doc3["cells"]["native|TWAP|0.01"] == doc["cells"]["native|TWAP|0.01"]
    assert doc3["dark"]["used"] is False

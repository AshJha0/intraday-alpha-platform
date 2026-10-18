"""REL-05 Level 1: report loader, recorder, recorded-vs-replay comparison, tape test.

Synthetic data only.  Level 2 (real recorded fills) is not covered and not claimed.
"""

from __future__ import annotations

import dataclasses
import json
import shutil

import numpy as np
import pytest
from iap.contracts.types import ExecStatus, ExecutionReport
from iap.execution import AlgoType, ExecutionReplay, ParentOrder
from iap.execution.fill_hazard import FillHazardModel
from iap.execution.passive import ExecPolicy
from iap.paper import fill_tape, sim_to_real
from iap.paper import recorder as R
from iap.tca import report_loader as L
from iap.tca.fills import MarketTimeline
from test_maker_economics import INS, T0, VEN, exec_config, synth_events

TICK = 0.01


# ------------------------------------------------------------------ loader
def _rep(**kw):
    base = dict(
        order_id=1,
        execution_id=1,
        status=ExecStatus.NEW,
        filled_qty=0,
        fill_price_ticks=0,
        venue_id=1,
        exchange_ts=100,
        receive_ts=150,
        fees=0.0,
    )
    base.update(kw)
    return ExecutionReport(**base)


GOOD = [
    _rep(),
    _rep(
        execution_id=2,
        status=ExecStatus.PARTIAL,
        filled_qty=40,
        fill_price_ticks=10_000,
        exchange_ts=200,
        receive_ts=230,
        fees=-0.08,
    ),
    _rep(
        execution_id=3,
        status=ExecStatus.FILLED,
        filled_qty=60,
        fill_price_ticks=10_001,
        exchange_ts=300,
        receive_ts=310,
        fees=0.12,
    ),
]


def test_loader_json_roundtrip_and_csv(tmp_path):
    text = L.dump_json(GOOD)
    assert L.parse_json(text) == GOOD
    csv_text = (
        "#iap.execution_reports/1\n"
        + ",".join(L.COLUMNS)
        + "\n"
        + "\n".join(
            f"{r.order_id},{r.execution_id},{r.status.name},{r.filled_qty},{r.fill_price_ticks},"
            f"{r.venue_id},{r.exchange_ts},{r.receive_ts},{r.fees}"
            for r in GOOD
        )
    )
    assert L.parse_csv(csv_text) == GOOD
    p = tmp_path / "r.json"
    p.write_text(text, encoding="utf-8")
    assert L.load_execution_reports(p) == GOOD
    with pytest.raises(L.ReportFileError, match="suffix"):
        L.load_execution_reports(tmp_path / "r.txt")


def _doc(rows):
    return json.dumps({"schema": L.SCHEMA, "version": 1, "reports": rows})


def _row(r):
    return json.loads(L.dump_json([r]))["reports"][0]


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda rows: rows.append(dict(rows[0])), "duplicate execution_id"),
        (lambda rows: rows[2].update(receive_ts=225, exchange_ts=225), "backwards"),
        (lambda rows: rows[1].update(exchange_ts=50), "backwards"),
        (lambda rows: rows[1].update(receive_ts=150, exchange_ts=160), "receive_ts"),
        (lambda rows: rows[0].update(status="BOGUS"), "bad status"),
        (lambda rows: rows[0].update(extra=1), "unknown columns"),
        (lambda rows: rows[1].update(filled_qty=0), "filled_qty"),
        (
            lambda rows: rows.append(_row(_rep(execution_id=9, exchange_ts=400, receive_ts=410))),
            "terminal",
        ),
    ],
)
def test_loader_rejects(mutate, match):
    rows = [_row(r) for r in GOOD]
    mutate(rows)
    with pytest.raises(L.ReportFileError, match=match):
        L.parse_json(_doc(rows))


def test_loader_rejects_bad_schema():
    with pytest.raises(L.ReportFileError):
        L.parse_json(json.dumps({"schema": "x", "version": 1, "reports": []}))
    with pytest.raises(L.ReportFileError):
        L.parse_json(json.dumps({"schema": L.SCHEMA, "version": 2, "reports": []}))
    with pytest.raises(L.ReportFileError):
        L.parse_csv("a,b\n1,2")


def test_parent_from_reports_and_overfill():
    tl = MarketTimeline()
    for i in range(5):
        tl.append(i * 100, 99.99, 100.01, 500, 500)
    kw = dict(
        parent_id=7,
        instrument_id=1,
        side=0,
        decision_ts=100,
        arrival_ts=100,
        end_ts=400,
        timeline=tl,
        tick_size=TICK,
    )
    p = L.parent_from_reports(qty_target=100, reports=GOOD, **kw)
    assert p.qty_filled == 100 and len(p.fills) == 2
    with pytest.raises(L.ReportFileError, match="filled"):
        L.parent_from_reports(qty_target=90, reports=GOOD, **kw)
    with pytest.raises(L.ReportFileError, match="outside"):
        L.parent_from_reports(qty_target=100, reports=GOOD, **{**kw, "end_ts": 250})


# ------------------------------------------------------- recording builders
def _parents():
    return [
        ParentOrder(
            parent_id=1,
            instrument_id=INS,
            venue_id=VEN,
            side=0,
            qty=600,
            algo=AlgoType.TWAP,
            start_ts=T0 + 1_000_000_000,
            end_ts=T0 + 9_000_000_000,
            slices=6,
            max_child_qty=200,
        ),
        ParentOrder(
            parent_id=2,
            instrument_id=INS,
            venue_id=VEN,
            side=1,
            qty=400,
            algo=AlgoType.TWAP,
            start_ts=T0 + 2_000_000_000,
            end_ts=T0 + 10_000_000_000,
            slices=4,
            max_child_qty=100,
            policy=ExecPolicy.PASSIVE,
            urgency=0.3,
        ),
    ]


@pytest.fixture(scope="module")
def events():
    return synth_events(n_steps=1500)


@pytest.fixture(scope="module")
def replay(events):
    return ExecutionReplay(exec_config(), _parents()).run(events)


def make_recording(events, replay, tmp_path, *, price_shift=0, drop_fills=False, name="rec"):
    """A recorder fed by a replay's own children and fills (mono clock == event time)."""
    rec = R.PaperRecorder(R.ClockMapping(wall_ns0=0, mono_ns0=0, uncertainty_ns=1), mono=lambda: 0)
    for p in _parents():
        rec.register_parent(
            p,
            {
                "signal_id": f"s{p.parent_id}",
                "risk_decision_id": f"r{p.parent_id}",
                "routing_id": f"x{p.parent_id}",
            },
        )
    items = []  # (mono, rank, idx, fn)
    for i, ev in enumerate(events):
        items.append((ev.exchange_ts, 0, i, lambda m, ev=ev: rec.record_market_event(ev, m)))
    by_order = {}
    for f in replay.fills:
        by_order.setdefault(f.order_id, []).append(f)
    n = len(events)
    for kids in replay.children.values():
        for c in kids:
            cid = c.order_id
            t0 = c.decision_ts
            items.append(
                (
                    t0,
                    1,
                    n + cid,
                    lambda m, c=c, cid=cid: rec.send(
                        cid,
                        c.parent_id,
                        instrument_id=c.instrument_id,
                        venue_id=c.venue_id,
                        side=c.side,
                        qty=c.qty,
                        order_type=c.type.name,
                        price_ticks=c.limit_ticks,
                        decision_ts=c.decision_ts,
                        mono_ns=m,
                    ),
                )
            )
            items.append((c.arrival_ts, 2, n + cid, lambda m, cid=cid: rec.ack(cid, m)))
            filled = 0
            for f in by_order.get(cid, []):
                if drop_fills:
                    continue
                filled += f.qty
                items.append(
                    (
                        f.ts + 1,
                        3,
                        f.fill_id,
                        lambda m, f=f: rec.fill(
                            f.order_id,
                            fill_id=f.fill_id,
                            qty=f.qty,
                            price_ticks=f.price_ticks + price_shift,
                            venue_id=f.venue_id,
                            liquidity=f.liquidity.name,
                            fee=f.fee,
                            exchange_ts=f.ts,
                            mono_ns=m,
                        ),
                    )
                )
            if filled < c.qty:
                end = max([c.arrival_ts] + [f.ts + 1 for f in by_order.get(cid, [])]) + 10
                items.append(
                    (
                        max(end, c.arrival_ts) + 1_000_000_000,
                        4,
                        n + cid,
                        lambda m, cid=cid: rec.cancel_send(cid, m),
                    )
                )
                items.append(
                    (
                        max(end, c.arrival_ts) + 1_000_000_001,
                        5,
                        n + cid,
                        lambda m, cid=cid: rec.cancel_ack(cid, m),
                    )
                )
    items.sort(key=lambda t: t[:3])
    for m, _, _, fn in items:
        fn(m)
    path = rec.write(tmp_path / name)
    return rec, path.parent


# ---------------------------------------------------------------- recorder
def test_recorder_roundtrip_and_deterministic_bytes(events, replay, tmp_path):
    rec, d1 = make_recording(events, replay, tmp_path, name="a")
    _, d2 = make_recording(events, replay, tmp_path, name="b")
    for f in ("market.iap1", "orders.jsonl", "recording.json"):
        assert (d1 / f).read_bytes() == (d2 / f).read_bytes()
    loaded = R.load_recording(d1)
    assert loaded.events == rec.events and len(loaded.orders) == len(rec.orders)
    assert all(e.receive_ts == e.exchange_ts for e in loaded.events)  # mapping = identity here
    seqs = [o["seq"] for o in loaded.orders]
    assert seqs == list(range(len(seqs)))
    assert [o["mono_ns"] for o in loaded.orders] == sorted(o["mono_ns"] for o in loaded.orders)
    assert loaded.manifest["clock"]["uncertainty_ns"] == 1
    assert set(loaded.parents) == {1, 2}


def test_recorder_tamper_detected(events, replay, tmp_path):
    _, d = make_recording(events, replay, tmp_path)
    with (d / "orders.jsonl").open("ab") as fh:
        fh.write(b"\n")
    with pytest.raises(R.RecorderError, match="sha256"):
        R.load_recording(d)


def test_clock_mapping_sample_and_monotone():
    wall = iter([1000, 1010, 2000, 2004, 3000, 3100])
    mono = iter([50, 60, 70])
    cm = R.ClockMapping.sample(wall=lambda: next(wall), mono=lambda: next(mono), tries=3)
    assert cm.mono_ns0 == 60 and cm.wall_ns0 == 2002 and cm.uncertainty_ns == 3
    assert cm.to_wall(70) == 2012
    r = R.PaperRecorder(cm, mono=lambda: 100)
    r.record_market_event(synth_events(n_steps=1)[0], mono_ns=100)
    with pytest.raises(R.RecorderError, match="backwards"):
        r.record_market_event(synth_events(n_steps=1)[0], mono_ns=99)


def _fresh():
    r = R.PaperRecorder(R.ClockMapping(0, 0, 1), mono=lambda: 0)
    r.register_parent(_parents()[0], {k: "x" for k in R.TRACE_KEYS})
    return r


def _send(r, cid=1, qty=100, m=1):
    return r.send(
        cid,
        1,
        instrument_id=INS,
        venue_id=VEN,
        side=0,
        qty=qty,
        order_type="LIMIT",
        price_ticks=10_000,
        decision_ts=5,
        mono_ns=m,
    )


def test_state_transitions_and_quantity_conservation():
    r = _fresh()
    with pytest.raises(R.RecorderError, match="before SEND"):
        r.ack(1, 1)
    _send(r)
    with pytest.raises(R.RecorderError, match="duplicate SEND"):
        _send(r, m=2)
    with pytest.raises(R.RecorderError, match="invalid in state SENT"):
        r.fill(1, fill_id=1, qty=10, price_ticks=1, venue_id=1, liquidity="MAKER", mono_ns=3)
    r.ack(1, 4)
    with pytest.raises(R.RecorderError, match="conservation"):
        r.fill(1, fill_id=1, qty=101, price_ticks=1, venue_id=1, liquidity="MAKER", mono_ns=5)
    r.fill(1, fill_id=1, qty=60, price_ticks=1, venue_id=1, liquidity="MAKER", mono_ns=5)
    with pytest.raises(R.RecorderError, match="duplicate fill_id"):
        r.fill(1, fill_id=1, qty=10, price_ticks=1, venue_id=1, liquidity="MAKER", mono_ns=6)
    r.cancel_send(1, 7)
    r.fill(
        1, fill_id=2, qty=40, price_ticks=1, venue_id=1, liquidity="MAKER", mono_ns=8
    )  # lost race
    with pytest.raises(R.RecorderError, match="terminal"):
        r.cancel_ack(1, 9)
    assert sum(o["qty"] for o in r.orders if o["kind"] == R.FILL) == 100


def test_recorder_requires_trace_and_registered_parent():
    r = R.PaperRecorder(R.ClockMapping(0, 0, 1), mono=lambda: 0)
    with pytest.raises(R.RecorderError, match="trace ids missing"):
        r.register_parent(_parents()[0], {"signal_id": "s"})
    with pytest.raises(R.RecorderError, match="not registered"):
        _send(r)
    r2 = _fresh()
    with pytest.raises(R.RecorderError, match="opt-ins"):
        r2.register_parent(
            dataclasses.replace(_parents()[1], parent_id=9, pov_exact=True),
            {k: "x" for k in R.TRACE_KEYS},
        )
    r2.reject_ok = _send(r2, cid=5, m=1)
    r2.reject(5, 2)
    with pytest.raises(R.RecorderError, match="terminal"):
        r2.ack(5, 3)


def test_reports_from_recording_are_valid(events, replay, tmp_path):
    _, d = make_recording(events, replay, tmp_path)
    rec = R.load_recording(d)
    reps = R.reports_from_recording(rec)
    assert reps and L.validate_sequence(reps)  # same checks as an external file
    fills = [r for r in reps if r.status in (ExecStatus.PARTIAL, ExecStatus.FILLED)]
    assert sum(r.filled_qty for r in fills) == sum(f.qty for f in replay.fills)


# -------------------------------------------------------------- comparison
def test_identical_paper_run_has_zero_error(events, replay, tmp_path):
    _, d = make_recording(events, replay, tmp_path)
    rec = R.load_recording(d)
    rep = sim_to_real.compare(rec, exec_config())
    assert rep["level2"] == "not validated"
    assert any("not empirical validation" in s for s in rep["limitations"])
    assert rep["unmatched_recorded"] == rep["unmatched_replayed"] == 0
    assert rep["n_children_recorded"] == sum(len(v) for v in replay.children.values()) > 0
    assert rep["overall"]["fill_frac_err"]["bias"] == 0.0
    assert rep["outcome"]["recorded_only"] == rep["outcome"]["replay_only"] == 0
    assert rep["outcome"]["both"] > 0
    pe = rep["overall"]["price_err_bps"]
    assert pe["bias"] == 0.0 and pe["abs_max"] == 0.0
    assert pe["underpowered"] is True  # tiny sample is flagged, not hidden
    assert set(rep["by_venue"]) == {str(VEN)} and rep["by_regime"] and rep["by_order_type"]
    assert rep == sim_to_real.compare(R.load_recording(d), exec_config())  # reproducible
    for row in rep["trace"]:
        assert row["signal_id"] and row["risk_decision_id"] and row["routing_id"]
        assert row["recorded_filled"] == row["replayed_filled"]
        assert row["tca_note"] == "" and row["tca_recorded_is_bps"] == row["tca_replayed_is_bps"]


def test_perturbed_run_shows_bias_and_missing_fills(events, replay, tmp_path):
    _, d = make_recording(events, replay, tmp_path, price_shift=1, name="shift")
    rep = sim_to_real.compare(R.load_recording(d), exec_config())
    pe = rep["overall"]["price_err_bps"]
    assert pe["n"] > 0
    # buys pay one tick more, sells receive one tick less: recorded is worse, bias > 0
    assert pe["bias"] > 0 and pe["abs_max"] < 20
    _, d2 = make_recording(events, replay, tmp_path, drop_fills=True, name="nofill")
    rep2 = sim_to_real.compare(R.load_recording(d2), exec_config())
    assert rep2["outcome"]["replay_only"] > 0 and rep2["outcome"]["both"] == 0
    assert rep2["overall"]["fill_frac_err"]["bias"] < 0


# --------------------------------------------------------------- tape test
def _day(seed, day):
    ev = synth_events(n_steps=2500, seed=seed)
    ts = np.array([e.exchange_ts for e in ev[40::12]], dtype=np.int64)
    return fill_tape.day_samples(
        ev,
        ts,
        instrument_id=INS,
        venue_id=VEN,
        exec_config=exec_config(),
        ttl_ns=2 * 10**9,
        day=day,
    )


@pytest.fixture(scope="module")
def tape_days():
    return {"A1": _day(1, "A1"), "A2": _day(2, "A2"), "B1": _day(3, "B1")}


def test_tape_test_separation_and_scores(tape_days):
    rep = fill_tape.tape_test(
        {k: tape_days[k] for k in ("A1", "A2")}, {"B1": tape_days["B1"]}, ttl_ns=2 * 10**9
    )
    assert rep["level2"] == "not validated"
    assert rep["fit_days"] == ["A1", "A2"] and rep["eval_days"] == ["B1"]
    o = rep["overall"]
    assert o["n"] == rep["n_eval"] > 0
    assert 0.0 <= o["brier"] <= 1.0 and 0.0 <= o["brier_naive"] <= 0.25 + 1e-9
    assert sum(b["n"] for b in rep["reliability"]) == o["n"]
    assert set(rep["by_liquidity_bucket"]) == {"low", "mid", "high"}
    assert set(rep["by_venue"]) == {str(VEN)}
    assert sum(c["n"] for c in rep["by_liquidity_bucket"].values()) == o["n"]
    # same inputs, same report (deterministic)
    assert rep == fill_tape.tape_test(
        {k: tape_days[k] for k in ("A1", "A2")}, {"B1": tape_days["B1"]}, ttl_ns=2 * 10**9
    )


def test_tape_test_refuses_overlap(tape_days):
    with pytest.raises(ValueError, match="both"):
        fill_tape.tape_test(
            {"A1": tape_days["A1"], "B1": tape_days["B1"]}, {"B1": tape_days["B1"]}, ttl_ns=10**9
        )
    relabelled = tape_days["B1"].assign(day="A1")
    with pytest.raises(ValueError, match="overlaps"):
        fill_tape.tape_test({"A1": tape_days["A1"]}, {"X": relabelled}, ttl_ns=10**9)


def test_tape_buckets_use_fit_edges_only(tape_days):
    rep = fill_tape.tape_test({"A1": tape_days["A1"]}, {"B1": tape_days["B1"]}, ttl_ns=2 * 10**9)
    q = np.quantile(tape_days["A1"]["queue_ahead"].to_numpy(float), [1 / 3, 2 / 3])
    assert rep["queue_ahead_edges_from_fit"] == [float(x) for x in q]
    assert isinstance(FillHazardModel(), FillHazardModel)


def test_unused_imports_guard():
    assert shutil and L and R

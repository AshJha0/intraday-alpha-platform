"""v1.9 maker economics (M1-M4) on synthetic MBO data.

M1 ``iap.execution.calibration``, M2/M4 ``iap.backtest.maker``, M3
``iap.labels.maker_labels`` and the optional z cap of
``LinearAlpha.score_uncapped``. The default (taker, synthetic-config,
clipped) paths are untouched; the goldens pin them elsewhere.
"""

from __future__ import annotations

import json
import math
import random

import numpy as np
import pandas as pd
import pytest
from iap.backtest.maker import MakerBacktester, MakerConfig, MakerFilter
from iap.core.events import EventType, MarketEvent
from iap.execution import ChildOrder, ExecConfig, ExecutionSimulator, InstrumentSpec, VenueSpec
from iap.execution.calibration import (
    SCHEMA,
    ExecCalibration,
    LatencyTable,
    apply_calibration,
    estimate_calibration,
    load_calibration,
    parametric_latency,
    queue_bucket,
    write_calibration,
)
from iap.labels.maker_labels import maker_label_columns, maker_labels

T0 = 1_700_000_000_000_000_000
INS = 7
VEN = 1
TICK = 0.01


def exec_config(jitter_ns: int = 0) -> ExecConfig:
    return ExecConfig(
        seed=42,
        venues={
            VEN: VenueSpec(
                venue_id=VEN,
                name="TST",
                taker_fee_per_share=0.003,
                maker_rebate_per_share=0.002,
                latency_mean_ns=150_000,
                latency_jitter_ns=jitter_ns,
            )
        },
        instruments={INS: InstrumentSpec(INS, TICK, 1.0, 1_000_000.0)},
    )


def synth_events(
    n_steps: int = 3000, seed: int = 1, drift: float = 0.0, recv_lag: bool = False
) -> list[MarketEvent]:
    """A small random MBO stream: adds at/near the touch, cancels, executes
    against the head of the touch queue; ``drift`` biases which side gets hit
    (> 0: asks are lifted more, so the price tends to rise)."""
    rng = random.Random(seed)
    evs: list[MarketEvent] = []
    book: dict[int, dict[int, list[list[int]]]] = {0: {}, 1: {}}
    seq = [0]
    oid = [1]

    def emit(t, et, side, px, qty, o):
        seq[0] += 1
        lag = rng.randint(20_000, 400_000) if recv_lag else 0
        evs.append(
            MarketEvent(
                event_id=seq[0],
                instrument_id=INS,
                venue_id=VEN,
                exchange_ts=t,
                receive_ts=t + lag,
                sequence=seq[0],
                event_type=int(et),
                side=side,
                price_ticks=px,
                qty=qty,
                order_id=o,
                trade_id=0,
            )
        )

    def add(t, side, px, qty):
        o = oid[0]
        oid[0] += 1
        book[side].setdefault(px, []).append([o, qty])
        emit(t, EventType.ADD, side, px, qty, o)

    def best(side):
        lv = [p for p, q in book[side].items() if q]
        if not lv:
            return None
        return max(lv) if side == 0 else min(lv)

    t = T0
    for k in range(5):
        for _ in range(3):
            add(t, 0, 10_000 - k, 100)
            add(t, 1, 10_002 + k, 100)
            t += 1000
    for _ in range(n_steps):
        t += rng.randint(2_000_000, 20_000_000)
        u = rng.random()
        side = rng.randint(0, 1)
        bb, ba = best(0), best(1)
        if bb is None or ba is None:
            add(t, 0, (bb or ba - 1), 100) if bb is None else add(t, 1, ba or bb + 1, 100)
            continue
        if u < 0.40:
            # add at the touch, or improve when the spread is wide
            if ba - bb > 1 and rng.random() < 0.5:
                px = bb + 1 if side == 0 else ba - 1
            else:
                px = bb if side == 0 else ba
            add(t, side, px, rng.choice((100, 200, 300)))
        elif u < 0.65:
            lvl = book[side].get(bb if side == 0 else ba)
            if lvl:
                o, q = lvl.pop(rng.randrange(len(lvl)))
                emit(t, EventType.CANCEL, side, bb if side == 0 else ba, q, o)
        else:
            # aggressor: hit the bid (side 0 resting) or lift the ask
            rest_side = 1 if rng.random() < 0.5 + drift else 0
            px = ba if rest_side == 1 else bb
            lvl = book[rest_side][px]
            o, q = lvl[0]
            take = min(q, rng.choice((50, 100, 200)))
            emit(t, EventType.EXECUTE, rest_side, px, take, o)
            if take == q:
                lvl.pop(0)
            else:
                lvl[0][1] = q - take
        # keep depth behind the touch
        for s in (0, 1):
            b = best(s)
            if b is None:
                continue
            deeper = b - 3 if s == 0 else b + 3
            if not book[s].get(deeper):
                add(t, s, deeper, 100)
    return evs


@pytest.fixture(scope="module")
def events() -> list[MarketEvent]:
    return synth_events()


@pytest.fixture(scope="module")
def calib(events) -> dict:
    return estimate_calibration(events, {INS: TICK}, source={"test": "synthetic"})


# ------------------------------------------------------------------ M1


def test_calibration_document_shape(calib):
    assert calib["schema"] == SCHEMA and calib["version"] == 1
    assert calib["instruments"] == [INS]
    fr = calib["fill_rates"]["all"]
    assert fr["n"] > 100
    assert 0.0 < fr["p_any_fill"] <= 1.0
    assert 0.0 <= fr["p_full_fill"] <= fr["p_any_fill"]
    assert 0.0 < fr["qty_fill_rate"] <= 1.0
    assert fr["median_time_to_fill_ns"] > 0
    for side in ("bid", "ask"):
        cells = calib["queue_depletion"][side]
        assert cells
        for cell in cells.values():
            assert cell["exposure_s"] >= 0
            if cell["hazard_per_s"] is not None:
                assert cell["hazard_per_s"] >= 0
                assert 0 <= cell["p_deplete_1s"] < 1
    for h in ("100ms", "1s", "10s"):
        assert calib["markouts"]["all"][h]["n"] > 50
        assert calib["markouts"]["all"][h]["mean_bps"] is not None
    assert calib["impact"]["n_sweeps"] > 50
    assert calib["impact"]["slope_bps_per_pct_adv"] is not None
    # no receive stamp in this stream: no feed latency table
    assert calib["latency"] == {}


def test_calibration_fill_rates_fall_with_queue_ahead(calib):
    cells = calib["fill_rates"]["by_queue_ahead"]
    front = cells.get("0")
    back = [c for k, c in cells.items() if k.startswith(">") and c["p_any_fill"] is not None]
    assert front is not None and front["p_any_fill"] is not None
    for c in back:
        assert c["p_any_fill"] <= front["p_any_fill"]


def test_calibration_adverse_selection_tracks_drift():
    up = estimate_calibration(synth_events(seed=3, drift=0.0), {INS: TICK})
    toxic = estimate_calibration(synth_events(seed=3, drift=0.3), {INS: TICK})
    # a one-sided aggressor flow makes resting orders more adversely selected
    assert toxic["markouts"]["all"]["10s"]["mean_bps"] > up["markouts"]["all"]["10s"]["mean_bps"]


def test_calibration_round_trip_and_deterministic(calib, tmp_path, events):
    p = tmp_path / "calib.json"
    write_calibration(calib, p)
    again = estimate_calibration(events, {INS: TICK}, source={"test": "synthetic"})
    write_calibration(again, tmp_path / "again.json")
    assert p.read_bytes() == (tmp_path / "again.json").read_bytes()
    loaded = load_calibration(p)
    assert isinstance(loaded, ExecCalibration)
    assert loaded.adverse_selection_bps("1s") == pytest.approx(
        calib["markouts"]["all"]["1s"]["mean_bps"]
    )
    assert loaded.adverse_selection_bps("1s", INS) == pytest.approx(
        calib["markouts"]["by_instrument"][str(INS)]["1s"]["mean_bps"]
    )
    assert load_calibration(None) is None


def test_calibration_rejects_wrong_schema(calib):
    bad = dict(calib, version=2)
    with pytest.raises(ValueError, match="iap.exec_calibration"):
        ExecCalibration.from_dict(bad)
    with pytest.raises(ValueError):
        ExecCalibration.from_dict({"schema": "other", "version": 1})


def test_feed_latency_table_and_tail():
    cal = estimate_calibration(synth_events(n_steps=400, recv_lag=True), {INS: TICK})
    row = cal["latency"][str(VEN)]
    assert row["source"] == "feed_receive_minus_exchange"
    q = row["quantiles_ns"]
    assert q == sorted(q) and 20_000 <= q[0] and q[-1] <= 400_000
    tail = parametric_latency(150_000, 50_000, tail_prob=0.01, tail_mult=10.0)
    tq = tail["quantiles_ns"]
    assert tq[0] == 150_000 and tq[-1] == 2_000_000 and tq == sorted(tq)
    table = LatencyTable(tuple(tail["probs"]), tuple(tq))
    assert table.sample(0.0) == 150_000
    assert table.sample(0.5) == pytest.approx(150_000 + 0.5 / 0.99 * 50_000, abs=2)
    assert table.sample(0.9995) > 200_000


def test_simulator_default_latency_unchanged_and_calibrated_latency_used():
    cfg = exec_config(jitter_ns=50_000)
    order = ChildOrder(
        instrument_id=INS, venue_id=VEN, side=0, qty=10, limit_ticks=100, decision_ts=T0
    )
    a = ExecutionSimulator(cfg)
    b = ExecutionSimulator(cfg, None)
    arr = [a.orders[a.submit(order)].arrival_ts for _ in range(20)]
    assert arr == [b.orders[b.submit(order)].arrival_ts for _ in range(20)]
    doc = {
        "schema": SCHEMA,
        "version": 1,
        "latency": {str(VEN): {"probs": [0.0, 1.0], "quantiles_ns": [5_000_000, 5_000_000]}},
    }
    cal = ExecCalibration.from_dict(doc)
    c = ExecutionSimulator(cfg, cal)
    internal = cfg.latency.internal_ns
    for _ in range(5):
        oid = c.submit(order)
        assert c.orders[oid].arrival_ts == T0 + internal + 5_000_000
    # a venue the calibration does not cover keeps the config rule
    assert ExecCalibration.from_dict({"schema": SCHEMA, "version": 1}).latency_for(VEN) is None


def test_apply_calibration_sets_impact(calib):
    cal = ExecCalibration.from_dict(calib)
    cfg = apply_calibration(exec_config(), cal)
    assert cfg.impact_coeff_bps_per_pct_adv == pytest.approx(
        calib["impact"]["slope_bps_per_pct_adv"]
    )
    assert apply_calibration(exec_config(), None) == exec_config()


def test_queue_bucket_labels():
    assert queue_bucket(0) == "0"
    assert queue_bucket(50) == "1-100"
    assert queue_bucket(100) == "1-100"
    assert queue_bucket(101) == "101-500"
    assert queue_bucket(5000) == ">2000"


# ------------------------------------------------------------------ M3


def test_maker_labels_shape_and_semantics(events):
    dts = np.array([e.exchange_ts for e in events[100:2500:40]], dtype=np.int64)
    lab = maker_labels(events, dts, instrument_id=INS, exec_config=exec_config())
    assert list(lab.columns) == maker_label_columns()
    assert len(lab) == len(dts)
    for s in ("bid", "ask"):
        filled = lab[f"{s}_filled"].to_numpy()
        assert 0 < filled.sum() < len(lab)
        m = lab[f"{s}_markout_1s_bps"].to_numpy()
        assert np.all(np.isnan(m[~filled]))
        nro = lab[f"{s}_not_run_over"].to_numpy()
        assert np.all(nro[~filled] == 0.0)
        ok = filled & np.isfinite(m)
        assert np.array_equal(nro[ok], (m[ok] >= 0).astype(float))
        # a fill happens after the decision and before the ttl
        ft = lab[f"{s}_fill_ts"].to_numpy()
        assert np.all(ft[filled] > dts[filled])
        assert np.all(ft[filled] <= dts[filled] + 1_000_000_000)
    assert (lab["bid_post_ticks"] < lab["ask_post_ticks"]).all()


def test_maker_labels_longer_ttl_fills_more(events):
    dts = np.array([e.exchange_ts for e in events[100:2500:40]], dtype=np.int64)
    short = maker_labels(
        events, dts, instrument_id=INS, exec_config=exec_config(), ttl_ns=50_000_000
    )
    long = maker_labels(events, dts, instrument_id=INS, exec_config=exec_config(), ttl_ns=5 * 10**9)
    assert long["bid_filled"].sum() >= short["bid_filled"].sum()
    assert long["ask_filled"].sum() >= short["ask_filled"].sum()


def test_maker_labels_validation(events):
    with pytest.raises(ValueError, match="run_over_horizon"):
        maker_labels(
            events, [T0], instrument_id=INS, exec_config=exec_config(), run_over_horizon="5s"
        )
    with pytest.raises(ValueError, match="non-decreasing"):
        maker_labels(events, [T0 + 5, T0], instrument_id=INS, exec_config=exec_config())


# ------------------------------------------------------------------ M2 / M4


def make_scores(events, every: int = 25, er_scale: float = 2e-4, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = np.array([e.exchange_ts for e in events[50::every]], dtype=np.int64)
    z = rng.standard_normal(ts.size) * 1.5
    return pd.DataFrame(
        {
            "exchange_ts": ts,
            "expected_return": er_scale * z,
            "confidence": np.minimum(1.0, np.abs(z) / 2.0),
            "z": z,
        }
    )


def test_maker_backtest_accounting_identity(events):
    scores = make_scores(events)
    res = MakerBacktester(exec_config(), MakerConfig(qty=100)).run_instrument(events, scores, INS)
    t = res.trips
    assert len(t) > 5
    c = res.counters
    assert c["decisions"] == len(scores)
    assert c["posted"] >= c["filled_orders"] == len(t)
    s = np.where(t["side"] == 0, 1.0, -1.0)
    gross = s * (t["exit_px"] - t["entry_px"]) * t["qty"]
    np.testing.assert_allclose(t["gross"], gross, rtol=0, atol=1e-9)
    np.testing.assert_allclose(
        t["net"], t["gross"] - t["entry_fees"] - t["exit_fee"] - t["exit_impact"], atol=1e-9
    )
    decomposed = (
        (t["half_spread_earned_bps"] - t["adverse_selection_bps"] - t["exit_slippage_bps"])
        * t["entry_px"]
        * t["qty"]
        / 1e4
    )
    np.testing.assert_allclose(t["gross"], decomposed, atol=1e-6)
    maker = t["liquidity"] == "MAKER"
    # maker entries earn the rebate: negative fees of 0.002 per share
    np.testing.assert_allclose(t.loc[maker, "entry_fees"], -0.002 * t.loc[maker, "qty"])
    # holding period: exit at or after the horizon from the first fill
    unforced = ~t["forced"]
    assert (t.loc[unforced, "exit_ts"] - t.loc[unforced, "entry_ts"] >= 1_000_000_000).all()
    summ = res.summary()
    assert summ["n_trips"] == len(t)
    assert summ["total_net"] == pytest.approx(res.net_pnl)
    assert 0 < summ["fill_rate"] <= 1


def test_maker_backtest_deterministic(events):
    scores = make_scores(events)
    a = MakerBacktester(exec_config(50_000), MakerConfig()).run_instrument(events, scores, INS)
    b = MakerBacktester(exec_config(50_000), MakerConfig()).run_instrument(events, scores, INS)
    pd.testing.assert_frame_equal(a.trips, b.trips)
    assert a.counters == b.counters


def test_maker_gate_uses_calibrated_adverse_selection(events, calib):
    scores = make_scores(events)
    cal = ExecCalibration.from_dict(calib)
    bt = MakerBacktester(exec_config(), MakerConfig(as_horizon="1s"), cal)
    as_bps, src = bt.adverse_selection(INS)
    assert src == "calibration:1s"
    assert as_bps == pytest.approx(cal.adverse_selection_bps("1s", INS))
    # an adverse-selection bar nothing can clear blocks every post
    huge = MakerBacktester(exec_config(), MakerConfig(adverse_selection_bps=1e6))
    res = huge.run_instrument(events, scores, INS)
    assert huge.adverse_selection(INS) == (1e6, "config")
    assert res.counters["posted"] == 0 and len(res.trips) == 0
    none = MakerBacktester(exec_config(), MakerConfig())
    assert none.adverse_selection(INS) == (0.0, "none")


def test_maker_mid_exit_has_no_exit_costs(events):
    scores = make_scores(events)
    res = MakerBacktester(exec_config(), MakerConfig(exit="mid")).run_instrument(
        events, scores, INS
    )
    t = res.trips
    assert len(t) > 0
    assert (t["exit_fee"] == 0).all() and (t["exit_impact"] == 0).all()
    np.testing.assert_allclose(t["exit_slippage_bps"], 0.0, atol=1e-9)


def test_tail_conditions_reduce_posts(events, calib):
    scores = make_scores(events)
    base = MakerBacktester(exec_config(), MakerConfig()).run_instrument(events, scores, INS)
    tails = MakerBacktester(exec_config(), MakerConfig(min_abs_z=2.0)).run_instrument(
        events, scores, INS
    )
    assert tails.counters["posted"] < base.counters["posted"]
    assert tails.counters["tail_out"] > 0
    qi = MakerBacktester(exec_config(), MakerConfig(min_queue_imbalance=0.99)).run_instrument(
        events, scores, INS
    )
    assert qi.counters["posted"] < base.counters["posted"]
    cal = ExecCalibration.from_dict(calib)
    dep = MakerBacktester(exec_config(), MakerConfig(min_p_far_deplete=1.0), cal).run_instrument(
        events, scores, INS
    )
    assert dep.counters["posted"] == 0
    with pytest.raises(ValueError, match="calibration"):
        MakerBacktester(exec_config(), MakerConfig(min_p_far_deplete=0.5)).run_instrument(
            events, scores, INS
        )
    with pytest.raises(ValueError, match="'z'"):
        MakerBacktester(exec_config(), MakerConfig(min_abs_z=1.0)).run_instrument(
            events, scores.drop(columns="z"), INS
        )


def test_allow_mask_filters_decisions(events):
    scores = make_scores(events)
    allow = np.zeros(len(scores), dtype=bool)
    res = MakerBacktester(exec_config(), MakerConfig()).run_instrument(
        events, scores, INS, allow=allow
    )
    assert res.counters["posted"] == 0
    assert res.counters["filtered"] + res.counters["busy"] == len(scores)
    with pytest.raises(ValueError, match="row-aligned"):
        MakerBacktester(exec_config(), MakerConfig()).run_instrument(
            events, scores, INS, allow=allow[:-1]
        )


def test_maker_filter_models():
    rng = np.random.default_rng(0)
    X = rng.standard_normal((600, 3))
    y = (X[:, 0] + 0.3 * rng.standard_normal(600) > 0).astype(float)
    f = MakerFilter("meta_gbm", tau=0.5).fit(X[:400], y[:400])
    acc = (f.allow(X[400:]) == (y[400:] > 0)).mean()
    assert acc > 0.75
    r = MakerFilter("ridge", tau=0.0).fit(X[:400], X[:400, 0] * 2.0)
    assert np.corrcoef(r.score(X[400:]), X[400:, 0])[0, 1] > 0.99
    with pytest.raises(RuntimeError):
        MakerFilter().score(X)
    with pytest.raises(ValueError, match="both classes"):
        MakerFilter().fit(X, np.ones(600))


def test_maker_config_validation():
    with pytest.raises(ValueError):
        MakerConfig(exit="auction")
    with pytest.raises(ValueError):
        MakerConfig(qty=0)
    with pytest.raises(ValueError):
        MakerConfig(min_queue_imbalance=1.5)


def test_maker_backtest_on_labels_filter_end_to_end(events):
    """M3 labels train the M4 filter that gates the M2 backtest (wiring)."""
    scores = make_scores(events)
    lab = maker_labels(
        events, scores["exchange_ts"].to_numpy(), instrument_id=INS, exec_config=exec_config()
    )
    X = np.column_stack([scores["z"], np.abs(scores["z"]), lab["bid_queue_ahead"]])
    y = lab["bid_not_run_over"].to_numpy()
    half = len(scores) // 2
    f = MakerFilter("ridge", tau=0.0).fit(X[:half], y[:half])
    allow = np.zeros(len(scores), dtype=bool)
    allow[half:] = f.allow(X[half:])
    res = MakerBacktester(exec_config(), MakerConfig()).run_instrument(
        events, scores, INS, allow=allow
    )
    assert res.counters["filtered"] >= half
    json.dumps(res.summary())  # report-serialisable


# ------------------------------------------------------------------ M4 z cap


def test_score_uncapped_is_optional_and_default_is_pinned():
    from iap.alpha.base import LinearAlpha

    class _Toy(LinearAlpha):
        """Toy alpha.

        Economic rationale: test fixture only.
        """

        alpha_id = "EQ99"
        name = "toy"
        features = ("x",)
        horizon = "1s"

        def raw_signal(self, df):
            return df["x"]

        def universe(self, ids):
            return list(ids)

    a = _Toy.__new__(_Toy)
    LinearAlpha.__init__(a)
    a.mu, a.sigma, a.beta, a._fitted = 0.0, 1.0, 1e-4, True
    df = pd.DataFrame({"exchange_ts": np.arange(4), "x": [0.5, 3.0, 9.0, -12.0]})
    capped = a.score({1: df})[1]["expected_return"].to_numpy()
    np.testing.assert_allclose(capped, 1e-4 * np.array([0.5, 3.0, 4.0, -4.0]), atol=1e-12)
    raw = a.score_uncapped({1: df})[1]["expected_return"].to_numpy()
    np.testing.assert_allclose(raw, 1e-4 * np.array([0.5, 3.0, 9.0, -12.0]), atol=1e-12)
    six = a.score_uncapped({1: df}, z_cap=6.0)[1]["expected_return"].to_numpy()
    np.testing.assert_allclose(six, 1e-4 * np.array([0.5, 3.0, 6.0, -6.0]), atol=1e-12)
    assert math.isclose(a.z_clip, 4.0)
    with pytest.raises(ValueError):
        a.score_uncapped({1: df}, z_cap=0.0)


# ------------------------------------------------------------------ passive exit


def _check_identity(t: pd.DataFrame) -> None:
    s = np.where(t["side"] == 0, 1.0, -1.0)
    np.testing.assert_allclose(t["gross"], s * (t["exit_px"] - t["entry_px"]) * t["qty"], atol=1e-9)
    np.testing.assert_allclose(
        t["net"], t["gross"] - t["entry_fees"] - t["exit_fee"] - t["exit_impact"], atol=1e-9
    )
    decomposed = (
        (t["half_spread_earned_bps"] - t["adverse_selection_bps"] - t["exit_slippage_bps"])
        * t["entry_px"]
        * t["qty"]
        / 1e4
    )
    np.testing.assert_allclose(t["gross"], decomposed, atol=1e-6)


def test_passive_exit_identity_rebates_and_rates(events):
    scores = make_scores(events)
    mc = MakerConfig(exit="passive", exit_timeout_ns=3 * 10**9, margin_bps=-1e9)
    res = MakerBacktester(exec_config(), mc).run_instrument(events, scores, INS)
    t = res.trips
    assert len(t) > 5
    _check_identity(t)
    assert (t["exit_mode"] == "passive").all()
    full = t["exit_passive_qty"] == t["qty"]
    assert full.any() and t["exit_timeout"].any()
    # a fully passive exit is a maker fill: rebate on both legs, no impact,
    # and the second half-spread earned (negative exit slippage)
    fp = t[full & ~t["forced"]]
    np.testing.assert_allclose(fp["exit_fee"], -0.002 * fp["qty"])
    assert (fp["exit_impact"] == 0).all()
    assert (fp["exit_slippage_bps"] < 0).all()
    # a timeout crosses the remainder and pays the taker fee on it
    to = t[t["exit_timeout"]]
    rem = to["qty"] - to["exit_passive_qty"]
    np.testing.assert_allclose(
        to["exit_fee"], -0.002 * to["exit_passive_qty"] + 0.003 * rem, atol=1e-9
    )
    summ = res.summary()
    assert summ["passive_exit_trips"] == len(t)
    assert summ["passive_exit_fill_rate"] == pytest.approx(full.mean())
    assert summ["timeout_cross_rate"] == pytest.approx(t["exit_timeout"].mean())
    assert 0.0 <= summ["passive_exit_qty_share"] <= 1.0
    assert res.counters["exit_posts"] >= len(t) - res.counters["forced_exits"]
    assert res.counters["exit_timeouts"] == int(t["exit_timeout"].sum())


def test_passive_exit_timeout_and_reprices(events):
    scores = make_scores(events)
    quick = MakerConfig(exit="passive", exit_timeout_ns=1, margin_bps=-1e9)
    a = MakerBacktester(exec_config(), quick).run_instrument(events, scores, INS)
    patient = MakerConfig(exit="passive", exit_timeout_ns=5 * 10**9, margin_bps=-1e9)
    b = MakerBacktester(exec_config(), patient).run_instrument(events, scores, INS)
    assert a.summary()["timeout_cross_rate"] >= b.summary()["timeout_cross_rate"]
    rep = MakerConfig(exit="passive", exit_timeout_ns=10**8, exit_reprices=2, margin_bps=-1e9)
    c = MakerBacktester(exec_config(), rep).run_instrument(events, scores, INS)
    _check_identity(c.trips)
    assert c.trips["exit_reprices"].max() <= 2
    crossed = c.trips["exit_timeout"] & ~c.trips["forced"]
    assert (c.trips.loc[crossed, "exit_reprices"] == 2).all()
    again = MakerBacktester(exec_config(), rep).run_instrument(events, scores, INS)
    pd.testing.assert_frame_equal(c.trips, again.trips)


def test_passive_exit_gate_is_cheaper_than_taker_gate(events):
    scores = make_scores(events)
    taker = MakerBacktester(exec_config(), MakerConfig(adverse_selection_bps=3.0))
    passive = MakerBacktester(
        exec_config(),
        MakerConfig(exit="passive", passive_exit_fill_prob=1.0, adverse_selection_bps=3.0),
    )
    rt = taker.run_instrument(events, scores, INS)
    rp = passive.run_instrument(events, scores, INS)
    assert rt.counters["gated_out"] > rp.counters["gated_out"]
    with pytest.raises(ValueError):
        MakerConfig(exit="passive", passive_exit_fill_prob=1.5)
    with pytest.raises(ValueError):
        MakerConfig(exit="passive", exit_timeout_ns=0)


def test_default_exit_is_taker(events):
    assert MakerConfig().exit == "taker"
    res = MakerBacktester(exec_config(), MakerConfig()).run_instrument(
        events, make_scores(events), INS
    )
    assert (res.trips["exit_mode"] == "taker").all()
    assert (res.trips["exit_passive_qty"] == 0).all()
    assert res.summary()["passive_exit_trips"] == 0
    assert res.summary()["timeout_cross_rate"] is None

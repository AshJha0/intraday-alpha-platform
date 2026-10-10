"""v1.12 X4: fill hazard, post-only, multiple reprices, markout feedback.

All synthetic. The default (option-off) paths are pinned by the goldens
elsewhere and, for the maker / quoting backtesters, by the fingerprint in
``test_default_outputs_byte_identical``.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pandas as pd
import pytest
from iap.backtest.maker import MakerBacktester, MakerConfig
from iap.backtest.quoting import QuotingBacktester, QuotingConfig
from iap.core.events import EventType, MarketEvent
from iap.execution import (
    CancelReason,
    ChildOrder,
    ExecutionSimulator,
    FeedbackConfig,
    FillHazardModel,
    Liquidity,
    MarkoutFeedback,
    OrderState,
    OrderType,
)
from iap.execution.fill_hazard import hazard_features, samples_from_maker_labels
from iap.labels.maker_labels import maker_labels
from test_maker_economics import INS, T0, VEN, exec_config, make_scores, synth_events

# sha256 of the default MakerBacktester (taker + passive exit) and
# QuotingBacktester outputs on synth_events(1500), computed on v1.11.0
# (origin/main) before any X4 change.
DEFAULT_FINGERPRINT = "da52ae338bf67b79025370f55b6662c4aa1c304b4665c09a6c8f32629596b2dc"


@pytest.fixture(scope="module")
def events():
    return synth_events(n_steps=1500)


def test_default_outputs_byte_identical(events):
    sc = make_scores(events)
    h = hashlib.sha256()
    for ex in ("taker", "passive"):
        r = MakerBacktester(exec_config(), MakerConfig(exit=ex, exit_reprices=1)).run_instrument(
            events, sc, INS
        )
        h.update(r.trips.to_csv().encode())
        h.update(json.dumps(r.summary(), sort_keys=True, default=str).encode())
    q = QuotingBacktester(exec_config()).run_instrument(events, sc, INS)
    h.update(q.fills.to_csv().encode())
    h.update(json.dumps(q.summary(), sort_keys=True, default=str).encode())
    assert h.hexdigest() == DEFAULT_FINGERPRINT


# ------------------------------------------------------------ fill hazard
def planted_samples(n=20_000, lam0=0.8, b=-0.7, seed=11):
    rng = np.random.default_rng(seed)
    q = np.floor(rng.lognormal(5.0, 1.0, n))
    lam = lam0 * (1.0 + q) ** b  # per second; log-hazard linear in log1p(q)
    dur = rng.exponential(1.0 / lam) * 1e9
    ttl = 5_000_000_000
    filled = dur < ttl
    return pd.DataFrame(
        {
            "duration_ns": np.where(filled, dur, ttl).astype(np.int64),
            "filled": filled,
            "queue_ahead": q,
            "depletion_per_s": np.zeros(n),
            "spread_ticks": np.ones(n),
            "imbalance": rng.uniform(0, 1, n),  # no effect planted
            "tod": np.full(n, 0.6),
        }
    )


def test_hazard_recovers_planted_rate():
    m = FillHazardModel().fit(planted_samples())
    # log-hazard slope on log1p(queue) (cloglog = grouped proportional hazards)
    assert m.beta[0] / m.std[0] == pytest.approx(-0.7, abs=0.06)
    assert abs(m.beta[3] / m.std[3]) < 0.1  # imbalance: nothing planted
    for q in (10.0, 150.0, 2000.0):
        x = hazard_features(queue_ahead=q, spread_ticks=1, imbalance=0.5, ts_ns=0)
        x["tod"] = 0.6
        for t in (250_000_000, 1_000_000_000, 3_000_000_000):
            truth = 1.0 - math.exp(-0.8 * (1 + q) ** -0.7 * t / 1e9)
            assert m.p_fill_within(x, t) == pytest.approx(truth, abs=0.02)
    # monotone in t, bounded
    x = hazard_features(queue_ahead=100, spread_ticks=1, ts_ns=0)
    ps = [m.p_fill_within(x, t) for t in (0, 10**8, 10**9, 10**10)]
    assert ps[0] == 0.0 and all(0 <= a <= b <= 1 for a, b in zip(ps, ps[1:], strict=False))


def test_hazard_json_round_trip_deterministic_and_registry(tmp_path):
    s = planted_samples(n=4000)
    a, b = FillHazardModel().fit(s), FillHazardModel().fit(s)
    assert a.to_json() == b.to_json()
    a.save(tmp_path / "h.json")
    c = FillHazardModel.load(tmp_path / "h.json")
    assert c.to_json() == a.to_json()
    assert json.loads(a.to_json())["schema"] == "iap.fill_hazard/1"
    rec = a.register(
        tmp_path / "reg",
        dataset_version="synthetic",
        date_range=("2026-01-01", "2026-01-02"),
        features=["queue_ahead"],
        seed=11,
        exploratory=True,
    )
    d = FillHazardModel.from_registry(tmp_path / "reg", rec.model_id)
    assert d.model_id == rec.model_id and d.to_json() == a.to_json()
    with pytest.raises(ValueError):
        FillHazardModel.from_dict({**a.to_dict(), "schema": "x"})


def test_hazard_fits_from_maker_labels(events):
    ts = np.array([e.exchange_ts for e in events[50::10]], dtype=np.int64)
    lab = maker_labels(events, ts, instrument_id=INS, exec_config=exec_config(), ttl_ns=2 * 10**9)
    s = samples_from_maker_labels(lab, ttl_ns=2 * 10**9)
    assert len(s) > 0 and s["filled"].any()
    m = FillHazardModel(edges_ns=(0, 250_000_000, 500_000_000, 1_000_000_000, 2_000_000_000))
    m.fit(s)
    p = m.p_fill_within(s, 2 * 10**9)
    # calibrated on average against the empirical fill rate inside the ttl
    assert float(np.mean(p)) == pytest.approx(float(s["filled"].mean()), abs=0.05)


# --------------------------------------------------------------- post-only
def _ev(k, t, et, side, px, qty, oid):
    return MarketEvent(
        event_id=k,
        instrument_id=INS,
        venue_id=VEN,
        exchange_ts=t,
        receive_ts=t,
        sequence=k,
        event_type=int(et),
        side=side,
        price_ticks=px,
        qty=qty,
        order_id=oid,
        trade_id=0,
    )


def _book_then_tick():
    return [
        _ev(1, T0, EventType.ADD, 0, 100, 500, 1),
        _ev(2, T0, EventType.ADD, 1, 101, 500, 2),
        _ev(3, T0 + 10_000_000, EventType.ADD, 0, 99, 100, 3),
    ]


@pytest.mark.parametrize("mode", ["", "reject", "slide"])
def test_post_only_never_crosses(mode):
    evs = _book_then_tick()
    sim = ExecutionSimulator(exec_config())
    sim.on_event(evs[0])
    sim.on_event(evs[1])
    oid = sim.submit(
        ChildOrder(
            instrument_id=INS,
            venue_id=VEN,
            side=0,
            type=OrderType.LIMIT,
            qty=100,
            limit_ticks=101,  # marketable: at the ask
            decision_ts=T0,
            post_only=mode,
        )
    )
    sim.on_event(evs[2])
    o = sim.orders[oid]
    if mode == "":
        assert [f.liquidity for f in sim.fills] == [Liquidity.TAKER]  # pinned behaviour
    elif mode == "reject":
        assert not sim.fills
        assert o.state == OrderState.CANCELLED
        assert o.cancel_reason == CancelReason.POST_ONLY_REJECT
        assert sim.post_only_rejects == 1
    else:
        assert not sim.fills and o.state == OrderState.ACTIVE
        assert o.limit_ticks == 100 and sim.post_only_slides == 1
    assert sim.counters.to_dict() == ExecutionSimulator(exec_config()).counters.to_dict()


def test_post_only_validation():
    sim = ExecutionSimulator(exec_config())
    with pytest.raises(ValueError):
        sim.submit(ChildOrder(instrument_id=INS, venue_id=VEN, qty=1, limit_ticks=1, post_only="x"))
    with pytest.raises(ValueError):
        sim.submit(
            ChildOrder(instrument_id=INS, venue_id=VEN, qty=1, type=OrderType.IOC, limit_ticks=1,
                       post_only="reject")
        )  # fmt: skip


@pytest.mark.parametrize("mode", ["reject", "slide"])
def test_post_only_backtests_take_no_liquidity(events, mode):
    sc = make_scores(events)
    r = MakerBacktester(exec_config(), MakerConfig(exit="passive", post_only=mode)).run_instrument(
        events, sc, INS
    )
    assert r.counters["taker_entries"] == 0
    assert (r.trips["liquidity"] == "MAKER").all()
    assert "post_only_rejects" in r.counters
    q = QuotingBacktester(exec_config(), QuotingConfig(post_only=mode)).run_instrument(
        events, sc, INS
    )
    quotes = q.fills[~q.fills["flatten"]]
    assert (quotes["liquidity"] == "MAKER").all()


# -------------------------------------------------------------- reprices
def test_entry_reprice_count_respected(events):
    sc = make_scores(events)
    base = MakerBacktester(exec_config(), MakerConfig(ttl_ns=200_000_000)).run_instrument(
        events, sc, INS
    )
    for n in (1, 3):
        r = MakerBacktester(
            exec_config(), MakerConfig(ttl_ns=200_000_000, entry_reprices=n)
        ).run_instrument(events, sc, INS)
        c = r.counters
        assert 0 < c["entry_reprices"] <= n * c["posted"]
        # every chain ends filled or given up after exactly n reprices
        assert c["give_ups"] <= c["posted"]
        assert c["filled_orders"] >= base.counters["filled_orders"]
    zero = MakerBacktester(
        exec_config(), MakerConfig(ttl_ns=200_000_000, give_up="cross")
    ).run_instrument(events, sc, INS)
    assert zero.counters["entry_reprices"] == 0 and zero.counters["give_up_crosses"] > 0
    assert zero.counters["taker_entries"] > 0


def test_hazard_give_up(events):
    sc = make_scores(events)
    m = FillHazardModel().fit(planted_samples(n=3000))
    never = MakerBacktester(
        exec_config(),
        MakerConfig(ttl_ns=200_000_000, entry_reprices=3, reprice_policy="hazard",
                    hazard_give_up_prob=1.0),
        fill_hazard=m,
    ).run_instrument(events, sc, INS)  # fmt: skip
    assert never.counters["entry_reprices"] == 0 and never.counters["hazard_give_ups"] > 0
    always = MakerBacktester(
        exec_config(),
        MakerConfig(ttl_ns=200_000_000, entry_reprices=3, reprice_policy="hazard"),
        fill_hazard=m,
    ).run_instrument(events, sc, INS)
    assert always.counters["hazard_give_ups"] == 0 and always.counters["entry_reprices"] > 0
    gated = MakerBacktester(exec_config(), MakerConfig(min_fill_prob=1.0), fill_hazard=m)
    g = gated.run_instrument(events, sc, INS)
    assert g.counters["posted"] == 0 and g.counters["fill_prob_out"] > 0
    with pytest.raises(ValueError):
        MakerBacktester(exec_config(), MakerConfig(reprice_policy="hazard"))


# --------------------------------------------------------------- feedback
def test_feedback_unit_ewma_and_causality():
    fb = MarkoutFeedback(FeedbackConfig(horizon_ns=10, min_fills=2, cooldown_ns=100))
    fb.on_fill(0, 0, 100.0)
    fb.on_fill(1, 0, 100.0)
    fb.advance(9, 50.0)  # not due yet
    assert fb.n_known == 0 and fb.action(9).neutral
    fb.advance(11, 99.0)  # both due: -100 bps each
    assert fb.n_known == 2 and fb.ewma == pytest.approx(-100.0)
    assert fb.action(12).stand_down and fb.action(50).stand_down
    assert fb.action(112).neutral and fb.counters["feedback_resets"] == 1
    w = MarkoutFeedback(FeedbackConfig(mode="widen", horizon_ns=1, min_fills=1, widen_ticks=2))
    w.on_fill(0, 1, 100.0)
    w.advance(5, 101.0)  # a sell run over
    assert w.action(5).widen_ticks == 2
    r = MarkoutFeedback(FeedbackConfig(mode="reduce", horizon_ns=1, min_fills=1, size_mult=0.3))
    r.on_fill(0, 0, 100.0)
    r.advance(5, 99.0)
    assert r.action(5).size(100) == 30


def _toxic_scores(events, every=20):
    ts = np.array([e.exchange_ts for e in events[50::every]], dtype=np.int64)
    # always sell into a rising market: every ask fill is run over
    return pd.DataFrame({"exchange_ts": ts, "expected_return": -1e-3, "confidence": 1.0, "z": -3.0})


def test_feedback_reduces_adverse_selection_on_toxic_regime():
    evs = synth_events(n_steps=2500, drift=0.35, seed=4)
    sc = _toxic_scores(evs)
    cfg = {"exit": "mid", "horizon_ns": 500_000_000}
    base = MakerBacktester(exec_config(), MakerConfig(**cfg)).run_instrument(evs, sc, INS)
    fb = FeedbackConfig(horizon_ns=500_000_000, min_fills=3, cooldown_ns=60_000_000_000)
    with_fb = MakerBacktester(exec_config(), MakerConfig(**cfg, feedback=fb)).run_instrument(
        evs, sc, INS
    )
    assert base.trips["adverse_selection_bps"].mean() > 0  # the regime is toxic
    tot = lambda r: float(r.trips["adverse_selection_bps"].sum())  # noqa: E731
    assert with_fb.counters["feedback_stood_down"] > 0
    assert tot(with_fb) < tot(base)
    assert len(with_fb.trips) < len(base.trips)
    # quoting: two-sided, so the bid fills offset the run-over asks; a
    # negative threshold demands at least 2 bps earned per fill (EWMA)
    qfb = FeedbackConfig(
        horizon_ns=500_000_000, min_fills=3, cooldown_ns=60_000_000_000, threshold_bps=-2.0
    )
    qb = QuotingBacktester(exec_config(), QuotingConfig(alpha_weight=0.0)).run_instrument(
        evs, sc, INS
    )
    qf = QuotingBacktester(
        exec_config(), QuotingConfig(alpha_weight=0.0, feedback=qfb)
    ).run_instrument(evs, sc, INS)
    assert qf.counters["feedback_stood_down"] > 0
    assert qb.pnl["markout"] < 0  # adverse selection in the base run
    assert qf.pnl["markout"] > qb.pnl["markout"]
    assert qf.pnl["net"] > qb.pnl["net"]


def test_x4_options_deterministic(events):
    sc = make_scores(events)
    m = FillHazardModel().fit(planted_samples(n=3000))
    cfg = MakerConfig(
        ttl_ns=300_000_000,
        exit="passive",
        post_only="slide",
        entry_reprices=2,
        reprice_policy="hazard",
        hazard_give_up_prob=0.05,
        give_up="cross",
        feedback=FeedbackConfig(mode="reduce"),
    )
    a = MakerBacktester(exec_config(), cfg, fill_hazard=m).run_instrument(events, sc, INS)
    b = MakerBacktester(exec_config(), cfg, fill_hazard=m).run_instrument(events, sc, INS)
    pd.testing.assert_frame_equal(a.trips, b.trips)
    assert a.counters == b.counters
    qc = QuotingConfig(post_only="reject", feedback=FeedbackConfig(mode="widen"))
    qa = QuotingBacktester(exec_config(), qc).run_instrument(events, sc, INS)
    qb = QuotingBacktester(exec_config(), qc).run_instrument(events, sc, INS)
    pd.testing.assert_frame_equal(qa.fills, qb.fills)
    assert qa.counters == qb.counters


def test_x4_config_validation():
    for bad in (
        {"post_only": "x"},
        {"entry_reprices": -1},
        {"reprice_policy": "x"},
        {"give_up": "x"},
        {"min_fill_prob": 2.0},
    ):
        with pytest.raises(ValueError):
            MakerConfig(**bad)
    with pytest.raises(ValueError):
        QuotingConfig(post_only="x")
    with pytest.raises(ValueError):
        FeedbackConfig(mode="x")

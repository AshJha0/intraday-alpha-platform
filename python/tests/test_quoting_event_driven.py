"""Rule 10 of iap.backtest.quoting: the opt-in event-driven quoter (development)."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from iap.backtest import quoting as qmod
from iap.backtest.quoting import QuotingBacktester, QuotingConfig
from iap.core.events import EventType, MarketEvent
from test_maker_economics import INS, VEN, exec_config, synth_events
from test_quoting import make_scores

FEED = 100_000
#: rule-1 latency of exec_config() (no jitter): internal + venue leg
LAT = exec_config().latency.internal_ns + 150_000
SEC = 1_000_000_000
T0 = 1_000_000_000_000


def digest(res) -> str:
    # lineterminator is fixed: pandas writes the platform's line ending by
    # default, which made this hash differ between Windows and Linux
    h = hashlib.sha256(res.fills.to_csv(index=False, lineterminator="\n").encode())
    blob = {"pnl": res.pnl, "c": res.counters, "d": res.diagnostics}
    h.update(json.dumps(blob, sort_keys=True, default=str).encode())
    return h.hexdigest()


def v2_like(**kw) -> QuotingConfig:
    """The shape of the signed v2 quoting config (ticks, join the touch)."""
    base = dict(
        skew_mode="ticks",
        h_min_ticks=0,
        inv_skew_ticks=1.0,
        max_inventory=300,
        feed_latency_ns=FEED,
        flatten_route="sim",
        amend_size_down=True,
        hidden_print_fills=True,
        flatten_lead_ns=5 * SEC,
        alpha_weight=0.0,
    )
    base.update(kw)
    return QuotingConfig(**base)


def run(events, scores, cfg, capture=None):
    """Run, optionally keeping the simulator for inspection."""
    if capture is None:
        return QuotingBacktester(exec_config(), cfg).run_instrument(events, scores, INS)
    real = qmod.ExecutionSimulator

    def spy(*a, **k):
        sim = real(*a, **k)
        capture.append(sim)
        return sim

    qmod.ExecutionSimulator = spy
    try:
        return QuotingBacktester(exec_config(), cfg).run_instrument(events, scores, INS)
    finally:
        qmod.ExecutionSimulator = real


class Stream:
    """Hand-built MBO stream on one instrument."""

    def __init__(self) -> None:
        self.evs: list[MarketEvent] = []
        self.oid = 1

    def emit(self, t, et, side, px, qty, o):
        n = len(self.evs) + 1
        self.evs.append(MarketEvent(n, INS, VEN, t, t, n, int(et), side, px, qty, o, 0))

    def add(self, t, side, px, qty) -> int:
        o = self.oid
        self.oid += 1
        self.emit(t, EventType.ADD, side, px, qty, o)
        return o

    def execute(self, t, side, px, qty, o):
        self.emit(t, EventType.EXECUTE, side, px, qty, o)


def scores_at(*ts) -> pd.DataFrame:
    return pd.DataFrame(
        {"exchange_ts": np.array(ts, dtype=np.int64), "expected_return": np.zeros(len(ts))}
    )


# ---------------------------------------------------------------- byte-identity

#: digests of the two runs below computed with the code BEFORE rule 10
#: (git HEAD eb8405f3): the grid mode must reproduce them bit for bit, on
#: Windows and on the Linux CI runners alike
PINNED_GRID = {
    "default": "eaadfe56544858c51ca6c83e077f4c6dc3a1993b33ffe490536a215e94c55819",
    "v2_like": "20db62f4e2ed9ff2b1565684b2570751c99a93382fcc3f9393df89529e887d7f",
}


@pytest.fixture(scope="module")
def synth():
    ev = synth_events(n_steps=2500, seed=11)
    return ev, make_scores(ev)


def test_grid_mode_byte_identical_to_before(synth):
    ev, sc = synth
    assert digest(run(ev, sc, QuotingConfig())) == PINNED_GRID["default"]
    cfg = v2_like(alpha_weight=1.0)
    assert digest(run(ev, sc, cfg)) == PINNED_GRID["v2_like"]


def test_dev_diagnostics_do_not_change_the_run(synth):
    ev, sc = synth
    a = run(ev, sc, v2_like())
    b = run(ev, sc, v2_like(dev_diagnostics=True))
    assert a.pnl == b.pnl and a.counters == b.counters
    pd.testing.assert_frame_equal(a.fills, b.fills[a.fills.columns])
    assert {"mid_1s", "mid_10s", "mid_60s"} <= set(b.fills.columns)
    s = b.summary()
    assert 0.0 <= s["time_at_touch_bid"] <= s["time_quoted_bid"] <= 1.0
    assert s["messages"] == s["requotes"] + s["cancels"] + s["amends"]


def test_events_mode_deterministic(synth):
    ev, sc = synth
    cfg = v2_like(trigger_mode="events", dev_diagnostics=True)
    a, b = run(ev, sc, cfg), run(ev, sc, cfg)
    assert digest(a) == digest(b)
    assert a.counters["ev_decisions"] > 0


def test_events_mode_pnl_identity_and_inventory_cap(synth):
    ev, sc = synth
    res = run(ev, sc, v2_like(trigger_mode="events"))
    p = res.pnl
    parts = p["spread_captured"] + p["markout"] + p["inventory_pnl"] + p["flatten_cost"]
    assert parts == pytest.approx(p["gross"], abs=1e-6)
    assert res.inventory["q"].abs().max() <= 300
    assert res.inventory["q"].iloc[-1] == 0


# ---------------------------------------------------------------- latency path


def _touch_move_stream():
    s = Stream()
    for k in range(3):
        s.add(T0, 0, 10_000 - k, 300)
        s.add(T0, 1, 10_002 + k, 300)
    t1 = T0 + 2 * SEC
    s.add(t1, 0, 10_001, 200)  # the bid touch moves up, away from our bid
    s.add(T0 + 10 * SEC, 1, 10_010, 100)  # a late quiet event
    return s.evs, t1


@pytest.mark.parametrize("mode", ["grid", "events"])
def test_reprice_on_touch_move_only_after_latency(mode):
    evs, t1 = _touch_move_stream()
    sims: list = []
    cfg = v2_like(trigger_mode=mode, flatten_lead_ns=0, ev_min_rest_ns=SEC // 2)
    run(evs, scores_at(T0 + SEC // 1000), cfg, capture=sims)
    bids = sorted((o for o in sims[0].orders.values() if o.side == 0), key=lambda o: o.order_id)
    first = bids[0]
    assert first.limit_ticks == 10_000
    assert first.arrival_ts == T0 + SEC // 1000 + FEED + LAT
    if mode == "grid":
        # one score row: the grid quoter never reacts to the move
        assert len(bids) == 1
        return
    new = bids[1]
    assert new.limit_ticks == 10_001
    # decided at the event, sent through feed + order latency
    assert new.decision_ts == t1 + FEED
    assert new.arrival_ts == t1 + FEED + LAT
    assert first.cancel_arrival_ts == t1 + FEED + LAT


def test_min_rest_defers_the_reprice():
    evs, t1 = _touch_move_stream()
    sims: list = []
    cfg = v2_like(trigger_mode="events", flatten_lead_ns=0, ev_min_rest_ns=5 * SEC)
    res = run(evs, scores_at(T0 + SEC // 1000), cfg, capture=sims)
    first = min((o for o in sims[0].orders.values() if o.side == 0), key=lambda o: o.order_id)
    # too young at t1: the cancel waits for the next event (T0 + 10 s)
    assert first.cancel_arrival_ts == T0 + 10 * SEC + FEED + LAT
    assert res.counters["ev_deferred_rest"] >= 1


# ---------------------------------------------------------------- the guard


def _sweep_stream(gap_ns: int, build: int = 2_000):
    """Our bid joins 10000 behind 300; the queue ahead is executed down to
    50 while 2000 builds at the ask; ``gap_ns`` later an informed sweep
    executes the rest of 10000 and 300 at 9999 (through our bid)."""
    s = Stream()
    b1 = s.add(T0, 0, 10_000, 300)
    b2 = s.add(T0, 0, 9_999, 300)
    s.add(T0, 0, 9_998, 300)
    s.add(T0, 1, 10_002, 150)  # 150 / (50 + 150) < 0.8: no guard yet
    s.add(T0, 1, 10_003, 300)
    tg = T0 + 2 * SEC
    s.execute(tg - SEC // 2, 0, 10_000, 250, b1)  # queue ahead 300 -> 50
    # opposite touch builds (guard condition), or a deep-book add that does not
    s.add(tg, 1, 10_002 if build >= 1_000 else 10_003, build)
    ts = tg + gap_ns
    s.execute(ts, 0, 10_000, 50, b1)
    s.execute(ts, 0, 9_999, 300, b2)
    s.add(ts + 30 * SEC, 1, 10_010, 100)
    return s.evs, tg, ts


def _bid_fills_after(res, t):
    f = res.fills
    return int(f[(~f["flatten"]) & (f["side"] == 0) & (f["ts"] >= t)]["qty"].sum())


@pytest.mark.parametrize("gap", [FEED + LAT - 1, FEED + LAT, 10_000_000])
def test_guard_cancels_before_sweep_iff_latency_allows(gap):
    evs, tg, ts = _sweep_stream(gap)
    sc = scores_at(T0 + SEC // 1000)
    ev_cfg = v2_like(trigger_mode="events", flatten_lead_ns=0)
    grid = run(evs, sc, v2_like(flatten_lead_ns=0))
    event = run(evs, sc, ev_cfg)
    assert _bid_fills_after(grid, ts) == 100  # the stale quote is picked off
    assert event.counters["ev_guard_pulls"] == 1
    # cancel decided at tg arrives at tg + FEED + LAT; activation/cancel
    # arrivals are applied before an event with exchange_ts >= arrival
    expect = 0 if gap >= FEED + LAT else 100
    assert _bid_fills_after(event, ts) == expect


def test_guard_needs_the_imbalance():
    """Same depletion, but the ask does not build: no pull, picked off."""
    evs, tg, ts = _sweep_stream(10_000_000, build=100)
    res = run(evs, scores_at(T0 + SEC // 1000), v2_like(trigger_mode="events", flatten_lead_ns=0))
    assert res.counters["ev_guard_pulls"] == 0
    assert _bid_fills_after(res, ts) == 100


# ---------------------------------------------------------------- pacing


def test_message_rate_cap(synth):
    ev, sc = synth
    cap = 3
    sims: list = []
    cfg = v2_like(trigger_mode="events", ev_min_rest_ns=0, ev_max_msgs_per_s=cap)
    res = run(ev, sc, cfg, capture=sims)
    sim = sims[0]
    stop = res.counters["flatten_start_ts"]
    quotes = [o for o in sim.orders.values() if o.type.name == "LIMIT"]
    times = [o.decision_ts for o in quotes if o.decision_ts - FEED < stop]
    # a cancel decided at d arrives at d + LAT (no jitter) unless its order
    # was still in flight; quotes are never cancelled in flight in events mode
    times += [
        o.cancel_arrival_ts - LAT
        for o in quotes
        if o.cancel_arrival_ts and o.cancel_arrival_ts - LAT - FEED < stop
    ]
    times = np.sort(np.asarray(times, dtype=np.int64))
    worst = max(
        int(np.searchsorted(times, x, side="right") - np.searchsorted(times, x - SEC, side="right"))
        for x in times
    )
    assert worst <= cap
    assert res.counters["ev_deferred_rate"] > 0
    loose = run(ev, sc, v2_like(trigger_mode="events", ev_min_rest_ns=0, ev_max_msgs_per_s=1000))
    assert loose.counters["requotes"] > res.counters["requotes"]


def test_min_rest_respected_on_every_cancel(synth):
    ev, sc = synth
    rest = 200_000_000
    sims: list = []
    res = run(ev, sc, v2_like(trigger_mode="events", ev_min_rest_ns=rest), capture=sims)
    stop = res.counters["flatten_start_ts"]
    for o in sims[0].orders.values():
        if o.type.name != "LIMIT" or not o.cancel_arrival_ts:
            continue
        decided = o.cancel_arrival_ts - LAT  # = t + feed
        if decided - FEED >= stop:
            continue  # flatten-sweep cancels are not policy decisions
        assert decided >= o.arrival_ts + rest

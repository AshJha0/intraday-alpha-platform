"""Opt-in extended features + event-time sampling (v1.12.0, M6)."""

from __future__ import annotations

import json
import math
import random

import pytest
from conftest import GOLDEN_DIR, REPO_ROOT
from iap.core.codec import read_jsonl
from iap.core.events import EventType, MarketEvent
from iap.features import extended as ext
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import build_registry, feature_names, registry_hash

NS = 1_000_000_000
T0 = 1_787_578_200_000_000_000  # 2026-08-24 09:30 New York (in session)
EQ, FX = 1, 101


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


class Feed:
    """Hand-built event stream for one instrument / venue."""

    def __init__(self, iid: int = EQ, venue: int = 1) -> None:
        self.iid, self.venue = iid, venue
        self.events: list[MarketEvent] = []
        self.seq = 0
        self.oid = 1000
        self.tid = 1

    def _ev(self, ts, et, side=0, price=0, qty=0, oid=0, tid=0) -> MarketEvent:
        self.seq += 1
        ev = MarketEvent(
            self.seq, self.iid, self.venue, ts, ts, self.seq, int(et), side, price, qty, oid, tid
        )
        self.events.append(ev)
        return ev

    def add(self, ts, side, price, qty) -> int:
        self.oid += 1
        self._ev(ts, EventType.ADD, side, price, qty, self.oid)
        return self.oid

    def cancel(self, ts, side, price, oid):
        return self._ev(ts, EventType.CANCEL, side, price, 0, oid)

    def execute(self, ts, side, price, qty, oid):
        return self._ev(ts, EventType.EXECUTE, side, price, qty, oid)

    def trade(self, ts, aggressor, price, qty):
        self.tid += 1
        return self._ev(ts, EventType.TRADE, aggressor, price, qty, 0, self.tid)


def _run(contexts, events, **kw):
    eng = ext.ExtendedFeatureEngine(contexts, **kw)
    return eng, [eng.apply(ev) for ev in events]


def _same(a, b) -> bool:
    return len(a) == len(b) and all(
        x == y or (math.isnan(x) and math.isnan(y)) for x, y in zip(a, b, strict=True)
    )


def _val(eng, vec, name):
    j = eng.feature_names.index(name)
    return vec.values[j], vec.validity[j]


# ------------------------------------------------------------- registry


def test_default_registry_and_hash_unchanged():
    with open(GOLDEN_DIR / "expected_features.json") as f:
        golden = json.load(f)
    before = registry_hash()
    reg = ext.extended_registry()
    assert registry_hash() == before == golden["registry_hash"]
    assert len(build_registry()) == golden.get("count", len(build_registry()))
    names = [s.name for s in reg]
    base = feature_names()
    assert names[: len(base)] == base  # appended, default order untouched
    added = names[len(base) :]
    assert added and all(n.endswith("_v1") for n in added)
    assert {s.family for s in reg[len(base) :]} == set(ext.EXT_FAMILY_ORDER)
    assert ext.extended_registry_hash() != before
    # a different parameterisation is a different feature_version
    other = ext.ExtendedConfig(hawkes_alpha=0.5)
    assert ext.extended_registry_hash(other) != ext.extended_registry_hash()


def test_default_feature_set_matches_base_engine(contexts):
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")[:1500]
    base = FeatureEngine(contexts, cadence_ns=0)
    eng = ext.ExtendedFeatureEngine(contexts, cadence_ns=0, feature_set="default")
    xeng = ext.ExtendedFeatureEngine(contexts, cadence_ns=0)
    n = len(base.feature_names)
    for ev in events:
        a, b, c = base.apply(ev), eng.apply(ev), xeng.apply(ev)
        assert _same(a.values, b.values)
        assert a.feature_version == b.feature_version
        assert _same(c.values[:n], a.values)
        assert c.validity[:n] == a.validity
        assert c.feature_version == ext.extended_registry_hash()
        for v, ok in zip(c.values, c.validity, strict=True):
            assert not (ok and math.isnan(v))  # NaN is never valid


def test_causal_future_events_do_not_change_past_rows(contexts):
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")[:1200]
    k = 700
    _, full = _run(contexts, events, cadence_ns=0)
    # perturb the future: drop every other event after k
    _, cut = _run(contexts, events[:k] + events[k::2], cadence_ns=0)
    for a, b in zip(full[:k], cut[:k], strict=True):
        assert a.timestamp == b.timestamp
        assert _same(a.values, b.values)
        assert a.validity == b.validity


# ------------------------------------------------------------- hawkes


def test_hawkes_matches_closed_form_after_burst(contexts):
    f = Feed()
    f.add(T0, 0, 2450, 500)
    f.add(T0, 1, 2451, 500)
    burst = [T0 + 60 * NS + i * 37_000_000 for i in range(25)]
    for i, t in enumerate(burst):
        f.trade(t, 0 if i % 5 else 1, 2451, 100)  # 20 buys, 5 sells
    probe = burst[-1] + 1_500_000_000
    f.add(probe, 0, 2440, 100)  # non-trade event: the emission point
    eng, vecs = _run(contexts, f.events, cadence_ns=0)
    vec = vecs[-1]
    cfg = ext.DEFAULT_CONFIG
    for beta in cfg.hawkes_betas:
        b = ext._beta_label(beta)
        exp_buy = sum(
            cfg.hawkes_alpha * math.exp(-beta * (probe - t) / NS)
            for i, t in enumerate(burst)
            if i % 5
        )
        exp_sell = sum(
            cfg.hawkes_alpha * math.exp(-beta * (probe - t) / NS)
            for i, t in enumerate(burst)
            if not i % 5
        )
        buy, ok = _val(eng, vec, f"hawkes_buy_b{b}_v1")
        assert ok and buy == pytest.approx(cfg.hawkes_mu + exp_buy, rel=1e-12)
        sell, _ = _val(eng, vec, f"hawkes_sell_b{b}_v1")
        assert sell == pytest.approx(cfg.hawkes_mu + exp_sell, rel=1e-12)
        tot, _ = _val(eng, vec, f"hawkes_total_b{b}_v1")
        assert tot == pytest.approx(buy + sell, rel=1e-12)
        imb, ok = _val(eng, vec, f"hawkes_imb_b{b}_v1")
        assert ok and imb == pytest.approx((buy - sell) / (buy + sell), rel=1e-12)
    # warmup: beta=0.1 needs 50s, so the first vector is invalid
    assert not _val(eng, vecs[0], "hawkes_total_b0p1_v1")[1]


def _simulate_hawkes(mu, alpha, beta, horizon, seed):
    rng = random.Random(seed)
    t, s, out = 0.0, 0.0, []  # s = excitation at t
    while True:
        lam_bar = mu + s
        w = rng.expovariate(lam_bar)
        s *= math.exp(-beta * w)
        t += w
        if t > horizon:
            return out
        if rng.random() * lam_bar <= mu + s:
            out.append(t)
            s += alpha


def test_hawkes_mle_helper_recovers_parameters():
    pytest.importorskip("scipy")
    mu, alpha, beta = 0.5, 0.8, 1.6
    times = _simulate_hawkes(mu, alpha, beta, 3000.0, 7)
    fit = ext.fit_hawkes_exp(times, 3000.0)
    assert fit.branching_ratio == pytest.approx(alpha / beta, abs=0.1)
    assert fit.mu == pytest.approx(mu, rel=0.3)
    assert fit.loglik >= ext.hawkes_loglik(times, 3000.0, mu, alpha, beta) - 1e-6


# ------------------------------------------------------------- trade signs


def test_sign_autocorrelation_of_markov_sign_process(contexts):
    p = 0.8  # P(sign repeats) -> acf(k) = (2p-1)^k
    cfg = ext.ExtendedConfig(sign_n=6000, sign_lags=(1, 2, 3))
    rng = random.Random(3)
    f = Feed()
    f.add(T0, 0, 2450, 500)
    f.add(T0, 1, 2451, 500)
    s, signs = 1, []
    for i in range(6000):
        if rng.random() > p:
            s = -s
        signs.append(s)
        f.trade(T0 + NS + i * 1_000_000, 0 if s > 0 else 1, 2451, 100)
    eng = ext.ExtendedFeatureEngine(contexts, cadence_ns=0, config=cfg)
    vec = [eng.apply(ev) for ev in f.events][-1]
    m = sum(signs) / len(signs)
    var = sum((x - m) ** 2 for x in signs)
    for k in cfg.sign_lags:
        v, ok = _val(eng, vec, f"sign_acf_l{k}_n6000_v1")
        exact = sum((signs[i] - m) * (signs[i - k] - m) for i in range(k, len(signs))) / var
        assert ok and v == pytest.approx(exact, rel=1e-12)
        assert v == pytest.approx((2 * p - 1) ** k, abs=0.05)


def test_sign_acf_invalid_until_window_full_and_on_zero_variance(contexts):
    f = Feed()
    f.add(T0, 0, 2450, 500)
    f.add(T0, 1, 2451, 500)
    for i in range(150):
        f.trade(T0 + NS + i * 1_000_000, 0, 2451, 100)  # all buys: zero variance
    eng, vecs = _run(contexts, f.events, cadence_ns=0)
    for v in (vecs[50], vecs[-1]):
        val, ok = _val(eng, v, "sign_acf_l1_n100_v1")
        assert not ok and math.isnan(val)


# ------------------------------------------------------------- depletion


def test_queue_time_to_depletion_for_planted_rate(contexts):
    f = Feed()
    big = 50_000
    f.add(T0, 0, 2450, big)
    f.add(T0, 1, 2451, 800)
    small = [f.add(T0, 0, 2450, 100) for _ in range(200)]
    cancels = []
    for i, oid in enumerate(small[:150]):  # 10 cancels/s of 100 shares at the best bid
        t = T0 + 5 * NS + i * 100_000_000
        f.cancel(t, 0, 2450, oid)
        cancels.append(t)
    probe = cancels[-1] + 1
    f.add(probe, 0, 2400, 5)  # away from the best: emission point
    eng, vecs = _run(contexts, f.events, cadence_ns=0)
    vec = vecs[-1]
    q = big + 100 * (200 - 150)
    for w, wns in (("1s", NS), ("10s", 10 * NS)):
        dep = 100 * sum(1 for t in cancels if probe - wns < t <= probe)
        expected = min(q / (dep / (wns / NS)), ext.DEFAULT_CONFIG.ttd_cap_s)
        v, ok = _val(eng, vec, f"qttd_bid_w{w}_v1")
        assert ok and v == pytest.approx(expected, rel=1e-12)
        a, ok = _val(eng, vec, f"qttd_ask_w{w}_v1")
        assert ok and a == ext.DEFAULT_CONFIG.ttd_cap_s  # undepleted -> cap
    assert not _val(eng, vecs[2], "qttd_bid_w10s_v1")[1]  # warmup


# ------------------------------------------------------------- hidden / odd lot


def test_oddlot_and_hidden_share(contexts):
    f = Feed()
    f.add(T0, 0, 2450, 1000)
    rest = f.add(T0, 1, 2451, 1000)
    t = T0 + 61 * NS
    f.trade(t, 0, 2451, 50)  # odd lot, hidden (no EXECUTE)
    f.execute(t + 1, 1, 2451, 200, rest)  # displayed fill ...
    f.trade(t + 1, 0, 2451, 200)  # ... and its print
    f.trade(t + 2, 1, 2450, 300)  # hidden print
    eng, vecs = _run(contexts, f.events, cadence_ns=0)
    odd, ok = _val(eng, vecs[-1], "oddlot_share_w1m_v1")
    assert ok and odd == pytest.approx(50 / 550)
    hid, ok = _val(eng, vecs[-1], "hidden_share_w1m_v1")
    assert ok and hid == pytest.approx(1 - 200 / 550)
    # before the first EXECUTE the source has not shown order-level fills
    assert not _val(eng, vecs[2], "hidden_share_w1m_v1")[1]


def test_hidden_liquidity_degrades_without_source_fields(contexts):
    f = Feed(iid=FX, venue=11)
    f.add(T0, 0, 108648, 1_000_000)
    f.add(T0, 1, 108650, 1_000_000)
    for i in range(10):
        f.trade(T0 + 61 * NS + i, 0, 108650, 50)
    eng, vecs = _run(contexts, f.events, cadence_ns=0)
    for name in ("oddlot_share_w1m_v1", "hidden_share_w1m_v1"):
        v, ok = _val(eng, vecs[-1], name)
        assert not ok and math.isnan(v)


# ------------------------------------------------------------- event-time sampling


def _trade_feed(n, qty=30):
    f = Feed()
    f.add(T0, 0, 2450, 500)
    f.add(T0, 1, 2451, 500)
    for i in range(n):
        f.trade(T0 + NS + i * 1_000_000, i % 2, 2451, qty)
    return f


@pytest.mark.parametrize("feature_set", ["default", "extended"])
def test_event_clock_row_counts(contexts, feature_set):
    f = _trade_feed(100)
    m = len(f.events)
    _, vecs = _run(contexts, f.events, feature_set=feature_set, sampling="events", sample_n=7)
    rows = [v for v in vecs if v is not None]
    assert len(rows) == 1 + (m - 1) // 7
    emitted = [i for i, v in enumerate(vecs) if v is not None]
    assert emitted[:3] == [0, 7, 14]


def test_volume_clock_row_counts(contexts):
    f = _trade_feed(100, qty=30)  # 3000 shares traded
    _, vecs = _run(contexts, f.events, sampling="volume", sample_n=200)
    rows = [v for v in vecs if v is not None]
    # first event emits; then one row per 7 trades (7*30 = 210 >= 200)
    assert len(rows) == 1 + 100 // 7


def test_bad_options_rejected(contexts):
    with pytest.raises(ValueError):
        ext.ExtendedFeatureEngine(contexts, sampling="events", sample_n=0)
    with pytest.raises(ValueError):
        ext.ExtendedFeatureEngine(contexts, feature_set="bogus")
    with pytest.raises(ValueError):
        ext.ExtendedConfig(sign_n=5, sign_lags=(5,))

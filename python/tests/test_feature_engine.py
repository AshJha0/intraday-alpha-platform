"""Incremental engine vs independent brute-force recomputation (1e-9).

One representative feature per family is recomputed from scratch with the
reference OrderBook + pandas (tests/bruteforce_features.py — deliberately
not sharing any window code with the engine) at several emission times.
"""

from __future__ import annotations

import math

import pytest

from bruteforce_features import (
    approx,
    at_or_before,
    book_frames,
    mid_change_frame,
    window,
)
from conftest import GOLDEN_DIR, REPO_ROOT
from iap.core.codec import read_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import feature_index

NS = 1_000_000_000
# emission indices probed (1-based, spread across warmup and steady state)
EQ_PROBES = (120, 400, 800, 1300, 1700, 2000)
FX_PROBES = (100, 300, 500, 800)


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


@pytest.fixture(scope="module")
def idx():
    return feature_index()


def _run(events, contexts):
    engine = FeatureEngine(contexts, cadence_ns=0)
    vecs = [engine.apply(ev) for ev in events]
    return engine, vecs


@pytest.fixture(scope="module")
def eq(contexts):
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    engine, vecs = _run(events, contexts)
    book_df, trade_df = book_frames(events)
    return {
        "events": events, "vecs": vecs, "book": book_df, "trades": trade_df,
        "chg": mid_change_frame(book_df), "first_ts": events[0].exchange_ts,
        "tick": 0.01,
    }


@pytest.fixture(scope="module")
def fx(contexts):
    events = read_jsonl(GOLDEN_DIR / "events_fx_quote.jsonl")
    engine, vecs = _run(events, contexts)
    return {"events": events, "vecs": vecs, "first_ts": events[0].exchange_ts}


def _probe(data, i, idx, name):
    vec = data["vecs"][i - 1]
    j = idx[name]
    return vec.timestamp, vec.values[j], vec.validity[j]


# ------------------------------------------------------------------- price

def test_price_ret_log_30s_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "ret_log_30s_v1")
        past = at_or_before(eq["chg"], t - 30 * NS, i)
        now = at_or_before(eq["chg"], t, i)
        if past is None or now is None:
            assert not ok
            continue
        assert ok
        assert approx(got, math.log(now.mid2) - math.log(past.mid2))
        checked += 1
    assert checked >= 3


def test_price_ret_simple_5s_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "ret_simple_5s_v1")
        past = at_or_before(eq["chg"], t - 5 * NS, i)
        now = at_or_before(eq["chg"], t, i)
        if past is None or now is None:
            assert not ok
            continue
        assert ok and approx(got, now.mid2 / past.mid2 - 1.0)
        checked += 1
    assert checked >= 3


# ------------------------------------------------------------------- micro

def test_micro_imbalance_l5_and_spread(eq, idx):
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "imbalance_l5_v1")
        state = at_or_before(eq["book"].dropna(subset=["bp", "ap"]), t, i)
        assert ok
        assert approx(got, (state.b5 - state.a5) / (state.b5 + state.a5))
        _, spread, sok = _probe(eq, i, idx, "spread_bps_v1")
        mid = (state.bp + state.ap) * eq["tick"] / 2
        assert sok
        assert approx(spread, (state.ap - state.bp) * eq["tick"] / mid * 1e4)


def test_micro_depth_avg_w10s_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "depth_bid_l1_avg_w10s_v1")
        if t - eq["first_ts"] < 10 * NS:
            assert not ok
            continue
        w = window(eq["book"].dropna(subset=["bp", "ap"]), t, 10 * NS, i)
        if w.empty:
            assert not ok
            continue
        assert ok and approx(got, float(w.b1.mean()))
        checked += 1
    assert checked >= 3


def test_micro_queue_depletion_rate_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "queue_depletion_rate_bid_w10s_v1")
        if t - eq["first_ts"] < 10 * NS:
            assert not ok
            continue
        w = window(eq["book"][eq["book"].has_prev], t, 10 * NS, i)
        assert ok and approx(got, float(w.dep_b.sum()) / 10.0)
        checked += 1
    assert checked >= 3


# -------------------------------------------------------------------- flow

def test_flow_ofi_multilevel_vs_pandas(eq, idx):
    combos = [("ofi_l1_w1s_v1", "ofi1", 1 * NS),
              ("ofi_l3_w5s_v1", "ofi3", 5 * NS),
              ("ofi_l5_w5s_v1", "ofi5", 5 * NS),
              ("ofi_l10_w30s_v1", "ofi10", 30 * NS)]
    checked = 0
    for i in EQ_PROBES:
        for name, col, w_ns in combos:
            t, got, ok = _probe(eq, i, idx, name)
            if t - eq["first_ts"] < w_ns:
                assert not ok
                continue
            w = window(eq["book"][eq["book"].has_prev], t, w_ns, i)
            assert ok, name
            assert approx(got, float(w[col].sum())), name
            checked += 1
    assert checked >= 8


def test_flow_signed_volume_and_imbalance_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "signed_volume_w10s_v1")
        if t - eq["first_ts"] < 10 * NS:
            assert not ok
            continue
        w = window(eq["trades"], t, 10 * NS, i)
        assert ok and approx(got, float(w.signed.sum()))
        buys = float(w[w.signed > 0].qty.sum())
        sells = float(w[w.signed < 0].qty.sum())
        t2, got2, ok2 = _probe(eq, i, idx, "trade_imbalance_w10s_v1")
        if buys + sells > 0:
            assert ok2 and approx(got2, (buys - sells) / (buys + sells))
        else:
            assert not ok2
        checked += 1
    assert checked >= 3


def test_flow_cancel_intensity_vs_pandas(eq, idx):
    from iap.core.events import EventType
    cancels = [(ev.exchange_ts, n) for n, ev in enumerate(eq["events"], 1)
               if ev.event_type == EventType.CANCEL]
    import pandas as pd
    cdf = pd.DataFrame(cancels, columns=["ts", "n"])
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "cancel_intensity_w10s_v1")
        if t - eq["first_ts"] < 10 * NS:
            assert not ok
            continue
        n = len(window(cdf, t, 10 * NS, i))
        assert ok and approx(got, n / 10.0)
        checked += 1
    assert checked >= 3


# --------------------------------------------------------------- liquidity

def test_liquidity_effective_spread_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "effective_spread_bps_w1m_v1")
        if t - eq["first_ts"] < 60 * NS:
            assert not ok
            continue
        w = window(eq["trades"].dropna(subset=["mid2"]), t, 60 * NS, i)
        if w.empty:
            assert not ok
            continue
        tick = eq["tick"]
        eff = (2.0 * (w.price_ticks * tick - w.mid2 * tick / 2).abs()
               / (w.mid2 * tick / 2) * 1e4)
        assert ok and approx(got, float(eff.mean()))
        checked += 1
    assert checked >= 2


def test_liquidity_quoted_depth_total(eq, idx):
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "quoted_depth_total_v1")
        state = at_or_before(eq["book"].dropna(subset=["bp", "ap"]), t, i)
        assert ok and approx(got, float(state.b10 + state.a10))


# --------------------------------------------------------------------- vol

def test_vol_rvol_w1m_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "rvol_w1m_v1")
        if t - eq["first_ts"] < 60 * NS:
            assert not ok
            continue
        w = window(eq["chg"].dropna(subset=["dlm"]), t, 60 * NS, i)
        assert ok and approx(got, math.sqrt(float((w.dlm ** 2).sum()) / 60.0))
        checked += 1
    assert checked >= 3


def test_vol_range_bps_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "range_bps_w1m_v1")
        if t - eq["first_ts"] < 60 * NS:
            assert not ok
            continue
        w = window(eq["chg"], t, 60 * NS, i)
        if w.empty:
            assert not ok
            continue
        now = at_or_before(eq["chg"], t, i)
        expected = (w.mid2.max() - w.mid2.min()) / now.mid2 * 1e4
        assert ok and approx(got, float(expected))
        checked += 1
    assert checked >= 3


# --------------------------------------------------------------------- tod

def test_tod_minute_of_day_and_norm_spread(eq, idx):
    # independent expanding-profile recomputation for norm_spread_m5
    tick = eq["tick"]
    import pandas as pd
    prof_count = {}
    prof_sum = {}
    checked = 0
    probe_set = set(EQ_PROBES)
    for i, vec in enumerate(eq["vecs"], start=1):
        t = vec.timestamp
        # ok-ness is that of the LATEST refresh (a one-sided book at
        # emission time means no spread metric — pinned _metric_value rule)
        state = at_or_before(eq["book"], t, i)
        spread_bps = None
        if (state is not None and pd.notna(state.bp)
                and pd.notna(state.ap)):
            mid = (state.bp + state.ap) * tick / 2
            spread_bps = (state.ap - state.bp) * tick / mid * 1e4
        bucket = int(((t // NS) % 86_400) // 300)
        if i in probe_set:
            mod = ((t // NS) % 86_400) / 60.0 + (t % NS) / 6e10
            j = idx["minute_of_day_v1"]
            assert vec.validity[j] and approx(vec.values[j], mod)
            j = idx["norm_spread_m5_v1"]
            n = prof_count.get(bucket, 0)
            if spread_bps is not None and n >= 10:
                mean = prof_sum[bucket] / n
                assert vec.validity[j]
                assert approx(vec.values[j], spread_bps / mean)
                checked += 1
        if spread_bps is not None:
            prof_count[bucket] = prof_count.get(bucket, 0) + 1
            prof_sum[bucket] = prof_sum.get(bucket, 0.0) + spread_bps
    assert checked >= 2


# ------------------------------------------------------------------ xasset

def test_xasset_ref_ret_is_own_ret_for_reference_instrument(fx, idx):
    """EUR/USD is its own reference: ref_ret_h must equal ret_log_h."""
    checked = 0
    for i in FX_PROBES:
        vec = fx["vecs"][i - 1]
        for h in ("1s", "10s", "1m"):
            jr = idx[f"ref_ret_{h}_v1"]
            jo = idx[f"ret_log_{h}_v1"]
            if vec.validity[jo]:
                assert vec.validity[jr]
                assert approx(vec.values[jr], vec.values[jo])
                checked += 1
    assert checked >= 5


def test_xasset_beta_and_corr_self_reference(fx, idx):
    """Against itself: beta = 1 and contemporaneous corr = 1 (when valid)."""
    seen = 0
    for vec in fx["vecs"]:
        jb, jc = idx["beta_w5m_v1"], idx["corr_contemp_w5m_v1"]
        if vec.validity[jb]:
            assert approx(vec.values[jb], 1.0)
            seen += 1
        if vec.validity[jc]:
            assert approx(vec.values[jc], 1.0)
    assert seen > 0


# ------------------------------------------------------------------- venue

def test_venue_update_share_vs_pandas(fx, idx):
    import pandas as pd
    updates = pd.DataFrame(
        {"ts": [ev.exchange_ts for ev in fx["events"]],
         "vid": [ev.venue_id for ev in fx["events"]]})
    checked = 0
    for i in FX_PROBES:
        vec = fx["vecs"][i - 1]
        t = vec.timestamp
        if t - fx["first_ts"] < 10 * NS:
            continue
        w = window(updates, t, 10 * NS, i)
        if w.empty:
            continue
        shares = w.vid.value_counts() / len(w)
        j = idx["venue_update_share_top_w10s_v1"]
        assert vec.validity[j]
        assert approx(vec.values[j], float(shares.max()))
        j = idx["venue_update_hhi_w10s_v1"]
        assert approx(vec.values[j], float((shares ** 2).sum()))
        checked += 1
    assert checked >= 2


# ------------------------------------------------------------------ regime

def test_regime_meanrev_vs_pandas(eq, idx):
    checked = 0
    for i in EQ_PROBES:
        t, got, ok = _probe(eq, i, idx, "meanrev_score_w1m_v1")
        if t - eq["first_ts"] < 60 * NS:
            assert not ok
            continue
        w = window(eq["chg"], t, 60 * NS, i)
        if w.empty:
            assert not ok
            continue
        now = at_or_before(eq["chg"], t, i)
        mean = float(w.mid2.mean())
        var = float((w.mid2 ** 2).mean()) - mean * mean
        std = math.sqrt(max(var, 0.0))
        assert ok and approx(got, -(now.mid2 - mean) / (std + 1e-12))
        checked += 1
    assert checked >= 3


# -------------------------------------------------------------------- exec

def test_exec_half_spread_and_fill_prob_consistency(eq, idx):
    for i in EQ_PROBES:
        vec = eq["vecs"][i - 1]
        js, jh = idx["spread_bps_v1"], idx["half_spread_cost_bps_v1"]
        if vec.validity[js]:
            assert vec.validity[jh]
            assert approx(vec.values[jh], vec.values[js] / 2.0)
        # fill_prob formula from its registered inputs
        jd = idx["queue_depletion_rate_bid_w10s_v1"]
        jq = idx["depth_bid_l1_v1"]
        jf = idx["fill_prob_bid_h1s_v1"]
        if vec.validity[jd] and vec.validity[jq] and vec.validity[jf]:
            expected = 1.0 - math.exp(
                -vec.values[jd] * 1.0 / vec.values[jq])
            assert approx(vec.values[jf], expected)


# -------------------------------------------------------- jump detector trim

def _quote(seq, side, price, qty, ts):
    from conftest import mkev
    from iap.core.events import EventType
    return mkev(seq, EventType.QUOTE, side, price, qty, order_id=seq, ts=ts)


def test_jump_detector_trims_stale_rvol_window(contexts):
    """The 1m |dlm| baseline must be trimmed to (t-1m, t] BEFORE the jump
    comparison: with a coarse emission cadence, samples older than 1m would
    otherwise stay in the window and manufacture false jumps."""
    from iap.features.volatility import JUMP_MIN_OBS

    base = 1_700_000_000_000_000_000
    # huge cadence: after the first event, no emissions => trim_all never runs
    engine = FeatureEngine(contexts, cadence_ns=10**18)
    seq = 0

    def q(side, price, ts):
        nonlocal seq
        seq += 1
        engine.apply(_quote(seq, side, price, 1000, ts))

    q(1, 1002, base)  # fixed ask
    t = base
    # warm phase: > JUMP_MIN_OBS small 1-tick mid changes inside ~65s
    for k in range(JUMP_MIN_OBS + 10):
        t = base + (k + 1) * 1_500_000_000
        q(0, 1000 + (k % 2), t)
    st = engine.states[1]
    assert st.jumps.count == 0  # 1-tick alternation is never a 4x jump

    # long quiet gap: every warm-phase sample is now OLDER than 1m
    t_jump = t + 80_000_000_000
    q(0, 1040, t_jump)  # big move; stale-window mean would flag a jump
    assert st.jumps.count == 0, (
        "stale samples outside (t-1m, t] contaminated the jump baseline"
    )

    # control: rebuild a warm in-window baseline, then a genuine jump fires
    for k in range(JUMP_MIN_OBS + 35):
        t_jump += 1_500_000_000
        q(0, 1040 + (k % 2), t_jump)
    assert st.jumps.count == 0
    q(0, 1080, t_jump + 1_000_000_000)
    assert st.jumps.count == 1

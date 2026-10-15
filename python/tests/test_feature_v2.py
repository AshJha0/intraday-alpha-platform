"""Feature spec v2: 15 m / 30 m / 1 h windows (v1.17 C4) and the share-normalised
``sharenorm`` family with bust removal (v1.17 A5) as an engine extension.

A synthetic 2.5-hour one-venue book (SplitMix64) drives a
``FeatureEngine`` with the :class:`FeatureV2Extension` attached; the v2
values are checked against an independent pandas recomputation from the
recorded log mids, against the golden ``tests/golden/expected_features_v2.json``
and the base vectors are shown to be untouched by the extension.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest
from conftest import GOLDEN_DIR, REPO_ROOT
from iap.core.events import EventType, MarketEvent
from iap.core.rng import SplitMix64
from iap.features import feature_v2 as f2
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import registry_hash
from iap.features.spec import EPS
from iap.reference.session_adv import SessionAdvTable

NS = 1_000_000_000
T0 = 1_787_578_200 * NS  # 2026-08-24 09:30 New York
EQ = 1
GOLDEN = GOLDEN_DIR / "expected_features_v2.json"
TOL = 1e-9
ADV = 2_000_000.0
N_LONGWIN = 12
N_SHARENORM = 14


@pytest.fixture(scope="module")
def contexts():
    return build_contexts(REPO_ROOT / "configs")


def _events(minutes: int = 150, seed: int = 3, start: int = T0, iid: int = EQ) -> list[MarketEvent]:
    """A one-venue book: a bid and an ask order re-placed every 5 s one tick
    either side of a random-walk mid (prices in ticks), one trade a minute."""
    rng = SplitMix64(seed)
    out: list[MarketEvent] = []
    seq = 0
    oid = 1000
    mid = 10_000  # price in ticks (instrument 1: tick 0.01 -> 100.00)
    cur: list[int] = []  # live order ids (bid, ask)

    def ev(ts, et, side=0, price=0, qty=0, order_id=0, trade_id=0):
        nonlocal seq
        seq += 1
        out.append(
            MarketEvent(seq, iid, 1, ts, ts, seq, int(et), side, price, qty, order_id, trade_id)
        )

    for i in range(minutes * 12):
        t = start + i * 5 * NS
        if i and rng.uniform() < 0.6:
            mid += 2 if rng.uniform() < 0.5 else -2  # two ticks (keeps the spread centred)
        for o in cur:
            ev(t, EventType.CANCEL, 0, 0, 0, o)
        cur = []
        for side, price in ((0, mid - 1), (1, mid + 1)):
            oid += 1
            ev(t, EventType.ADD, side, price, 100 + int(rng.uniform() * 400), oid)
            cur.append(oid)
        if i % 12 == 0:
            ev(t + NS, EventType.TRADE, 0 if rng.uniform() < 0.5 else 1, mid, 100, 0, i + 1)
    return out


def _run(contexts, events, keep=True, adv=None, families=None, depths=None):
    eng = FeatureEngine(contexts, cadence_ns=0)
    ext = f2.FeatureV2Extension(
        eng, keep=keep, adv={EQ: ADV} if adv is None else adv, families=families
    )
    rec: list[tuple[int, float, bool]] = []  # (t, logmid, book_ok) per emission
    base: list = []

    def cb(vec, v2):
        st = eng.states[vec.instrument_id]
        rec.append((vec.timestamp, float(st.logmid), bool(st.book_ok)))
        base.append(vec)
        if depths is not None:
            depths.append((st.bid_q, st.ask_q, st.db10, st.da10))

    ext._user_cb = cb
    for e in events:
        eng.apply(e)
    return eng, ext, rec, base


def _bruteforce(rec, t, w_ns):
    """The four v2 values at emission ``t`` from the recorded log mids."""
    if not rec or rec[-1][0] != t or not rec[-1][2]:
        return [None] * 4  # the book is not two-sided at this emission
    rows = [(ts, lm) for ts, lm, ok in rec if ok and ts <= t]
    if not rows:
        return [None] * 4
    changes = [rows[0]]
    for r in rows[1:]:
        if r[1] != changes[-1][1]:
            changes.append(r)
    past = [lm for ts, lm in changes if ts <= t - w_ns]
    ret = rows[-1][1] - past[-1] if past else None
    sq = 0.0
    for a, b in zip(changes, changes[1:], strict=False):
        if t - w_ns < b[0] <= t:
            sq += (b[1] - a[1]) ** 2
    rvol = math.sqrt(sq / (w_ns / 1e9))
    trend = ret / (rvol * math.sqrt(w_ns / 1e9) + EPS) if ret is not None and rvol > 0 else None
    win = [lm for ts, lm in rows if t - w_ns < ts <= t]
    rng = max(win) - min(win) if win else None
    return [ret, rvol, trend, rng]


# ---------------------------------------------------------------- registry


def test_v2_registry_is_separate_and_pinned():
    names = f2.v2_feature_names()
    assert len(names) == N_LONGWIN + N_SHARENORM and all(n.endswith("_v2") for n in names)
    assert names[:4] == ["ret_log_w15m_v2", "rvol_w15m_v2", "trend_score_w15m_v2", "range_w15m_v2"]
    assert names[N_LONGWIN] == "session_volume_adv_v2"
    assert names[N_LONGWIN + 1 : N_LONGWIN + 4] == [
        "traded_volume_adv_w1m_v2",
        "signed_volume_adv_w1m_v2",
        "traded_volume_session_w1m_v2",
    ]
    assert names[-4:] == [
        "depth_l1_adv_v2",
        "depth_diff_l1_adv_v2",
        "depth_l10_adv_v2",
        "depth_diff_l10_adv_v2",
    ]
    assert f2.WINDOWS_V2_NS == {"15m": 900 * NS, "30m": 1800 * NS, "1h": 3600 * NS}
    assert f2.WINDOWS_SHARENORM_NS == {"1m": 60 * NS, "5m": 300 * NS, "15m": 900 * NS}
    assert f2.FAMILIES_V2 == ("longwin", "sharenorm")
    assert f2.v2_feature_names(["longwin"]) == names[:N_LONGWIN]
    assert f2.v2_feature_names(["sharenorm"]) == names[N_LONGWIN:]
    assert f2.v2_feature_names(["sharenorm", "longwin"]) == names  # order pinned
    with pytest.raises(ValueError):
        f2.v2_feature_names(["longwin", "nope"])
    h = f2.v2_registry_hash()
    assert len(h) == 64 and h != registry_hash() != f2.v2_registry_hash(["longwin"])
    assert f2.v2_registry_dicts()[0] == {"base_registry_hash": registry_hash()}
    assert registry_hash() == "585dd7b92b738f9da7df863dba1ac049ff10b75b60a8559baac1e49b6f3062ac"


# ---------------------------------------------------------------- values


def test_values_match_bruteforce_and_base_vectors_are_untouched(contexts):
    events = _events()
    eng, ext, rec, base = _run(contexts, events)
    plain = FeatureEngine(contexts, cadence_ns=0)
    plain_vecs = [v for v in (plain.apply(e) for e in events) if v is not None]
    assert len(plain_vecs) == len(base) == len(ext.vectors) == ext.vectors_emitted
    for a, b in zip(plain_vecs, base, strict=True):
        assert a.timestamp == b.timestamp and a.feature_version == b.feature_version
        assert a.values == b.values or all(
            x == y or (math.isnan(x) and math.isnan(y))
            for x, y in zip(a.values, b.values, strict=True)
        )
        assert a.validity == b.validity
    checked = 0
    for idx in range(0, len(ext.vectors), 97):
        vec = ext.vectors[idx]
        assert vec.feature_version == ext.feature_version
        t = vec.timestamp
        for wi, (w, w_ns) in enumerate(f2.WINDOWS_V2_NS.items()):
            warm = t - T0 >= w_ns
            exp = _bruteforce(rec[: idx + 1], t, w_ns) if warm else [None] * 4
            for k in range(4):
                v, ok = vec.values[4 * wi + k], vec.validity[4 * wi + k]
                if exp[k] is None:
                    assert not ok and math.isnan(v), (w, k, idx)
                else:
                    assert ok and abs(v - exp[k]) <= TOL * max(1.0, abs(exp[k])), (w, k, idx)
                    checked += 1
    assert checked > 100
    assert ext.session_resets == 0
    assert ext.prints_recorded == len([e for e in events if e.event_type == EventType.TRADE])
    assert ext.busts_applied == 0 and ext.busts_unmatched == 0
    ext.detach()  # the engine is restored
    assert "apply" not in vars(eng) and "on_cross_print" not in vars(eng)


def test_windows_are_warm_only_after_their_length(contexts):
    _, ext, rec, _ = _run(contexts, _events(minutes=70))
    for vec, (_, _, book_ok) in zip(ext.vectors, rec, strict=True):
        dt = vec.timestamp - T0
        for wi, w_ns in enumerate(f2.WINDOWS_V2_NS.values()):
            rvol_ok = vec.validity[4 * wi + 1]
            assert rvol_ok == (book_ok and dt >= w_ns), (dt, w_ns)
    assert any(v.validity[4 * 2 + 1] for v in ext.vectors)  # the 1 h window did warm


def test_new_session_day_resets_the_windows(contexts):
    """Day 2's first hour carries no 1 h value even though day 1 is in the stream."""
    day1 = _events(minutes=80)
    day2 = _events(minutes=80, start=T0 + 86_400 * NS, seed=4)
    _, ext, rec, _ = _run(contexts, day1 + day2)
    assert ext.session_resets == 1
    for vec, (_, _, book_ok) in zip(ext.vectors, rec, strict=True):
        if vec.timestamp >= T0 + 86_400 * NS:
            dt = vec.timestamp - (T0 + 86_400 * NS)
            assert vec.validity[4 * 2 + 1] == (book_ok and dt >= 3600 * NS)
            assert vec.validity[4 * 0 + 1] == (book_ok and dt >= 900 * NS)


def test_v2_frame_and_detach(contexts):
    eng, ext, _, _ = _run(contexts, _events(minutes=40))
    df = f2.v2_frame(ext.vectors)
    assert df["session_volume_adv_v2"].iloc[-1] == 40 * 100 / ADV
    assert list(df.columns) == ["exchange_ts", *f2.v2_feature_names()]
    assert len(df) == len(ext.vectors)
    assert df["ret_log_w15m_v2"].notna().sum() > 0 and df["rvol_w1h_v2"].isna().all()
    assert np.all(np.diff(df["exchange_ts"].to_numpy()) >= 0)
    ext.detach()
    assert eng.on_vector is None


# ---------------------------------------------------------------- golden


def _golden_doc(contexts) -> dict:
    _, ext, _, _ = _run(contexts, _events())
    n = len(ext.vectors)
    points = sorted({n // 6, n // 3, n // 2, (2 * n) // 3, (5 * n) // 6, n - 1})
    return {
        "description": (
            "iap.features.feature_v2 golden: a synthetic one-venue 150-minute book "
            "(test_feature_v2._events, SplitMix64 seed 3, 5 s requotes one tick either "
            "side of a two-tick random-walk mid, one trade a minute) through FeatureEngine(cadence 0) with the "
            "FeatureV2Extension attached (both families, static ADV 2e6 shares); per "
            "checkpoint (emission index) every v2 feature's "
            "value (null when invalid) and validity. Tolerance abs/rel 1e-9. Validated "
            "against an independent pandas-free brute-force recomputation in the same test."
        ),
        "x-version": 2,
        "v2_registry_hash": ext.feature_version,
        "base_registry_hash": registry_hash(),
        "feature_names": ext.feature_names,
        "n_vectors": n,
        "checkpoints": {
            str(i): {
                "timestamp": ext.vectors[i].timestamp,
                "values": [
                    v if ok else None
                    for v, ok in zip(ext.vectors[i].values, ext.vectors[i].validity, strict=True)
                ],
                "validity": ext.vectors[i].validity,
            }
            for i in points
        },
    }


def test_golden_features_v2(contexts):
    doc = _golden_doc(contexts)
    if os.environ.get("IAP_REGEN_V2_GOLDEN") == "1":
        Path(GOLDEN).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert doc["v2_registry_hash"] == expected["v2_registry_hash"]
    assert doc["feature_names"] == expected["feature_names"]
    assert doc["n_vectors"] == expected["n_vectors"]
    assert set(doc["checkpoints"]) == set(expected["checkpoints"])
    for cp, exp in expected["checkpoints"].items():
        got = doc["checkpoints"][cp]
        assert got["timestamp"] == exp["timestamp"] and got["validity"] == exp["validity"]
        for a, b in zip(got["values"], exp["values"], strict=True):
            if b is None:
                assert a is None
            else:
                assert abs(a - b) <= TOL * max(1.0, abs(b))
    last = expected["checkpoints"][max(expected["checkpoints"], key=int)]
    assert all(last["validity"]), "every v2 feature is valid at the end of the fixture"


# ------------------------------------------------------------- sharenorm


def _trades(events):
    return [(e.exchange_ts, e.qty, e.side) for e in events if e.event_type == EventType.TRADE]


def _bruteforce_sharenorm(trades, depth, t, book_ok, busted=()):
    """The 14 sharenorm values at emission ``t`` from the trade list."""
    live = [(ts, q, s) for ts, q, s in trades if ts <= t and (ts, q, s) not in busted]
    sess = sum(q for _, q, _ in live)
    out = [sess / ADV]
    for w_ns in f2.WINDOWS_SHARENORM_NS.values():
        warm = t - T0 >= w_ns
        win = [(q, s) for ts, q, s in live if t - w_ns < ts <= t]
        vol = sum(q for q, _ in win)
        sgn = sum(q if s == 0 else -q for q, s in win)
        out += [vol / ADV if warm else None, sgn / ADV if warm else None]
        out.append(vol / sess if warm and sess > 0 else None)
    bq, aq, b10, a10 = depth
    for s_, d_ in ((bq + aq, bq - aq), (b10 + a10, b10 - a10)):
        out += [s_ / ADV if book_ok else None, d_ / ADV if book_ok else None]
    return out


def _check(vec, exp, off=N_LONGWIN):
    for k, e in enumerate(exp):
        v, ok = vec.values[off + k], vec.validity[off + k]
        if e is None:
            assert not ok and math.isnan(v), k
        else:
            assert ok and abs(v - e) <= TOL * max(1.0, abs(e)), (k, v, e)


def _bust_event(t, trade_id):
    return MarketEvent(0, EQ, 1, t, t, 0, int(EventType.TRADE_BUST), 0, 0, 0, 0, trade_id)


def test_sharenorm_matches_bruteforce(contexts):
    events = _events(minutes=70)
    depths: list = []
    _, ext, rec, _ = _run(contexts, events, depths=depths)
    trades = _trades(events)
    for idx in range(0, len(ext.vectors), 53):
        vec = ext.vectors[idx]
        _check(vec, _bruteforce_sharenorm(trades, depths[idx], vec.timestamp, rec[idx][2]))
    # without an ADV every *_adv_* feature is invalid, the session shares are not
    _, ext0, _, _ = _run(contexts, events, adv={})
    last = ext0.vectors[-1]
    names = ext0.feature_names
    for j in range(N_LONGWIN, len(names)):
        assert last.validity[j] == ("_session_" in names[j]), names[j]
    # a per-session table: the session's own value (the fixture day is 2026-08-24)
    table = SessionAdvTable({EQ: {"2026-08-24": ADV / 2}}, {EQ: ADV})
    _, ext2, _, _ = _run(contexts, events, adv=table)
    j = names.index("session_volume_adv_v2")
    assert ext2.vectors[-1].values[j] == pytest.approx(2 * ext.vectors[-1].values[j])


def test_family_selection(contexts):
    events = _events(minutes=40)
    _, both, _, _ = _run(contexts, events)
    _, lw, _, _ = _run(contexts, events, families=["longwin"])
    _, sn, _, _ = _run(contexts, events, families=["sharenorm"])
    assert lw.feature_names == both.feature_names[:N_LONGWIN]
    assert sn.feature_names == both.feature_names[N_LONGWIN:]
    assert lw.feature_version != both.feature_version != sn.feature_version
    for a, b, c in zip(both.vectors, lw.vectors, sn.vectors, strict=True):
        assert a.validity == b.validity + c.validity
        for x, y in zip(a.values, b.values + c.values, strict=True):
            assert x == y or (math.isnan(x) and math.isnan(y))


def _nan_equal(a, b):
    return all(x == y or (math.isnan(x) and math.isnan(y)) for x, y in zip(a, b, strict=True))


def test_bust_removes_the_print_from_the_bust_time_forward(contexts):
    """A TRADE_BUST at minute 40 naming the print of minute 38: every
    emission before the bust is unchanged, every one after it has 100
    fewer shares in the session total and in each window the print is
    still inside; longwin and the base vectors never change."""
    events = _events(minutes=70)
    trades = _trades(events)
    target = next(
        e for e in events if e.event_type == EventType.TRADE and e.trade_id == 38 * 12 + 1
    )
    t_bust = T0 + 40 * 60 * NS + 3 * NS
    stream = sorted(events + [_bust_event(t_bust, target.trade_id)], key=lambda e: e.exchange_ts)
    depths: list = []
    eng, ext, rec, base = _run(contexts, stream, depths=depths)
    _, plain_ext, _, plain_base = _run(contexts, events)
    assert ext.busts_applied == 1 and ext.busts_unmatched == 0 and eng.trade_busts_skipped == 1
    assert len(ext.vectors) == len(plain_ext.vectors)
    removed = {(target.exchange_ts, target.qty, target.side)}
    for idx, (vec, ref) in enumerate(zip(ext.vectors, plain_ext.vectors, strict=True)):
        assert _nan_equal(vec.values[:N_LONGWIN], ref.values[:N_LONGWIN])
        if idx % 7:
            continue
        t = vec.timestamp
        gone = removed if t >= t_bust else ()
        _check(vec, _bruteforce_sharenorm(trades, depths[idx], t, rec[idx][2], gone))
    for a, b in zip(base, plain_base, strict=True):
        assert a.timestamp == b.timestamp and a.validity == b.validity
        assert _nan_equal(a.values, b.values)
    # a bust naming an unknown print changes nothing and is counted
    other = sorted(events + [_bust_event(t_bust, 999_999)], key=lambda e: e.exchange_ts)
    _, ext3, _, _ = _run(contexts, other)
    assert ext3.busts_unmatched == 1 and ext3.busts_applied == 0
    for a, b in zip(ext3.vectors, plain_ext.vectors, strict=True):
        assert _nan_equal(a.values, b.values)


def test_cross_prints_count_in_volume_not_in_signed_volume(contexts):
    events = _events(minutes=20)
    eng, ext, _, _ = _run(contexts, events)
    names = ext.feature_names
    j_sess = names.index("session_volume_adv_v2")
    before = ext.vectors[-1].values[j_sess]
    t_last = events[-1].exchange_ts
    assert eng.on_cross_print(EQ, t_last + NS, 5_000)  # the hook sees the folded cross
    hb = MarketEvent(0, EQ, 1, t_last + 2 * NS, t_last + 2 * NS, 10**6, 9, 0, 0, 0, 0, 0)
    eng.apply(hb)
    after = ext.vectors[-1]
    assert after.values[j_sess] == pytest.approx(before + 5_000 / ADV)
    j_vol = names.index("traded_volume_adv_w1m_v2")
    j_sgn = names.index("signed_volume_adv_w1m_v2")
    assert after.values[j_vol] >= 5_000 / ADV and abs(after.values[j_sgn]) < 5_000 / ADV

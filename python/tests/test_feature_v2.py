"""Feature spec v2: 15 m / 30 m / 1 h windows as an engine extension (v1.17 C4).

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

NS = 1_000_000_000
T0 = 1_787_578_200 * NS  # 2026-08-24 09:30 New York
EQ = 1
GOLDEN = GOLDEN_DIR / "expected_features_v2.json"
TOL = 1e-9


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


def _run(contexts, events, keep=True):
    eng = FeatureEngine(contexts, cadence_ns=0)
    ext = f2.FeatureV2Extension(eng, keep=keep)
    rec: list[tuple[int, float, bool]] = []  # (t, logmid, book_ok) per emission
    base: list = []

    def cb(vec, v2):
        st = eng.states[vec.instrument_id]
        rec.append((vec.timestamp, float(st.logmid), bool(st.book_ok)))
        base.append(vec)

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
    assert len(names) == 12 and all(n.endswith("_v2") for n in names)
    assert names[:4] == ["ret_log_w15m_v2", "rvol_w15m_v2", "trend_score_w15m_v2", "range_w15m_v2"]
    assert f2.WINDOWS_V2_NS == {"15m": 900 * NS, "30m": 1800 * NS, "1h": 3600 * NS}
    h = f2.v2_registry_hash()
    assert len(h) == 64 and h != registry_hash()
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
            "FeatureV2Extension attached; per checkpoint (emission index) every v2 feature's "
            "value (null when invalid) and validity. Tolerance abs/rel 1e-9. Validated "
            "against an independent pandas-free brute-force recomputation in the same test."
        ),
        "x-version": 1,
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

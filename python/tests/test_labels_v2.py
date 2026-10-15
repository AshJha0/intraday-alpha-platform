"""Labels v2: 15 m / 30 m / 1 h horizons truncated at the session close (v1.17 C4)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest
from conftest import GOLDEN_DIR
from iap.core.rng import SplitMix64
from iap.labels.labels import HORIZONS_NS, LabelReason, MidSeries, compute_labels
from iap.labels.labels_v2 import (
    HORIZON_V2_ORDER,
    HORIZONS_V2_NS,
    LabelReasonV2,
    compute_labels_v2,
    label_columns_v2,
    session_close_ts,
)

NS = 1_000_000_000
T0 = 1_787_578_200 * NS  # 2026-08-24 09:30 New York
GOLDEN = GOLDEN_DIR / "expected_labels_v2.json"


def _series(n_minutes: int = 150, seed: int = 21) -> MidSeries:
    """One sample every 5 s for ``n_minutes``; a random walk mid, 1 ct
    half-spread, a 2-minute halt (non-tradable) at minute 70."""
    rng = SplitMix64(seed)
    s = MidSeries()
    mid = 100.0
    for i in range(n_minutes * 12):
        t = T0 + i * 5 * NS
        mid += 0.01 * (1 if rng.uniform() < 0.5 else -1)
        minute = i // 12
        tradable = not (70 <= minute < 72)
        s.append(t, mid if tradable else float("nan"), 0.01, tradable)
    return s


def test_pinned_horizons_and_columns():
    assert HORIZON_V2_ORDER == ("15m", "30m", "1h")
    assert HORIZONS_V2_NS["1h"] == 3600 * NS and HORIZONS_V2_NS["15m"] == HORIZONS_NS["15m"]
    cols = label_columns_v2("30m")
    assert cols["mid"] == "label_mid_30m_v2" and cols["truncated"] == "label_truncated_30m_v2"
    with pytest.raises(ValueError):
        label_columns_v2("5m")
    assert LabelReasonV2.describe(LabelReasonV2.AFTER_CLOSE | LabelReason.BLACKOUT) == [
        "blackout",
        "after_close",
    ]


def test_full_horizon_labels_equal_v1_at_15m():
    """Inside the session the 15 m v2 label IS the v1 15 m label."""
    s = _series()
    last = s.ts[-1]
    anchors = [T0 + k * 60 * NS for k in range(0, 120)]
    close = T0 + 10 * 3600 * NS  # far away: nothing truncates
    v1 = compute_labels(anchors, s, last, horizons=("15m",))["15m"]
    v2 = compute_labels_v2(anchors, s, last, close, horizons=("15m",))["15m"]
    assert v2.valid == v1.valid and v2.reason == v1.reason
    for a, b in zip(v1.mid, v2.mid, strict=True):
        assert (math.isnan(a) and math.isnan(b)) or a == b
    for a, b in zip(v1.cost, v2.cost, strict=True):
        assert (math.isnan(a) and math.isnan(b)) or a == b
    assert not any(v2.truncated)
    assert all(e == HORIZONS_V2_NS["15m"] for e in v2.effective_horizon_ns)


def test_truncation_at_the_session_close():
    s = _series()
    last = s.ts[-1]
    close = T0 + 120 * 60 * NS
    anchors = [close - 45 * 60 * NS, close - 20 * 60 * NS, close - 5 * NS, close, close + NS]
    out = compute_labels_v2(anchors, s, last, close)
    one_h = out["1h"]
    # 45 min before the close: the 1 h label is truncated to 45 min and valid
    assert one_h.truncated[0] and one_h.valid[0]
    assert one_h.effective_horizon_ns[0] == 45 * 60 * NS
    # it equals the return to the prevailing mid at the close
    k = max(i for i, t in enumerate(s.ts) if t <= close)
    b = max(i for i, t in enumerate(s.ts) if t <= anchors[0])
    assert one_h.mid[0] == s.mid[k] / s.mid[b] - 1.0
    # 20 min before: 15 m is a full label, 30 m and 1 h are truncated
    assert not out["15m"].truncated[1] and out["30m"].truncated[1] and one_h.truncated[1]
    assert out["15m"].effective_horizon_ns[1] == 15 * 60 * NS
    assert out["30m"].effective_horizon_ns[1] == 20 * 60 * NS
    # 5 s before: all three truncated to 5 s
    for h in HORIZON_V2_ORDER:
        assert out[h].truncated[2] and out[h].effective_horizon_ns[2] == 5 * NS
    # at or after the close: invalid, AFTER_CLOSE, not truncated, horizon 0
    for i in (3, 4):
        for h in HORIZON_V2_ORDER:
            assert not out[h].valid[i] and out[h].reason[i] == LabelReasonV2.AFTER_CLOSE
            assert not out[h].truncated[i] and out[h].effective_horizon_ns[i] == 0


def test_truncated_label_still_obeys_the_v1_rules():
    """A halt inside the truncated horizon still invalidates (BLACKOUT);
    a close past the end of the stream is NOT_OBSERVED."""
    s = _series()
    last = s.ts[-1]
    halt_start = T0 + 70 * 60 * NS
    close = halt_start + 60 * NS  # the close falls inside the halt
    out = compute_labels_v2([halt_start - 10 * 60 * NS], s, last, close, horizons=("1h",))["1h"]
    assert out.truncated[0] and not out.valid[0] and out.reason[0] & LabelReason.BLACKOUT
    far = compute_labels_v2([last - NS], s, last, last + 3600 * NS, horizons=("15m",))["15m"]
    assert far.reason[0] & LabelReason.NOT_OBSERVED and not far.truncated[0]


def test_per_anchor_closes_and_argument_checks():
    s = _series()
    last = s.ts[-1]
    c1 = T0 + 30 * 60 * NS
    anchors = [T0 + 20 * 60 * NS, T0 + 40 * 60 * NS]
    out = compute_labels_v2(anchors, s, last, [c1, c1 + 3600 * NS], horizons=("15m",))["15m"]
    assert out.truncated[0] and out.effective_horizon_ns[0] == 10 * 60 * NS
    assert not out.truncated[1]
    with pytest.raises(ValueError):
        compute_labels_v2(anchors, s, last, [c1], horizons=("15m",))
    with pytest.raises(ValueError):
        compute_labels_v2(anchors, s, last, [c1 + NS, c1], horizons=("15m",))
    with pytest.raises(ValueError):
        compute_labels_v2(anchors[::-1], s, last, c1)
    with pytest.raises(ValueError):
        compute_labels_v2(anchors, s, last, c1, horizons=("5m",))
    with pytest.raises(ValueError):
        compute_labels_v2(anchors, s, last, c1, max_age_ns=NS, freshness="trailing")
    trailing = compute_labels_v2(anchors, s, last, c1 + 10**13, freshness="trailing")
    assert set(trailing) == set(HORIZON_V2_ORDER)


def test_no_lookahead_truncation_invariance():
    """A label at anchor t depends only on samples at or before its forward instant."""
    s = _series()
    last = s.ts[-1]
    close = T0 + 100 * 60 * NS
    anchors = [T0 + k * 60 * NS for k in range(10, 95, 7)]
    full = compute_labels_v2(anchors, s, last, close, max_age_ns=10 * NS)
    for i, t in enumerate(anchors):
        for h in HORIZON_V2_ORDER:
            fwd = min(t + HORIZONS_V2_NS[h], close)
            keep = [k for k, ts in enumerate(s.ts) if ts <= fwd]
            trunc = MidSeries(
                [s.ts[k] for k in keep],
                [s.mid[k] for k in keep],
                [s.half_spread[k] for k in keep],
                [s.tradable[k] for k in keep],
            )
            got = compute_labels_v2([t], trunc, fwd, close, horizons=(h,), max_age_ns=10 * NS)[h]
            exp = full[h]
            assert got.valid[0] == exp.valid[i] and got.reason[0] == exp.reason[i]
            if exp.valid[i]:
                assert got.mid[0] == exp.mid[i] and got.cost[0] == exp.cost[i]


def test_session_close_ts():
    from iap.features.context import SessionClock

    ny = SessionClock("America/New_York")
    close = session_close_ts(T0, 16 * 3600, ny.offset_seconds(T0))
    assert close == T0 + int(6.5 * 3600) * NS  # 09:30 -> 16:00
    assert (
        session_close_ts(T0, 16 * 3600, 0) == (T0 // (86_400 * NS)) * 86_400 * NS + 16 * 3600 * NS
    )


# ---------------------------------------------------------------- golden


def _golden_doc() -> dict:
    s = _series()
    last = s.ts[-1]
    close = T0 + 120 * 60 * NS
    anchors = [T0 + k * 60 * NS for k in (5, 30, 55, 68, 69, 90, 105, 115, 119)] + [close + NS]
    out = compute_labels_v2(anchors, s, last, close, max_age_ns=10 * NS)
    return {
        "description": (
            "iap.labels.labels_v2 golden: one synthetic 150-minute mid series (SplitMix64 "
            "seed 21, 5 s samples, a 2-minute halt at minute 70), session close at +120 min, "
            "max_age 10 s; per horizon and anchor the mid / cost labels, validity, reason "
            "bits, truncation flag and effective horizon. Exact equality."
        ),
        "x-version": 1,
        "anchors": anchors,
        "session_close": close,
        "labels": {
            h: {
                "mid": [None if math.isnan(v) else v for v in r.mid],
                "cost": [None if math.isnan(v) else v for v in r.cost],
                "valid": r.valid,
                "reason": r.reason,
                "truncated": r.truncated,
                "effective_horizon_ns": r.effective_horizon_ns,
            }
            for h, r in out.items()
        },
    }


def test_golden_labels_v2():
    doc = _golden_doc()
    if os.environ.get("IAP_REGEN_V2_GOLDEN") == "1":
        Path(GOLDEN).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert doc == expected
    # the fixture exercises every outcome the rule defines
    flags = expected["labels"]["1h"]
    assert any(flags["truncated"]) and any(v for v in flags["valid"])
    assert any(r & LabelReasonV2.AFTER_CLOSE for r in flags["reason"])
    assert any(r & LabelReason.BLACKOUT for r in flags["reason"])

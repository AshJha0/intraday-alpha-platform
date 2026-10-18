"""v1.16 AI4 rebuild: real-data LOB adapter (per-session, mid-relative, masked)."""

from __future__ import annotations

import numpy as np
import pytest
from iap.core.events import EventType, MarketEvent, SessionStatus
from iap.models.lob_real import (
    WindowIndex,
    _refuse_holdout,
    fit_column_normalizer,
    session_snapshots,
)

T0 = 1_700_000_000_000_000_000


class Feed:
    def __init__(self) -> None:
        self.seq = 0
        self.events: list[MarketEvent] = []

    def push(self, et, side, px, qty, oid):
        self.seq += 1
        self.events.append(
            MarketEvent(
                self.seq,
                1,
                1,
                T0 + self.seq,
                T0 + self.seq,
                self.seq,
                int(et),
                side,
                px,
                qty,
                oid,
                0,
            )
        )

    def add(self, side, px, qty, oid):
        self.push(EventType.ADD, side, px, qty, oid)


def _book() -> Feed:
    f = Feed()
    f.add(0, 100, 300, 1)  # bid
    f.add(1, 102, 200, 2)  # ask, spread 2 ticks
    f.add(0, 99, 50, 3)
    return f


def test_layout_offsets_and_empty_levels():
    f = _book()
    ts, mid, x, empty = session_snapshots(f.events, 0.01, levels=3)
    # first row needs a two-sided book: rows from the 2nd event on
    assert len(ts) == 2 and np.all(np.diff(ts) > 0)
    assert mid[-1] == pytest.approx(1.01)
    r = x[-1]
    # ask: +1 tick (102 vs mid 101), size log1p(200); then empty levels +2, +3
    assert r[0] == 1.0 and r[1] == pytest.approx(np.log1p(200))
    assert (r[4], r[5], r[8], r[9]) == (2.0, 0.0, 3.0, 0.0)
    # bid: -1 (100), -2 (99, size 50), then empty -3
    assert (r[2], r[6], r[10]) == (-1.0, -2.0, -3.0)
    assert r[7] == pytest.approx(np.log1p(50)) and r[11] == 0.0
    assert empty[-1] == 3  # 2 empty ask levels + 1 empty bid level


def test_closed_or_one_sided_book_is_skipped():
    f = Feed()
    f.add(0, 100, 300, 1)  # one-sided: no row
    f.push(EventType.STATUS, 0, 0, int(SessionStatus.HALT), 0)
    f.add(1, 102, 200, 2)  # halted: no row
    ts, *_ = session_snapshots(f.events, 0.01, levels=2)
    assert len(ts) == 0


def test_holdout_sessions_refused():
    # v1.18 REL-02b: refused through research/holdout_registry.json (dates and labels)
    for name in ("r7_holdout", "r7_20251128", "ds_20260612", "holdout2026"):
        with pytest.raises(ValueError, match="holdout"):
            _refuse_holdout(name)
    _refuse_holdout("ds3_20190130")  # in-sample: fine


def test_windows_never_cross_units_and_labels_are_per_unit():
    rng = np.random.default_rng(0)
    units = []
    for k in range(2):
        x = rng.normal(size=(40, 4)).astype(np.float32) + 10 * k
        mid = 100 + np.cumsum(rng.normal(size=40)) * 0.01
        units.append((x, mid))
    norm = fit_column_normalizer([u[0] for u in units[:1]])
    wi = WindowIndex(units, window=5, horizon=3, threshold=1e-5, normalizer=norm)
    # per unit: ends 4 .. 36 (label needs 3 rows ahead) -> 33 windows each
    assert len(wi) == 66
    w, y = wi[0]
    assert w.shape == (5, 4) and y in (0, 1, 2)
    w_last, _ = wi[len(wi) - 1]
    np.testing.assert_allclose(w_last, norm.transform(units[1][0][32:37]))
    xb, yb = wi.batch([0, 33, 65])
    assert xb.shape == (3, 5, 4) and yb.shape == (3,)
    assert len(WindowIndex(units, window=5, horizon=3, threshold=1e-5, stride=4)) == 18


def test_normalizer_streams_like_numpy():
    a, b = np.arange(12.0).reshape(6, 2), np.arange(12.0, 20.0).reshape(4, 2)
    n = fit_column_normalizer([a, b])
    allx = np.vstack([a, b])
    np.testing.assert_allclose(n.mean, allx.mean(axis=0), rtol=1e-6)
    np.testing.assert_allclose(n.std, allx.std(axis=0), rtol=1e-6)

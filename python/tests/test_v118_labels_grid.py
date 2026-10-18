"""v1.18 REL-07: 15 m / 30 m / 1 h labels on the session grid (iap.labels.grid_v2)."""

from __future__ import annotations

import json

import numpy as np
import pyarrow.parquet as pq
import pytest
from iap.core.codec import write_jsonl
from iap.core.rng import SplitMix64
from iap.features.context import build_contexts
from iap.labels.grid_v2 import grid_labels_v2, parse_step, session_grid
from iap.labels.labels import MidSeries
from iap.labels.labels_v2 import HORIZON_V2_ORDER, HORIZONS_V2_NS, label_columns_v2
from rel07_fixture import NS, T0, tape, write_configs

CLOSE = T0 + 390 * 60 * NS  # 16:00 EST


@pytest.fixture()
def ctx(tmp_path):
    return build_contexts(write_configs(tmp_path, (1, 2, 3)))[1]


def _series(start=T0, minutes=390, seed=3, step_s=5) -> MidSeries:
    rng = SplitMix64(seed)
    s = MidSeries()
    mid = 100.0
    for i in range(minutes * 60 // step_s):
        mid += 0.01 * (1 if rng.uniform() < 0.5 else -1)
        s.append(start + i * step_s * NS, mid, 0.005, True)
    return s


def _table(series, ctx, last=CLOSE):
    """The stream is observed to the close (as a real session with its closing cross)."""
    return grid_labels_v2(1, series, last, ctx, 60 * NS)


def test_session_grid_is_session_local(ctx):
    anchors, close = session_grid(T0 + 3600 * NS, ctx, 60 * NS)
    assert anchors[0] == T0 and close == CLOSE and len(anchors) == 390
    assert parse_step("1m") == 60 * NS and parse_step("30s") == 30 * NS
    with pytest.raises(ValueError):
        parse_step("0m")


def test_truncation_probe(ctx):
    """Anchors whose forward instant passes 16:00 are pulled back to the close,
    flagged, and measure the return to the last sample at or before it."""
    s = _series()
    t = _table(s, ctx).to_pydict()
    c = label_columns_v2("30m")
    i_full = t["anchor_ts"].index(CLOSE - 31 * 60 * NS)
    i_trunc = t["anchor_ts"].index(CLOSE - 10 * 60 * NS)
    assert not t[c["truncated"]][i_full] and t[c["horizon_ns"]][i_full] == HORIZONS_V2_NS["30m"]
    assert t[c["truncated"]][i_trunc] and t[c["horizon_ns"]][i_trunc] == 10 * 60 * NS
    assert t[c["valid"]][i_trunc]
    ts = np.asarray(s.ts)
    a = int(np.searchsorted(ts, CLOSE - 10 * 60 * NS, "right")) - 1
    f = int(np.searchsorted(ts, CLOSE, "right")) - 1
    assert t[c["mid"]][i_trunc] == s.mid[f] / s.mid[a] - 1.0
    # every anchor in the last hour is truncated at 1h, none before it
    c1 = label_columns_v2("1h")
    for k, at in enumerate(t["anchor_ts"]):
        assert t[c1["truncated"]][k] == (at + HORIZONS_V2_NS["1h"] > CLOSE)


def test_no_lookahead_truncation_invariance(ctx):
    """A grid label depends only on samples at or before its forward instant."""
    s = _series()
    full = _table(s, ctx).to_pydict()
    for k in range(0, 390, 37):
        at = full["anchor_ts"][k]
        for h in HORIZON_V2_ORDER:
            fwd = min(at + HORIZONS_V2_NS[h], CLOSE)
            keep = [j for j, x in enumerate(s.ts) if x <= fwd]
            cut = MidSeries(
                [s.ts[j] for j in keep],
                [s.mid[j] for j in keep],
                [s.half_spread[j] for j in keep],
                [s.tradable[j] for j in keep],
            )
            # the stream is observed to the forward instant, not beyond
            got = grid_labels_v2(1, cut, fwd, ctx, 60 * NS, freshness="whole_day")
            ref = grid_labels_v2(1, s, CLOSE, ctx, 60 * NS, freshness="whole_day")
            c = label_columns_v2(h)
            g, r = got.to_pydict(), ref.to_pydict()
            assert g[c["valid"]][k] == r[c["valid"]][k]
            if r[c["valid"]][k]:
                assert g[c["mid"]][k] == r[c["mid"]][k] and g[c["cost"]][k] == r[c["cost"]][k]


def test_shift_by_one_canary(ctx):
    """Shifting the mids one sample forward (an off-by-one look-ahead) must
    change most labels: the alignment test above can see a leak."""
    s = _series()
    shifted = MidSeries(s.ts[:-1], s.mid[1:], s.half_spread[1:], s.tradable[1:])
    c = label_columns_v2("15m")
    good = _table(s, ctx).to_pydict()
    bad = _table(shifted, ctx).to_pydict()
    both = [k for k in range(390) if good[c["valid"]][k] and bad[c["valid"]][k]]
    diff = sum(1 for k in both if good[c["mid"]][k] != bad[c["mid"]][k])
    assert len(both) > 300 and diff / len(both) > 0.5


def test_store_cli_writes_cross_sectional_grid(tmp_path):
    """--labels-v2 1m: one file per instrument on the SAME anchors, a late
    tape (15:40-16:01) exercises truncation through the real store path."""
    from iap.features.__main__ import main

    data = tmp_path / "norm"
    data.mkdir()
    write_jsonl(
        data / "eq_20190130.normalized.jsonl", tape(seconds=1300, start=CLOSE - 20 * 60 * NS)
    )
    cfg = write_configs(tmp_path / "cfg", (1, 2, 3))
    out = tmp_path / "out"
    rc = main(
        ["--data-dir", str(data), "--configs", str(cfg), "--out-dir", str(out),
         "--registry-out", str(out / "r.json"), "--labels-v2", "1m"]
    )  # fmt: skip
    assert rc == 0
    summary = json.loads((out / "features_summary.json").read_text())
    assert summary["labels_v2"]["grid_step_ns"] == 60 * NS
    tables = {i: pq.read_table(out / f"labels_v2_{i}.parquet").to_pydict() for i in (1, 2, 3)}
    anchors = tables[1]["anchor_ts"]
    assert len(anchors) == 390 and all(t["anchor_ts"] == anchors for t in tables.values())
    t = tables[1]
    c = label_columns_v2("30m")
    k = anchors.index(CLOSE - 10 * 60 * NS)
    assert t[c["truncated"]][k] and t[c["horizon_ns"]][k] == 10 * 60 * NS and t[c["valid"]][k]
    assert all(t["session_close_ts"][j] == CLOSE for j in range(390))
    early = anchors.index(T0 + 60 * NS)  # before the tape starts: no anchor sample
    assert not t[c["valid"]][early]
    # the v1 store columns and the default summary keys are untouched
    v1 = pq.read_schema(out / "features_1.parquet").names
    assert not any(n.endswith("_v2") for n in v1)

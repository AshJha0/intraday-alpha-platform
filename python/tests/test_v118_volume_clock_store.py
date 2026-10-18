"""v1.18 REL-07: a store built on the volume clock carries causal timestamps.

``--sampling volume:N`` (v1.12 M6) emits a row when N more shares have traded.
A row's ``exchange_ts`` is the event that completed the N shares (the end of
its same-timestamp group), its features read only events at or before it,
and its labels are the same event-time forward returns as on the cadence
clock: the row clock chooses WHICH instants get a row, never what a label at
an instant is.
"""

from __future__ import annotations

import numpy as np
import pyarrow.parquet as pq
import pytest
from iap.core.codec import write_jsonl
from iap.core.events import EventType
from iap.features.__main__ import main
from rel07_fixture import tape, write_configs

N = 100  # shares; the fixture prints 40 per instrument per second


@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    root = tmp_path_factory.mktemp("vclock")
    events = tape(seconds=150)
    data = root / "norm"
    data.mkdir()
    write_jsonl(data / "eq_20190130.normalized.jsonl", events)
    cfg = write_configs(root / "cfg", (1, 2, 3))
    out = {}
    for name, extra in (
        ("clock", ["--cadence-ms", "0"]),
        ("volume", ["--sampling", f"volume:{N}"]),
    ):
        d = root / name
        args = ["--data-dir", str(data), "--configs", str(cfg), "--out-dir", str(d)]
        assert main([*args, "--registry-out", str(d / "r.json"), *extra]) == 0
        out[name] = {i: pq.read_table(d / f"features_{i}.parquet").to_pandas() for i in (1, 2, 3)}
    return events, out


def test_volume_rows_sit_on_event_times_and_advance_by_n_shares(stores):
    events, out = stores
    for iid, df in out["volume"].items():
        mine = [e for e in events if e.instrument_id == iid]
        ts = df["exchange_ts"].to_numpy()
        assert len(ts) > 10 and np.all(np.diff(ts) >= 0)
        assert set(ts) <= {e.exchange_ts for e in mine}
        trade_ts = np.array([e.exchange_ts for e in mine if e.event_type == EventType.TRADE])
        trade_qty = np.array([e.qty for e in mine if e.event_type == EventType.TRADE])
        for a, b in zip(ts[:-1], ts[1:], strict=True):
            traded = trade_qty[(trade_ts > a) & (trade_ts <= b)].sum()
            assert traded >= N  # never a row before N shares
            assert traded < N + 40  # ... and at the trade that completed them


def test_volume_rows_match_the_event_clock_at_the_same_instants(stores):
    """Features and labels of a volume row equal the every-event store's row
    at that instant: no information from after the row time enters either."""
    _, out = stores
    for iid in (1, 2, 3):
        vol = out["volume"][iid].set_index("exchange_ts")
        clk = out["clock"][iid].drop_duplicates("exchange_ts", keep="last").set_index("exchange_ts")
        common = vol.index.intersection(clk.index)
        assert len(common) == len(vol)
        cols = [c for c in vol.columns if c.startswith("label_") or c.endswith("_v1")]
        cols = [c for c in cols if not c.startswith(("norm_",))]
        a, b = vol.loc[common, cols], clk.loc[common, cols]
        assert a.equals(b) or np.allclose(
            a.to_numpy(float), b.to_numpy(float), rtol=0, atol=1e-12, equal_nan=True
        )

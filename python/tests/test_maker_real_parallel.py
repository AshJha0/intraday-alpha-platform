"""maker_real: the per-alpha process pool gives the serial path's numbers.

A two-session synthetic dataset (the golden MBO stream as both sessions,
random feature rows) runs once serially and once through the spawn pool;
the unit JSON must match exactly apart from wall-clock and memory fields.
Also: (unit, alpha) checkpoints are reused on resume.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from iap.alpha import configure_universe
from iap.research import maker_real as mr

ROOT = Path(__file__).resolve().parents[2]
SESSIONS = ["s0", "s1"]
VOLATILE = {"seconds", "peak_rss_gb", "timing_s", "workers"}


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("maker_real_ds")
    events = [
        json.loads(line)
        for line in (ROOT / "tests" / "golden" / "events_eq_mbo.jsonl").read_text().splitlines()
    ]
    ev = pd.DataFrame(events)
    for s in SESSIONS:
        d = root / s / "normalized"
        d.mkdir(parents=True)
        ev.to_parquet(d / "events.parquet", index=False)
    cfg = root / "multi7" / "configs"
    for sub in ("execution", "instruments", "venues"):
        shutil.copytree(ROOT / "configs" / sub, cfg / sub)
    fdir = root / "multi7" / "features"
    fdir.mkdir(parents=True)
    t0, t1 = int(ev["exchange_ts"].iloc[0]), int(ev["exchange_ts"].iloc[-1])
    ts = np.arange(t0 + 2 * mr.SEC, t1, 2 * mr.SEC, dtype=np.int64)
    rng = np.random.default_rng(7)
    cols = {
        "micro_mid_dev_bps_v1",
        "ofi_norm_l1_w1s_v1",
        *mr.FILTER_FEATURES,
    }
    writer = None
    for _ in SESSIONS:
        df = pd.DataFrame({"instrument_id": np.ones(len(ts), dtype=np.int64), "exchange_ts": ts})
        for c in sorted(cols):
            df[c] = rng.normal(size=len(ts))
        df["spread_ticks_v1"] = rng.integers(1, 4, size=len(ts)).astype(float)
        for h in ("1s", "5s"):
            df[f"label_mid_{h}"] = 1e-4 * df["micro_mid_dev_bps_v1"] + rng.normal(
                scale=1e-4, size=len(ts)
            )
            df[f"label_valid_{h}"] = True
        df["is_trading_v1"] = 1
        df["is_auction_v1"] = 0
        df["is_halt_v1"] = 0
        table = pa.Table.from_pandas(df, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(fdir / "features_1.parquet", table.schema)
        writer.write_table(table)
    writer.close()
    prereg = json.loads((ROOT / "research" / "maker_real" / "prereg.json").read_text())
    prereg["cells"] = [c for c in prereg["cells"] if c["alpha"] in ("EQ01", "EQ02")]
    prereg["latency_ASSUMED"]["venue_id"] = 1
    (root / "prereg.json").write_text(json.dumps(prereg), encoding="utf-8")
    yield root
    configure_universe(None)


def _args(root: Path, out: Path, workers: int) -> argparse.Namespace:
    return argparse.Namespace(
        data_root=str(root),
        out=str(out),
        prereg=str(root / "prereg.json"),
        sessions=None,
        symbols=None,
        slice=None,
        summary_only=False,
        workers=workers,
        worker_mem_gb=0.01,
        session_list=SESSIONS,
        symbol_map={"AAPL": 1},
    )


def test_parallel_unit_matches_serial_and_resumes(dataset, tmp_path):
    serial, parallel = tmp_path / "serial", tmp_path / "parallel"
    mr.Study(_args(dataset, serial, 1)).run()
    mr.Study(_args(dataset, parallel, 2)).run()
    a = json.loads((serial / "units" / "s1_AAPL.json").read_text())
    b = json.loads((parallel / "units" / "s1_AAPL.json").read_text())
    assert b["workers"] == 2 and a["workers"] == 1
    assert set(a["results"]) == {"EQ01", "EQ02"}
    assert a["results"]["EQ01"]["taker/ungated"]["posted"] > 0  # not a vacuous match
    assert _strip(a) == _strip(b)
    for aid in ("EQ01", "EQ02"):
        ca = json.loads((serial / "alpha_units" / f"s1_AAPL_{aid}.json").read_text())
        cb = json.loads((parallel / "alpha_units" / f"s1_AAPL_{aid}.json").read_text())
        assert _strip(ca) == _strip(cb)

    # resume: drop the unit file, keep one alpha checkpoint (poisoned so a
    # recompute would show), and the rerun reuses it verbatim
    (parallel / "units" / "s1_AAPL.json").unlink()
    ck = parallel / "alpha_units" / "s1_AAPL_EQ01.json"
    doc = json.loads(ck.read_text())
    doc["n_scores"] = -1
    ck.write_text(json.dumps(doc))
    (parallel / "alpha_units" / "s1_AAPL_EQ02.json").unlink()
    mr.Study(_args(dataset, parallel, 1)).run()
    c = json.loads((parallel / "units" / "s1_AAPL.json").read_text())
    assert c["results"]["EQ01"]["n_scores"] == -1
    assert _strip(c["results"]["EQ02"]) == _strip(a["results"]["EQ02"])


def test_worker_entry_point_is_importable_by_module_name():
    assert mr.alpha_worker.__module__ == "iap.research.maker_real"

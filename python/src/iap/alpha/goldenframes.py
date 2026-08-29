"""Golden-vector feature frames for alpha golden tests (conventions §5).

Replays a pinned golden event vector (tests/golden/events_*.jsonl) through
the reference FeatureEngine at cadence 0 (one emission per event) and
returns the same frame layout the feature pipeline writes to parquet:
``exchange_ts`` + one float64 column per registry feature (NaN = invalid) +
label columns.  Used by ``python/tools/make_golden_alpha.py`` (generation)
and ``python/tests/test_alpha_golden.py`` (verification) so both sides
construct byte-identical inputs.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from iap.core.codec import read_jsonl
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.registry import build_registry
from iap.labels.labels import HORIZON_ORDER, MidSeries, compute_labels


def build_golden_frame(events_path, configs_dir, instrument_id: int) -> pd.DataFrame:
    """Feature+label frame for one instrument from a golden event vector.

    Cadence 0: row k corresponds to the emission after 1-based event k of
    the instrument's stream.
    """
    events = read_jsonl(Path(events_path))
    contexts = build_contexts(configs_dir)
    engine = FeatureEngine(contexts, cadence_ns=0)
    names = [s.name for s in build_registry()]

    ts_list = []
    rows = []
    series = MidSeries()
    last_event_ts = 0
    for ev in events:
        vec = engine.apply(ev)
        if ev.instrument_id != instrument_id:
            continue
        last_event_ts = ev.exchange_ts
        st = engine.states[instrument_id]
        if st.book_ok:
            series.append(ev.exchange_ts, st.mid, st.spread_ticks * st.tick / 2.0)
        if vec is None:
            raise RuntimeError("cadence 0 must emit after every event")
        vals = np.asarray(vec.values, dtype=float).copy()
        vals[~np.asarray(vec.validity, dtype=bool)] = np.nan
        ts_list.append(vec.timestamp)
        rows.append(vals)

    frame = pd.DataFrame(np.vstack(rows), columns=names)
    frame.insert(0, "exchange_ts", np.asarray(ts_list, dtype=np.int64))
    frame.insert(0, "instrument_id", np.uint32(instrument_id))
    labels = compute_labels(ts_list, series, last_event_ts)
    for h in HORIZON_ORDER:
        lab = labels[h]
        frame[f"label_mid_{h}"] = np.asarray(lab.mid, dtype=float)
        frame[f"label_cost_{h}"] = np.asarray(lab.cost, dtype=float)
        frame[f"label_valid_{h}"] = np.asarray(lab.valid, dtype=bool)
    return frame

"""(Re)generate tests/golden/expected_features_snapshot_burst.json.

Checkpoints INSIDE and right after every SNAPSHOT recovery burst of the two
anomaly vectors (interior records have ``trade_id > 0``), from the Python
reference engine at cadence 0: the native-45 sub-vector. The anomaly golden
(expected_features_anomalies.json) never lands inside a burst, which hid a
Rust divergence until v1.11 (CHANGELOG v1.11.0, Fixed). Consumed by
rust/features/tests/golden_features.rs and the pyo3 parity tests.

Usage (from python/ with PYTHONPATH=src):  python tools/make_golden_snapshot_burst.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from iap.core.codec import read_jsonl
from iap.core.events import EventType
from iap.features.context import build_contexts
from iap.features.engine import FeatureEngine
from iap.features.native import NATIVE_NAMES
from iap.features.registry import feature_index

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden"
#: events checked after the final record of each burst
AFTER = 5


def _side(vector: str, instrument_id: int) -> dict:
    events = read_jsonl(GOLDEN / vector)
    want: set[int] = set()
    for i, ev in enumerate(events, start=1):
        if ev.event_type == EventType.SNAPSHOT:
            want.update(range(i, i + AFTER + 1))
    want = {i for i in want if i <= len(events)}
    idx = feature_index()
    eng = FeatureEngine(build_contexts(REPO / "configs"), cadence_ns=0)
    cps = {}
    for i, ev in enumerate(events, start=1):
        vec = eng.apply(ev)
        if i in want and vec is not None and ev.instrument_id == instrument_id:
            feats = {}
            for n in NATIVE_NAMES:
                v, ok = vec.values[idx[n]], vec.validity[idx[n]]
                feats[n] = {"valid": bool(ok), "value": v if ok and math.isfinite(v) else None}
            cps[str(i)] = {"timestamp": vec.timestamp, "features": feats}
    return {"vector": vector, "instrument_id": instrument_id, "checkpoints": cps}


def main() -> int:
    doc = {
        "description": "Native-45 features at every event inside and up to "
        f"{AFTER} events after each SNAPSHOT recovery burst of the anomaly "
        "vectors (Python reference, cadence 0). Keys are 1-based event "
        "indices. Tolerance: abs 1e-9 / rel 1e-9.",
        "tolerance": {"abs": 1e-9, "rel": 1e-9},
        "eq": _side("events_eq_anomalies.jsonl", 1),
        "fx": _side("events_fx_anomalies.jsonl", 101),
    }
    out = GOLDEN / "expected_features_snapshot_burst.json"
    out.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(out, {s: len(doc[s]["checkpoints"]) for s in ("eq", "fx")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

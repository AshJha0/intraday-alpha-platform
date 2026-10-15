"""F7 rule 6: B2 alpha urgency (``iap.execution.signal.ArraySignalSource`` /
``zscore``, ``iap.execution.urgency.alignment`` / ``regime_from_z`` /
``AlphaUrgencyPolicy`` and the replay's causal reading with latency and
staleness: ``ExecutionReplay._reading`` + the ``_signal_child`` regime)."""

from __future__ import annotations

from iap.execution.signal import ArraySignalSource, zscore
from iap.execution.urgency import AlphaUrgencyPolicy, alignment, regime_from_z

from . import SEC, SLICE_INSTRUMENT, SLICE_PROVENANCE, base_document
from ._slice import READINGS_RULE, readings, samples

NAME = "expected_f7_urgency.json"
T0 = 1_787_578_200_000_000_000

ZSCORE_CASES = [
    [1.0, 2.0, 3.0, 4.0],
    [0.5],
    [],
    [2.0, 2.0, 2.0],
    [-1e-5, 3e-5, 0.0, 7e-6, -2.2e-5, 1.1e-5],
    [1e150, -1e150, 0.0],
]

REGIME_CASES = [
    {"side": 0, "z": -1.2, "threshold_z": 1.0},
    {"side": 1, "z": -1.2, "threshold_z": 1.0},
    {"side": 0, "z": 1.0, "threshold_z": 1.0},
    {"side": 0, "z": -1.0, "threshold_z": 1.0},
    {"side": 1, "z": 0.999999, "threshold_z": 1.0},
    {"side": 0, "z": 0.0, "threshold_z": 0.0},
    {"side": 0, "z": -1e-300, "threshold_z": 0.0},
    {"side": 1, "z": -1e-300, "threshold_z": 0.0},
    {"side": 1, "z": 0.3, "threshold_z": 0.25},
]

SOURCES = {
    "11": [
        [T0 + k * SEC, z, z * 1e-4]
        for k, z in enumerate([0.0, 1.5, -2.0, 0.4, 2.5, -0.9, 1.0, -1.0])
    ],
    "12": [[T0 + 3 * SEC, -1.7, -3e-4], [T0 + 30 * SEC, 1.1, 2e-4]],
}
SOURCE_HORIZON_S = 1.0

POLICIES = [
    {"threshold_z": 1.0, "passive_urgency": 0.1, "max_age_ns": 10 * SEC, "latency_ns": 0},
    {
        "threshold_z": 1.0,
        "passive_urgency": 0.1,
        "max_age_ns": 10 * SEC,
        "latency_ns": 1_500_000_000,
    },
    {"threshold_z": 0.5, "passive_urgency": 0.2, "max_age_ns": 0, "latency_ns": 0},
    {"threshold_z": 2.0, "passive_urgency": 0.0, "max_age_ns": 2 * SEC, "latency_ns": 100_000_000},
]

QUERIES = [
    {"policy": pi, "instrument_id": iid, "side": side, "t": T0 + dt}
    for pi in range(len(POLICIES))
    for iid in (11, 12, 13)
    for side in (0, 1)
    for dt in (
        -SEC,
        0,
        SEC,
        2 * SEC + 1,
        4 * SEC,
        7 * SEC - 1,
        7 * SEC,
        12 * SEC,
        25 * SEC,
        40 * SEC,
    )
]


def reading_at(src, policy: dict, iid: int, side: int, t: int) -> dict:
    """``ExecutionReplay._reading`` + the ``_signal_child`` classification."""
    at = t - policy["latency_ns"]
    r = src.at(iid, at)
    if r is None:
        status = "no_signal"
    elif policy["max_age_ns"] > 0 and at - r.ts > policy["max_age_ns"]:
        r, status = None, "stale"
    else:
        status = "ok"
    z = 0.0 if r is None else r.z
    regime = status if r is None else regime_from_z(side, z, policy["threshold_z"])
    return {
        "status": status,
        "regime": regime,
        "z": z,
        "reading_ts": None if r is None else r.ts,
        "expected_return": None if r is None else r.expected_return,
        "alignment": alignment(side, z),
    }


def expected_fixed() -> dict:
    zs = []
    for vals in ZSCORE_CASES:
        z, mean, std = zscore(vals)
        zs.append({"z": z, "mean": mean, "std": std})
    regimes = [regime_from_z(c["side"], c["z"], c["threshold_z"]) for c in REGIME_CASES]
    src = ArraySignalSource(
        {int(k): [tuple(r) for r in v] for k, v in SOURCES.items()}, horizon_s=SOURCE_HORIZON_S
    )
    pols = [AlphaUrgencyPolicy(**p) for p in POLICIES]
    queries = []
    for q in QUERIES:
        p = POLICIES[q["policy"]]
        assert pols[q["policy"]].threshold_z == p["threshold_z"]
        queries.append(reading_at(src, p, q["instrument_id"], q["side"], q["t"]))
    return {"zscore": zs, "regimes": regimes, "queries": queries}


def slice_block() -> tuple[dict, list, list]:
    rd = readings()
    src = ArraySignalSource({SLICE_INSTRUMENT: rd})
    rows = []
    for s in samples():
        for pi, p in enumerate(POLICIES):
            for side in (0, 1):
                rows.append(
                    dict(
                        k=s["k"],
                        policy=pi,
                        side=side,
                        **reading_at(src, p, SLICE_INSTRUMENT, side, s["ts"]),
                    )
                )
    inputs = dict(
        SLICE_PROVENANCE,
        readings_rule=READINGS_RULE,
        policies=POLICIES,
        queries="every policy x side at every sample's exchange_ts",
    )
    return inputs, [[ts, z, er] for ts, z, er in rd], rows


def document() -> dict:
    doc = base_document(
        "F7 rule 6 golden: B2 alpha urgency (API_TRADING.md section 2.14): "
        "iap.execution.signal.zscore cases ([z], mean, std); "
        "iap.execution.urgency.regime_from_z cases; queries against "
        "ArraySignalSource(sources) under each AlphaUrgencyPolicy, classified as the replay "
        "does at a child decision (ExecutionReplay._reading: the last reading with "
        "ts <= t - latency_ns, stale when older than max_age_ns > 0; _signal_child: the "
        "regime is the status without a reading, else regime_from_z). The slice block "
        "derives readings from the book (readings_rule) and records their z (zscore on "
        "real data) and every query.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r06_urgency --force",
    )
    sl_inputs, sl_readings, sl_rows = slice_block()
    doc["inputs"] = {
        "zscore": ZSCORE_CASES,
        "regimes": REGIME_CASES,
        "sources": SOURCES,
        "source_horizon_s": SOURCE_HORIZON_S,
        "policies": POLICIES,
        "queries": QUERIES,
        "slice": sl_inputs,
    }
    doc["expected"] = dict(expected_fixed(), slice={"readings": sl_readings, "queries": sl_rows})
    return doc

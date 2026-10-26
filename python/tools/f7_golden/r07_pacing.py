"""F7 rule 7: B3 signal-aware pacing (``iap.execution.pacing``: ``pacing_scale``,
``apportion``, ``tilted_slice_qty``; the replay's POV pacing accumulation
``p_t = min(p * scale, max_participation)``, ``rate = p_t / (1 - p_t)`` under
``pov_exact``, ``target_f += rate * dV``, ``target = floor(target_f)``, and
the slice tilt over the remaining slices)."""

from __future__ import annotations

import math

from iap.execution import AlgoType, ParentOrder
from iap.execution.algos import slice_weights
from iap.execution.pacing import PacingParams, apportion, pacing_scale, tilted_slice_qty

from . import SEC, SLICE_INSTRUMENT, SLICE_PREFIX, SLICE_PROVENANCE, base_document
from ._slice import READINGS_RULE, readings, states

NAME = "expected_f7_pacing.json"

PARAMS = [
    {
        "gain": 0.5,
        "min_scale": 0.25,
        "max_scale": 3.0,
        "max_participation": 0.5,
        "max_age_ns": 10 * SEC,
        "latency_ns": 0,
    },
    {
        "gain": 1.0,
        "min_scale": 0.0,
        "max_scale": 2.0,
        "max_participation": 0.3,
        "max_age_ns": 10 * SEC,
        "latency_ns": 0,
    },
    {
        "gain": 0.0,
        "min_scale": 0.5,
        "max_scale": 1.5,
        "max_participation": 0.9,
        "max_age_ns": 0,
        "latency_ns": 0,
    },
    {
        "gain": 0.25,
        "min_scale": 0.1,
        "max_scale": 10.0,
        "max_participation": 0.5,
        "max_age_ns": 10 * SEC,
        "latency_ns": 0,
    },
]

SCALE_CASES = [
    {"side": side, "z": z, "params": pi}
    for pi in range(len(PARAMS))
    for side in (0, 1)
    for z in (None, 0.0, 0.7, -0.7, 1.5, -1.5, 4.0, -4.0, 100.0, -100.0, 1e-9)
]

APPORTION_CASES = [
    {"qty": 1000, "weights": [1.0, 1.0, 1.0, 1.0]},
    {"qty": 1002, "weights": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]},
    {"qty": 400, "weights": [2.0, 10.0 / 9.0, 10.0 / 9.0, 2.0]},
    {"qty": 600, "weights": [1.0, math.exp(-0.5), math.exp(-1.0)]},
    {"qty": 7, "weights": [0.0, 0.0, 0.0]},
    {"qty": 5, "weights": []},
    {"qty": 0, "weights": [1.0, 2.0]},
    {"qty": 999, "weights": [0.3, 0.3, 0.3, 0.1]},
    {"qty": 10, "weights": [1e-300, 1e-300]},
    {"qty": 123, "weights": [5.0, 0.0, 2.5, 2.5]},
]

TILT_CASES = [
    {"remaining_qty": 1000, "remaining_weights": [1.0, 1.0, 1.0, 1.0], "scale": 1.0},
    {"remaining_qty": 1000, "remaining_weights": [1.0, 1.0, 1.0, 1.0], "scale": 3.0},
    {"remaining_qty": 1000, "remaining_weights": [1.0, 1.0, 1.0, 1.0], "scale": 0.25},
    {"remaining_qty": 1000, "remaining_weights": [1.0, 1.0, 1.0, 1.0], "scale": 0.0},
    {"remaining_qty": 1000, "remaining_weights": [1.0, 1.0, 1.0, 1.0], "scale": -2.0},
    {
        "remaining_qty": 333,
        "remaining_weights": [2.0, 1.1111111111111112, 1.1111111111111112, 2.0],
        "scale": 1.7,
    },
    {"remaining_qty": 50, "remaining_weights": [0.5], "scale": 0.1},
    {"remaining_qty": 0, "remaining_weights": [1.0, 1.0], "scale": 2.0},
    {"remaining_qty": -5, "remaining_weights": [1.0, 1.0], "scale": 2.0},
    {"remaining_qty": 999, "remaining_weights": [0.0, 0.0, 0.0], "scale": 2.0},
]

POV_SEQUENCES = [
    {
        "params": 0,
        "participation": 0.1,
        "pov_exact": False,
        "side": 0,
        "steps": [[100, 0.5], [40, -2.0], [250, None], [1, 3.0], [999, -0.1], [40, 1.0]],
    },
    {
        "params": 0,
        "participation": 0.1,
        "pov_exact": True,
        "side": 1,
        "steps": [[100, 0.5], [40, -2.0], [250, None], [1, 3.0], [999, -0.1], [40, 1.0]],
    },
    {
        "params": 1,
        "participation": 0.25,
        "pov_exact": True,
        "side": 0,
        "steps": [[300, 2.0], [300, -2.0], [300, 0.0], [7, None]],
    },
    {
        "params": 3,
        "participation": 0.05,
        "pov_exact": False,
        "side": 1,
        "steps": [[1000, -30.0], [1000, 30.0], [1, 0.0]],
    },
]


def pov_sequence(seq: dict) -> list:
    pac = PacingParams(**PARAMS[seq["params"]])
    p = seq["participation"]
    target_f = 0.0
    out = []
    for dv, z in seq["steps"]:
        scale = pacing_scale(seq["side"], z, pac)
        p_eff = min(p * scale, pac.max_participation)
        rate = p_eff / (1.0 - p_eff) if seq["pov_exact"] else p_eff
        target_f += rate * float(dv)
        out.append(
            {
                "scale": scale,
                "p_eff": p_eff,
                "rate": rate,
                "target_f": target_f,
                "target": int(math.floor(target_f)),
            }
        )
    return out


def tilt_sequence(qty: int, weights: list[float], scales: list[float]) -> list:
    """The replay's slice tilt: at slice k the remaining qty is re-apportioned
    over the remaining weights with the current weight scaled."""
    remaining = qty
    out = []
    n = len(weights)
    for k in range(n):
        q = tilted_slice_qty(remaining, weights[k:], scales[k]) if remaining > 0 else 0
        out.append(q)
        remaining -= q
    return out


def expected_fixed() -> dict:
    scales = [
        pacing_scale(c["side"], c["z"], PacingParams(**PARAMS[c["params"]])) for c in SCALE_CASES
    ]
    app = [apportion(c["qty"], c["weights"]) for c in APPORTION_CASES]
    tilt = [
        tilted_slice_qty(c["remaining_qty"], c["remaining_weights"], c["scale"]) for c in TILT_CASES
    ]
    pov = [pov_sequence(s) for s in POV_SEQUENCES]
    return {"scale": scales, "apportion": app, "tilt": tilt, "pov": pov}


SLICE_T0 = 1_548_858_600_000_000_000
SLICE_SLICES = 8
SLICE_TILT_QTY = 10_000
POV_RECORD_EVERY = 20  #: every trade accumulates; every 20th is recorded


def slice_block() -> tuple[dict, dict]:
    """POV pacing over the slice's TRADEs after the prefix with the planted
    readings (latency 0), and the tilt over 8 VWAP / TWAP / IS slices decided
    at the samples."""
    from iap.execution.signal import ArraySignalSource

    src = ArraySignalSource({SLICE_INSTRUMENT: readings()})
    pac = PacingParams(**PARAMS[0])
    pov_rows = []
    runs = {}
    for seq in ((0.1, False, 0), (0.1, True, 0), (0.05, True, 1)):
        runs[seq] = 0.0
    n_trades = 0
    for s in states():
        if s["k"] <= SLICE_PREFIX or s["trade_qty"] <= 0:
            continue
        n_trades += 1
        r = src.at(SLICE_INSTRUMENT, s["ts"])
        z = None if r is None else r.z
        row = {"k": s["k"], "dv": s["trade_qty"], "z": z, "runs": []}
        for (p, exact, side), target_f in list(runs.items()):
            scale = pacing_scale(side, z, pac)
            p_eff = min(p * scale, pac.max_participation)
            rate = p_eff / (1.0 - p_eff) if exact else p_eff
            target_f += rate * float(s["trade_qty"])
            runs[(p, exact, side)] = target_f
            row["runs"].append(
                {
                    "scale": scale,
                    "p_eff": p_eff,
                    "rate": rate,
                    "target_f": target_f,
                    "target": int(math.floor(target_f)),
                }
            )
        if n_trades % POV_RECORD_EVERY == 0:
            row["n_trades"] = n_trades
            pov_rows.append(row)
    # tilt: weights of the pinned curves for 8 slices, scale from the reading at
    # the sample's ts for a BUY (params 0), one sample per slice (first 8 samples)
    from ._slice import samples

    sm = samples()[:SLICE_SLICES]
    scales = []
    for s in sm:
        r = src.at(SLICE_INSTRUMENT, s["ts"])
        scales.append(pacing_scale(0, None if r is None else r.z, pac))
    tilt = {}
    for algo in (AlgoType.TWAP, AlgoType.VWAP, AlgoType.IS):
        p = ParentOrder(
            qty=SLICE_TILT_QTY,
            algo=algo,
            slices=SLICE_SLICES,
            risk_aversion=1.0,
            start_ts=0,
            end_ts=1,
        )
        w = slice_weights(p)
        tilt[algo.name] = {"weights": w, "quantities": tilt_sequence(SLICE_TILT_QTY, w, scales)}
    inputs = dict(
        SLICE_PROVENANCE,
        readings_rule=READINGS_RULE,
        params=PARAMS[0],
        pov_runs=[
            {"participation": 0.1, "pov_exact": False, "side": 0},
            {"participation": 0.1, "pov_exact": True, "side": 0},
            {"participation": 0.05, "pov_exact": True, "side": 1},
        ],
        pov_rule=(
            "every TRADE event past the prefix accumulates, z = the reading at the event's "
            "exchange_ts (latency 0, no staleness), one accumulation per run; every 20th "
            "trade (n_trades) is recorded"
        ),
        tilt_rule=(
            "weights = slice_weights of an 8-slice TWAP / VWAP / IS (risk_aversion 1) parent; "
            "scale_k = pacing_scale(buy, z at the k-th sample's exchange_ts); the remaining "
            "qty of 10000 is re-apportioned at every slice with tilted_slice_qty"
        ),
    )
    return inputs, {"pov": pov_rows, "scales": scales, "tilt": tilt}


def document() -> dict:
    doc = base_document(
        "F7 rule 7 golden: B3 signal-aware pacing (iap.execution.pacing; docs/API_REFERENCE.md "
        "section 2.14): pacing_scale cases (z null = no reading), apportion cases, "
        "tilted_slice_qty cases, and the replay's POV pacing accumulation per step "
        "(_schedule_signal: scale, p_eff = min(p * scale, max_participation), rate = "
        "p_eff / (1 - p_eff) under pov_exact else p_eff, target_f += rate * dV, target = "
        "floor(target_f)). The slice block runs the accumulation over the real TRADEs with "
        "the planted readings and tilts the pinned 8-slice curves.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r07_pacing --force",
    )
    sl_inputs, sl = slice_block()
    doc["inputs"] = {
        "params": PARAMS,
        "scale": SCALE_CASES,
        "apportion": APPORTION_CASES,
        "tilt": TILT_CASES,
        "pov": POV_SEQUENCES,
        "slice": sl_inputs,
    }
    doc["expected"] = dict(expected_fixed(), slice=sl)
    return doc

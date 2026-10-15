"""F7 rule 9: B5 queue-aware cancels (``iap.execution.fading.queue_cancel_rule``),
the hazard probability leg (``FillHazardModel.features`` / ``bucket_hazards``
/ ``p_fill_within`` on the model's UTC clock) and the X-4 depletion split
read (``ExecCalibration.queue_exec_depletion_hazard`` and its pooled / cancel
siblings via ``queue_bucket``), plus ``hazard_fill_prob``."""

from __future__ import annotations

import sys
from pathlib import Path

from iap.execution.calibration import SCHEMA as CAL_SCHEMA
from iap.execution.calibration import VERSION as CAL_VERSION
from iap.execution.calibration import ExecCalibration
from iap.execution.fading import book_pressure, queue_cancel_rule
from iap.execution.fill_hazard import FillHazardModel, time_of_day
from iap.execution.sor_v2 import hazard_fill_prob
from iap.tca.markout import queue_bucket

from . import SEC, SLICE_PROVENANCE, base_document
from ._slice import samples
from ._sor import calibration_docs

NAME = "expected_f7_queue_cancel.json"

RULE_CASES = [
    {"ahead_qty": a, "max_ahead_qty": m, "p_fill": p, "min_p_fill": mp}
    for a in (0, 500, 5000, 5001)
    for m in (0, 5000)
    for p in (None, 0.0, 0.19, 0.2, 0.9)
    for mp in (0.0, 0.2)
]

BUCKET_CASES = [
    {"q": q, "edges": e}
    for e in ([0, 500, 2000], [0, 300, 1000, 5000], [0])
    for q in (0, 1, 300, 301, 500, 501, 1000, 1001, 2000, 2001, 5000, 5001, 10**9)
]

DEPLETION_CASES = [
    {"venue": v, "side": s, "q": q, "source": src}
    for v in ("1", "2", "3", "101", "9")
    for s in (0, 1)
    for q in (0, 250, 700, 3000, 9000)
    for src in ("exec", "pooled", "cancel")
]

HAZARD_PROB_CASES = [
    {"lam": lam, "horizon_s": h}
    for lam in (None, 0.0, -0.5, 0.3, 2.0, 50.0)
    for h in (0.5, 1.0, 5.0)
]

FEATURE_CASES = [
    {"queue_ahead": q, "spread_ticks": sp, "imbalance": imb, "depletion_per_s": dep, "ts_ns": ts}
    for q in (0, 150.0, 2000, 40000)
    for sp in (1, 3.0)
    for imb in (0.0, 0.5, 0.93)
    for dep in (None, 0.0, 0.7, 3.1)
    for ts in (0, 1_548_858_600_000_000_000, 1_548_858_666_123_456_789)
][::3]
P_FILL_T_NS = [100_000_000, 250_000_000, 999_999_999, SEC, 1_500_000_000, 5 * SEC, 7_250_000_000]


def fitted_model() -> FillHazardModel:
    """A model fitted on the planted survival samples of the X4 tests (seeded)."""
    tests = Path(__file__).resolve().parents[2] / "tests"
    sys.path.insert(0, str(tests))
    from test_fill_hazard_x4 import planted_samples

    return FillHazardModel().fit(planted_samples(n=6000, seed=11))


def _cal(doc: dict) -> ExecCalibration:
    return ExecCalibration.from_dict(dict(doc, schema=CAL_SCHEMA, version=CAL_VERSION))


def depletion(docs, venue: str, side: int, q: int, source: str):
    d = docs.get(venue)
    if d is None:
        return None
    cal = _cal(d)
    if source == "exec":
        return cal.queue_exec_depletion_hazard(side, q)
    if source == "cancel":
        return cal.queue_cancel_depletion_hazard(side, q)
    return cal.queue_depletion_hazard(side, q)


def expected_fixed(model_doc: dict, docs: dict) -> dict:
    m = FillHazardModel.from_dict(model_doc)
    rules = [
        queue_cancel_rule(c["ahead_qty"], c["max_ahead_qty"], c["p_fill"], c["min_p_fill"])
        for c in RULE_CASES
    ]
    buckets = [queue_bucket(c["q"], c["edges"]) for c in BUCKET_CASES]
    dep = [depletion(docs, c["venue"], c["side"], c["q"], c["source"]) for c in DEPLETION_CASES]
    hp = []
    for c in HAZARD_PROB_CASES:
        fp = hazard_fill_prob(lambda v, s, q, lam=c["lam"]: lam, c["horizon_s"])
        hp.append(fp(1, 0, 0))
    feats = []
    for c in FEATURE_CASES:
        x = m.features(**c)
        h = m.bucket_hazards(x)[0]
        feats.append(
            {
                "features": x,
                "bucket_hazards": [float(v) for v in h],
                "p_fill": [float(m.p_fill_within(x, t)) for t in P_FILL_T_NS],
            }
        )
    tod = [
        float(time_of_day(ts))
        for ts in (0, 1, 86_399_999_999_999, 86_400_000_000_000, 1_548_858_600_000_000_000, -1)
    ]
    return {
        "rules": rules,
        "buckets": buckets,
        "depletion": dep,
        "hazard_fill_prob": hp,
        "features": feats,
        "tod_utc": tod,
    }


SLICE_MAX_AHEAD = 5000
SLICE_MIN_P_FILL = 0.2
SLICE_HORIZON_NS = SEC


def slice_block(model_doc: dict, docs: dict) -> tuple[dict, list]:
    m = FillHazardModel.from_dict(model_doc)
    cal = _cal(docs["101"])
    rows = []
    for s in samples():
        for side in (0, 1):
            ahead = s["bid_sz"] if side == 0 else s["ask_sz"]
            own, opp = (s["bid_sz"], s["ask_sz"]) if side == 0 else (s["ask_sz"], s["bid_sz"])
            spread = float(s["ask"] - s["bid"])
            imb = 1.0 - book_pressure(own, opp)
            dep = cal.queue_exec_depletion_hazard(side, ahead)
            x = m.features(
                queue_ahead=ahead,
                spread_ticks=spread,
                imbalance=imb,
                depletion_per_s=dep,
                ts_ns=s["ts"],
            )
            p = float(m.p_fill_within(x, SLICE_HORIZON_NS))
            rows.append(
                {
                    "k": s["k"],
                    "side": side,
                    "ahead_qty": ahead,
                    "features": x,
                    "depletion_per_s": dep,
                    "p_fill": p,
                    "rule": queue_cancel_rule(ahead, SLICE_MAX_AHEAD, p, SLICE_MIN_P_FILL),
                    "rule_ahead_only": queue_cancel_rule(ahead, SLICE_MAX_AHEAD, None, 0.0),
                }
            )
    inputs = dict(
        SLICE_PROVENANCE,
        max_ahead_qty=SLICE_MAX_AHEAD,
        min_p_fill=SLICE_MIN_P_FILL,
        horizon_ns=SLICE_HORIZON_NS,
        rule=(
            "per sample and side, a child resting at the touch: ahead_qty = own L1 size; "
            "spread_ticks = ask - bid; imbalance = 1 - book_pressure(own, opp); "
            "depletion_per_s = queue_exec_depletion_hazard(side, ahead_qty) of "
            "calibration_docs['101'] (X-4); features = model.features(...); p_fill = "
            "p_fill_within(features, horizon_ns); rule = queue_cancel_rule(ahead_qty, "
            "max_ahead_qty, p_fill, min_p_fill)"
        ),
    )
    return inputs, rows


def document() -> dict:
    doc = base_document(
        "F7 rule 9 golden: B5 queue-aware cancels (iap.execution.fading.queue_cancel_rule; "
        "API_TRADING.md section 2.16) with the hazard probability leg "
        "(iap.execution.fill_hazard.FillHazardModel.features / bucket_hazards / "
        "p_fill_within on the UTC clock; `model` = the fitted document the port loads) "
        "and the X-4 depletion split read (iap.tca.markout.queue_bucket, "
        "ExecCalibration.queue_{exec_,cancel_,}depletion_hazard over `calibration_docs`, "
        "null = no document / no cell / no rate), plus iap.execution.sor_v2.hazard_fill_prob "
        "(lam null = None) and time_of_day on the UTC clock.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r09_queue_cancel --force",
    )
    model_doc = fitted_model().to_dict()
    docs = calibration_docs()
    sl_inputs, sl_rows = slice_block(model_doc, docs)
    doc["inputs"] = {
        "rules": RULE_CASES,
        "buckets": BUCKET_CASES,
        "depletion": DEPLETION_CASES,
        "calibration_docs": docs,
        "hazard_fill_prob": HAZARD_PROB_CASES,
        "model": model_doc,
        "features": FEATURE_CASES,
        "p_fill_t_ns": P_FILL_T_NS,
        "slice": sl_inputs,
    }
    doc["expected"] = dict(expected_fixed(model_doc, docs), slice=sl_rows)
    return doc

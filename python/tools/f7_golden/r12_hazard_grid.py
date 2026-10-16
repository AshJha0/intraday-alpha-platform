"""F7 part 2, rule 12: the hazard evaluation over MANY models (B5 probability
leg; ``FillHazardModel.bucket_hazards`` / ``p_fill_within`` after #200 made the
Python scalar and left-to-right, the C++ port's order).

Part 1 pinned one fitted model; its betas happened to agree between the two
sides while another fit diverged by one ulp (the numpy BLAS dot of the
time). This golden evaluates a grid of feature rows under several models:
the part-1 fixture, the fit that diverged, the integrated tree's fit, and
perturbed / seeded coefficient sets (the same SplitMix64 stream both sides
could rebuild, drawn here once and committed as inputs)."""

from __future__ import annotations

import json
from pathlib import Path

from iap.core.rng import SplitMix64
from iap.execution.fill_hazard import FillHazardModel

from . import SEC, base_document

NAME = "expected_f7_hazard_grid.json"
FIXTURE = Path(__file__).resolve().parent / "hazard_model_fixture.json"

#: the fit of the integrated tree at wave 5 (e206f4b) that diverged by 1 ulp
#: against the C++ of the time (the parent session's report)
DIVERGED_BETA = [
    -0.7060710703594265,
    0.0,
    0.0,
    0.00811358953150072,
    -1.5021671639127372e-17,
    -3.285990578220045e-18,
]


def base_model() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def models() -> dict[str, dict]:
    base = base_model()
    out: dict[str, dict] = {"fixture": base}
    d = dict(base)
    d["beta"] = list(DIVERGED_BETA)
    out["diverged_wave5"] = d
    # hand-shaped models: dense betas, both links, a wide alpha range
    dense = dict(base)
    dense["beta"] = [-0.7, 0.35, -0.12, 0.9, 0.05, -0.08]
    dense["mean"] = [5.1, 0.4, 1.7, 0.5, 0.0, 0.0]
    dense["std"] = [1.3, 0.9, 1.1, 0.29, 0.71, 0.7]
    out["dense_cloglog"] = dense
    logit = dict(dense)
    logit["link"] = "logit"
    logit["alpha"] = [-3.0, -2.5, -2.0, -1.0, 0.0, 1.5]
    out["dense_logit"] = logit
    extreme = dict(dense)
    extreme["alpha"] = [-40.0, -20.0, -5.0, 5.0, 20.0, 40.0]
    extreme["beta"] = [3.0, -3.0, 2.5, -2.5, 1.0, -1.0]
    out["extreme_eta_clip"] = extreme
    # seeded coefficient sets
    rng = SplitMix64(0xF7A2)
    for k in range(6):
        m = dict(base)
        m["alpha"] = [float(a) + rng.uniform() * 2.0 - 1.0 for a in base["alpha"]]
        m["beta"] = [rng.uniform() * 2.0 - 1.0 for _ in range(6)]
        m["mean"] = [float(x) + rng.uniform() - 0.5 for x in base["mean"]]
        m["std"] = [float(x) * (0.5 + rng.uniform()) for x in base["std"]]
        if k % 2 == 1:
            m["link"] = "logit"
        out[f"seeded_{k}"] = m
    return out


FEATURES = [
    {"queue_ahead": q, "spread_ticks": sp, "imbalance": imb, "depletion_per_s": dep, "ts_ns": ts}
    for q in (0, 37.0, 150, 2000, 40000)
    for sp in (1, 2.0, 3.5)
    for imb in (0.0, 0.31, 0.5, 0.93)
    for dep in (None, 0.0, 0.7, 3.1)
    for ts in (0, 1_548_858_600_000_000_000, 1_548_880_200_123_456_789)
][::6]
T_NS = [100_000_000, 400_000_000, SEC, 2_500_000_000, 5 * SEC, 9 * SEC]


def expected(docs: dict[str, dict]) -> dict:
    out = {}
    for name, doc in docs.items():
        m = FillHazardModel.from_dict(doc)
        rows = []
        for c in FEATURES:
            x = m.features(**c)
            rows.append(
                {
                    "tod": x["tod"],
                    "bucket_hazards": [float(v) for v in m.bucket_hazards(x)[0]],
                    "p_fill": [float(m.p_fill_within(x, t)) for t in T_NS],
                }
            )
        out[name] = rows
    return out


def document() -> dict:
    doc = base_document(
        "F7 part 2 golden, rule 12: FillHazardModel.features / bucket_hazards / p_fill_within "
        "over a grid of feature rows under many models (`models`: the part-1 fixture, the "
        "wave-5 fit that diverged by 1 ulp, dense cloglog / logit models, an eta-clipping "
        "model, six seeded coefficient sets). The Python reference evaluates scalar and "
        "left-to-right (fill_hazard._row_hazards, #200), the order of "
        "cpp/src/execution/fill_hazard.cpp; byte parity on every row.",
        "PYTHONPATH=src python tools/make_golden_f7.py --only r12_hazard_grid --force",
    )
    docs = models()
    doc["inputs"] = {"models": docs, "features": FEATURES, "t_ns": T_NS}
    doc["expected"] = expected(docs)
    return doc

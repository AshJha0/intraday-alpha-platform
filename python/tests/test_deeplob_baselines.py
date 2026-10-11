"""AI4 baseline harness + net-of-cost acceptance: synthetic data only."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from iap.models.deeplob import DOWN, FLAT, UP, NumpyLOBSoftmax, build_lob_dataset, synthetic_lob
from iap.models.deeplob_baselines import (
    BASELINES,
    accept,
    compare_models,
    directions,
    macro_f1,
    net_of_cost,
    spread,
)

REPO = Path(__file__).resolve().parents[2]


def _flat_book(mids: list[float], half: float = 0.005) -> np.ndarray:
    m = np.asarray(mids)
    return np.stack([m + half, np.ones_like(m), m - half, np.ones_like(m)], axis=1)


def test_net_of_cost_hand_computed():
    snaps = _flat_book([100.0, 100.0, 101.0, 101.0])
    res = net_of_cost(snaps, np.array([0]), np.array([1]), horizon=2, fee_bp=0.5)
    gross = 100.0  # 1% in bp
    cost = 0.5 * (0.01 / 100.0 + 0.01 / 101.0) * 1e4 + 0.5
    assert res.n_trades == 1
    assert res.gross_bp == pytest.approx(gross)
    assert res.cost_bp == pytest.approx(cost)
    assert res.mean_net_bp == pytest.approx(gross - cost)
    np.testing.assert_allclose(spread(snaps), 0.01)


def test_short_and_flat_and_overlap_thinning():
    snaps = _flat_book([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
    ends = np.arange(4)
    res = net_of_cost(snaps, ends, np.array([-1, -1, 0, -1]), horizon=2)
    # signals at 0,1,3; 1 overlaps 0's holding period -> trades at 0 and 3? 3 >= 0+2 yes
    assert res.n_signals == 3
    assert res.n_trades == 2
    assert res.gross_bp > 0


def test_net_of_cost_validation():
    snaps = _flat_book([100.0, 100.0])
    with pytest.raises(ValueError):
        net_of_cost(snaps, np.array([0]), np.array([1]), horizon=2)
    with pytest.raises(ValueError):
        net_of_cost(snaps, np.array([0]), np.array([1, 1]), horizon=1)
    empty = net_of_cost(snaps, np.array([0]), np.array([0]), horizon=1)
    assert empty.n_trades == 0 and empty.mean_net_bp == 0.0


def test_directions_confidence_gate():
    p = np.array([[0.1, 0.1, 0.8], [0.6, 0.3, 0.1], [0.2, 0.7, 0.1], [0.4, 0.3, 0.3]])
    assert directions(p).tolist() == [1, -1, 0, -1]
    assert directions(p, min_confidence=0.5).tolist() == [1, -1, 0, 0]


def test_macro_f1_perfect_and_zero():
    y = np.array([DOWN, FLAT, UP])
    assert macro_f1(y, y) == 1.0
    assert macro_f1(y, np.array([UP, DOWN, FLAT])) == 0.0


def _dataset(seed: int = 3):
    snaps = synthetic_lob(1500, levels=3, seed=seed)
    ds = build_lob_dataset(snaps, window=5, horizon=5, threshold=2e-5)
    return snaps, ds


def test_compare_models_same_split_and_deterministic():
    snaps, ds = _dataset()
    models = {name: f(0) for name, f in BASELINES.items()}
    models["softmax"] = NumpyLOBSoftmax(epochs=100, seed=0)
    r1 = compare_models(snaps, ds, models, horizon=5)
    r2 = compare_models(snaps, ds, {n: f(0) for n, f in BASELINES.items()}, horizon=5)
    assert set(r1) == {"linear", "gbm", "softmax"}
    for name in ("linear", "gbm"):
        assert r1[name] == r2[name]
    for r in r1.values():
        assert 0.0 <= r["accuracy"] <= 1.0
        assert r["net"]["n_trades"] <= r["net"]["n_signals"]
    # the synthetic imbalance leads the mid, so a baseline beats chance
    assert r1["linear"]["accuracy"] > 1.0 / 3.0


def test_costs_bite():
    snaps, ds = _dataset()
    models = {"linear": BASELINES["linear"](0)}
    free = compare_models(snaps, ds, models, horizon=5)["linear"]["net"]
    dear = compare_models(snaps, ds, {"linear": BASELINES["linear"](0)}, horizon=5, fee_bp=50.0)
    assert dear["linear"]["net"]["mean_net_bp"] == pytest.approx(free["mean_net_bp"] - 50.0)


def _res(mean: float, lower: float, n: int) -> dict:
    return {"net": {"mean_net_bp": mean, "lower95_bp": lower, "n_trades": n}}


def test_accept_rules():
    base = {"linear": _res(0.1, -1.0, 100), "gbm": _res(0.2, -1.0, 100)}
    ok = accept({**base, "deeplob": _res(0.5, 0.1, 100)}, "deeplob")
    assert ok["accepted"] and ok["best_baseline_mean_net_bp"] == 0.2
    assert not accept({**base, "deeplob": _res(0.5, -0.1, 100)}, "deeplob")["accepted"]
    assert not accept({**base, "deeplob": _res(0.5, 0.1, 10)}, "deeplob")["accepted"]
    assert not accept({**base, "deeplob": _res(0.15, 0.1, 100)}, "deeplob")["accepted"]
    neg = accept({**base, "deeplob": _res(-0.5, -1.0, 100)}, "deeplob")
    assert neg["checks"]["mean_net_positive"] is False


def test_prereg_is_draft_and_unsigned():
    p = REPO / "research" / "r7" / "ai4_deeplob_prereg_DRAFT.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["status"].startswith("DRAFT")
    assert d["signed"] is False and d["signature"] is None
    assert d["on_blackboard"] is False
    assert set(d["baselines"]) == set(BASELINES)
    cand = json.loads((REPO / "research" / "r7" / "r7_candidates.json").read_text(encoding="utf-8"))
    holdout = [x["date"] for x in cand["days"] if x["role"] == "holdout"]
    assert d["holdout_days"] == holdout
    # owner decisions 2026-10-11
    assert d["dataset"]["sampling_clock"].startswith("event")
    assert d["dataset"]["window"] == 100 and d["dataset"]["horizon_rows"] == 50
    assert d["acceptance"]["min_confidence"] == 0.6
    assert "fee_bp" not in d["acceptance"]
    cm = d["acceptance"]["cost_model"]
    assert cm["execution_config"] == "configs/prereg/v2.json" and cm["regulatory_fees"] is True
    assert (REPO / cm["execution_config"]).is_file()


def test_r7_confirm_prereg_is_draft_without_quoting():
    p = REPO / "research" / "r7" / "r7_confirm_prereg_DRAFT.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["status"].startswith("DRAFT")
    assert d["signed"] is False and d["signature"] is None and d["on_blackboard"] is False
    assert "H2" not in d["hypotheses"]
    assert set(d["hypotheses"]) == {"H4"} and "H1" in d["dropped"]
    assert (REPO / d["execution_config"]).is_file()

"""v1.17 review R-4: the ML looks reach the multiple-testing ledger.

* the model-zoo gate decision (``run_model_comparison(ledger=)``);
* the meta-label tau decision (``run_meta_labeling(ledger=)``);
* a shadow promotion (``promotion_decision(ledger=)``), judged with a paired
  day-block bootstrap of the candidate-minus-champion IC when timestamps
  are given to ``ShadowRunner.compare``.

Every one is opt-in (``ledger=None`` keeps the v1.16 behaviour) and each is
ONE look, de-duplicated by its identity like every other ledger entry.
"""

from __future__ import annotations

import numpy as np
import pytest
from iap.backtest.maker import MakerFilter
from iap.lifecycle.evidence import Evidence
from iap.mlops.shadow import ShadowRunner, promotion_decision
from iap.models.metalabel import run_meta_labeling
from iap.models.pipeline import run_model_comparison
from iap.validation.ledger import ExperimentLedger
from iap.validation.metrics import day_block_bootstrap_tstat, paired_day_block_bootstrap
from test_metalabel import _meta_dataset
from test_mlops import _data, _fit
from test_model_gate import _signal_dataset

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S


def test_paired_day_block_bootstrap_is_paired_and_seeded():
    rng = np.random.default_rng(7)
    n_days, per_day = 12, 400
    ts = np.concatenate(
        [d * NS_DAY + np.arange(per_day, dtype=np.int64) * NS_S for d in range(n_days)]
    )
    y = rng.standard_normal(ts.size)
    # a shared day effect: both models' signal strength moves together
    # day by day, which is what pairing is for
    day_gain = np.repeat(rng.uniform(0.0, 2.0, size=n_days), per_day)
    a = 0.10 * day_gain * y + rng.standard_normal(ts.size)
    b = 0.25 * day_gain * y + rng.standard_normal(ts.size)
    out = paired_day_block_bootstrap(ts, a, b, y, seed=1)
    assert out["n_days"] == n_days and out["low_power"] is False
    assert out["delta"] == pytest.approx(out["ic_b"] - out["ic_a"])
    assert out["ic_a"] == pytest.approx(day_block_bootstrap_tstat(ts, a, y)["ic"])
    assert out["p05"] < out["delta"] < out["p95"] and out["p05"] > 0 and out["tstat"] > 2
    assert out == paired_day_block_bootstrap(ts, a, b, y, seed=1)
    # the same model against itself: a delta of exactly 0 with no spread
    same = paired_day_block_bootstrap(ts, a, a, y, seed=1)
    assert same["delta"] == 0.0 and same["se"] == pytest.approx(0.0, abs=1e-12)
    # paired: the common day-to-day variation cancels, so the paired SE is
    # below the unpaired one (two independent bootstraps of each IC)
    sa = day_block_bootstrap_tstat(ts, a, y, seed=1)["se"]
    sb = day_block_bootstrap_tstat(ts, b, y, seed=2)["se"]
    assert out["se"] < np.hypot(sa, sb)
    few = paired_day_block_bootstrap(ts[:per_day], a[:per_day], b[:per_day], y[:per_day])
    assert few["n_days"] == 1 and np.isnan(few["se"]) and few["low_power"] is True


def test_model_zoo_gate_is_one_ledger_look(tmp_path):
    ledger = ExperimentLedger(tmp_path / "experiments.json", dataset_version="ds1")
    ds = _signal_dataset(flip_after_first_segment=False)
    res = run_model_comparison(ds, ledger=ledger)
    assert res["gate"]["ledger_n"] == 1 and ledger.total_experiments == 1
    (entry,) = ledger.entries
    assert entry["alpha_id"] == "ML_ZOO" and entry["kind"] == "model_zoo_gate"
    assert entry["dataset_version"] == "ds1"
    assert entry["config"]["features"] == list(ds.feature_names)
    assert entry["result"]["passed"] == res["gate"]["passed"]
    assert set(entry["result"]["models"]) == set(res["models"])
    # a rerun of the same identity is a rerun, not a new look
    run_model_comparison(ds, ledger=ledger)
    assert ledger.total_experiments == 1 and ledger.entries[0]["reruns"] == 1
    # without a ledger nothing is recorded and the result has no ledger field
    assert "ledger_n" not in run_model_comparison(ds)["gate"]


def test_metalabel_tau_decision_is_one_ledger_look(tmp_path):
    ledger = ExperimentLedger(tmp_path / "experiments.json", dataset_version="ds1")
    ds, pred = _meta_dataset()
    res = run_meta_labeling(ds, pred, ledger=ledger, primary_name="p")
    assert res["ledger_n"] == 1 and ledger.total_experiments == 1
    (entry,) = ledger.entries
    assert entry["alpha_id"] == "META_p" and entry["kind"] == "metalabel_tau"
    assert entry["config"]["tau"] == 0.5
    assert entry["result"]["best_tau_from_calibration"] == res["best_tau_from_calibration"]
    assert entry["result"]["economics_gate_tau"] == res["economics_gate_tau"]
    # another pinned tau is another look
    run_meta_labeling(ds, pred, ledger=ledger, primary_name="p", tau=0.6)
    assert ledger.total_experiments == 2


def _shadow(seed_c: int, seed_k: int, n_days: int = 8, per_day: int = 250):
    champ = _fit(seed=seed_c)
    cand = MakerFilter("meta_gbm", tau=0.3).fit(*_data(seed=seed_k))
    frames, ys = [], []
    for i in range(n_days):
        X, y = _data(seed=100 + i, n=per_day)
        frames.append(X)
        ys.append(y)
    sr = ShadowRunner(champ, cand, champion_id="c", candidate_id="k")
    sr.run(frames)
    y = np.concatenate(ys)
    ts = np.concatenate(
        [d * NS_DAY + np.arange(per_day, dtype=np.int64) * NS_S for d in range(n_days)]
    )
    return sr, y, ts


def test_shadow_compare_bootstrap_gates_candidate_better_and_promotion_is_a_look(tmp_path):
    sr, y, ts = _shadow(0, 5)
    plain = sr.compare(y)
    assert plain.ic_delta_bootstrap is None
    cmp = sr.compare(y, ts=ts, seed=3)
    b = cmp.ic_delta_bootstrap
    assert b is not None and b["n_days"] == 8
    assert b["delta"] == pytest.approx(b["ic_b"] - b["ic_a"])
    # the bootstrap reads the same quantity the Spearman IC reads
    assert b["ic_a"] == pytest.approx(cmp.champion["ic"], abs=1e-9)
    assert b["ic_b"] == pytest.approx(cmp.candidate["ic"], abs=1e-9)
    # candidate_better now also needs p05 > 0
    better_plain = plain.candidate_better()
    assert cmp.candidate_better() == (better_plain and b["p05"] > 0)
    with pytest.raises(ValueError, match="ts has"):
        sr.compare(y, ts=ts[:-1])
    # the same model as champion and candidate: delta 0, never "better"
    same = ShadowRunner(sr.champion, sr.champion, "c", "c")
    frames = [_data(seed=100 + i, n=250)[0] for i in range(8)]
    same.run(frames)
    assert same.compare(y, ts=ts).candidate_better() is False

    ledger = ExperimentLedger(tmp_path / "experiments.json", dataset_version="ds1")
    dec = promotion_decision(cmp, Evidence.empty(), alpha_id="EQ03", ledger=ledger)
    assert dec["promote"] is False and dec["ledger_n"] == 1
    (entry,) = ledger.entries
    assert entry["alpha_id"] == "EQ03" and entry["kind"] == "shadow_promotion"
    assert entry["config"]["champion_id"] == "c" and entry["config"]["candidate_id"] == "k"
    assert entry["result"]["ic_delta_bootstrap"] == b
    assert entry["result"]["promote"] is False
    # no ledger: no ledger field, same decision
    dec2 = promotion_decision(cmp, Evidence.empty(), alpha_id="EQ03")
    assert "ledger_n" not in dec2 and dec2["promote"] == dec["promote"]

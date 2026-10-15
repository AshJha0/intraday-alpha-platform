"""v1.17 WS-D: GBM as an AlphaModel (D1) and the two gate fixes (D4).

* D1 - ``iap.alpha.gbm.GbmAlpha`` (``ML01``, opt-in registry): fit / score /
  params / load_params round trip through a digest-checked sidecar, the
  hypothesis sign check, the label guard, and the whole chain:
  ``validate_alpha``, the research runner (prereg gate, exploratory
  eligibility, a scratch ledger) and the ``iap.research run`` CLI.
* D4 / R-6 - the model-zoo gate: ``gate="clustered"`` reads the fold-mean
  rank IC vs mid with the day-clustered t; both readings are recorded
  whichever rule decides, and the literal default is unchanged.
* D4 / R-9 - the CPCV path IC: ``cpcv_path_ic="demeaned"`` standardises
  every (split, group) before pooling; two fits of different scale show the
  pooled rule is biased and the demeaned one is not.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from conftest import CONFIGS_DIR, REPO_ROOT
from iap.alpha import (
    ALPHA_CLASSES,
    ALPHA_IDS,
    build,
    configure_universe,
    is_ml_alpha_id,
    register_ml_alphas,
    unregister_ml_alphas,
)
from iap.alpha.gbm import BACKENDS, GbmAlpha, ml_alpha_classes
from iap.backtest import BacktestConfig, Backtester
from iap.contracts.types import Verdict
from iap.contracts.versions import content_hash
from iap.core.rng import SplitMix64
from iap.features.registry import registry_hash
from iap.models.dataset import Dataset
from iap.models.pipeline import (
    DEFAULT_GATE_RULE,
    DEFAULT_GATE_T_THRESHOLD,
    GATE_RULES,
    run_model_comparison,
)
from iap.models.zoo import model_names
from iap.research import build_spec
from iap.research.__main__ import main as cli_main
from iap.research.runner import EXPLORATORY_REASON, ExperimentRunner
from iap.research.specs import model_definition_hash, pinned_horizon
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import METHODS, methods
from iap.validation.metrics import ic
from iap.validation.validate import (
    CPCV_PATH_IC_RULES,
    DEFAULT_CPCV_PATH_IC,
    _standardise_group,
    validate_alpha,
)
from test_research_validity_v19 import _cost_model, _multi_day_frames

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _SmallGbm(GbmAlpha):
    """Fixture: ML01 on the test frames' one feature, ten trees.

    Economic rationale: fixture only - exists inside the test suite.
    """

    horizon = "1s"
    horizons = ("1s",)
    features = ("sig_feature",)
    max_iter = 10

    def universe(self, ids):
        return sorted(ids)


@pytest.fixture
def ml_registry():
    """``ML01`` registered as the small fixture for the test, then removed."""
    unregister_ml_alphas()
    ALPHA_CLASSES["ML01"] = _SmallGbm
    try:
        yield
    finally:
        unregister_ml_alphas()


@pytest.fixture(scope="module")
def frames():
    return _multi_day_frames()


def _backtester(frames):
    meta = {
        i: {
            "tick_size": 0.01,
            "lot_size": 1,
            "adv": 1_000_000.0,
            "asset_class": "EQUITY",
            "ref_price": 25.0,
        }
        for i in frames
    }
    return Backtester(_cost_model(), meta, BacktestConfig(max_pos_qty=100, latency_rows=1))


def _validate(frames, factory=_SmallGbm, **kw):
    return validate_alpha(
        factory,
        frames,
        _backtester(frames),
        {i: {"adv": 1_000_000.0, "ref_price": 25.0} for i in frames},
        0.1,
        n_folds=4,
        embargo_ns=NS_S,
        ledger_t_threshold=3.5,
        ledger_looks=88,
        n_boot=50,
        **kw,
    )


def _split(frames, frac=0.6):
    ts = frames[1]["exchange_ts"].to_numpy()
    cut = int(ts[int(len(ts) * frac)])
    train = {i: df[df["exchange_ts"] < cut].reset_index(drop=True) for i, df in frames.items()}
    test = {i: df[df["exchange_ts"] >= cut].reset_index(drop=True) for i, df in frames.items()}
    return train, test


# ---------------------------------------------------------------------------
# D1 - the registry opt-in
# ---------------------------------------------------------------------------


def test_ml_alphas_are_opt_in_and_never_extend_alpha_ids():
    unregister_ml_alphas()
    assert "ML01" not in ALPHA_CLASSES and "ML01" not in ALPHA_IDS
    with pytest.raises(ValueError, match="register_ml_alphas"):
        build("ML01")
    assert is_ml_alpha_id("ML01") and not is_ml_alpha_id("EQ01")
    try:
        assert register_ml_alphas() == ["ML01"]
        assert register_ml_alphas() == ["ML01"]  # idempotent
        assert ALPHA_CLASSES["ML01"] is GbmAlpha and ml_alpha_classes() == {"ML01": GbmAlpha}
        assert "ML01" not in ALPHA_IDS  # run_all.py / fit_all never see it
        model = build("ML01")
        assert isinstance(model, GbmAlpha) and model.horizon == "5s"
        assert pinned_horizon("ML01") == "5s"
        assert model.fit_horizons[0] == "5s" and set(model.fit_horizons) == {"1s", "5s", "30s"}
        assert "Economic rationale:" in GbmAlpha.economic_rationale()
        # the structural identity the spec hashes exists for ML01 too
        assert len(model_definition_hash("ML01", "5s")) == 64
        assert model.backend == "hist_gbdt" and model.backend in BACKENDS
    finally:
        unregister_ml_alphas()
    assert "ML01" not in ALPHA_CLASSES


def test_gbm_feature_list_is_the_curated_ml_predictor_set():
    from iap.models.dataset import FEATURE_SET

    assert GbmAlpha.features == tuple(FEATURE_SET)
    p = GbmAlpha().params()
    assert p["model"] == "gbm_v1" and p["fitted"] is False and p["dead"] is True
    assert p["z_clip"] == 4.0 and p["conf_scale"] == 2.0 and p["beta"] == 0.0
    assert p["hyperparams"]["max_iter"] == 120  # the zoo's pinned count


# ---------------------------------------------------------------------------
# D1 - fit / score / params round trip
# ---------------------------------------------------------------------------


def test_gbm_fit_scores_sign_check_and_sidecar_round_trip(frames, tmp_path):
    train, test = _split(frames)
    model = _SmallGbm()
    model.fit(train)
    p = model.params()
    assert p["fitted"] and not p["dead"] and p["beta"] == 1.0
    assert p["n_train"] > 1000 and p["n_train_by_horizon"] == {"1s": p["n_train"]}
    # the sign check is an OOS IC on the held-out slice of the training fold
    assert p["sign_check"]["1s"] is not None and p["sign_check_frac"] == 0.2
    assert p["hypothesis_confirmed"] == (p["sign_check"]["1s"] > 0.0)
    assert p["model_sha256"] and p["sidecar"] is None
    scores = model.score(test)
    for iid, sc in scores.items():
        assert list(sc.columns) == ["exchange_ts", "expected_return", "confidence"]
        assert len(sc) == len(test[iid])
        assert np.all((sc["confidence"] >= 0) & (sc["confidence"] <= 1))
        # clipped at z_clip * sigma
        sigma = p["pred_sigma"]["1s"]
        assert np.all(np.abs(sc["expected_return"]) <= 4.0 * sigma + 1e-15)
    pooled = np.concatenate([scores[i]["expected_return"].to_numpy() for i in scores])
    labels = np.concatenate([test[i]["label_mid_1s"].to_numpy() for i in scores])
    assert ic(pooled, labels) > 0.05  # the fixture signal is learnable

    # frozen-params path: params + sidecar reproduce the scores exactly
    path = model.save_sidecar(tmp_path / "gbm")
    blob = json.loads(json.dumps(model.params()))
    assert blob["sidecar"] == str(path.resolve()) or blob["sidecar"].endswith(path.name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == blob["model_sha256"]
    again = _SmallGbm()
    again.load_params(blob)
    scores2 = again.score(test)
    for iid in scores:
        assert np.array_equal(scores[iid]["expected_return"], scores2[iid]["expected_return"])
        assert np.array_equal(scores[iid]["confidence"], scores2[iid]["confidence"])
    assert again.params()["sign_check"] == blob["sign_check"]

    # a tampered sidecar is refused before anything is unpickled
    path.write_bytes(pickle.dumps({"1s": object()}))
    with pytest.raises(ValueError, match="digest"):
        _SmallGbm().load_params(blob)
    bad = dict(blob, sidecar=None)
    with pytest.raises(ValueError, match="sidecar"):
        _SmallGbm().load_params(bad)
    with pytest.raises(ValueError, match="params for"):
        _SmallGbm().load_params(dict(blob, alpha_id="EQ01"))
    with pytest.raises(ValueError, match="z_clip"):
        _SmallGbm().load_params(dict(blob, z_clip=3.0))


def test_gbm_dead_fit_scores_zero_and_round_trips_without_a_sidecar(frames):
    train, test = _split(frames)
    tiny = {i: df.iloc[:10] for i, df in train.items()}
    model = _SmallGbm()
    model.fit(tiny)
    p = model.params()
    assert p["dead"] and p["beta"] == 0.0 and p["model_sha256"] is None
    assert not p["hypothesis_confirmed"]
    with pytest.raises(RuntimeError, match="dead"):
        model.save_sidecar()
    for sc in model.score(test).values():
        assert np.all(sc["expected_return"] == 0.0) and np.all(sc["confidence"] == 0.0)
    again = _SmallGbm()
    again.load_params(json.loads(json.dumps(p)))
    assert again.is_dead and again.is_fitted()


def test_gbm_score_reads_only_its_features(frames):
    """The label guard's invariant, directly: labels and other columns do not
    enter ``score`` (a leak here would be the catastrophic kind)."""
    train, test = _split(frames)
    model = _SmallGbm()
    model.fit(train)
    a = model.score(test)
    masked = {}
    for i, df in test.items():
        df = df.copy()
        for c in df.columns:
            if c not in ("exchange_ts", "sig_feature"):
                df[c] = np.nan
        masked[i] = df
    b = model.score(masked)
    for i in a:
        assert np.array_equal(a[i]["expected_return"], b[i]["expected_return"])
    with pytest.raises(ValueError, match="feature columns missing"):
        model.score({1: test[1].drop(columns=["sig_feature"])})


def test_gbm_rejects_unknown_backend_and_horizon():
    with pytest.raises(ValueError, match="unknown backend"):
        _SmallGbm().load_params({"alpha_id": "ML01", "model": "gbm_v1", "backend": "catboost"})

    class _BadHorizon(_SmallGbm):
        """Fixture.

        Economic rationale: fixture only - exists inside the test suite.
        """

        horizons = ("2s",)

    with pytest.raises(ValueError, match="unknown horizon"):
        _ = _BadHorizon().fit_horizons


# ---------------------------------------------------------------------------
# D1 - through validate_alpha, the runner and the CLI
# ---------------------------------------------------------------------------


def test_gbm_through_validate_alpha(frames):
    report = _validate(frames)
    assert report["alpha_id"] == "ML01" and report["horizon"] == "1s"
    assert report["leakage"]["passed"]
    assert report["gate_ic"] > 0.05 and report["gate_tstat"] > 3.0
    assert report["n_nondegenerate_folds"] == 4 and report["fold_sign_consistency"] == 1.0
    assert report["hypothesis_confirmed"] is True
    assert report["verdict"] in {v.value for v in Verdict}
    for row in report["folds"]:
        assert row["n_train"] > 0 and row["hypothesis_confirmed"] in (True, False)


def test_gbm_through_the_runner_prereg_gate_and_scratch_ledger(frames, tmp_path, ml_registry):
    """The runner's whole protocol on ML01 with a scratch ledger: the gate
    callable refuses first; without it the run is recorded as exploratory
    and debits its looks on the scratch ledger only."""
    spec = build_spec(
        "ML01",
        "1s",
        {},
        dataset_version=content_hash("wsd-test-dataset"),
        feature_version=content_hash("wsd-test-features"),
        frames=frames,
    )
    assert spec.alpha_id == "ML01" and spec.horizon == "1s"

    def refuse(s):
        raise RuntimeError(f"not pre-registered: {s.alpha_id}")

    gated = ExperimentRunner(
        None, tmp_path / "l.json", tmp_path / "e", CONFIGS_DIR, frames=frames, gate=refuse
    )
    with pytest.raises(RuntimeError, match="not pre-registered: ML01"):
        gated.run(spec)
    assert not (tmp_path / "l.json").exists()

    runner = ExperimentRunner(
        None,
        tmp_path / "l.json",
        tmp_path / "e",
        CONFIGS_DIR,
        frames=frames,
        dry_run=True,
        exploratory=True,
    )
    result = runner.run(spec)
    assert result.alpha_id == "ML01" and result.ic > 0.05
    assert EXPLORATORY_REASON in runner.last_eligibility.reasons
    assert not runner.last_eligibility.eligible
    ledger = ExperimentLedger(tmp_path / "l.json")
    assert ledger.total_experiments == methods(spec.configuration["methods"]).looks(4)
    assert not (tmp_path / "e").exists()  # dry run: no experiment directory


def test_gbm_through_the_research_cli_dry_run(frames, tmp_path, ml_registry, capsys):
    """``python -m iap.research run --alpha ML01 --horizon 1s --no-prereg
    --dry-run --ledger <scratch>`` on a scratch dataset directory: the ML
    opt-in, the prereg gate (skipped, stated), the spec, the verdict and the
    ledger note all print; the committed ledger is never touched."""
    ds = tmp_path / "dataset"
    (ds / "features").mkdir(parents=True)
    for iid, df in frames.items():
        df.assign(instrument_id=iid).to_parquet(ds / "features" / f"features_{iid}.parquet")
    (ds / "features" / "features_summary.json").write_text(
        json.dumps({"registry_hash": registry_hash()})
    )
    (ds / "dataset.json").write_text(
        json.dumps({"dataset_version": content_hash("wsd-cli-dataset")})
    )
    shutil.copytree(CONFIGS_DIR, ds / "configs")
    ledger_path = tmp_path / "scratch_ledger.json"
    committed = REPO_ROOT / "research" / "experiments.json"
    before = committed.read_bytes() if committed.exists() else None
    try:
        code = cli_main(
            [
                "run",
                "--alpha",
                "ML01",
                "--horizon",
                "1s",
                "--no-prereg",
                "--dry-run",
                "--dataset-dir",
                str(ds),
                "--ledger",
                str(ledger_path),
            ]
        )
    finally:
        configure_universe(None)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ML alpha ML01 registered" in out
    assert "NOT pre-registered (--no-prereg)" in out
    assert "ML01" in out and "VERDICT" in out and "dry run" in out
    assert ledger_path.exists() and ExperimentLedger(ledger_path).total_experiments > 0
    after = committed.read_bytes() if committed.exists() else None
    assert after == before


# ---------------------------------------------------------------------------
# D4 / R-6 - the model-zoo gate
# ---------------------------------------------------------------------------


def _zoo_dataset(n_days: int = 4, per_day: int = 400, rho: float = 0.6, seed: int = 11) -> Dataset:
    """A learnable linear relation over ``n_days`` UTC sessions."""
    rng = SplitMix64(seed)
    ts, xs, ys = [], [], []
    for d in range(n_days):
        base = d * NS_DAY + 14 * 3600 * NS_S
        for i in range(per_day):
            ts.append(base + i * 30 * NS_S)
            x0, x1, e = rng.normal(), rng.normal(), rng.normal()
            xs.append((x0, x1))
            ys.append((rho * x0 + np.sqrt(1 - rho * rho) * e) * 1e-3)
    ts = np.array(ts, dtype=np.int64)
    X = np.array(xs)
    y = np.array(ys)
    meta = np.zeros((len(y), 6))
    meta[:, 4] = 1.0
    return Dataset(
        X=X,
        y=y,
        y_mid=y.copy(),
        ts=ts,
        instrument_id=np.ones(len(y), dtype=np.int32),
        feature_names=["f0", "f1"],
        meta_context=meta,
    )


def test_zoo_gate_default_is_the_literal_rule_with_both_readings_recorded():
    ds = _zoo_dataset()
    res = run_model_comparison(ds, n_folds=3, embargo_ns=NS_S)
    gate = res["gate"]
    assert DEFAULT_GATE_RULE == "literal" and GATE_RULES == ("literal", "clustered")
    assert gate["gate_rule"] == "literal" and "> 0" in gate["rule"] and "MID-TO-MID" in gate["rule"]
    assert gate["passed"] is gate["literal"]["passed"] is True
    assert gate["best_linear_model"] == gate["literal"]["best_linear_model"]
    assert gate["best_linear_pooled_ic_vs_mid"] == gate["literal"]["pooled_ic_vs_mid"]
    clustered = gate["clustered"]
    assert clustered["t_threshold"] == DEFAULT_GATE_T_THRESHOLD == 2.0
    assert clustered["mean_rank_ic_vs_mid"] > 0.4
    assert clustered["n_days"] == 3 and clustered["tstat_day_cluster"] > 2.0  # OOS rows: days 2-4
    assert clustered["passed"] is True
    assert ">= 2" in clustered["rule"] and "rank IC" in clustered["rule"]
    for name in model_names(0):
        m = res["models"][name]
        assert np.isfinite(m["mean_rank_ic_vs_mid"]) and np.isfinite(m["tstat_day_cluster_vs_mid"])
        assert m["n_days"] == 3
        for f in m["per_fold"]:
            assert "rank_ic_vs_mid" in f


def test_zoo_gate_clustered_needs_sign_and_significance(tmp_path):
    ds = _zoo_dataset()
    res = run_model_comparison(ds, n_folds=3, embargo_ns=NS_S, gate="clustered")
    gate = res["gate"]
    assert gate["gate_rule"] == "clustered" and gate["rule"] == gate["clustered"]["rule"]
    assert gate["passed"] is True and gate["skipped_models"] == []
    assert gate["best_linear_model"] == gate["clustered"]["best_linear_model"]

    # a threshold above the observed t: the clustered gate closes, the
    # literal reading beside it still passes, and the skip is recorded
    t_obs = gate["clustered"]["tstat_day_cluster"]
    ledger = ExperimentLedger(tmp_path / "l.json", dataset_version=content_hash("zoo"))
    strict = run_model_comparison(
        ds,
        n_folds=3,
        embargo_ns=NS_S,
        gate="clustered",
        gate_t_threshold=t_obs + 1.0,
        ledger=ledger,
    )
    assert strict["gate"]["passed"] is False and strict["gate"]["literal"]["passed"] is True
    assert sorted(strict["gate"]["skipped_models"]) == sorted(model_names(1) + model_names(2))
    assert strict["gate"]["ledger_n"] == 1
    look = ledger.entries[0]
    assert look["kind"] == "model_zoo_gate"
    assert look["result"]["gate_rule"] == "clustered" and look["result"]["passed"] is False
    assert look["result"]["literal"]["passed"] is True
    assert look["result"]["clustered"]["t_threshold"] == pytest.approx(t_obs + 1.0)
    assert "mean_rank_ic_vs_mid" in look["result"]["models"]["ols"]

    # one session day: no clustered t exists, so the clustered gate fails
    # even though the IC is large - the literal rule would have passed
    one_day = _zoo_dataset(n_days=1, per_day=1200)
    single = run_model_comparison(one_day, n_folds=3, embargo_ns=NS_S, gate="clustered")
    assert single["gate"]["clustered"]["n_days"] == 1
    assert np.isnan(single["gate"]["clustered"]["tstat_day_cluster"])
    assert single["gate"]["passed"] is False and single["gate"]["literal"]["passed"] is True

    with pytest.raises(ValueError, match="unknown gate"):
        run_model_comparison(ds, n_folds=3, embargo_ns=NS_S, gate="soft")
    with pytest.raises(ValueError, match="gate_t_threshold"):
        run_model_comparison(ds, n_folds=3, embargo_ns=NS_S, gate_t_threshold=0.0)


# ---------------------------------------------------------------------------
# D4 / R-9 - the CPCV path IC
# ---------------------------------------------------------------------------


def _group(n, rho, scale, offset, seed):
    rng = np.random.default_rng(seed)
    s = rng.standard_normal(n)
    y = rho * s + np.sqrt(1 - rho * rho) * rng.standard_normal(n)
    return scale * s + offset, y


def test_cpcv_path_ic_pooled_is_biased_by_fit_scale_and_demeaned_is_not():
    """Two groups of one path from two fits: the same true IC (0.3) in each,
    one fit's signal ten times larger with an offset. The pooled rule reads
    the big fit; standardising per group reads the signal."""
    za, ya = _group(4000, 0.3, scale=10.0, offset=5.0, seed=1)
    zb, yb = _group(4000, 0.3, scale=1.0, offset=0.0, seed=2)
    truth = 0.5 * (ic(za, ya) + ic(zb, yb))
    pooled = ic(np.concatenate([za, zb]), np.concatenate([ya, yb]))
    sa = _standardise_group(za, ya)
    sb = _standardise_group(zb, yb)
    demeaned = ic(np.concatenate([sa[0], sb[0]]), np.concatenate([sa[1], sb[1]]))
    assert abs(pooled - truth) > 0.05, (pooled, truth)
    assert abs(demeaned - truth) < 0.01, (demeaned, truth)
    # per-group standardisation leaves each group's own IC untouched
    assert ic(*sa) == pytest.approx(ic(za, ya)) and ic(*sb) == pytest.approx(ic(zb, yb))
    # degenerate groups drop out instead of entering at scale 0
    z0, y0 = _standardise_group(np.ones(5), np.arange(5.0))
    assert np.all(np.isnan(z0)) and np.all(np.isnan(y0))
    z1, y1 = _standardise_group(np.array([1.0]), np.array([2.0]))
    assert np.all(np.isnan(z1))


class _ScaleByFit(GbmAlpha):
    """Fixture: a GBM whose score scale depends on the fit's row count, so
    every CPCV split predicts at its own scale.

    Economic rationale: fixture only - exists inside the test suite.
    """

    horizon = "1s"
    horizons = ("1s",)
    features = ("sig_feature",)
    max_iter = 10
    sign_check_frac = 0.0

    def universe(self, ids):
        return sorted(ids)

    def score(self, data):
        out = super().score(data)
        factor = 1.0 + (self.n_train % 7) * 50.0
        for sc in out.values():
            sc["expected_return"] = sc["expected_return"] * factor + factor * 1e-3
        return out


def test_cpcv_block_reports_both_path_rules_and_the_opt_in_names_the_headline(frames):
    assert CPCV_PATH_IC_RULES == ("pooled", "demeaned") and DEFAULT_CPCV_PATH_IC == "pooled"
    default = _validate(frames, factory=_ScaleByFit, cpcv=True)["cpcv"]
    assert default["path_ic_rule"] == "pooled"
    assert len(default["path_ics"]) == len(default["path_ics_demeaned"]) == default["n_paths"]
    assert default["path_ic_mean"] == pytest.approx(float(np.mean(default["path_ics"])))
    assert default["path_ic_mean_demeaned"] == pytest.approx(
        float(np.mean(default["path_ics_demeaned"]))
    )
    for key in ("std", "min", "max"):
        assert f"path_ic_{key}_demeaned" in default
    assert "path_frac_positive_demeaned" in default
    # the fits differ in scale, so the two rules disagree on this fixture
    assert default["path_ic_mean"] != pytest.approx(default["path_ic_mean_demeaned"], abs=1e-6)

    opted = _validate(frames, factory=_ScaleByFit, cpcv=True, cpcv_path_ic="demeaned")
    block = opted["cpcv"]
    assert block["path_ic_rule"] == "demeaned"
    assert block["path_ics"] == default["path_ics"]
    assert block["path_ics_demeaned"] == default["path_ics_demeaned"]
    assert block["path_ic_mean"] == pytest.approx(default["path_ic_mean_demeaned"])
    assert block["path_ic_min"] == pytest.approx(default["path_ic_min_demeaned"])
    assert opted["methods"]["cpcv_path_ic"] == "demeaned"
    assert "cpcv_path_ic" not in _validate(frames, cpcv=True)["methods"]
    # the per-group-standardised path IC is a scale-free reading of the
    # same evidence: positive, as the fixture signal is learnable
    assert block["path_ic_mean_demeaned"] > 0.05

    with pytest.raises(ValueError, match="unknown cpcv_path_ic"):
        _validate(frames, cpcv=True, cpcv_path_ic="scaled")


def test_cpcv_path_ic_bundle_field_is_opt_in():
    for name in METHODS:
        bundle = methods(name)
        assert bundle.cpcv_path_ic == "pooled"
        assert "cpcv_path_ic" not in bundle.validate_kwargs()
    flipped = replace(methods("v4"), cpcv_path_ic="demeaned")
    assert flipped.validate_kwargs()["cpcv_path_ic"] == "demeaned"
    assert flipped.validate_kwargs()["cpcv"] is True


# ---------------------------------------------------------------------------
# an ML01 fit on real-shaped frames keeps the committed artefacts untouched
# ---------------------------------------------------------------------------


def test_gbm_sidecar_default_directory_is_under_research_models(tmp_path, monkeypatch):
    from iap.alpha import gbm as gbm_mod

    assert gbm_mod.SIDECAR_DIR == Path("research") / "models" / "gbm"
    monkeypatch.setattr(gbm_mod, "_REPO", tmp_path)
    train, _ = _split(_multi_day_frames(per_day=300))
    model = _SmallGbm()
    model.fit(train)
    path = model.save_sidecar()
    assert path.parent == tmp_path / "research" / "models" / "gbm"
    assert model.params()["sidecar"] == f"research/models/gbm/{path.name}"

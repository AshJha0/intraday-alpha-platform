"""``iap.research`` — spec building, period derivation, the runner, the
ledger interaction, persistence and the registry.

Fast tests run on the golden equity frame (``iap.research.golden``:
2 000 rows, one instrument); the one bundled-data test reproduces the
flagship EQ03 promotion report through the runner and skips when
``data/features`` is absent.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
import pytest

from iap.alpha.equity import EQ03OfiMultiLevel
from iap.contracts import protocols
from iap.contracts.types import ExperimentSpec, Period, Verdict
from iap.contracts.validate import validate
from iap.contracts.versions import content_hash
from iap.research import (
    DEFAULT_CONFIGURATION,
    LEDGER_KIND,
    LOOKS_PER_EXPERIMENT,
    ExperimentRegistry,
    ExperimentRunner,
    ResearchError,
    build_result,
    build_spec,
    derive_periods,
    experiment_id_of,
    normalise_configuration,
    verify_experiment_id,
)
from iap.research.__main__ import main as cli_main
from iap.research.golden import GOLDEN_INSTRUMENT, golden_frames, golden_spec
from iap.research.runner import document_drift, render_document
from iap.validation.ledger import ExperimentLedger
from iap.validation.metrics import HORIZONS_NS
from iap.validation.splits import Fold

from conftest import CONFIGS_DIR, GOLDEN_DIR, REPO_ROOT

TOL = 1e-9
FEATURES_DIR = REPO_ROOT / "data" / "features"
REPORTS_DIR = REPO_ROOT / "research" / "alpha_reports"
DATA_VERSION = content_hash("test-dataset")
FEATURE_VERSION = content_hash("test-features")
NS_DAY = 86_400_000_000_000
NS_S = 1_000_000_000


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frames() -> Dict[int, pd.DataFrame]:
    return golden_frames(GOLDEN_DIR, CONFIGS_DIR)


@pytest.fixture(scope="module")
def spec(frames) -> ExperimentSpec:
    return golden_spec(GOLDEN_DIR, frames)


def _periods(frames, horizon: str, test_row: int = 1200):
    ts = frames[GOLDEN_INSTRUMENT]["exchange_ts"].to_numpy()
    test_start = int(ts[test_row])
    purge = test_start - HORIZONS_NS[horizon] - DEFAULT_CONFIGURATION["embargo_ns"]
    return dict(
        train_period=Period(start_ts=int(ts[0]), end_ts=purge),
        validation_period=Period(start_ts=purge, end_ts=test_start),
        test_period=Period(start_ts=test_start, end_ts=int(ts[-1]) + 1),
    )


def _spec(frames, horizon: str = "5s", configuration=None, seed: int = 1,
          alpha_id: str = "EQ03", **overrides) -> ExperimentSpec:
    kwargs = dict(dataset_version=DATA_VERSION, feature_version=FEATURE_VERSION,
                  seed=seed, **_periods(frames, horizon))
    kwargs.update(overrides)
    return build_spec(alpha_id, horizon, configuration, **kwargs)


def _runner(frames, tmp_path: Path, **kwargs) -> ExperimentRunner:
    return ExperimentRunner(None, tmp_path / "experiments.json", tmp_path / "experiments",
                            CONFIGS_DIR, frames=frames, **kwargs)


def _two_day_frames(rows_per_day: int = 200, step_ns: int = 30 * NS_S,
                    day0: int = 20_000) -> Dict[int, pd.DataFrame]:
    """Timestamp-only frames covering two UTC sessions (derivation input)."""
    out: Dict[int, pd.DataFrame] = {}
    for iid, offset in ((1, 0), (2, 7 * NS_S)):
        ts = []
        for day in (day0, day0 + 1):
            start = day * NS_DAY + 13 * 3600 * NS_S + offset
            ts.extend(start + k * step_ns for k in range(rows_per_day))
        out[iid] = pd.DataFrame({"exchange_ts": np.asarray(ts, dtype=np.int64)})
    return out


# ---------------------------------------------------------------------------
# specs
# ---------------------------------------------------------------------------


def test_spec_is_deterministic_and_pinned_id(frames):
    a = _spec(frames)
    b = _spec(frames)
    assert a == b and a.experiment_id == b.experiment_id
    body = a.to_dict()
    del body["experiment_id"]
    assert a.experiment_id == content_hash(body)[:16] == experiment_id_of(body)
    verify_experiment_id(a)


@pytest.mark.parametrize("change", [
    dict(configuration={"n_folds": 3}),
    dict(configuration={"embargo_ns": 0}),
    dict(configuration={"cost_multiplier": 2.0}),
    dict(configuration={"flatten_at_session_end": False}),
    dict(horizon="1s"),
    dict(seed=2),
    dict(alpha_id="EQ01"),
    dict(dataset_version=content_hash("other-dataset")),
    dict(feature_version=content_hash("other-features")),
])
def test_any_change_gives_a_different_experiment_id(frames, change):
    base = _spec(frames)
    assert _spec(frames, **change).experiment_id != base.experiment_id


def test_equivalent_configurations_hash_identically(frames):
    """1 and 1.0 are the same cost multiplier; an empty configuration is
    the pinned default protocol."""
    assert (_spec(frames, configuration={"cost_multiplier": 1}).experiment_id
            == _spec(frames, configuration={"cost_multiplier": 1.0}).experiment_id
            == _spec(frames, configuration=None).experiment_id
            == _spec(frames, configuration=dict(DEFAULT_CONFIGURATION)).experiment_id)
    assert normalise_configuration({}) == DEFAULT_CONFIGURATION
    assert list(normalise_configuration({"n_folds": 4})) == list(DEFAULT_CONFIGURATION)


@pytest.mark.parametrize("bad", [
    {"purge": True},                    # unknown knob would change the id for nothing
    {"n_folds": 0},
    {"n_folds": True},                  # a boolean is not a fold count
    {"n_folds": 2.5},
    {"embargo_ns": -1},
    {"cost_multiplier": 0.0},
    {"cost_multiplier": float("nan")},
    {"max_decision_age_ns": 0},
    {"flatten_at_session_end": 1},
])
def test_configuration_is_strictly_validated(bad):
    with pytest.raises(ResearchError):
        normalise_configuration(bad)


def test_spec_defaults_to_the_alpha_pinned_horizon_and_model_hash(frames):
    s = build_spec(
        "EQ03", None, {}, dataset_version=DATA_VERSION, feature_version=FEATURE_VERSION,
        seed=1, **_periods(frames, "5s"))
    assert s.horizon == "5s"
    assert s.model_version is not None and len(s.model_version) == 64
    assert s.model_version != build_spec(
        "EQ03", "1s", {}, dataset_version=DATA_VERSION, feature_version=FEATURE_VERSION,
        seed=1, **_periods(frames, "1s")).model_version


@pytest.mark.parametrize("kwargs, message", [
    (dict(alpha_id="ZZ99"), "unknown alpha_id"),
    (dict(dataset_version="no-normalized-data"), "dataset_version"),
    (dict(feature_version="abc"), "feature_version"),
])
def test_spec_rejects_bad_identity(frames, kwargs, message):
    with pytest.raises(ResearchError, match=message):
        _spec(frames, **kwargs)


def test_spec_rejects_an_unknown_horizon(frames):
    with pytest.raises(ResearchError, match="unknown horizon"):
        build_spec("EQ03", "2s", {}, dataset_version=DATA_VERSION,
                   feature_version=FEATURE_VERSION, seed=1, **_periods(frames, "5s"))


def test_periods_must_come_together_or_be_derivable(frames):
    with pytest.raises(ResearchError, match="together"):
        build_spec("EQ03", "5s", {}, dataset_version=DATA_VERSION,
                   feature_version=FEATURE_VERSION, seed=1,
                   train_period=Period(0, 1))
    with pytest.raises(ResearchError, match="no frames"):
        build_spec("EQ03", "5s", {}, dataset_version=DATA_VERSION,
                   feature_version=FEATURE_VERSION, seed=1)


# ---------------------------------------------------------------------------
# period derivation
# ---------------------------------------------------------------------------


def test_derive_periods_rule():
    f = _two_day_frames()
    train, validation, test = derive_periods(f, "5s", 60 * NS_S)
    ts1 = f[1]["exchange_ts"].to_numpy()
    ts2 = f[2]["exchange_ts"].to_numpy()
    t_first = int(min(ts1[0], ts2[0]))
    t_last = int(max(ts1[-1], ts2[-1]))
    day2 = ts1 // NS_DAY == 20_001
    test_start = int(min(ts1[day2][0], ts2[ts2 // NS_DAY == 20_001][0]))
    assert test == Period(test_start, t_last + 1)
    assert validation == Period(test_start - 5 * NS_S - 60 * NS_S, test_start)
    assert train == Period(t_first, validation.start_ts)
    # ordered, non-overlapping, half-open: every row lands in exactly one
    assert train.end_ts == validation.start_ts and validation.end_ts == test.start_ts
    for ts in (ts1, ts2):
        in_train = (ts >= train.start_ts) & (ts < train.end_ts)
        in_val = (ts >= validation.start_ts) & (ts < validation.end_ts)
        in_test = (ts >= test.start_ts) & (ts < test.end_ts)
        assert np.all(in_train.astype(int) + in_val + in_test == 1)
        # the last session is the holdout, everything earlier trains
        assert np.array_equal(in_test, ts // NS_DAY == 20_001)
    # the derived set is accepted by the contract as-is
    s = build_spec("EQ03", "5s", {}, dataset_version=DATA_VERSION,
                   feature_version=FEATURE_VERSION, seed=1, frames=f)
    assert (s.train_period, s.validation_period, s.test_period) == (train, validation, test)


def test_derive_periods_validation_is_the_purge_and_embargo_tail():
    """On contiguous data the validation period holds exactly the rows the
    pinned splitter refuses to train on."""
    f = {1: pd.DataFrame({"exchange_ts": np.arange(
        20_000 * NS_DAY + 86_000 * NS_S, 20_000 * NS_DAY + 87_000 * NS_S, NS_S,
        dtype=np.int64)})}
    train, validation, test = derive_periods(f, "10s", 30 * NS_S)
    ts = f[1]["exchange_ts"].to_numpy()
    fold = Fold(0, test.start_ts, test.start_ts, test.end_ts)
    trainable = fold.train_mask(ts, HORIZONS_NS["10s"], 30 * NS_S)
    in_val = (ts >= validation.start_ts) & (ts < validation.end_ts)
    in_train = (ts >= train.start_ts) & (ts < train.end_ts)
    assert np.array_equal(trainable, in_train)
    assert in_val.sum() == 40 and not np.any(trainable & in_val)


def test_derive_periods_needs_two_sessions_and_trainable_rows(frames):
    with pytest.raises(ResearchError, match=">= 2 sessions"):
        derive_periods(frames, "5s", 60 * NS_S)  # the golden vector is one session
    tiny = _two_day_frames(rows_per_day=3, step_ns=NS_S)
    with pytest.raises(ResearchError, match="nothing is left to train"):
        derive_periods(tiny, "15m", 3600 * NS_S * 24)
    with pytest.raises(ResearchError, match="empty"):
        derive_periods({}, "5s", 0)


def test_non_overlapping_periods_are_enforced(frames):
    good = _periods(frames, "5s")
    bad = dict(good, validation_period=Period(good["validation_period"].start_ts - 1,
                                              good["validation_period"].end_ts))
    with pytest.raises(ResearchError, match="overlap"):
        _spec(frames, **bad)
    reversed_ = dict(good, train_period=good["test_period"], test_period=good["train_period"])
    with pytest.raises(ResearchError, match="overlap"):
        _spec(frames, **reversed_)
    with pytest.raises(ValueError, match="end_ts < start_ts"):
        Period(start_ts=10, end_ts=9)


# ---------------------------------------------------------------------------
# result mapping
# ---------------------------------------------------------------------------


def _report(**overrides) -> dict:
    report = {
        "oos_ic": 0.02, "oos_rank_ic": 0.03, "nw_tstat": 3.5, "nw_lags": 2,
        "oos_hit_rate": 0.52, "turnover_flips_per_hour": 40.0,
        "fold_sign_consistency": 1.0, "n_folds_run": 4,
        "leakage": {"passed": True, "ic_unshifted": 0.02, "ic_shifted": 0.001,
                    "label_guard_ok": True, "shift_ok": True, "truncation_ok": True,
                    "suspicious_ic": 0.03, "required_shift_ratio": 0.2,
                    "median_row_gap_ns": 3_000_000_000},
        "hypothesis_confirmed": True, "verdict": "PROMOTE",
    }
    report.update(overrides)
    return report


def _holdout(**overrides) -> dict:
    h = {"gross_return_bps": 12.0, "transaction_cost_bps": 4.5,
         "max_drawdown_bps": 8.0, "sharpe": 1.2}
    h.update(overrides)
    return h


def test_build_result_mapping(spec):
    r = build_result(spec, _report(), _holdout(), 21, "deadbeef")
    assert (r.ic, r.rank_ic, r.t_stat, r.nw_lags, r.hit_rate, r.turnover,
            r.fold_consistency, r.n_folds) == (0.02, 0.03, 3.5, 2, 0.52, 40.0, 1.0, 4)
    assert r.net_return_bps == 12.0 - 4.5
    assert r.leakage_passed and r.hypothesis_sign_confirmed
    assert r.verdict is Verdict.PROMOTE
    assert r.n_experiments_in_ledger == 21 and r.git_commit == "deadbeef"
    assert r.created_ts == spec.test_period.end_ts
    assert r.leakage_detail == _report()["leakage"]


@pytest.mark.parametrize("report_overrides, holdout_overrides, name", [
    ({"oos_ic": None}, {}, "oos_ic"),
    ({"oos_ic": float("nan")}, {}, "oos_ic"),
    ({"nw_tstat": float("inf")}, {}, "nw_tstat"),
    ({"oos_hit_rate": None}, {}, "oos_hit_rate"),
    ({"turnover_flips_per_hour": None}, {}, "turnover_flips_per_hour"),
    ({"fold_sign_consistency": None}, {}, "fold_sign_consistency"),
    ({}, {"sharpe": float("nan")}, "sharpe"),
    ({}, {"max_drawdown_bps": None}, "max_drawdown_bps"),
    ({}, {"gross_return_bps": float("-inf")}, "gross_return_bps"),
])
def test_nan_metric_is_a_runner_error_naming_the_metric(
        spec, report_overrides, holdout_overrides, name):
    with pytest.raises(ResearchError, match=name):
        build_result(spec, _report(**report_overrides), _holdout(**holdout_overrides),
                     21, "deadbeef")


def test_nan_inside_leakage_detail_is_an_error_too(spec):
    leak = dict(_report()["leakage"], ic_shifted=float("nan"))
    with pytest.raises(ResearchError, match="leakage.ic_shifted"):
        build_result(spec, _report(leakage=leak), _holdout(), 21, "c")


def test_leakage_failure_forces_reject_via_the_contract(spec):
    leak = dict(_report()["leakage"], passed=False, shift_ok=False)
    r = build_result(spec, _report(leakage=leak, verdict="REJECT"), _holdout(), 21, "c")
    assert r.verdict is Verdict.REJECT and not r.leakage_passed
    with pytest.raises(ResearchError, match="leakage failure forces REJECT"):
        build_result(spec, _report(leakage=leak, verdict="PROMOTE"), _holdout(), 21, "c")


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------


def test_runner_satisfies_the_contract_protocol(frames, tmp_path):
    runner = _runner(frames, tmp_path, dry_run=True)
    assert isinstance(runner, protocols.ExperimentRunner)


def test_runner_rejects_a_spec_whose_id_is_not_its_hash(frames, tmp_path, spec):
    forged = dataclasses.replace(spec, experiment_id="0123456789abcdef")
    with pytest.raises(ResearchError, match="does not match"):
        _runner(frames, tmp_path, dry_run=True).run(forged)


def test_runner_rejects_an_unnormalised_configuration(frames, tmp_path, spec):
    body = spec.to_dict()
    del body["experiment_id"]
    body["configuration"] = {"n_folds": 4}          # missing the other pinned keys
    partial = ExperimentSpec.from_dict({"experiment_id": experiment_id_of(body), **body})
    with pytest.raises(ResearchError, match="not normalised"):
        _runner(frames, tmp_path, dry_run=True).run(partial)


def test_runner_needs_an_input_source(tmp_path):
    with pytest.raises(ResearchError, match="feature_store_dir or frames"):
        ExperimentRunner(None, tmp_path / "l.json", tmp_path / "e", CONFIGS_DIR)


def test_dry_run_computes_without_persisting(frames, tmp_path, spec):
    runner = _runner(frames, tmp_path, dry_run=True)
    result = runner.run(spec)
    assert result.experiment_id == spec.experiment_id
    assert result.n_experiments_in_ledger == LOOKS_PER_EXPERIMENT
    assert result.created_ts == spec.test_period.end_ts
    assert not (tmp_path / "experiments.json").exists()
    assert not (tmp_path / "experiments").exists()


def test_ledger_increments_by_exactly_the_looks_and_deduplicates(frames, tmp_path):
    runner = _runner(frames, tmp_path)
    a = _spec(frames, "5s")
    b = _spec(frames, "1s")
    assert ExperimentLedger(tmp_path / "experiments.json").total_experiments == 0
    ra = runner.run(a)
    ledger = ExperimentLedger(tmp_path / "experiments.json")
    assert ledger.total_experiments == LOOKS_PER_EXPERIMENT == ra.n_experiments_in_ledger
    assert ledger.distinct_experiments == 1
    # same spec again: a rerun, not a new look
    ra2 = runner.run(a)
    ledger = ExperimentLedger(tmp_path / "experiments.json")
    assert ledger.total_experiments == LOOKS_PER_EXPERIMENT == ra2.n_experiments_in_ledger
    entry = ledger.entries[0]
    assert (entry["alpha_id"], entry["kind"], entry["count"], entry["reruns"]) == (
        "EQ03", LEDGER_KIND, LOOKS_PER_EXPERIMENT, 1)
    assert entry["config"] == a.to_dict()
    assert entry["result"]["experiment_id"] == a.experiment_id
    # a different spec is one more batch of looks
    rb = runner.run(b)
    ledger = ExperimentLedger(tmp_path / "experiments.json")
    assert ledger.total_experiments == 2 * LOOKS_PER_EXPERIMENT == rb.n_experiments_in_ledger
    assert ledger.distinct_experiments == 2
    assert ra == ra2


def test_persisted_documents_are_canonical_and_byte_identical_on_rerun(frames, tmp_path, spec):
    runner = _runner(frames, tmp_path)
    runner.run(spec)
    target = tmp_path / "experiments" / spec.experiment_id
    first = {n: (target / n).read_bytes() for n in ("spec.json", "result.json")}
    ledger_first = (tmp_path / "experiments.json").read_bytes()
    _runner(frames, tmp_path).run(spec)          # a fresh runner, same store
    second = {n: (target / n).read_bytes() for n in ("spec.json", "result.json")}
    assert first == second
    # the ledger moves only in its rerun counter (its own pinned semantics)
    before = json.loads(ledger_first)
    after = json.loads((tmp_path / "experiments.json").read_text())
    assert after["total_experiments"] == before["total_experiments"]
    assert after["entries"][0].pop("reruns") == 1 and "reruns" not in before["entries"][0]
    assert after == before
    for name, schema in (("spec.json", "research/experiment_spec.schema.json"),
                         ("result.json", "research/experiment_result.schema.json")):
        text = first[name].decode("ascii")
        doc = json.loads(text)
        validate(doc, schema)
        assert text == render_document(doc)          # sorted keys, 2-space, trailing \n
        assert text.endswith("}\n") and "  \"" in text
        assert "NaN" not in text and "Infinity" not in text
    assert json.loads(first["spec.json"])["experiment_id"] == spec.experiment_id
    assert json.loads(first["result.json"])["experiment_id"] == spec.experiment_id


def test_rerun_with_a_different_result_is_refused(frames, tmp_path, spec):
    runner = _runner(frames, tmp_path)
    runner.run(spec)
    path = tmp_path / "experiments" / spec.experiment_id / "result.json"
    doc = json.loads(path.read_text())
    doc["ic"] += 1e-3
    path.write_text(render_document(doc))
    with pytest.raises(ResearchError, match="reproduced different values at \\['\\$\\.ic'\\]"):
        runner.run(spec)
    doc["ic"] -= 1e-3
    doc["git_commit"] = "somewhere-else"          # provenance may differ
    path.write_text(render_document(doc))
    runner.run(spec)


def test_holdout_never_fits_on_validation_or_test_rows(frames, tmp_path, monkeypatch):
    """Leakage-free by construction: the holdout model's training rows all
    satisfy the splitter's purge + embargo mask against the test start."""
    fits = []

    class Recording(EQ03OfiMultiLevel):
        def fit(self, train):
            fits.append({iid: df["exchange_ts"].to_numpy().copy()
                         for iid, df in train.items()})
            super().fit(train)

    monkeypatch.setattr("iap.research.runner.build", lambda alpha_id: Recording())
    s = _spec(frames, "5s", configuration={"embargo_ns": 90 * NS_S})
    _runner(frames, tmp_path, dry_run=True).run(s)
    holdout_train = fits[-1][GOLDEN_INSTRUMENT]      # the last fit is the holdout
    limit = s.test_period.start_ts - HORIZONS_NS["5s"] - 90 * NS_S
    assert holdout_train.size > 0
    assert np.all(holdout_train + HORIZONS_NS["5s"] + 90 * NS_S < s.test_period.start_ts)
    assert np.all(holdout_train < min(limit, s.train_period.end_ts))
    # the walk-forward fits (1 per fold) precede it
    assert len(fits) == s.configuration["n_folds"] + 1


def test_horizon_override_drives_fit_and_labels(frames, tmp_path):
    """The spec's horizon is the label the alpha is fitted/scored against."""
    r5 = _runner(frames, tmp_path, dry_run=True).run(_spec(frames, "5s"))
    r1 = _runner(frames, tmp_path, dry_run=True).run(_spec(frames, "1s"))
    assert r5.ic != r1.ic
    assert r5.nw_lags == r1.nw_lags == 2


def test_runner_reports_an_unsplittable_window_honestly(frames, tmp_path):
    ts = frames[GOLDEN_INSTRUMENT]["exchange_ts"].to_numpy()
    small = {GOLDEN_INSTRUMENT: frames[GOLDEN_INSTRUMENT].iloc[:120].reset_index(drop=True)}
    s = build_spec("EQ03", "5s", {}, dataset_version=DATA_VERSION,
                   feature_version=FEATURE_VERSION, seed=1,
                   train_period=Period(int(ts[0]), int(ts[60])),
                   validation_period=Period(int(ts[60]), int(ts[80])),
                   test_period=Period(int(ts[80]), int(ts[119]) + 1))
    with pytest.raises(ResearchError, match="walk-forward validation impossible"):
        _runner(small, tmp_path, dry_run=True).run(s)


def test_universe_mismatch_is_an_error(frames, tmp_path):
    s = _spec(frames, "1m", alpha_id="FX01")   # FX alpha, equity frame
    with pytest.raises(ResearchError, match="no rows"):
        _runner(frames, tmp_path, dry_run=True).run(s)


# ---------------------------------------------------------------------------
# registry + CLI
# ---------------------------------------------------------------------------


def test_registry_lists_loads_and_finds(frames, tmp_path):
    runner = _runner(frames, tmp_path)
    a = _spec(frames, "5s")
    b = _spec(frames, "1s")
    ra, rb = runner.run(a), runner.run(b)
    reg = ExperimentRegistry(tmp_path / "experiments")
    assert reg.experiment_ids() == sorted([a.experiment_id, b.experiment_id])
    assert reg.load_spec(a.experiment_id) == a
    assert reg.load_result(b.experiment_id) == rb
    assert [r.experiment_id for r in reg.records()] == reg.experiment_ids()
    found = reg.find(alpha_id="EQ03", horizon="5s")
    assert len(found) == 1 and found[0].spec == a and found[0].result == ra
    assert len(reg.find(alpha_id="EQ03")) == 2
    assert reg.find(alpha_id="EQ01") == []
    assert reg.find(horizon="1s")[0].experiment_id == b.experiment_id
    assert len(reg.find(verdict=ra.verdict)) >= 1
    assert ExperimentRegistry(tmp_path / "nowhere").experiment_ids() == []


def test_registry_rejects_corrupt_documents(frames, tmp_path, spec):
    runner = _runner(frames, tmp_path)
    runner.run(spec)
    reg = ExperimentRegistry(tmp_path / "experiments")
    spec_path = tmp_path / "experiments" / spec.experiment_id / "spec.json"
    doc = json.loads(spec_path.read_text())
    doc["seed"] = doc["seed"] + 1                  # body edited, id stale
    spec_path.write_text(render_document(doc))
    with pytest.raises(ResearchError, match="does not match the spec body"):
        reg.load_spec(spec.experiment_id)
    with pytest.raises(ResearchError):
        list(reg.records())
    (tmp_path / "experiments" / spec.experiment_id / "result.json").unlink()
    with pytest.raises(ResearchError, match="missing"):
        reg.load_result(spec.experiment_id)


def test_cli_list_and_show(frames, tmp_path, spec, capsys):
    _runner(frames, tmp_path).run(spec)
    out_dir = str(tmp_path / "experiments")
    assert cli_main(["--out-dir", out_dir, "list"]) == 0
    listed = capsys.readouterr().out
    assert spec.experiment_id in listed and "REJECT" in listed
    assert cli_main(["--out-dir", out_dir, "list", "--alpha", "EQ01"]) == 0
    assert "no experiments" in capsys.readouterr().out
    assert cli_main(["--out-dir", out_dir, "show", spec.experiment_id]) == 0
    shown = capsys.readouterr().out
    assert "VERDICT: REJECT" in shown and spec.dataset_version in shown
    assert cli_main(["--out-dir", out_dir, "show", "0000000000000000"]) == 1
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# bundled data: the flagship EQ03 report through the runner
# ---------------------------------------------------------------------------


def test_eq03_report_reproduces_through_the_runner(tmp_path):
    """EQ03 at its pinned 5 s horizon with the default (= run_all.py)
    configuration on the bundled dataset: the derived periods are the
    day-1 / day-2 split, the walk-forward runs over the full two-session
    window exactly as run_all.py's validate_alpha call, so IC / rank IC /
    NW t / hit rate / fold consistency / leakage / verdict must equal
    research/alpha_reports/EQ03.json at 1e-9.  (The report has no 1 s
    EQ03 row: the flagship pipeline evaluates each alpha at its pinned
    horizon only, so a 1 s EQ03 experiment is a different configuration
    and is not compared here.)"""
    if not FEATURES_DIR.is_dir() or not list(FEATURES_DIR.glob("features_1*.parquet")):
        pytest.skip("data/features missing — regenerate via python3 -m iap.features")
    report_path = REPORTS_DIR / "EQ03.json"
    if not report_path.is_file():
        pytest.skip("research/alpha_reports/EQ03.json missing — run run_all.py")
    report = json.loads(report_path.read_text())
    assert report["horizon"] == "5s"
    runner = ExperimentRunner(FEATURES_DIR, tmp_path / "experiments.json",
                              tmp_path / "experiments", CONFIGS_DIR, dry_run=True)
    frames = runner.frames()
    spec = build_spec("EQ03", None, {}, frames=frames)
    assert spec.horizon == "5s" and spec.configuration == DEFAULT_CONFIGURATION
    days = sorted({int(d) for df in frames.values()
                   for d in np.unique(df["exchange_ts"].to_numpy() // NS_DAY)})
    assert len(days) == 2
    assert spec.test_period.start_ts // NS_DAY == days[1]
    assert spec.train_period.start_ts // NS_DAY == days[0]
    result = runner.run(spec)
    for field, key in (("ic", "oos_ic"), ("rank_ic", "oos_rank_ic"),
                       ("t_stat", "nw_tstat"), ("hit_rate", "oos_hit_rate"),
                       ("turnover", "turnover_flips_per_hour"),
                       ("fold_consistency", "fold_sign_consistency")):
        got, want = getattr(result, field), report[key]
        assert math.isfinite(got) and abs(got - want) <= TOL + TOL * abs(want), (field, got, want)
    assert result.nw_lags == report["nw_lags"]
    assert result.n_folds == report["n_folds_run"]
    assert result.leakage_passed == report["leakage"]["passed"]
    # ic_shifted / ic_unshifted are BLAS reductions: last-ulp CPU dependence,
    # so 1e-9 like every other research double (CI runners differ here).
    assert document_drift(report["leakage"], result.leakage_detail, tol=TOL) == []
    assert result.hypothesis_sign_confirmed == report["hypothesis_confirmed"]
    assert result.verdict.value == report["verdict"]
    assert result.created_ts == spec.test_period.end_ts
    assert result.n_experiments_in_ledger == LOOKS_PER_EXPERIMENT


def test_document_drift_tolerates_last_ulp_but_not_semantics():
    """The rerun guard and the goldens compare research documents at 1e-9
    on floats and exactly on everything else (CI runners' BLAS reductions
    differ from a laptop's in the last ulp)."""
    base = {"ic": 0.021453492319862478, "n": 4, "ok": True, "tag": "x",
            "leak": {"ic_shifted": -0.0680157774176, "passed": True}, "seq": [1.0, 2.0]}
    same = json.loads(json.dumps(base))
    same["ic"] = 0.021453492319862492                 # last-ulp difference
    same["leak"]["ic_shifted"] = -0.0680157774176 * (1 + 1e-12)
    assert document_drift(base, same) == []
    for path, mutate in (
        ("$.ic", lambda d: d.__setitem__("ic", 0.0215)),                # > 1e-9
        ("$.n", lambda d: d.__setitem__("n", 5)),
        ("$.ok", lambda d: d.__setitem__("ok", 1)),                    # bool vs int
        ("$.tag", lambda d: d.__setitem__("tag", "y")),
        ("$.leak.passed", lambda d: d["leak"].__setitem__("passed", False)),
        ("$.seq", lambda d: d.__setitem__("seq", [1.0])),
        ("$", lambda d: d.__setitem__("extra", 1)),
    ):
        doc = json.loads(json.dumps(base))
        mutate(doc)
        assert document_drift(base, doc) == [path], path


def test_persist_keeps_committed_bytes_when_numbers_agree(tmp_path, spec):
    """A rerun whose floats differ only in the last ulp is a reproduction:
    result.json keeps its committed bytes; a real change is refused."""
    runner = ExperimentRunner(None, tmp_path / "ledger.json", tmp_path / "exp",
                              CONFIGS_DIR, frames={}, dry_run=False)
    result = build_result(spec, _report(), _holdout(), 21, "deadbeef")
    runner._persist(spec, result)
    path = runner.experiment_dir(spec.experiment_id) / "result.json"
    first = path.read_bytes()
    nudged = build_result(spec, _report(oos_ic=_report()["oos_ic"] * (1 + 1e-13)),
                          _holdout(), 21, "deadbeef")
    runner._persist(spec, nudged)
    assert path.read_bytes() == first
    with pytest.raises(ResearchError, match="reproduced different values"):
        runner._persist(spec, build_result(spec, _report(oos_ic=0.5), _holdout(), 21, "deadbeef"))

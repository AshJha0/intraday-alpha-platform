"""Experiment tracker: run ids, ledger, manifest completeness (spec §14/§26)."""

from __future__ import annotations

import json

import pytest

from iap.experiment.tracker import (
    ExperimentTracker,
    data_version,
    feature_version,
    git_commit,
    hardware_summary,
)

REQUIRED_MANIFEST_KEYS = {
    "experiment_id", "git_commit", "data_version", "feature_version",
    "model_version", "hyperparams", "train_window", "test_window", "hardware",
}


@pytest.fixture()
def tracker(tmp_path):
    return ExperimentTracker(models_dir=tmp_path / "models")


def test_new_run_sequential_ids(tracker):
    r1 = tracker.new_run("ols")
    r2 = tracker.new_run("ridge")
    assert r1 == "run_0001_ols"
    assert r2 == "run_0002_ridge"
    assert tracker.experiment_count == 2
    assert tracker.run_dir(r1).is_dir()


def test_new_run_rejects_bad_names(tracker):
    for bad in ("", "a b", "a/b"):
        with pytest.raises(ValueError):
            tracker.new_run(bad)


def test_manifest_completeness(tracker):
    run_id = tracker.new_run("m")
    manifest = tracker.write_manifest(
        run_id, model_version="m_v1", hyperparams={"alpha": 1.0},
        train_window={"start_ts": 1, "end_ts": 2},
        test_window={"start_ts": 3, "end_ts": 4})
    assert set(manifest) == REQUIRED_MANIFEST_KEYS
    on_disk = json.loads(
        (tracker.run_dir(run_id) / "manifest.json").read_text())
    assert on_disk == manifest
    assert manifest["experiment_id"] == run_id
    hw = manifest["hardware"]
    assert hw["cpu_count"] >= 1 and hw["python"]


def test_manifest_versions_are_real(tracker):
    # workspace is not a git checkout -> pinned fallback string
    assert git_commit() == "unversioned-workspace"
    # qc_report + feature registry exist in this repo -> sha256 hex digests
    assert len(data_version()) == 64
    assert len(feature_version()) == 64
    hw = hardware_summary()
    assert set(hw) == {"cpu_model", "cpu_count", "machine", "system",
                       "python"}


def test_manifest_rejects_incomplete_window(tracker):
    run_id = tracker.new_run("m")
    with pytest.raises(ValueError):
        tracker.write_manifest(run_id, "v", {}, {"start_ts": 1}, {})


def test_metrics_and_model_roundtrip(tracker):
    run_id = tracker.new_run("m")
    tracker.write_metrics(run_id, {"ic": 0.02})
    assert json.loads(
        (tracker.run_dir(run_id) / "metrics.json").read_text()) == {
            "ic": 0.02}
    tracker.save_model(run_id, {"weights": [1, 2, 3]})
    assert tracker.load_model(run_id) == {"weights": [1, 2, 3]}
    with pytest.raises(ValueError):
        tracker.load_model("run_9999_missing")


def test_ledger_persists_across_instances(tmp_path):
    t1 = ExperimentTracker(models_dir=tmp_path / "models")
    t1.new_run("a")
    t2 = ExperimentTracker(models_dir=tmp_path / "models")
    assert t2.new_run("b") == "run_0002_b"
    ledger = t2.read_ledger()
    assert ledger["experiment_count"] == 2
    assert [r["run_id"] for r in ledger["runs"]] == ["run_0001_a",
                                                     "run_0002_b"]

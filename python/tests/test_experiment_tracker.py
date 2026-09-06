"""Experiment tracker: run ids, ledger, manifest completeness (spec §14/§26)."""

from __future__ import annotations

import json

import pytest

from iap.experiment.tracker import (
    ExperimentTracker,
    data_version,
    feature_version,
    git_commit,
    git_status,
    hardware_summary,
    library_versions,
)

REQUIRED_MANIFEST_KEYS = {
    "experiment_id", "git_commit", "git_dirty", "git_dirty_hash",
    "data_version", "feature_version", "model_version", "hyperparams",
    "features", "target", "train_window", "test_window", "folds",
    "library_versions", "hardware",
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


def test_manifest_versions_are_real(tracker, tmp_path):
    # inside a git checkout -> the 40-hex HEAD sha; outside one -> the
    # pinned fallback string (the workspace may be either)
    commit = git_commit()
    assert commit == "unversioned-workspace" or (
        len(commit) == 40 and all(c in "0123456789abcdef" for c in commit))
    # an empty directory is never a checkout -> pinned fallback string
    assert git_commit(tmp_path) == "unversioned-workspace"
    # normalized data + feature registry exist in this repo -> sha256 digests
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


# -- round-3: real provenance ---------------------------------------------


def test_manifest_records_real_commit_and_dirty_flag():
    """In a git checkout the manifest carries the 40-hex HEAD plus an
    explicit dirty flag — never the string 'unversioned-workspace'."""
    st = git_status()
    if st["git_commit"] == "unversioned-workspace":
        pytest.skip("not a git checkout")
    assert len(st["git_commit"]) == 40
    assert all(c in "0123456789abcdef" for c in st["git_commit"])
    assert st["git_dirty"] in (True, False)
    if st["git_dirty"]:
        assert len(st["git_dirty_hash"]) == 64
    else:
        assert st["git_dirty_hash"] is None


def test_data_version_hashes_content_not_paths(tmp_path):
    """The fingerprint is invariant under moving the checkout and changes
    when one byte of a normalized file changes."""
    import shutil

    src = data_version()
    a = tmp_path / "a" / "data" / "normalized"
    b = tmp_path / "bbbbbbbbbbbb" / "data" / "normalized"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    for name, payload in (("eq_1.normalized.iap1", b"one"),
                          ("fx_1.normalized.iap1", b"two")):
        (a / name).write_bytes(payload)
        (b / name).write_bytes(payload)
    va = data_version(tmp_path / "a")
    vb = data_version(tmp_path / "bbbbbbbbbbbb")
    assert va == vb, "moving the checkout must not change data_version"
    assert va != src and len(va) == 64
    # one changed byte changes the fingerprint
    (b / "eq_1.normalized.iap1").write_bytes(b"onE")
    assert data_version(tmp_path / "bbbbbbbbbbbb") != vb
    # an absent dataset is named, never silently hashed to something
    shutil.rmtree(a)
    assert data_version(tmp_path / "a") == "no-normalized-data"


def test_manifest_records_features_target_folds_and_libraries(tracker):
    run_id = tracker.new_run("m")
    m = tracker.write_manifest(
        run_id, model_version="m_v1", hyperparams={},
        train_window={"start_ts": 1, "end_ts": 2}, test_window={},
        features=["ofi_l1_w1s_v1"], target="label_mid_1s",
        folds=[{"fold": 1, "test_start": 1, "test_end": 2}])
    assert m["features"] == ["ofi_l1_w1s_v1"]
    assert m["target"] == "label_mid_1s"
    assert m["folds"][0]["fold"] == 1
    libs = m["library_versions"]
    assert set(libs) >= {"numpy", "sklearn", "lightgbm", "xgboost"}
    assert libs == library_versions()

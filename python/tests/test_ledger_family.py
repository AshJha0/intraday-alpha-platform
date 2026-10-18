"""One ledger family (v1.18 REL-02c): data_family tag, real-data N, dry-run merge tool."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from iap.validation.ledger import ExperimentLedger

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lfr", REPO / "tools" / "ledger_family_report.py")
lfr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lfr)


def test_tag_is_opt_in_and_does_not_touch_identity_or_chain(tmp_path):
    a = ExperimentLedger(tmp_path / "a.json", dataset_version="D")
    b = ExperimentLedger(tmp_path / "b.json", dataset_version="D", data_family="real")
    for led in (a, b):
        led.record("EQ01", "run", {"h": "1s"})
        led.save()
    ea, eb = a.entries[0], b.entries[0]
    assert "data_family" not in ea and eb["data_family"] == "real"
    assert ea["key"] == eb["key"] and a.chain_head == b.chain_head  # not chained
    assert ExperimentLedger(tmp_path / "b.json").entries[0]["data_family"] == "real"  # round trip


def test_untagged_ledger_bytes_are_unchanged(tmp_path):
    led = ExperimentLedger(tmp_path / "x.json")
    led.record("EQ01", "run", {"h": "1s"})
    led.save()
    assert "data_family" not in (tmp_path / "x.json").read_text()


def test_bad_family_refused(tmp_path):
    with pytest.raises(ValueError, match="data_family"):
        ExperimentLedger(tmp_path / "x.json", data_family="simulated")


def test_n_counts_distinct_real_configurations_and_keeps_synthetic_recorded(tmp_path):
    real = {"R1", "R2"}
    led = ExperimentLedger(tmp_path / "l.json")
    for ds, cfg in (("R1", 1), ("R1", 2), ("R2", 1), ("S1", 1), ("S1", 2), (None, 9)):
        led.dataset_version = ds
        led.record("EQ01", "run", {"c": cfg})
    led.dataset_version = "R1"
    led.record("EQ01", "run", {"c": 1})  # a rerun: same identity, no new N
    led.record("EQ02", "run", {"c": 1}, count=3)  # an execution study declared as 3 looks
    fam = led.family_counts(real, synthetic_versions={"S1"})
    assert fam["real"] == {"entries": 4, "looks": 6}
    assert fam["synthetic"] == {"entries": 2, "looks": 2}
    assert fam["unknown"] == {"entries": 1, "looks": 1}  # never guessed
    assert led.real_distinct_experiments(real) == 4  # synthetic and unknown excluded from N
    assert led.total_experiments == 9  # ... but every look stays recorded


def test_explicit_tag_wins_over_the_version_lookup():
    e = {"dataset_version": "R1", "data_family": "synthetic"}
    assert ExperimentLedger.entry_data_family(e, {"R1"}) == "synthetic"


def test_dry_run_report_never_writes_the_repo_ledger(tmp_path, capsys):
    repo = tmp_path / "experiments.json"
    led = ExperimentLedger(repo, dataset_version="S1")
    led.record("EQ01", "run", {"c": 1})
    led.save()
    other = tmp_path / "data" / "ds" / "research" / "experiments.json"
    o = ExperimentLedger(other, dataset_version="R1")
    o.record("EQ01", "run", {"c": 1})
    o.record("EQ01", "run", {"c": 2})
    o.save()
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"datasets": {"R1": {}}}))
    before = repo.read_bytes()
    out = tmp_path / "merged.json"
    rc = lfr.main(
        [
            "--repo-ledger", str(repo), "--registry", str(reg),
            "--data-root", str(tmp_path / "data"), "--out", str(out),
        ]
    )  # fmt: skip
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and repo.read_bytes() == before
    assert doc["merged"]["n_real_distinct"] == 2 and doc["repo_ledger"]["n_real_distinct"] == 0
    assert doc["merged"]["unclassified_entries"] == 1  # S1 not declared synthetic here
    assert len(ExperimentLedger(out).entries) == 3
    with pytest.raises(SystemExit):  # refuses to overwrite the committed ledger
        lfr.main(["--repo-ledger", str(repo), "--registry", str(reg), "--out", str(repo)])


def test_committed_repo_ledger_dry_run_matches_the_baseline():
    """The committed ledger after the REL-02 migration: the 24 multi7 looks + the 14
    prereg entries + 12 merged per-dataset entries + 206 B7/B8 looks are real; the
    384 seeded-generator entries are synthetic; nothing is unclassified."""
    led = ExperimentLedger(REPO / "research" / "experiments.json")
    real = lfr.real_versions(REPO / "research" / "holdout_registry.json")
    synth = {
        v
        for e in led.entries
        if (v := ExperimentLedger.entry_dataset_version(e)) and v.startswith(lfr.SYNTHETIC_PREFIXES)
    }
    fam = led.family_counts(real, synth)
    assert fam["real"]["entries"] >= 256  # later real looks only append
    assert fam["synthetic"]["entries"] >= 384
    assert fam["unknown"]["entries"] == 0
    assert sum(f["entries"] for f in fam.values()) == len(led.entries)


def test_committed_ledger_is_migrated_rel02():
    """REL-02 migration (2026-10-18): every entry tagged, chain descends from the
    pre-migration head, retroactive B7/B8 looks present, migration idempotent."""
    from iap.validation.ledger import chain_heads

    path = REPO / "research" / "experiments.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    led = ExperimentLedger(path)  # verifies chain_head against the entries
    assert all(e.get("data_family") in ("real", "synthetic") for e in led.entries)
    pre_head = "1f95affdee760e380149f400b4db86f0b3e87ae0a17181890ede3301116e489d"
    assert chain_heads(led.entries)[421] == pre_head  # the 422 pre-migration entries
    kinds = [e["alpha_id"] for e in led.entries if e["kind"] == "execution_study"]
    assert kinds.count("B7") >= 44 and kinds.count("B8") >= 162
    assert led.family_counts()["unknown"]["entries"] == 0
    assert doc["chain_length"] == len(led.entries)
    spec2 = importlib.util.spec_from_file_location(
        "mig", REPO / "tools" / "migrate_ledger_rel02.py"
    )
    mig = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mig)
    again, rep = mig.migrate(REPO / "no-such-data-root", path)
    assert rep["tagged"] == 0 and rep["retroactive_study_entries"] == {}
    assert again._render() == path.read_text(encoding="utf-8").replace("\r\n", "\n")

"""The research store under parallel automated writers: lock + atomic write,
no lost ledger update across processes, run-id allocation, atomic experiment
directories, tolerant listing, gate eligibility and the tool-facing CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest
from conftest import CONFIGS_DIR, REPO_ROOT
from iap.contracts.types import Period
from iap.experiment.locking import FileLock, LockTimeout, atomic_write_text
from iap.experiment.tracker import ExperimentTracker
from iap.lifecycle.config import load_policy_config
from iap.lifecycle.evidence import Evidence
from iap.lifecycle.gates import GATE_SPECS, build_gates
from iap.research import (
    DEFAULT_CONFIGURATION,
    GATE_ELIGIBILITY_BOUNDS,
    ExperimentRegistry,
    ExperimentRunner,
    GateEligibility,
    ResearchError,
    build_result,
    build_spec,
    derive_periods,
    gate_eligibility,
)
from iap.research.__main__ import main as cli_main
from iap.research.runner import ELIGIBILITY_FILE, STAGING_PREFIX, render_document
from iap.store.__main__ import main as store_main
from iap.store.db import Store
from iap.validation.ledger import ExperimentLedger
from test_research_runner import (
    DATA_VERSION,
    FEATURE_VERSION,
    _holdout,
    _report,
    _runner,
    _spec,
    _two_day_frames,
)

SRC_DIR = REPO_ROOT / "python" / "src"


@pytest.fixture(scope="module")
def frames() -> dict[int, pd.DataFrame]:
    from conftest import GOLDEN_DIR
    from iap.research.golden import golden_frames

    return golden_frames(GOLDEN_DIR, CONFIGS_DIR)


# ---------------------------------------------------------------------------
# lock + atomic write
# ---------------------------------------------------------------------------


def test_file_lock_is_exclusive_and_times_out(tmp_path):
    target = tmp_path / "ledger.json"
    with FileLock(target) as held:
        assert held.lock_path == tmp_path / "ledger.json.lock"
        assert held.lock_path.read_text().strip() == str(os.getpid())
        with pytest.raises(LockTimeout, match="ledger.json.lock"):
            FileLock(target, timeout_s=0.05, retry_interval_s=0.01).acquire()
    assert not (tmp_path / "ledger.json.lock").exists()  # released
    with FileLock(target):  # re-acquirable
        pass


def test_file_lock_releases_on_error_and_validates_arguments(tmp_path):
    target = tmp_path / "x.json"
    with pytest.raises(RuntimeError, match="boom"):
        with FileLock(target):
            raise RuntimeError("boom")
    assert not (tmp_path / "x.json.lock").exists()
    with pytest.raises(ValueError):
        FileLock(target, timeout_s=-1)
    with pytest.raises(ValueError):
        FileLock(target, retry_interval_s=0)


def test_atomic_write_replaces_whole_documents_and_leaves_no_temp(tmp_path):
    path = tmp_path / "nested" / "doc.json"
    atomic_write_text(path, "first\n", encoding="ascii", newline="\n")
    atomic_write_text(path, "second\n", encoding="ascii", newline="\n")
    assert path.read_bytes() == b"second\n"
    assert [p.name for p in path.parent.iterdir()] == ["doc.json"]


def test_ledger_save_is_byte_identical_to_the_single_writer_form(tmp_path):
    """Locking and replay change nothing for one writer."""
    led = ExperimentLedger(tmp_path / "a.json")
    led.record("EQ01", "k", {"x": 1}, {"ic": 0.1}, count=3)
    led.record("EQ01", "k", {"x": 1}, {"ic": 0.2})  # rerun
    led.record("EQ02", "k", {"x": 2})
    led.save()
    doc = json.loads((tmp_path / "a.json").read_text())
    assert doc["total_experiments"] == 4 and doc["distinct_experiments"] == 2
    assert doc["entries"][0]["reruns"] == 1 and doc["entries"][0]["result"] == {"ic": 0.2}
    assert (tmp_path / "a.json").read_text() == json.dumps(doc, indent=2, sort_keys=True) + "\n"
    led.save()  # idempotent
    assert json.loads((tmp_path / "a.json").read_text()) == doc
    assert not (tmp_path / "a.json.lock").exists()


def test_ledger_save_merges_a_concurrent_writers_entries(tmp_path):
    """The lost update, deterministically: B loads before A saves."""
    path = tmp_path / "experiments.json"
    a, b = ExperimentLedger(path), ExperimentLedger(path)
    a.record("EQ01", "k", {"w": "a"}, count=28)
    b.record("EQ02", "k", {"w": "b"}, count=28)
    a.save()
    b.save()  # used to overwrite a's entry with b's stale view
    merged = ExperimentLedger(path)
    assert merged.total_experiments == 56 and merged.distinct_experiments == 2
    assert [e["alpha_id"] for e in merged.entries] == ["EQ01", "EQ02"]
    assert [e["n"] for e in merged.entries] == [28, 56]
    assert b.total_experiments == 56  # b now sees a's looks too
    # the same identity recorded by both is one experiment with a rerun
    c, d = ExperimentLedger(path), ExperimentLedger(path)
    c.record("EQ03", "k", {"w": "same"}, count=28)
    d.record("EQ03", "k", {"w": "same"}, count=28)
    c.save()
    d.save()
    final = ExperimentLedger(path)
    assert final.total_experiments == 84 and final.distinct_experiments == 3
    assert final.entries[-1]["reruns"] == 1


_WRITER = """
import sys
sys.path.insert(0, sys.argv[1])
from iap.validation.ledger import ExperimentLedger
path, writer, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
for i in range(n):
    ledger = ExperimentLedger(path)
    ledger.record("EQ01", "race", {"writer": writer, "i": i}, {"i": i}, count=2)
    ledger.save()
"""


def test_two_processes_lose_no_ledger_update(tmp_path):
    """Two OS processes, each doing load -> record -> save in a loop on the
    same ledger: every one of the 2 * n updates must be in the file."""
    path = tmp_path / "experiments.json"
    n = 25
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(SRC_DIR), str(path), w, str(n)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for w in ("a", "b")
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=300)
        assert proc.returncode == 0, err
    ledger = ExperimentLedger(path)
    assert ledger.distinct_experiments == 2 * n
    assert ledger.total_experiments == 2 * n * 2
    seen = sorted((e["config"]["writer"], e["config"]["i"]) for e in ledger.entries)
    assert seen == sorted((w, i) for w in ("a", "b") for i in range(n))
    assert [e["n"] for e in ledger.entries] == list(range(2, 4 * n + 1, 2))
    assert not Path(str(path) + ".lock").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["experiments.json"]


# ---------------------------------------------------------------------------
# tracker run-id allocation
# ---------------------------------------------------------------------------


def test_new_run_never_reuses_a_claimed_number(tmp_path):
    tracker = ExperimentTracker(models_dir=tmp_path / "models", repo_root=tmp_path)
    assert tracker.new_run("a") == "run_0001_a"
    # a writer that claimed 0002 and died before the ledger write
    (tmp_path / "models" / "run_0002_orphan").mkdir()
    assert tracker.new_run("b") == "run_0003_b"
    # the same NAME at a claimed number is skipped too (mkdir is the claim)
    (tmp_path / "models" / "run_0004_c").mkdir()
    assert tracker.new_run("c") == "run_0005_c"
    ledger = tracker.read_ledger()
    assert ledger["experiment_count"] == 5
    assert [r["run_id"] for r in ledger["runs"]] == ["run_0001_a", "run_0003_b", "run_0005_c"]


def test_new_run_under_concurrent_writers_allocates_distinct_ids(tmp_path):
    models = tmp_path / "models"
    ids, errors = [], []

    def worker(k: int) -> None:
        try:
            tracker = ExperimentTracker(models_dir=models, repo_root=tmp_path)
            for _ in range(6):
                ids.append(tracker.new_run(f"w{k}"))
        except Exception as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    numbers = sorted(int(r.split("_")[1]) for r in ids)
    assert numbers == list(range(1, 25))  # dense, no duplicates
    ledger = ExperimentTracker(models_dir=models, repo_root=tmp_path).read_ledger()
    assert ledger["experiment_count"] == 24
    assert sorted(r["run_id"] for r in ledger["runs"]) == sorted(ids)
    assert all((models / r).is_dir() for r in ids)


# ---------------------------------------------------------------------------
# runner persistence + registry listing
# ---------------------------------------------------------------------------


def test_new_experiment_directory_appears_complete_or_not_at_all(frames, tmp_path, monkeypatch):
    import iap.research.runner as runner_mod

    runner = _runner(frames, tmp_path)
    spec = _spec(frames)
    target = tmp_path / "experiments" / spec.experiment_id
    seen = []
    real_replace = os.replace

    def spying_replace(src, dst):
        if Path(dst) == target:
            seen.append(
                (Path(src).name, sorted(p.name for p in Path(src).iterdir()), target.exists())
            )
        return real_replace(src, dst)

    monkeypatch.setattr(runner_mod.os, "replace", spying_replace)
    runner.run(spec)
    ((staging, staged_files, target_existed),) = seen
    assert staging.startswith(STAGING_PREFIX + spec.experiment_id)
    assert staged_files == ["eligibility.json", "result.json", "spec.json"]
    assert not target_existed  # nothing visible before the move
    assert sorted(p.name for p in (tmp_path / "experiments").iterdir()) == [spec.experiment_id]
    assert sorted(p.name for p in target.iterdir()) == staged_files


def test_writer_that_loses_the_claim_takes_the_rerun_path(frames, tmp_path, monkeypatch):
    """Another writer creates the directory between the check and the move:
    this writer must compare against it (and refuse a different result)."""
    import iap.research.runner as runner_mod

    spec = _spec(frames)
    first = _runner(frames, tmp_path)
    first.run(spec)
    target = tmp_path / "experiments" / spec.experiment_id
    committed = (target / "result.json").read_bytes()
    late = ExperimentRunner(
        None, tmp_path / "experiments.json", tmp_path / "experiments", CONFIGS_DIR, frames=frames
    )
    monkeypatch.setattr(
        runner_mod.ExperimentRunner, "_claim_new_directory", lambda self, target, docs: False
    )
    monkeypatch.setattr(
        Path, "exists", lambda self: False if self == target else os.path.exists(self)
    )
    late.run(spec)  # same evidence: accepted
    assert (target / "result.json").read_bytes() == committed
    doc = json.loads(committed)
    doc["ic"] += 1e-3
    (target / "result.json").write_text(render_document(doc), encoding="ascii", newline="\n")
    with pytest.raises(ResearchError, match="reproduced different values") as err:
        late.run(spec)
    assert err.value.code == "not_reproducible"
    assert not [
        p for p in (tmp_path / "experiments").iterdir() if p.name.startswith(STAGING_PREFIX)
    ]


def test_persisted_documents_use_lf_on_every_platform(frames, tmp_path):
    runner = _runner(frames, tmp_path)
    spec = _spec(frames)
    runner.run(spec)
    for name in ("spec.json", "result.json", "eligibility.json"):
        raw = (tmp_path / "experiments" / spec.experiment_id / name).read_bytes()
        assert b"\r" not in raw and raw.endswith(b"}\n")


def test_listing_skips_and_reports_corrupt_and_incomplete_directories(frames, tmp_path):
    runner = _runner(frames, tmp_path)
    good, bad = _spec(frames, "5s"), _spec(frames, "1s")
    runner.run(good)
    runner.run(bad)
    root = tmp_path / "experiments"
    (root / bad.experiment_id / "result.json").write_text("{ torn", encoding="ascii")
    (root / "ffffffffffffffff").mkdir()  # claimed, never written
    (root / (STAGING_PREFIX + "0123456789abcdef-1")).mkdir()  # a run being staged
    (root / (STAGING_PREFIX + "0123456789abcdef-1") / "spec.json").write_text("{}")
    reg = ExperimentRegistry(root)
    records = list(reg.records())
    assert [r.experiment_id for r in records] == [good.experiment_id]
    assert [name for name, _ in reg.skipped] == sorted([bad.experiment_id, "ffffffffffffffff"])
    reasons = dict(reg.skipped)
    assert "not a canonical JSON document" in reasons[bad.experiment_id]
    assert "missing" in reasons["ffffffffffffffff"]
    assert reg.find(alpha_id="EQ03") == records  # find tolerates them too
    assert STAGING_PREFIX + "0123456789abcdef-1" not in reg.experiment_ids()
    # loading ONE experiment stays strict, with a stable code
    with pytest.raises(ResearchError) as err:
        reg.load(bad.experiment_id)
    assert err.value.code == "experiment_corrupt"
    with pytest.raises(ResearchError) as err:
        reg.load("ffffffffffffffff")
    assert err.value.code == "experiment_incomplete"
    with pytest.raises(ResearchError) as err:
        reg.load("0000000000000000")
    assert err.value.code == "experiment_not_found"


def test_store_import_ignores_staging_directories(frames, tmp_path):
    from iap.store.importers import import_experiment_documents

    runner = _runner(frames, tmp_path)
    spec = _spec(frames)
    runner.run(spec)
    root = tmp_path / "experiments"
    staging = root / (STAGING_PREFIX + spec.experiment_id + "-99")
    staging.mkdir()
    (staging / "spec.json").write_bytes((root / spec.experiment_id / "spec.json").read_bytes())
    with Store.open(tmp_path / "s.sqlite") as store:
        store.init()
        report = import_experiment_documents(store, root)
        assert store.counts()["experiments"] == 1
    assert not report.warnings


# ---------------------------------------------------------------------------
# gate eligibility
# ---------------------------------------------------------------------------


def _derived_spec(two_day, configuration=None, **overrides):
    kwargs = dict(dataset_version=DATA_VERSION, feature_version=FEATURE_VERSION, frames=two_day)
    kwargs.update(overrides)
    return build_spec("EQ03", None, configuration, **kwargs)


def test_default_protocol_on_derived_periods_is_gate_eligible():
    two_day = _two_day_frames()
    spec = _derived_spec(two_day)
    got = gate_eligibility(spec, two_day)
    assert got == GateEligibility(eligible=True, reasons=(), periods_verified=True)
    assert gate_eligibility(spec) == GateEligibility(True, (), False)
    assert {k: v[1] for k, v in GATE_ELIGIBILITY_BOUNDS.items()} == DEFAULT_CONFIGURATION


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({"cost_multiplier": 0.5}, "cost_multiplier=0.5 is below"),
        ({"latency_ns": 0}, "latency_ns=0 is below"),
        ({"embargo_ns": 0}, "embargo_ns=0 is below"),
        ({"n_folds": 2}, "n_folds=2 is below"),
        ({"max_decision_age_ns": 3_600_000_000_000}, "max_decision_age_ns=3600000000000 is above"),
        ({"flatten_at_session_end": False}, "flatten_at_session_end=False must be True"),
    ],
)
def test_flattering_configurations_run_but_are_not_gate_eligible(override, fragment):
    two_day = _two_day_frames()
    spec = _derived_spec(two_day, override)  # still a VALID spec
    got = gate_eligibility(spec, two_day)
    assert not got.eligible and len(got.reasons) == 1 and fragment in got.reasons[0]


@pytest.mark.parametrize(
    "override",
    [
        {"cost_multiplier": 2.0},
        {"latency_ns": 5_000_000_000},
        {"embargo_ns": 120_000_000_000},
        {"n_folds": 6},
        {"max_decision_age_ns": 1_000_000_000},
    ],
)
def test_more_conservative_configurations_stay_gate_eligible(override):
    two_day = _two_day_frames()
    assert gate_eligibility(_derived_spec(two_day, override), two_day).eligible


def test_caller_chosen_periods_are_not_gate_eligible():
    two_day = _two_day_frames()
    train, validation, test = derive_periods(two_day, "5s", DEFAULT_CONFIGURATION["embargo_ns"])
    shifted = Period(start_ts=test.start_ts + 60_000_000_000, end_ts=test.end_ts)
    spec = _derived_spec(
        two_day, frames=None, train_period=train, validation_period=validation, test_period=shifted
    )
    got = gate_eligibility(spec, two_day)
    assert not got.eligible and "caller-chosen" in got.reasons[0]
    # the same periods the calendar derives, passed explicitly, ARE eligible:
    # the rule is about the periods, not about who typed them
    same = _derived_spec(
        two_day, frames=None, train_period=train, validation_period=validation, test_period=test
    )
    assert gate_eligibility(same, two_day).eligible
    # a dataset no periods can be derived from makes every period set chosen
    one_day = {i: df[df["exchange_ts"] < test.start_ts] for i, df in two_day.items()}
    assert "none can be derived" in gate_eligibility(same, one_day).reasons[0]


def test_eligibility_never_changes_an_experiment_id():
    """The committed experiments must keep their ids: eligibility is not in
    the spec, and the pinned configuration normalises exactly as before.

    Two generations are committed: the five run on the v1.3.0 dataset, which
    predate the sidecar (judged on their configuration alone), and the same
    five alpha x horizon pairs run on the v1.4.0 dataset — new ids, because
    ``dataset_version`` is part of the spec — each with its
    ``eligibility.json`` and periods verified against the dataset."""
    legacy_dataset = "203c8f540f75de984fa80f5ec9c04a91a1252819586a9fca6d48483f1462e67b"
    legacy_ids = [
        "217fa0cb1d89a9c8",
        "4a2900e4a6705542",
        "c73bb6294d226163",
        "d0dd1ab0711d33a1",
        "d7b554d0a3fa3b26",
    ]
    current_ids = [
        "20f1b9093e7d0d04",
        "695e7b1e2bd2253e",
        "852863faa44b7b07",
        "876b08e20c46e6fd",
        "f0f6c49b553f6b59",
    ]
    reg = ExperimentRegistry(REPO_ROOT / "research" / "experiments")
    assert reg.experiment_ids() == sorted(legacy_ids + current_ids)
    current_datasets = set()
    for rec in reg.records():
        assert rec.spec.configuration == DEFAULT_CONFIGURATION
        got = reg.gate_eligibility(rec.experiment_id)
        assert got.eligible
        if rec.experiment_id in legacy_ids:
            assert rec.spec.dataset_version == legacy_dataset
            assert not got.periods_verified  # no sidecar: config only
        else:
            assert rec.spec.dataset_version != legacy_dataset
            assert got.periods_verified  # the sidecar the runner wrote
            current_datasets.add(rec.spec.dataset_version)
    assert len(current_datasets) == 1
    # the same five alpha x horizon pairs in both generations
    pairs = {
        ids_name: sorted(
            (rec.spec.alpha_id, rec.spec.horizon)
            for rec in reg.records()
            if rec.experiment_id in ids
        )
        for ids_name, ids in (("legacy", legacy_ids), ("current", current_ids))
    }
    assert pairs["legacy"] == pairs["current"]
    assert reg.skipped == []


def test_runner_records_eligibility_beside_the_result(frames, tmp_path):
    runner = _runner(frames, tmp_path)
    cheap = _spec(frames, configuration={"cost_multiplier": 0.5})
    runner.run(cheap)  # runs, is ledgered, is recorded
    assert runner.last_eligibility is not None and not runner.last_eligibility.eligible
    reg = ExperimentRegistry(tmp_path / "experiments")
    assert reg.load(cheap.experiment_id).result.experiment_id == cheap.experiment_id
    doc = json.loads(
        (tmp_path / "experiments" / cheap.experiment_id / ELIGIBILITY_FILE).read_text(
            encoding="ascii"
        )
    )
    assert doc["gate_eligible"] is False and doc["periods_verified"] is True
    assert doc["x-version"] == 1 and doc["experiment_id"] == cheap.experiment_id
    got = reg.gate_eligibility(cheap.experiment_id)
    assert not got.eligible
    assert any("cost_multiplier" in r for r in got.reasons)
    assert any("caller-chosen" in r for r in got.reasons)  # one-session fixture
    assert ExperimentLedger(tmp_path / "experiments.json").distinct_experiments == 1


def test_sidecar_cannot_overrule_the_configuration_bounds(frames, tmp_path):
    runner = _runner(frames, tmp_path)
    cheap = _spec(frames, configuration={"latency_ns": 0})
    runner.run(cheap)
    path = tmp_path / "experiments" / cheap.experiment_id / ELIGIBILITY_FILE
    forged = GateEligibility(True, (), True).to_dict(cheap.experiment_id)
    path.write_text(render_document(forged), encoding="ascii", newline="\n")
    reg = ExperimentRegistry(tmp_path / "experiments")
    got = reg.gate_eligibility(cheap.experiment_id)
    assert not got.eligible and "latency_ns=0" in got.reasons[0]
    path.write_text('{"gate_eligible": true}\n', encoding="ascii")
    with pytest.raises(ResearchError) as err:
        reg.gate_eligibility(cheap.experiment_id)
    assert err.value.code == "experiment_corrupt"


def _result(spec):
    return build_result(spec, _report(), _holdout(), 28, "deadbeef")


def test_lifecycle_gates_refuse_non_eligible_research_evidence(frames):
    """Every research-block gate fails on evidence flagged not eligible —
    and passes on the identical result when it is eligible."""
    gates = build_gates(load_policy_config())
    result = _result(_spec(frames))
    ok = Evidence(research=result, capacity_usd=1e9, validation=None, paper=None, live=None)
    refused = Evidence(
        research=result,
        capacity_usd=1e9,
        validation=None,
        paper=None,
        live=None,
        research_gate_eligible=False,
    )
    research_gates = [s.name for s in GATE_SPECS if s.block == "research"]
    assert len(research_gates) == 9
    for name in research_gates:
        assert gates[name].evaluate("EQ03", ok).passed, name
        got = gates[name].evaluate("EQ03", refused)
        assert not got.passed and got.value is None, name
    # the capacity gate reads its own block and is unaffected
    assert gates["capacity"].evaluate("EQ03", refused).passed
    assert len(GATE_SPECS) == 18  # no new row: the table is pinned


def test_evidence_document_is_unchanged_unless_flagged(frames):
    result = _result(_spec(frames))
    ok = Evidence(research=result, capacity_usd=1.0, validation=None, paper=None, live=None)
    assert sorted(ok.to_dict()) == ["capacity_usd", "live", "paper", "research", "validation"]
    assert Evidence.from_dict(ok.to_dict()) == ok
    flagged = Evidence(
        research=result,
        capacity_usd=1.0,
        validation=None,
        paper=None,
        live=None,
        research_gate_eligible=False,
    )
    assert flagged.to_dict()["research_gate_eligible"] is False
    assert Evidence.from_dict(flagged.to_dict()) == flagged
    with pytest.raises(ValueError, match="research_gate_eligible"):
        Evidence(
            research=None,
            capacity_usd=None,
            validation=None,
            paper=None,
            live=None,
            research_gate_eligible="no",
        )


def test_machine_does_not_promote_on_non_eligible_evidence(frames):
    from iap.contracts.types import LifecycleState
    from iap.lifecycle.machine import AlphaLifecycle
    from iap.lifecycle.registry import AlphaRegistry

    def advance(eligible: bool):
        config = load_policy_config()
        machine = AlphaLifecycle(config, AlphaRegistry(config.policy))
        machine.register("EQ03", 1)
        evidence = Evidence(
            research=_result(_spec(frames)),
            capacity_usd=1e9,
            validation=None,
            paper=None,
            live=None,
            research_gate_eligible=eligible,
        )
        return machine.advance("EQ03", 2, evidence), machine

    made, machine = advance(True)
    assert made is not None and machine.state("EQ03") is LifecycleState.CANDIDATE
    refused, machine = advance(False)
    assert refused is None and machine.state("EQ03") is LifecycleState.RESEARCH
    failed = machine.evaluations[-1].gates
    assert failed and not any(g.passed for g in failed.values())


# ---------------------------------------------------------------------------
# CLIs for tools
# ---------------------------------------------------------------------------


def test_cli_list_and_show_json(frames, tmp_path, capsys):
    runner = _runner(frames, tmp_path)
    a = _spec(frames, "5s")
    b = _spec(frames, "1s", configuration={"cost_multiplier": 0.5})
    ra, rb = runner.run(a), runner.run(b)
    out_dir = str(tmp_path / "experiments")
    (tmp_path / "experiments" / "ffffffffffffffff").mkdir()
    capsys.readouterr()

    assert cli_main(["--out-dir", out_dir, "list", "--json"]) == 0
    captured = capsys.readouterr()
    doc = json.loads(captured.out)  # ONE document, nothing else
    assert captured.err == ""
    assert sorted(doc) == ["experiments", "skipped"]
    by_id = {e["experiment_id"]: e for e in doc["experiments"]}
    assert sorted(by_id) == sorted([a.experiment_id, b.experiment_id])
    assert by_id[a.experiment_id]["spec"] == a.to_dict()
    assert by_id[a.experiment_id]["result"] == ra.to_dict()
    assert by_id[b.experiment_id]["result"] == rb.to_dict()
    assert by_id[b.experiment_id]["gate_eligibility"]["gate_eligible"] is False
    assert doc["skipped"][0]["directory"] == "ffffffffffffffff"

    assert cli_main(["--out-dir", out_dir, "list", "--json", "--alpha", "EQ01"]) == 0
    assert json.loads(capsys.readouterr().out)["experiments"] == []

    assert cli_main(["--out-dir", out_dir, "show", a.experiment_id, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert sorted(shown) == ["experiment_id", "gate_eligibility", "result", "spec"]
    assert shown["result"]["verdict"] == ra.verdict.value

    # the human listing warns about the skipped directory on stderr
    assert cli_main(["--out-dir", out_dir, "list"]) == 0
    captured = capsys.readouterr()
    assert "warning: skipped ffffffffffffffff" in captured.err
    assert cli_main(["--out-dir", out_dir, "show", b.experiment_id]) == 0
    assert "gate eligible: NO" in capsys.readouterr().out


def _error_doc(stderr: str) -> dict:
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, stderr  # exactly one JSON object
    doc = json.loads(lines[0])
    assert sorted(doc) == ["error"] and sorted(doc["error"]) == ["code", "message"]
    return doc["error"]


def test_cli_json_errors_have_stable_codes(frames, tmp_path, capsys):
    out_dir = str(tmp_path / "experiments")
    assert cli_main(["--out-dir", out_dir, "--json-errors", "show", "0000000000000000"]) == 1
    err = _error_doc(capsys.readouterr().err)
    assert err["code"] == "experiment_not_found" and "0000000000000000" in err["message"]

    # without the flag: the human form, unchanged
    assert cli_main(["--out-dir", out_dir, "show", "0000000000000000"]) == 1
    assert capsys.readouterr().err.startswith("error: ")

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["--json-errors", "show"])  # missing argument
    assert exit_info.value.code == 2
    assert _error_doc(capsys.readouterr().err)["code"] == "usage_error"

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["--json-errors", "frobnicate"])
    assert exit_info.value.code == 2
    assert _error_doc(capsys.readouterr().err)["code"] == "usage_error"


def test_cli_run_reports_spec_errors_as_json(tmp_path, capsys):
    two_day = _two_day_frames()
    features = tmp_path / "features"
    features.mkdir()
    for iid, df in two_day.items():
        df.to_parquet(features / f"features_{iid}.parquet")
    base = [
        "--out-dir",
        str(tmp_path / "experiments"),
        "--json-errors",
        "run",
        "--features-dir",
        str(features),
        "--ledger",
        str(tmp_path / "experiments.json"),
        "--configs-dir",
        str(CONFIGS_DIR),
    ]
    assert cli_main(base + ["--alpha", "EQ99"]) == 1
    assert _error_doc(capsys.readouterr().err)["code"] == "invalid_spec"
    assert cli_main(base + ["--alpha", "EQ03", "--config", "n_foldz=3"]) == 1
    captured = capsys.readouterr()
    assert _error_doc(captured.err)["code"] == "invalid_spec"
    assert cli_main(base + ["--alpha", "EQ03", "--config", "nonsense"]) == 1
    assert _error_doc(capsys.readouterr().err)["code"] == "invalid_spec"
    assert not (tmp_path / "experiments.json").exists()  # nothing was looked at


def test_runner_rejects_an_unknown_tstat_policy(frames, tmp_path):
    with pytest.raises(ResearchError, match="unknown tstat_threshold"):
        _runner(frames, tmp_path, tstat_threshold="bonferroni")


def test_runner_ledger_policy_uses_the_post_debit_threshold(frames, tmp_path, monkeypatch):
    import iap.research.runner as runner_mod

    seen = {}
    real = runner_mod.validate_alpha

    def spy(*args, **kwargs):
        seen.update(policy=kwargs["tstat_threshold"], threshold=kwargs["ledger_t_threshold"])
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "validate_alpha", spy)
    spec = _spec(frames)
    _runner(frames, tmp_path, dry_run=True).run(spec)
    assert seen == {"policy": "fixed", "threshold": None}
    runner = _runner(frames, tmp_path, dry_run=True, tstat_threshold="ledger")
    runner.run(_spec(frames, "1s"))  # a NEW spec: 28 more looks
    assert seen["policy"] == "ledger"
    assert seen["threshold"] == ExperimentLedger.bonferroni_t_threshold_at(56)
    runner = _runner(frames, tmp_path, dry_run=True, tstat_threshold="ledger")
    runner.run(spec)  # a rerun: no new looks
    assert seen["threshold"] == ExperimentLedger.bonferroni_t_threshold_at(56)


@pytest.fixture()
def store_db(tmp_path) -> Path:
    db = tmp_path / "iap.sqlite"
    with Store.open(db) as store:
        store.init()
    return db


def test_store_sql_read_only_hint_only_on_a_write(store_db, capsys):
    db = ["--db", str(store_db)]
    assert store_main(["sql", *db, "SELECT COUNT(*) AS n FROM alphas"]) == 0
    assert json.loads(capsys.readouterr().out) == {"n": 0}

    assert store_main(["sql", *db, "DELETE FROM alphas"]) == 1
    err = capsys.readouterr().err
    assert "readonly database" in err and "opened read-only" in err

    assert store_main(["sql", *db, "SELECT * FROM no_such_table"]) == 1
    err = capsys.readouterr().err
    assert "no such table" in err and "read-only" not in err

    assert store_main(["sql", *db, "SELEC 1"]) == 1
    err = capsys.readouterr().err
    assert "syntax error" in err and "read-only" not in err


def test_store_sql_refuses_several_statements_cleanly(store_db, capsys):
    db = ["--db", str(store_db)]
    assert store_main(["sql", *db, "SELECT 1; SELECT 2"]) == 1  # no traceback
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exactly one statement" in captured.err and "read-only" not in captured.err
    assert store_main(["sql", *db, "SELECT 1 AS one; DROP TABLE alphas"]) == 1
    assert "exactly one statement" in capsys.readouterr().err
    assert store_main(["sql", *db, "SELECT COUNT(*) AS n FROM alphas"]) == 0  # intact

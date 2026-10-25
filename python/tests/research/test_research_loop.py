"""RL1 research loop (v2.0 stage 7): refusals fail closed, the fixture run reproduces its hash, every run is ledgered."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest
from iap.contracts.versions import canonical_json
from iap.research.loop import (
    LEDGER_KIND,
    PartitionRefused,
    check_partition,
    load_family_spec,
    report_hash,
    run_family,
)
from iap.validation.ledger import ExperimentLedger
from stage7_fixtures import FAMILIES, REPO_ROOT, manifest, manifest_path, panel_path  # noqa: F401

D1 = FAMILIES / "D1_short_term_reversal.md"
D3 = FAMILIES / "D3_overnight_asymmetry.md"
D5 = FAMILIES / "D5_close_pressure.md"
H2 = FAMILIES / "H2_intraday_momentum.md"


def environment_key() -> str:
    return f"{sys.platform}-py{sys.version_info.major}.{sys.version_info.minor}"


def expected_hash(manifest, name):
    """The pinned hash (string = every environment; mapping = per platform and Python version, D-60)."""
    pinned = manifest["expected_report_sha256"][name]
    if isinstance(pinned, str):
        return pinned
    return pinned.get(environment_key())


def assert_pinned(manifest, name, sha):
    pinned = expected_hash(manifest, name)
    if pinned is None:
        pytest.skip(
            f"{name}: no pinned report hash for {environment_key()}; this environment produced {sha} "
            "(record it in the fixture manifest, D-60)"
        )
    assert sha == pinned, (
        f"{name} on {environment_key()}: report hash {sha} differs from the pinned {pinned}"
    )


def _tool():
    spec = importlib.util.spec_from_file_location(
        "run_family_tool", REPO_ROOT / "tools" / "research" / "run_family.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- refusals (fail closed)
def test_only_development_and_validation_partitions_are_accepted(manifest, panel_path) -> None:
    for bad in ("test_b", "confirmation_c", "sealed", "development ", ""):
        with pytest.raises(PartitionRefused):
            check_partition(bad, manifest, panel_path, identity="iap-dev")
    assert (
        check_partition("development", manifest, panel_path, identity="iap-dev")["partition"]
        == "development"
    )
    assert (
        check_partition("validation", manifest, panel_path, identity="iap-dev")["range"]
        == manifest["partitions"]["validation"]
    )


@pytest.mark.parametrize(
    "who", ["iap-runner", "iap-sealed-a", "iap-sealed-b", "iap-sealed-c", "iap-sealed-x"]
)
def test_the_runner_and_the_sealed_identities_are_refused(manifest, panel_path, who) -> None:
    with pytest.raises(PartitionRefused, match="development identity"):
        check_partition("development", manifest, panel_path, identity=who)


@pytest.mark.parametrize(
    "path",
    [r"C:\iap-sealed\b\daily\panel.csv", "/iap-sealed/c/panel.csv", r"C:\IAP-SEALED\a\x.csv"],
)
def test_a_data_path_under_a_sealed_root_is_refused(manifest, path) -> None:
    with pytest.raises(PartitionRefused, match="sealed partition root"):
        check_partition("development", manifest, path, identity="iap-dev")


def test_a_manifest_that_holds_a_sealed_partition_is_never_opened(manifest, panel_path) -> None:
    m = json.loads(json.dumps(manifest))
    m["partitions"]["test_b"] = ["2017-01-01", "2017-12-31"]
    with pytest.raises(PartitionRefused, match="never opened here"):
        check_partition("development", m, panel_path, identity="iap-dev")
    m = json.loads(json.dumps(manifest))
    m["sealed"] = True
    with pytest.raises(PartitionRefused, match="sealed"):
        check_partition("development", m, panel_path, identity="iap-dev")
    m = json.loads(json.dumps(manifest))
    del m["partitions"]["validation"]
    with pytest.raises(PartitionRefused, match="no 'validation' partition"):
        check_partition("validation", m, panel_path, identity="iap-dev")


def test_a_panel_whose_hash_differs_from_the_manifest_is_refused(manifest, panel_path) -> None:
    with pytest.raises(PartitionRefused, match="provenance"):
        check_partition(
            "development", manifest, panel_path, identity="iap-dev", data_sha256="0" * 64
        )


def test_a_family_whose_gate_forbids_a_fit_has_no_development_run(
    panel_path, manifest_path, tmp_path
) -> None:
    for spec in (D5, H2):
        with pytest.raises(PartitionRefused, match="forbids a fit"):
            run_family(
                spec, panel_path, manifest_path, identity="iap-dev", ledger_path=tmp_path / "l.json"
            )
    assert not (tmp_path / "l.json").exists()


def test_a_route_outside_the_specification_is_refused(panel_path, manifest, tmp_path) -> None:
    m = dict(manifest)
    m["route"] = (
        "daily_pit"  # the fixture is synthetic; a spec that excluded synthetic would refuse it
    )
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(m))
    text = D1.read_text(encoding="utf-8").replace(
        '"routes": ["daily_pit", "synthetic"]', '"routes": ["daily_pit"]'
    )
    sp = tmp_path / "D1_daily_only.md"
    sp.write_text(text, encoding="utf-8")
    with pytest.raises(PartitionRefused, match="route"):
        run_family(
            sp,
            panel_path,
            manifest_path=manifest_path_of(manifest),
            identity="iap-dev",
            ledger_path=tmp_path / "l.json",
        )


def manifest_path_of(manifest):
    import daily_panel

    return daily_panel.MANIFEST


# --------------------------------------------------------------------------- the fixture run
@pytest.fixture(scope="module")
def d1_development(panel_path, manifest_path, tmp_path_factory):
    out = tmp_path_factory.mktemp("d1")
    return run_family(
        D1,
        panel_path,
        manifest_path,
        partition="development",
        out_dir=out,
        ledger_path=out / "experiments.json",
        identity="iap-dev",
    )


def test_d1_development_run_reproduces_the_committed_report_hash(d1_development, manifest) -> None:
    assert_pinned(manifest, "d1_development", d1_development["sha256"])
    rep = d1_development["report"]
    assert report_hash(rep) == d1_development["sha256"]
    # the written report is the canonical JSON whose hash is recorded
    written = json.loads(open(d1_development["paths"]["report"], encoding="ascii").read())
    assert canonical_json(written) == canonical_json(rep)


def test_the_report_is_an_e5_class_report(d1_development) -> None:
    rep = d1_development["report"]
    assert set(rep["blocks"]) == {"A", "B", "C", "D", "E"}
    for b in rep["blocks"].values():
        assert b["units"] and "counts" in b and "uncertainty" in b
    assert (
        rep["sessions"]["counts"]["traded"] + rep["sessions"]["counts"]["usable_no_trades"]
        == rep["sessions"]["trading"]
    )
    assert (
        rep["economic_screen"]["column"] == "conservative"
        and rep["economic_screen"]["denominator"] == "registered capital C"
    )
    assert rep["decision_tree"]["branch"] in range(1, 7)
    assert rep["decision_tree"]["verdict"] in {
        "economically viable",
        "not viable",
        "insufficient evidence",
    }
    assert "synthetic" in rep["evidence_class"] and "no claim" in rep["evidence_class"]
    assert (
        rep["identity"]["partition"] == "development"
        and rep["identity"]["cost_column"] == "conservative"
    )
    assert rep["identity"][
        "eligibility_overrides"
    ]  # the fixture-size parameters are declared, not hidden
    assert "identity" not in rep["identity"]  # the OS user name is not in the report
    assert rep["model"]["class"] == "M0.pooled_linear" and len(rep["model"]["registry_id"]) == 64
    assert len(rep["r_d_bps"]) == rep["sessions"]["trading"]


def test_the_run_is_an_analysis_event_with_outcome_exposure_in_the_ledger(d1_development) -> None:
    led = d1_development["ledger"]
    e = ExperimentLedger(led["path"])
    assert e.total_experiments == 1 and len(e.entries) == 1
    entry = e.entries[0]
    assert entry["kind"] == LEDGER_KIND and entry["alpha_id"] == "d1"
    assert entry["data_family"] == "synthetic"
    assert entry["result"]["outcome_exposure"] is True
    assert entry["result"]["report_sha256"] == d1_development["sha256"]
    assert entry["config"]["spec_sha256"] == load_family_spec(D1).sha256
    assert entry["config"]["partition"] == "development"
    assert e.chain_head == led["chain_head"]


def test_a_second_identical_run_is_the_same_look_not_a_new_one(
    d1_development, panel_path, manifest_path
) -> None:
    led_path = d1_development["ledger"]["path"]
    again = run_family(
        D1,
        panel_path,
        manifest_path,
        partition="development",
        ledger_path=led_path,
        identity="iap-dev",
    )
    assert again["sha256"] == d1_development["sha256"]  # deterministic
    e = ExperimentLedger(led_path)
    assert e.total_experiments == 1 and len(e.entries) == 1  # de-duplicated by identity


def test_validation_run_is_a_different_look_with_its_own_hash(
    panel_path, manifest_path, manifest, tmp_path
) -> None:
    r = run_family(
        D1,
        panel_path,
        manifest_path,
        partition="validation",
        ledger_path=tmp_path / "l.json",
        identity="iap-dev",
    )
    assert_pinned(manifest, "d1_validation", r["sha256"])
    assert r["report"]["identity"]["partition"] == "validation"
    assert (
        r["report"]["identity"]["estimation_window"][1] <= manifest["partitions"]["development"][1]
    )
    assert r["report"]["identity"]["trading_window"][0] >= manifest["partitions"]["validation"][0]
    e = ExperimentLedger(tmp_path / "l.json")
    assert e.entries[0]["config"]["partition"] == "validation"


def test_d3_exits_at_the_next_open_and_reproduces_its_hash(
    panel_path, manifest_path, manifest, tmp_path
) -> None:
    r = run_family(
        D3,
        panel_path,
        manifest_path,
        partition="development",
        ledger_path=tmp_path / "l.json",
        identity="iap-dev",
    )
    assert_pinned(manifest, "d3_development", r["sha256"])
    rows = r["run"].research_ledger
    assert rows and all(row["horizon"] == "c2o" for row in rows)
    # every cohort expires at an open (09:30) of the following session, not at a close
    opens = {row["expiry_ts"] % 86_400_000_000_000 for row in rows}
    assert opens == {(9 * 3600 + 30 * 60) * 1_000_000_000}


# --------------------------------------------------------------------------- the tool
def test_tool_refuses_a_sealed_partition_with_exit_code_2(
    panel_path, manifest_path, tmp_path, capsys
) -> None:
    tool = _tool()
    rc = tool.main(
        [
            "--spec",
            str(D1),
            "--data",
            str(panel_path),
            "--manifest",
            str(manifest_path),
            "--partition",
            "test_b",
            "--ledger",
            str(tmp_path / "l.json"),
            "--identity",
            "iap-dev",
        ]
    )
    assert rc == 2 and "refused" in capsys.readouterr().err
    rc = tool.main(
        [
            "--spec",
            str(D1),
            "--data",
            str(panel_path),
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(tmp_path / "l.json"),
            "--identity",
            "iap-runner",
        ]
    )
    assert rc == 2
    rc = tool.main(
        [
            "--spec",
            str(D5),
            "--data",
            str(panel_path),
            "--manifest",
            str(manifest_path),
            "--ledger",
            str(tmp_path / "l.json"),
            "--identity",
            "iap-dev",
        ]
    )
    assert rc == 2 and "forbids a fit" in capsys.readouterr().err
    assert not (tmp_path / "l.json").exists()


def test_tool_records_a_run_and_prints_the_hash(
    panel_path, manifest_path, manifest, tmp_path, capsys
) -> None:
    tool = _tool()
    rc = tool.main(
        [
            "--spec",
            str(D1),
            "--data",
            str(panel_path),
            "--manifest",
            str(manifest_path),
            "--out",
            str(tmp_path / "out"),
            "--ledger",
            str(tmp_path / "l.json"),
            "--identity",
            "iap-dev",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert_pinned(manifest, "d1_development", out["report_sha256"])
    assert out["ledger"]["total_experiments"] == 1
    assert (tmp_path / "out" / "d1_development_report.json").exists()
    assert tool.DEFAULT_LEDGER.name == "experiments.json"

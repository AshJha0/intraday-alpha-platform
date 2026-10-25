"""U6 M1 demonstration on the synthetic fixture: the whole protocol mechanism runs, is ledgered and is labelled synthetic."""

from __future__ import annotations

import json

import pytest
from iap.alpha.registry import ForecastRegistry
from iap.contracts.versions import canonical_json
from iap.ml import load_protocol
from iap.ml.pipeline import run_m1_demo
from iap.ml.protocol import FROZEN_SHA256, ProtocolError
from iap.ml.trials import TrialLedger
from iap.research.loop import PartitionRefused
from iap.validation.ledger import ExperimentLedger
from stage8_fixtures import FAMILIES, manifest, manifest_path, panel_path  # noqa: F401

D1 = FAMILIES / "D1_short_term_reversal.md"


@pytest.fixture(scope="module")
def demo(panel_path, manifest_path, tmp_path_factory):
    out = tmp_path_factory.mktemp("m1")
    rep = run_m1_demo(
        D1,
        panel_path,
        manifest_path,
        out_dir=out,
        identity="iap-dev",
        selection_ledger=out / "experiments.json",
    )
    return rep, out


def test_the_demo_runs_the_protocol_end_to_end(demo) -> None:
    rep, out = demo
    proto = load_protocol()
    assert rep["protocol_sha256"] == FROZEN_SHA256 and rep["protocol_revision"] == 1
    assert rep["placeholders_filled"] is False
    assert "synthetic" in rep["evidence_class"] and "no claim" in rep["evidence_class"]
    assert rep["n_fits"] == proto["tiers"]["M1"]["budget"] == len(rep["candidates"]) == 5
    assert rep["incumbent"]["model_class"] == "M0.pooled_linear"
    assert rep["sessions_compared"] > 100
    assert rep["block_length"]["block_length"] >= 1 and rep["delta"]["delta_bps"] == pytest.approx(
        proto["delta"]["value_bps"], abs=0.005
    )
    for c in rep["candidates"]:
        adm = c["admission"]
        assert (
            adm["sessions"] == rep["sessions_compared"]
            and adm["block_length"] == rep["block_length"]["block_length"]
        )
        assert adm["delta_bps"] == pytest.approx(rep["delta"]["delta_bps"], abs=1e-6)
        assert adm["admitted"] == (adm["ci_lower_bps"] > adm["delta_bps"])
        assert c["admitted"] == (adm["admitted"] and c["holm_adjusted_p"] <= 0.05)
        assert len(c["registry_id"]) == 64 and len(c["report_sha256"]) == 64
    carried = rep["decision"]["carried"]
    admitted = [c for c in rep["candidates"] if c["admitted"]]
    if admitted:
        best = min(admitted, key=lambda c: (c["model_class"], c["complexity"], c["grid_index"]))
        assert carried == best["name"] and rep["decision"]["advanced"]
    else:
        assert carried.startswith("M0") and not rep["decision"]["advanced"]


def test_the_trial_ledger_has_the_freeze_first_then_block_length_and_delta_before_any_fit(
    demo,
) -> None:
    _rep, out = demo
    led = TrialLedger(out / "trial_ledger.json")
    kinds = [e["kind"] for e in led.entries]
    assert kinds[0] == "protocol_freeze" and led.entries[0]["protocol_sha256"] == FROZEN_SHA256
    first_fit = kinds.index("fit")
    assert "block_length_fixed" in kinds[:first_fit] and "delta_derived" in kinds[:first_fit]
    assert kinds.count("fit") == 5 and kinds.count("evaluation") == 5 and kinds[-1] == "decision"
    assert all(e["protocol_sha256"] == FROZEN_SHA256 for e in led.entries if e["kind"] == "fit")
    assert all(
        e["split"]["embargo"] >= e["split"]["horizon"] for e in led.entries if e["kind"] == "fit"
    )


def test_every_candidate_is_registered_with_lineage_and_selection_history(demo) -> None:
    rep, out = demo
    reg = ForecastRegistry.load(out / "forecast_registry.json")
    reg.verify()
    assert len(reg) == 5
    for e in reg:
        assert e.family_id == "d1" and e.horizon == "3d" and e.approval == "development"
        assert e.selection_history["kind"] == "candidate configuration"
        assert e.selection_history["protocol_sha256"] == FROZEN_SHA256
        assert len(e.feature_set_hash) == 64 and e.software_version == "iap v2.0 stage 8"
    assert {c["registry_id"] for c in rep["candidates"]} == set(reg.ids())


def test_runs_are_recorded_in_the_selection_ledger_as_analysis_events(demo) -> None:
    _rep, out = demo
    led = ExperimentLedger(out / "experiments.json")
    kinds = {e["kind"] for e in led.entries}
    assert kinds == {"analysis_event"}
    assert all(e["result"]["outcome_exposure"] for e in led.entries)
    # incumbent development + validation, five candidate validation runs, the admission decision
    assert led.total_experiments == 8


def test_the_report_is_canonical_and_reproducible(demo) -> None:
    rep, out = demo
    written = json.loads((out / "admission_report.json").read_text(encoding="ascii"))
    assert canonical_json(written) == canonical_json(rep)


def test_an_unfrozen_protocol_or_a_sealed_identity_never_fits(
    panel_path, manifest_path, tmp_path
) -> None:
    with pytest.raises(ProtocolError, match="new revision"):
        run_m1_demo(
            D1,
            panel_path,
            manifest_path,
            out_dir=tmp_path / "a",
            identity="iap-dev",
            expected_protocol_sha256="0" * 64,
        )
    with pytest.raises(PartitionRefused):
        run_m1_demo(D1, panel_path, manifest_path, out_dir=tmp_path / "b", identity="iap-runner")


def test_a_real_route_is_refused_while_the_placeholders_are_unfilled(
    panel_path, manifest, tmp_path
) -> None:
    m = dict(manifest)
    m["route"] = "daily_pit"
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(m))
    with pytest.raises(ProtocolError, match="placeholders"):
        run_m1_demo(D1, panel_path, mp, out_dir=tmp_path / "c", identity="iap-dev")

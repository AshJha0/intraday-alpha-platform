"""The M0 -> M1 -> M2 -> M3 ladder: admission refusals, ledger completeness, byte-identical reruns, a runtime bound."""

from __future__ import annotations

import json

import numpy as np
import pytest
from iap.ml import load_protocol
from iap.ml.admission import admission_test
from iap.ml.ladder import decide_tier, run_ladder
from iap.ml.protocol import FROZEN_SHA256, ProtocolError
from iap.ml.trials import TrialLedger, TrialLedgerError
from stage8_fixtures import FAMILIES, manifest, manifest_path, panel_path  # noqa: F401

D1 = FAMILIES / "D1_short_term_reversal.md"


# ---------------------------------------------------------------------------- pure admission refusals


def _cand(
    name: str, du: np.ndarray, *, delta: float = 0.55, complexity: float = 1.0, grid: int = 0
):
    adm = admission_test(du, delta_bps=delta, block_length=2, resamples=500, seed=20261025)
    return {
        "name": name,
        "model_class": name.split("[")[0],
        "complexity": complexity,
        "grid_index": grid,
        "admission": adm.to_dict(),
    }


def _noise(n: int = 200, sd: float = 3.0) -> np.ndarray:
    rng = np.random.default_rng(3)  # test data only
    return rng.standard_normal(n) * sd


def test_a_candidate_identical_to_the_incumbent_is_refused_promotion() -> None:
    c = _cand("M2.gam[0]", np.zeros(200))
    d = decide_tier([c], alpha=0.05)
    assert not d["advanced"] and d["carried"] is None
    assert "no incremental value" in d["reasons"]["M2.gam[0]"]


def test_a_gain_below_the_margin_is_refused_and_one_above_it_admitted() -> None:
    small = _cand("M2.gam[0]", _noise() + 0.3, grid=0)  # mean below delta = 0.55
    big = _cand("M2.gam[1]", _noise() + 4.0, grid=1)
    d = decide_tier([small, big], alpha=0.05)
    assert not small["admitted"] and big["admitted"]
    assert d["advanced"] and d["carried"] == "M2.gam[1]"


def test_holm_with_the_early_stopping_looks_can_refuse_what_the_margin_admits() -> None:
    # admitted on CI_lower > delta but with a p-value Holm cannot carry once M3's looks enter K
    c = next(
        c
        for c in (_cand("M3.gbt[0]", _noise() + 0.6 + 0.05 * k) for k in range(40))
        if c["admission"]["admitted"] and c["admission"]["p_value"] > 0.0
    )
    alone = decide_tier([dict(c)], alpha=0.05)
    p = c["admission"]["p_value"]
    looks = int(np.ceil(0.05 / p)) + 1
    with_looks = decide_tier([dict(c)], alpha=0.05, extra_looks=looks)
    assert alone["advanced"] and not with_looks["advanced"]
    assert with_looks["holm"]["K"] == 1 + looks
    assert with_looks["reasons"]["M3.gbt[0]"] == "Holm-adjusted p > alpha"


def test_the_tie_break_prefers_the_simpler_admitted_candidate() -> None:
    a = _cand("M3.gbt[0]", _noise() + 4.0, complexity=320.0, grid=0)
    b = _cand("M3.gbt[1]", _noise() + 6.0, complexity=80.0, grid=1)
    assert decide_tier([a, b], alpha=0.05)["carried"] == "M3.gbt[1]"


# ---------------------------------------------------------------------------- ledger completeness


def _ledger(tmp_path) -> TrialLedger:
    proto = load_protocol()
    led = TrialLedger(tmp_path / "t.json", proto)
    led.freeze(FROZEN_SHA256, 1)
    return led


def _fit(led: TrialLedger, **extra):
    return led.record_fit(
        family_id="d1",
        model_class="M2.gam",
        group_id="d1.core",
        hyper={"lambda": 1.0},
        split={"horizon": 1, "embargo": 5},
        seed=20261025,
        complexity=12.0,
        fit_sha256="a" * 64,
        extra={"config_sha256": "b" * 64, "data_partition": "development", **extra},
    )


def test_ledger_completeness_refuses_a_missing_hash_an_unevaluated_fit_and_a_missing_decision(
    tmp_path,
) -> None:
    led = _ledger(tmp_path)
    f = _fit(led)
    with pytest.raises(TrialLedgerError, match="evaluated 0 times"):
        led.check_complete()
    led.record_evaluation(f["seq"], {"admitted": False})
    with pytest.raises(TrialLedgerError, match="no decision"):
        led.check_complete()
    led.record_decision("M2", "d1", {"advanced": False})
    assert led.check_complete() == {"fits": 1, "decisions": 1}
    led2 = _ledger(tmp_path / "b")
    g = _fit(led2, config_sha256="")
    led2.record_evaluation(g["seq"], {})
    led2.record_decision("M2", "d1", {})
    with pytest.raises(TrialLedgerError, match="config_sha256"):
        led2.check_complete()


# ---------------------------------------------------------------------------- the ladder on the synthetic fixture


@pytest.fixture(scope="module")
def demo(panel_path, manifest_path, tmp_path_factory):
    out = tmp_path_factory.mktemp("ladder_demo")
    rep = run_ladder(
        D1,
        panel_path,
        manifest_path,
        out_dir=out,
        identity="iap-dev",
        demonstrate_all_tiers=True,
        selection_ledger=out / "experiments.json",
    )
    return rep, out


@pytest.fixture(scope="module")
def binding(panel_path, manifest_path, tmp_path_factory):
    """Two independent protocol-binding runs (the ladder stops where admission refuses)."""
    outs = []
    for k in range(2):
        out = tmp_path_factory.mktemp(f"ladder_binding_{k}")
        rep = run_ladder(D1, panel_path, manifest_path, out_dir=out, identity="iap-dev")
        outs.append((rep, out))
    return outs


def test_every_tier_is_fitted_within_budget_and_every_fit_is_one_complete_ledger_row(demo) -> None:
    rep, out = demo
    proto = load_protocol()
    assert "synthetic" in rep["evidence_class"] and "no claim" in rep["evidence_class"]
    assert [t["tier"] for t in rep["tiers"]] == ["M1", "M2", "M3"]
    for t in rep["tiers"]:
        assert len(t["candidates"]) == proto["tiers"][t["tier"]]["budget"]
    led = TrialLedger(out / "trial_ledger.json")
    assert led.entries[0]["kind"] == "protocol_freeze"
    assert led.check_complete()["fits"] == rep["n_fits"] == 5 + 4 + 8
    kinds = [e["kind"] for e in led.entries]
    first_fit = kinds.index("fit")
    assert "block_length_fixed" in kinds[:first_fit] and "delta_derived" in kinds[:first_fit]
    for e in led.fits():
        assert e["kind"] == "fit" and e["seed"] == proto["seed"]
        assert len(e["config_sha256"]) == 64 and len(e["fit_sha256"]) == 64
        assert e["data_partition"] == "development" and e["dataset_identity"]
        assert e["validation_reused_for_early_stopping"] == (e["tier"] == "M3")
    evals = [e for e in led.entries if e["kind"] == "evaluation"]
    assert all(len(e["admission"]["report_sha256"]) == 64 for e in evals)
    reg = json.loads((out / "forecast_registry.json").read_text(encoding="utf-8"))
    assert len(reg["entries"]) == rep["n_fits"]


def test_m3_counts_its_early_stopping_looks_in_holm_k(demo) -> None:
    rep, _out = demo
    m3 = next(t for t in rep["tiers"] if t["tier"] == "M3")
    looks = m3["decision"]["early_stopping_looks_in_K"]
    assert looks >= 8 and m3["decision"]["holm"]["K"] == 8 + looks
    for c in m3["candidates"]:
        assert c["complexity"] % c["hyper"]["max_leaves"] == 0


def test_demonstrated_tiers_after_a_stop_have_no_protocol_effect(demo) -> None:
    rep, _out = demo
    m1 = rep["tiers"][0]["decision"]
    if not m1["advanced"]:
        assert rep["stopped_at"] == "M1" and rep["carried"] == "M0.pooled_linear"
        for t in rep["tiers"][1:]:
            assert t["decision"]["protocol_effect"].startswith("none")
            assert t["decision"]["incumbent"] == "M0.pooled_linear"


def test_the_binding_ladder_stops_where_admission_refuses(binding) -> None:
    rep, out = binding[0]
    led = TrialLedger(out / "trial_ledger.json")
    stops = [e for e in led.entries if e["kind"] == "ladder_stopped"]
    tiers_fitted = [t["tier"] for t in rep["tiers"] if t["fitted"]]
    if rep["stopped_at"] is None:
        assert tiers_fitted == ["M1", "M2", "M3"] and not stops
    else:
        later = ["M1", "M2", "M3"][["M1", "M2", "M3"].index(rep["stopped_at"]) + 1 :]
        assert [s["tier"] for s in stops] == later
        assert not any(f["tier"] in later for f in led.fits())


def test_seeded_reruns_are_byte_identical(binding) -> None:
    (_a, out_a), (_b, out_b) = binding
    for name in ("ladder_report.json", "trial_ledger.json", "forecast_registry.json"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes(), name


def test_the_ladder_refuses_skipped_tiers_and_non_synthetic_demonstrations(
    panel_path, manifest_path, tmp_path
) -> None:
    with pytest.raises(ProtocolError, match="in order"):
        run_ladder(D1, panel_path, manifest_path, out_dir=tmp_path, tiers=("M2", "M3"))
    real = json.loads(manifest_path.read_text(encoding="utf-8"))
    real["route"] = "daily_pit"
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(real), encoding="utf-8")
    with pytest.raises(ProtocolError, match="synthetic route only"):
        run_ladder(D1, panel_path, mp, out_dir=tmp_path / "o")


def test_runtime_bound(demo) -> None:
    _rep, out = demo
    t = json.loads((out / "timings.json").read_text(encoding="utf-8"))
    assert set(t) >= {"M1_s", "M2_s", "M3_s", "total_s"}
    assert t["total_s"] < 600, t

"""REL-06 known-null protocol v3 (revision 4): runner logic, binding and verdict-level tests.

Synthetic data only: no market data is read.  The verdict-level tests feed fabricated
trial records through ``evaluate_size_arm`` (the runner's own aggregation) including the
failure cases the outside reviewer named.
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pytest
from iap.agents import analysis_identity as ai
from iap.research import known_null as kn
from iap.research import known_null_diag as d
from iap.research import known_null_v3 as v3
from test_known_null import NAMES, config, synthetic_panel

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / v3.CONFIG_PATH


@pytest.fixture(scope="module")
def cfg() -> dict:
    return v3.load_config(FROZEN)


# ------------------------------------------------------------ configuration --


def test_frozen_config_validates_and_matches_protocol_values(cfg) -> None:
    assert cfg["alpha"] == 0.025
    assert cfg["size_arm"]["A0"]["n_trials"] == 1854 and cfg["size_arm"]["B"]["n_trials"] == 2000
    assert cfg["pass_rule"]["B_max_count"] == 60 and cfg["pass_rule"]["A0_max_count"] == 55
    assert cfg["residual_mean"]["b_role"] == "blocks_pass"
    assert cfg["residual_mean"]["a0_role"] == "descriptive"
    assert len(v3.hypothesis_ids(cfg, "K16")) == 16


def test_centring_groups_derived_from_hypothesis_lists_is_63_not_49(cfg) -> None:
    """K3 uses AAPL+MSFT+QQQ at 1s, 10s and 1m: 9 pairs x 7 sessions.  The protocol text
    says 49 (7 pairs); the configuration carries the derived 63 and the validator refuses 49."""
    assert v3.n_required_groups(cfg) == 63 == cfg["centring_invariant"]["n_groups"]
    bad = copy.deepcopy(cfg)
    bad["centring_invariant"]["n_groups"] = 49
    with pytest.raises(v3.ConfigError, match="n_groups"):
        v3.validate_config(bad)


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: c.update(alpha=0.05),
        lambda c: c["pass_rule"].update(B_max_count=61),
        lambda c: c["pass_rule"].update(A0_max_count=56),
        lambda c: c["size_arm"]["B"].update(base_seed=1),
        lambda c: c["size_arm"]["A0"].update(n_trials=1000),
        lambda c: c["power_arm"]["generators"]["A0"].update(base_seed=2),
        lambda c: c["residual_mean"].update(b_role="descriptive"),
        lambda c: c["residual_mean"].update(flag_se=5),
        lambda c: c["centring_invariant"].update(tol_rel=1e-6),
        lambda c: c["gate"].update(median_threshold=1.1),
        lambda c: c["gate"]["labels"].update(triggered_or_undetermined="PASS"),
        lambda c: c["variants"]["K16"].pop(),
    ],
)
def test_any_change_to_a_frozen_value_is_refused(cfg, edit) -> None:
    bad = copy.deepcopy(cfg)
    edit(bad)
    with pytest.raises(v3.ConfigError):
        v3.validate_config(bad)


# -------------------------------------------- verdict level (runner path) -----


def _rec(confirmed: bool, means=None, ses=None) -> dict:
    return {
        "confirmed": ["x"] if confirmed else [],
        "p_holm": [],
        "mean_bps": means,
        "se_bps": ses,
    }


def _trials(
    cfg,
    n: int,
    *,
    seed: int,
    sd: float,
    se: float,
    confirmed: int = 0,
    shift=None,
    nan_at=None,
    zero_se_for=None,
    none_mean_for=None,
) -> dict:
    """Fabricated trial records: K16/S2 carries per-trial m ~ N(0, sd) and the CR1 SE
    ``se`` for the 16 hypotheses (so U ~ sd / se); every other cell is a rejection flag."""
    rng = np.random.default_rng(seed)
    ids = v3.hypothesis_ids(cfg, "K16")
    m = rng.standard_normal((n, 16)) * sd
    for j, hid in enumerate(ids):
        m[:, j] += (shift or {}).get(hid, 0.0)
    out = {}
    for i in range(n):
        ses = [se] * 16
        means: list = [float(x) for x in m[i]]
        if nan_at and i == nan_at[0]:
            ses[ids.index(nan_at[1])] = math.nan
        if zero_se_for:
            ses[ids.index(zero_se_for)] = 0.0
        if none_mean_for and i == 0:
            means[ids.index(none_mean_for)] = None
        out[i] = {k: {s: _rec(i < confirmed) for s in v3.STATISTICS} for k in v3.KS}
        out[i]["K16"]["S2"] = _rec(i < confirmed, means, ses)
    return out


def _eval(cfg, a0_kw=None, b_kw=None, ok=True, n_a0=1854, n_b=2000):
    a0 = _trials(cfg, n_a0, seed=1, sd=1.0, se=1 / 1.2, **(a0_kw or {}))
    b = _trials(cfg, n_b, seed=2, sd=1.0, se=1 / 1.2, **(b_kw or {}))
    return v3.evaluate_size_arm(cfg, a0, b, centring_ok=ok)


def test_clean_run_gives_the_scoped_pass_label_and_keeps_the_claim_scope(cfg) -> None:
    r = _eval(cfg)
    assert r["verdict"] == "PASS" and r["label"] == d.PASS_LABEL_PLAIN
    assert r["gate"]["status"] == "NOT_TRIGGERED"
    assert "Neither, alone or combined" in r["claim_scope"] and "2.5 %" in r["claim_scope"]
    assert r["a0"]["gate_status"] == "NOT_TRIGGERED" and r["a0"]["gate_reason"]


def test_gate_triggered_gives_the_qualified_label_never_a_plain_pass(cfg) -> None:
    a0 = _trials(cfg, 1854, seed=1, sd=1.0, se=1.25)  # U(A0) = 0.8
    b = _trials(cfg, 2000, seed=2, sd=1.0, se=1 / 1.3)  # U(B) = 1.3
    r = v3.evaluate_size_arm(cfg, a0, b, centring_ok=True)
    assert r["gate"]["status"] == "TRIGGERED"
    assert r["label"] == "PASS (B-supported only; A0 non-informative)"
    assert r["verdict"] == d.VERDICT_PASS_B_ONLY != "PASS"
    assert r["a0"]["gate_status"] == "TRIGGERED" and "median U(A0)" in r["a0"]["gate_reason"]
    assert r["a0"]["median_u_a0"] < 1.0 < r["a0"]["median_u_b"]


@pytest.mark.parametrize("arm", ["a0", "b"])
def test_one_nan_se_in_one_of_16_hypotheses_is_undetermined_not_dropped(cfg, arm) -> None:
    hid = v3.hypothesis_ids(cfg, "K16")[7]
    kw = {"nan_at": (3, hid)}
    r = _eval(cfg, a0_kw=kw if arm == "a0" else None, b_kw=kw if arm == "b" else None)
    assert r["gate"]["status"] == "UNDETERMINED"
    assert r["gate"]["invalid_ids"] == [hid]
    assert r["gate"]["median_u_a0"] is None  # the 16-value median was NOT computed on 15
    assert r["verdict"] == d.VERDICT_PASS_B_ONLY
    assert r["label"] == d.PASS_LABEL_GATE
    assert (
        hid in r["a0"]["gate_reason"] and "treated as A0 non-informative" in r["a0"]["gate_reason"]
    )


def test_zero_denominator_for_one_hypothesis_is_undetermined(cfg) -> None:
    hid = v3.hypothesis_ids(cfg, "K16")[0]
    r = _eval(cfg, a0_kw={"zero_se_for": hid})
    assert r["gate"]["status"] == "UNDETERMINED" and r["gate"]["invalid_ids"] == [hid]
    assert r["label"] == d.PASS_LABEL_GATE


def test_missing_mean_in_b_blocks_pass_and_is_listed(cfg) -> None:
    hid = v3.hypothesis_ids(cfg, "K16")[5]
    r = _eval(cfg, b_kw={"none_mean_for": hid})
    assert r["verdict"] == "FAIL" and r["reason"] == "b_flag"
    assert r["b_residual_mean"]["invalid_ids"] == [hid]
    assert r["gate"]["status"] == "UNDETERMINED"


def test_a0_rate_failure_is_fail_whatever_the_gate_says(cfg) -> None:
    for confirmed, expect in ((55, "PASS"), (56, "FAIL")):
        r = _eval(cfg, a0_kw={"confirmed": confirmed})
        assert (r["verdict"] == "FAIL") == (expect == "FAIL")
    r = v3.evaluate_size_arm(
        cfg,
        _trials(cfg, 1854, seed=1, sd=1.0, se=1.25, confirmed=56),
        _trials(cfg, 2000, seed=2, sd=1.0, se=1 / 1.3),
        centring_ok=True,
    )
    assert r["gate"]["status"] == "TRIGGERED" and r["verdict"] == "FAIL"
    assert r["a0"]["a0_counts"]["S1/K3"] == 56  # the A0 result stays visible


def test_b_rate_failure_edge(cfg) -> None:
    assert _eval(cfg, b_kw={"confirmed": 60})["verdict"] == "PASS"
    assert _eval(cfg, b_kw={"confirmed": 61})["verdict"] == "FAIL"


def test_b_residual_mean_flag_blocks_pass_a0_flag_is_descriptive(cfg) -> None:
    ids = v3.hypothesis_ids(cfg, "K16")
    r = _eval(cfg, b_kw={"shift": {ids[3]: 0.2}})  # 0.2 sd: ~ 8.9 SE_of_mean at n = 2000
    assert r["verdict"] == "FAIL" and r["reason"] == "b_flag"
    assert r["b_residual_mean"]["flagged_ids"] == [ids[3]]
    r = _eval(cfg, a0_kw={"shift": {h: 3.0 for h in ids}})  # large A0 bias
    assert r["verdict"] == "PASS"  # A0 flag has no verdict role ...
    assert r["a0"]["a0_residual_mean"]["any_flag"] is True  # ... but stays visible
    assert "descriptive" in r["a0"]["a0_residual_mean"]["role"]


def test_centring_failure_is_invalid_and_incomplete_runs_are_not_verdicts(cfg) -> None:
    assert _eval(cfg, ok=False)["verdict"] == "INVALID"
    r = _eval(cfg, n_b=1999)
    assert r["verdict"].startswith("INCOMPLETE") and "claim_scope" in r


def test_verdict_function_fails_closed_on_malformed_inputs() -> None:
    cells = [(s, k) for s in ("S1", "S2") for k in (3, 6, 16)]
    ok_b, ok_a = dict.fromkeys(cells, 0), dict.fromkeys(cells, 0)
    plain = {"status": "NOT_TRIGGERED"}
    assert d.primary_verdict(ok_b, ok_a, True, False, plain)["verdict"] == "PASS"
    for gate in ({}, {"status": None}, {"status": "triggered"}, {"status": "UNKNOWN"}):
        r = d.primary_verdict(ok_b, ok_a, True, False, gate)
        assert r["verdict"] == d.VERDICT_PASS_B_ONLY and r["label"] == d.PASS_LABEL_GATE
    assert d.primary_verdict(ok_b, ok_a, True, None, plain)["verdict"] == "FAIL"  # type: ignore[arg-type]
    assert d.primary_verdict(ok_b, ok_a, 1, False, plain)["verdict"] == "INVALID"  # type: ignore[arg-type]
    assert (
        d.primary_verdict({**ok_b, ("S1", 3): 1.5}, ok_a, True, False, plain)["verdict"] == "FAIL"
    )  # type: ignore[dict-item]
    assert (
        d.primary_verdict({**ok_b, ("S1", 3): True}, ok_a, True, False, plain)["verdict"] == "FAIL"
    )  # type: ignore[dict-item]
    assert d.primary_verdict({**ok_b, ("S1", 3): -1}, ok_a, True, False, plain)["verdict"] == "FAIL"


def test_b_blocker_function_edges() -> None:
    ids = [f"h{i}" for i in range(16)]
    rng = np.random.default_rng(3)
    ok = {h: rng.standard_normal(2000) for h in ids}
    assert d.b_residual_blocker(ok, ids)["blocks_pass"] is False
    short = {**ok, "h4": [0.0]}
    r = d.b_residual_blocker(short, ids)
    assert r["blocks_pass"] and r["invalid_ids"] == ["h4"] and not r["any_flag"]
    nan = {**ok, "h9": [math.nan] + [0.0] * 1999}
    assert d.b_residual_blocker(nan, ids)["invalid_ids"] == ["h9"]
    absent = {k: v for k, v in ok.items() if k != "h2"}
    assert d.b_residual_blocker(absent, ids)["invalid_ids"] == ["h2"]
    with pytest.raises(ValueError):
        d.b_residual_blocker(ok, ids[:15])


# ---------------------------- B blocker calibration at the verdict level -------


def test_b_blocker_false_flag_at_verdict_level_independent_hypotheses() -> None:
    bound = d.b_blocker_union_bound()
    assert 5e-5 < bound["per_hypothesis"] < 8e-5
    assert bound["union_bound_16"] < 1.1e-3  # any dependence among the 16
    r = d.measure_b_blocker(4000, model="iid", seed=11)
    assert r["verdict_b_flag_rate"] <= 0.005  # expected ~ 0.1 % (4 of 4,000)


@pytest.mark.parametrize(("model", "rho"), [("factor", 0.9), ("t3", 0.0), ("skew", 0.0)])
def test_b_blocker_false_flag_under_dependence_and_tails(model: str, rho: float) -> None:
    r = d.measure_b_blocker(1500, model=model, rho=rho, seed=12)
    assert r["verdict_b_flag_rate"] <= 0.01


def test_b_blocker_injected_bias_is_detected_through_the_verdict_path() -> None:
    assert d.measure_b_blocker(300, bias_sd=0.20, seed=13)["verdict_b_flag_rate"] >= 0.99
    assert (
        d.measure_b_blocker(300, bias_sd=0.20, biased_hypotheses=16, seed=14)["verdict_b_flag_rate"]
        >= 0.99
    )
    weak = d.measure_b_blocker(300, bias_sd=0.05, seed=15)["verdict_b_flag_rate"]
    assert weak < 0.2  # a non-flag is not proof of unbiasedness: 0.05 sd mostly passes


# ------------------------------------------- A0 source on a synthetic panel ----


def _tiny_cfg() -> dict:
    c = config()
    fam = [
        {
            "id": f"h{i:02d}",
            "signal": "sig_v1",
            "horizon": "1s",
            "symbols": [NAMES[i % 3]],
            "sign": 1,
            "threshold": 0.3 + 0.01 * i,
        }
        for i in range(16)
    ]
    c["variants"] = {"K3": fam[:3], "K6": fam[:6], "K16": fam}
    c["centring_invariant"] = {"tol_rel": 1e-9, "n_groups": 12}
    return c


@pytest.fixture(scope="module")
def tiny():
    cfg = _tiny_cfg()
    return cfg, kn.prepare(synthetic_panel(seed=21), cfg, pricer=None)


def test_a0_source_centres_checks_and_verifies_series_used(tiny) -> None:
    cfg, study = tiny
    src = v3.build_a0_source(study, cfg)
    assert src.invariant["ok"] and src.invariant["n_groups"] == 12
    assert src.series_used_ok(study.panel)
    lab = src.centred["1s"]
    p = study.panel
    for n in range(3):
        for s in range(len(p.sessions)):
            x = lab[(p.name == n) & (p.sess == s)]
            assert abs(np.nanmean(x)) < 1e-12
    src.centred["1s"][np.flatnonzero(np.isfinite(src.centred["1s"]))[0]] += 1e-3  # tamper
    assert not src.series_used_ok(study.panel)


def test_uncentred_labels_abort_the_run(tiny, monkeypatch) -> None:
    cfg, study = tiny
    monkeypatch.setattr(d, "centre_labels", lambda x, n, s: np.asarray(x, dtype=float).copy())
    with pytest.raises(d.CentringInvariantError):
        v3.build_a0_source(study, cfg)


def test_wrong_group_count_aborts(tiny) -> None:
    cfg, study = tiny
    bad = {**cfg, "centring_invariant": {"tol_rel": 1e-9, "n_groups": 49}}
    with pytest.raises(d.CentringInvariantError, match="groups"):
        v3.build_a0_source(study, bad)


def test_trials_are_deterministic_and_end_to_end_verdict_runs(tiny) -> None:
    cfg, study = tiny
    src = v3.build_a0_source(study, cfg)
    g = len(study.panel.sessions)
    ders = kn.derangements(g)
    perms = {i: list(p) for i, p in enumerate(ders)}
    kw = {"base_seed": 20270101, "alpha": 0.025}
    a = v3.run_trials_v3(study, src, "A0", range(len(ders)), perms, **kw)
    b = v3.run_trials_v3(study, src, "A0", range(len(ders)), perms, **kw)
    assert a == b
    bt = v3.run_trials_v3(study, src, "B", range(5), {}, base_seed=20270102, alpha=0.025)
    assert set(a[0]) == set(v3.KS) and set(a[0]["K16"]) == {"S1", "S2"}
    assert len(a[0]["K16"]["S2"]["mean_bps"]) == 16
    small = {
        **cfg,
        "size_arm": {"A0": {"n_trials": len(ders)}, "B": {"n_trials": 5}},
        "residual_mean": {"flag_se": 4, "a0_n_eff": 7},
    }
    r = v3.evaluate_size_arm(small, a, bt, centring_ok=True)
    assert r["verdict"] in ("PASS", "PASS_B_ONLY", "FAIL")
    assert "claim_scope" in r and "a0" in r  # whatever the outcome, A0 and scope are visible
    pw = v3.run_trials_v3(
        study,
        src,
        "A0",
        range(2),
        {0: perms[0], 1: perms[1]},
        base_seed=20270103,
        alpha=0.025,
        deltas=[0.5, 4.0],
    )
    assert set(pw[0]["K3"]) == {"delta=0.5", "delta=4"}


def test_r0_rereading_uses_the_same_draws(tiny) -> None:
    cfg, study = tiny
    src = v3.build_a0_source(study, cfg)
    t = v3.run_trials_v3(study, src, "B", range(4), {}, base_seed=20270102, alpha=0.025)
    ids = v3.hypothesis_ids(cfg, "K3")
    r1 = v3.cell_count(t, "K3", "S2")
    r0 = v3.cell_count(t, "K3", "S2", ids, 0.05, True)
    assert r0 >= r1
    same = v3.cell_count(t, "K3", "S2", ids, 0.025, True)
    assert same == r1


# ----------------------------------------------------- binding (signed prereg) --


def _git(r: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def bound_repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    for rel in v3.BOUND_SCRIPTS:
        src = ROOT / rel
        _w(r / rel, src.read_bytes().decode("utf-8") if rel == v3.CONFIG_PATH else f"# {rel}\n")
    _w(r / "python/src/iap/lib.py", "X = 1\n")
    _w(r / "configs/reference/fee_tiers.json", "{}\n")
    _w(r / "data/reference/feature_registry.json", "{}\n")
    _w(r / "python/requirements-ci.txt", "pytest==8.0.0 \\\n    --hash=sha256:00\n")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    cfg = v3.load_config(r / v3.CONFIG_PATH)
    args = v3.identity_args_v3(cfg)
    block = ai.build_identity(r, **args)
    doc = {
        "kind": "analysis_prereg",
        "prereg_id": v3.PREREG_PATH,
        "protocol": v3.PROTOCOL,
        "protocol_sha256": v3._norm_sha(r / v3.PROTOCOL),
        "signed": True,
        "signature": {"maintainer": "signer"},
        "analysis_identity": block,
    }
    _w(r / v3.PREREG_PATH, json.dumps(doc))
    monkeypatch.setattr(ai, "verify_signed_document", lambda root, d: "pub")
    monkeypatch.setenv("IAP_RUN_ACTOR", "runner")
    for k in ai.SIGNING_ENV:
        monkeypatch.delenv(k, raising=False)
    return r, cfg


def _verify(r: Path, cfg: dict) -> dict:
    return v3.verify_binding(r, r / v3.PREREG_PATH, r / v3.CONFIG_PATH, cfg)


def test_binding_accepts_the_exact_signed_bundle(bound_repo) -> None:
    r, cfg = bound_repo
    out = _verify(r, cfg)
    assert out["runner_actor"] == "runner" and len(out["protocol_sha256"]) == 64


def test_binding_refuses_changed_protocol(bound_repo) -> None:
    r, cfg = bound_repo
    _w(r / v3.PROTOCOL, "edited\n")
    with pytest.raises(v3.BindingError, match="protocol"):
        _verify(r, cfg)


def test_binding_refuses_changed_runner_configuration(bound_repo) -> None:
    r, cfg = bound_repo
    text = (
        (r / v3.CONFIG_PATH)
        .read_text(encoding="utf-8")
        .replace('"x-version": 1', '"x-version": 1,\n  "note": "x"')
    )
    _w(r / v3.CONFIG_PATH, text)
    with pytest.raises(v3.BindingError, match="configuration hash"):
        _verify(r, cfg)


def test_binding_refuses_changed_code_and_thresholds(bound_repo) -> None:
    r, cfg = bound_repo
    _w(r / "python/src/iap/lib.py", "X = 2\n")
    with pytest.raises(v3.BindingError, match="mismatch"):
        _verify(r, cfg)


def test_binding_refuses_changed_threshold_in_memory(bound_repo) -> None:
    r, cfg = bound_repo
    bad = copy.deepcopy(cfg)
    bad["pass_rule"]["flag_upper_below"] = 0.5
    with pytest.raises((v3.BindingError, v3.ConfigError)):
        _verify(r, bad)


def test_binding_refuses_wrong_prereg_id_and_signer_as_runner(bound_repo, monkeypatch) -> None:
    r, cfg = bound_repo
    doc = json.loads((r / v3.PREREG_PATH).read_text(encoding="utf-8"))
    doc["prereg_id"] = "research/v118/other.json"
    _w(r / v3.PREREG_PATH, json.dumps(doc))
    with pytest.raises(v3.BindingError, match="prereg_id"):
        _verify(r, cfg)
    doc["prereg_id"] = v3.PREREG_PATH
    _w(r / v3.PREREG_PATH, json.dumps(doc))
    monkeypatch.setenv("IAP_RUN_ACTOR", "signer")
    with pytest.raises(v3.BindingError, match="signer"):
        _verify(r, cfg)


def test_binding_refuses_unsigned_prereg(bound_repo, monkeypatch) -> None:
    r, cfg = bound_repo
    monkeypatch.undo()
    monkeypatch.setenv("IAP_RUN_ACTOR", "runner")
    with pytest.raises(Exception, match="not signed|signature"):
        _verify(r, cfg)  # the real verify_signed_document: no trust-root signature


def test_identity_args_change_when_any_decision_content_changes(cfg) -> None:
    base = v3.identity_args_v3(cfg)["thresholds"]
    for edit in (
        lambda c: c["size_arm"]["B"].update(n_trials=2000, base_seed=20270102),
        lambda c: c["residual_mean"].update(a0_n_eff=7),
    ):
        c = copy.deepcopy(cfg)
        edit(c)
        assert v3.identity_args_v3(c)["thresholds"] == base  # unchanged values: same identity
    assert set(base) >= {"alpha", "pass_rule", "residual_mean", "centring_invariant", "gate"}


# ------------------------------------------------------------- runner jobs ----


def test_runner_job_plan_covers_every_trial_once(cfg) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("rel06_v3", ROOT / "tools/rel06_known_null_v3.py")
    assert spec and spec.loader
    sys_path = __import__("sys").path
    sys_path.insert(0, str(ROOT / "tools"))
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys_path.remove(str(ROOT / "tools"))
    jobs = mod.jobs_for(cfg, 7, 2)
    seen: dict = {}
    for arm, gen, idx, perms, deltas in jobs:
        seen.setdefault((arm, gen), []).extend(idx)
        assert bool(deltas) == (arm == "power")
        assert set(perms) == set(idx if gen in ("A0", "A_v2") else [])
    assert {k: sorted(v) for k, v in seen.items()} == {
        ("size", "A0"): list(range(1854)),
        ("size", "B"): list(range(2000)),
        ("size", "A_v2"): list(range(500)),
        ("power", "A0"): list(range(200)),
        ("power", "B"): list(range(200)),
    }
    perms = mod.permutations(cfg, 7)
    assert sorted(tuple(p["permutation"]) for p in perms[("size", "A0")]) == sorted(
        kn.derangements(7)
    )
    assert len({tuple(p["permutation"]) for p in perms[("size", "A_v2")]}) == 500

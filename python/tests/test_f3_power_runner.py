"""research/v118/f3_power/run_f3_power.py: the F3 power-diagnostic runner (UNSIGNED DRAFT, non-gating).

Synthetic only.  Refusals run in a throw-away git repository holding copies of the bound files and a test trust root.
No power study is run here (cells use the dry-run seeds and small n).
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from iap.agents import signing
from scipy import stats

REPO = Path(__file__).resolve().parents[2]
D = REPO / "research" / "v118" / "f3_power"
TOOL = REPO / "tools" / "prereg_bind_identity.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp = _load("run_f3_power", D / "run_f3_power.py")
pbi = _load("prereg_bind_identity_pw", TOOL)
pc, pm = fp.pc, fp.pm
DECL = fp.load_decl()
PID = DECL["prereg"]


def _git(root: Path, *a: str) -> None:
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "python" / "src" / "iap").mkdir(parents=True)
    (r / "python" / "src" / "iap" / "lib.py").write_text("X = 1\n")
    for rel in [*fp.BOUND, *fp.INPUTS, DECL["protocol"]]:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, r / rel)
    priv, pub = signing.generate_keypair()
    (r / "research" / "agents").mkdir(parents=True)
    (r / "research" / "agents" / "trust_roots.json").write_text(
        json.dumps({"maintainers": {"owner": pub}})
    )
    for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
              ("config", "core.autocrlf", "false"), ("add", "-A"), ("commit", "-qm", "base")):  # fmt: skip
        _git(r, *a)
    key = tmp_path / "maintainer.key"
    key.write_text(json.dumps({"owner": priv}))
    return r, key


def _decl(r: Path) -> Path:
    return r / "research" / "v118" / "f3_power" / "declaration.json"


def _ns(r: Path) -> argparse.Namespace:
    return argparse.Namespace(root=str(r), decl=_decl(r), prereg=None, out=r / "out")


def _bind(r: Path, monkeypatch) -> None:
    import iap.agents.fingerprint as fpr

    monkeypatch.setattr(fpr, "check_registration_env", lambda **kw: True)
    assert fp.main(["--root", str(r), "--decl", str(_decl(r)), "bind"]) == 0


def _sign(r: Path, key: Path) -> None:
    argv = [
        "--root",
        str(r),
        "sign",
        "--prereg",
        PID,
        "--keyfile",
        str(key),
        "--maintainer",
        "owner",
    ]
    assert pbi.main(argv) == 0
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "signed prereg")


def _ok(monkeypatch):
    monkeypatch.setenv("IAP_RUN_ACTOR", "claude-queue-runner")
    monkeypatch.setattr(fp, "available_ram_gb", lambda: 64.0)


# ------------------------------------------------------------- refusals ----
def test_declaration_is_an_unsigned_non_gating_draft_with_the_reviewed_parameters():
    assert (
        "UNSIGNED" in DECL["status"]
        and "cannot change the completed F3 acceptance result" in DECL["non_gating"]
    )
    assert DECL["alpha_op"] == 0.02 and DECL["n_per_cell"] == 10000 and DECL["grid"][-1] == 16
    assert len(fp.cells(DECL)) == 9
    if (REPO / PID).exists():
        assert json.loads((REPO / PID).read_text(encoding="utf-8")).get("signed") is True


def test_refuses_when_the_prereg_does_not_exist(repo, monkeypatch):
    r, _ = repo
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2 and not (r / "out").exists()


def test_refuses_an_unsigned_prereg(repo, monkeypatch, capsys):
    r, _ = repo
    _bind(r, monkeypatch)
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2 and "not signed" in capsys.readouterr().err


def test_refuses_an_identity_mismatch(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    p = _decl(r)
    p.write_text(p.read_text().replace('"alpha_op": 0.02,', '"alpha_op": 0.03,'))
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2 and "mismatch" in capsys.readouterr().err


def test_refuses_a_changed_acceptance_core(repo, monkeypatch, capsys):
    """The acceptance core is bound as a script: editing it after signing breaks the identity."""
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    p = r / "research" / "v118" / "f3_accept" / "core.py"
    p.write_text(p.read_text() + "\n# edited\n")
    _ok(monkeypatch)
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2 and "mismatch" in capsys.readouterr().err


def test_refuses_the_signer_as_runner(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    _ok(monkeypatch)
    monkeypatch.setenv("IAP_RUN_ACTOR", "owner")
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2 and "is the prereg signer" in capsys.readouterr().err


def test_refuses_a_signing_key_in_the_environment(repo, monkeypatch):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    _ok(monkeypatch)
    monkeypatch.setenv("IAP_SIGNING_KEY", "x")
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2


def test_refuses_when_results_already_exist(repo, monkeypatch, capsys):
    r, key = repo
    _bind(r, monkeypatch)
    _sign(r, key)
    _ok(monkeypatch)
    (r / "out").mkdir()
    (r / "out" / "DECISION.json").write_text("{}")
    with pytest.raises(SystemExit) as e:
        fp.cmd_run(_ns(r))
    assert e.value.code == 2 and "one run only" in capsys.readouterr().err


# ----------------------------------------------------------- the maths -----
GRID = [float(v) for v in DECL["grid"]]


def test_wilson_matches_the_closed_form():
    lo, hi = pm.wilson(5000, 10000, 1.959963984540054)
    assert abs((hi - lo) / 2 - 0.0098) < 1e-4 and abs(lo + hi - 1.0) < 1e-12
    lo0, hi0 = pm.wilson(0, 10000, 1.96)
    assert lo0 == 0.0 and 0.0 < hi0 < 0.001


def test_mde_first_crossing_interpolation_boundaries_and_nonmonotone():
    g = [1.0, 2.0, 3.0, 4.0]
    assert pm.mde_point(g, [0.1, 0.6, 0.9, 1.0], 0.8) == pytest.approx(2.0 + (0.8 - 0.6) / 0.3)
    assert pm.mde_point(g, [0.85, 0.9, 0.95, 1.0], 0.8) == pm.LOW
    assert pm.mde_point(g, [0.1, 0.2, 0.3, 0.79], 0.8) == pm.HIGH
    assert pm.mde_point(g, [0.1, 0.8, 0.9, 1.0], 0.8) == pytest.approx(
        2.0
    )  # lands exactly on a grid point
    nm = [0.1, 0.5, 0.85, 0.7, 0.9]
    gg = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert pm.mde_point(gg, nm, 0.8) == pytest.approx(3.0 - 0.05 / 0.35)  # first crossing kept
    assert pm.mde_nonmonotone(gg, nm, 0.8) == [(4.0, 0.7)]
    assert pm.mde_nonmonotone(gg, [0.1, 0.5, 0.85, 0.9, 0.95], 0.8) == []
    assert pm.mde_label(pm.HIGH, GRID) == "> 16.0" or pm.mde_label(pm.HIGH, GRID).startswith("> 16")
    assert pm.mde_label(pm.LOW, GRID).startswith("<= 0.5")


def test_ordered_percentiles_put_boundaries_at_the_ends():
    vals = [pm.LOW] * 5 + [3.0] * 90 + [pm.HIGH] * 5
    lo, hi = pm.ordered_percentiles(vals, 0.025, 0.975)
    assert lo == 3.0 and hi == 3.0 or (lo in (pm.LOW, 3.0) and hi in (3.0, pm.HIGH))
    assert pm.ordered_percentiles([pm.HIGH] * 100, 0.025, 0.975) == (pm.HIGH, pm.HIGH)
    assert pm.numeric_share(vals) == 0.9


def test_ratio_definedness_and_interval():
    z = 1.959963984540054
    assert not pm.power_ratio(99, 5000, 10000, 100, z)["defined"]
    assert not pm.power_ratio(5000, 0, 10000, 100, z)["defined"]
    r = pm.power_ratio(2500, 5000, 10000, 100, z)
    assert r["defined"] and r["ratio"] == 0.5 and r["lo"] < 0.5 < r["hi"]
    assert pm.mde_ratio(8.0, 4.0, [], [])["ratio"] == 2.0
    assert pm.mde_ratio(pm.HIGH, 4.0, [], [])["kind"] == "bound"
    assert pm.mde_ratio(8.0, pm.HIGH, [], [])["kind"] == "undefined"
    assert pm.mde_ratio(8.0, pm.LOW, [], [])["kind"] == "undefined"
    assert pm.mde_ratio(pm.LOW, 4.0, [], [])["kind"] == "undefined"
    assert pm.mde_ratio(8.0, 4.0, [(5.0, 0.7)], [])["kind"] == "undefined"


def test_holm_planted_agrees_with_the_platform_holm():
    from iap.research import known_null as kn

    rng = np.random.default_rng(7)
    for k in DECL["families"]:
        for _ in range(1500):
            q = rng.random(k) ** 3
            adj = kn.holm(list(q))
            rej = [a <= DECL["alpha_op"] for a in adj]
            planted, anynull = pm.holm_planted_rejected(q[None, :], DECL["alpha_op"], k)
            assert bool(planted[0]) == rej[0] and bool(anynull[0]) == any(rej[1:])
    nan = np.array([[np.nan, 0.0001, 0.5]])
    planted, anynull = pm.holm_planted_rejected(nan, 0.02, 3)
    assert not planted[0] and anynull[0]  # NaN counts as p = 1


def test_the_added_shift_is_mu_equals_d_sigma_rms_over_root_g_on_the_planted_hypothesis_only():
    for reg in DECL["regimes"]:
        for g in DECL["G"]:
            x = pc.acc.draw_chunk(DECL, reg, g, 50, pc.SplitMix64(1))
            y = pc.plant(DECL, x, reg, g, 5.0)
            delta = y - x
            sc = np.ones(g) if reg == "a0" else np.exp(np.linspace(0.0, math.log(5.0), g))
            mu = 5.0 * math.sqrt(float(np.mean(sc**2))) / math.sqrt(g)
            assert np.allclose(delta[:, :, 0], mu, rtol=0, atol=1e-12)
            assert np.all(delta[:, :, 1:] == 0)
            assert np.array_equal(pc.plant(DECL, x, reg, g, 0.0), x)
    assert pc.sigma_rms(DECL, "a0", 7) == 1.0
    assert pc.sigma_rms(DECL, "b0", 5) == pytest.approx(
        math.sqrt(np.mean(np.exp(np.linspace(0, math.log(5), 5)) ** 2))
    )


def test_a0_noncentrality_equals_d():
    """For a0 the planted t statistic has noncentrality d (mean of G unit values has se 1/sqrt(G))."""
    g, d = 7, 3.0
    rng = pc.SplitMix64(11)
    x = pc.acc.draw_chunk(DECL, "a0", g, 40000, rng)
    t = stats.ttest_1samp(pc.plant(DECL, x, "a0", g, d)[:, :, 0], 0.0, axis=1).statistic
    # mean of a noncentral t(df, ncp) is ncp * sqrt(df/2) G((df-1)/2)/G(df/2)
    df = g - 1
    expect = d * math.sqrt(df / 2) * math.exp(math.lgamma((df - 1) / 2) - math.lgamma(df / 2))
    assert float(np.mean(t)) == pytest.approx(expect, rel=0.02)


def test_crn_mapping_null_hypotheses_identical_across_d():
    """Common random numbers: the 15 null hypotheses are the same datasets for every d; only hypothesis 0 moves."""
    x = pc.acc.draw_chunk(DECL, "b1", 7, 200, pc.SplitMix64(3))
    p = [pc.acc.p1_pvalues(pc.plant(DECL, x, "b1", 7, d)) for d in (0.0, 2.0, 8.0)]
    assert np.array_equal(p[0][:, 1:], p[1][:, 1:]) and np.array_equal(p[0][:, 1:], p[2][:, 1:])
    assert not np.array_equal(p[0][:, 0], p[1][:, 0])


def test_the_cell_uses_one_stream_and_power_is_monotone_in_the_planted_effect_under_crn(
    monkeypatch,
):
    made = []

    class Counting(pc.SplitMix64):
        def __init__(self, seed):
            made.append(seed)
            super().__init__(seed)

    monkeypatch.setattr(pc, "SplitMix64", Counting)
    r = pc.run_cell(DECL, "b0", 7, 12000, dry=True)
    assert made == [pc.seed_for(DECL, "b0", 7, True)] and r["n_chunks"] == 3
    for k in DECL["families"]:
        c = r["counts"][f"K{k}"]
        assert c == sorted(
            c
        )  # common random numbers: a larger shift never rejects fewer of the same datasets
        ind = pc.unpack_indicators(r, k, len(GRID))
        assert ind.shape == (len(GRID), 12000) and [int(v) for v in ind.sum(axis=1)] == c


def test_dataset_i_is_the_same_null_dataset_for_every_d():
    r = pc.run_cell(DECL, "a0", 5, 600, dry=True)
    # rebuilding the null datasets from the same stream reproduces the per-dataset indicators of every d
    rng = pc.SplitMix64(pc.seed_for(DECL, "a0", 5, True))
    x = np.concatenate([pc.acc.draw_chunk(DECL, "a0", 5, 600, rng)])
    for j, d in enumerate(GRID):
        p = pc.acc.p1_pvalues(pc.plant(DECL, x, "a0", 5, d))
        planted, _ = pm.holm_planted_rejected(p, DECL["alpha_op"], 3)
        assert np.array_equal(planted, pc.unpack_indicators(r, 3, len(GRID))[j])


# ------------------------------------------------------ integrity band -----
def test_integrity_band_contains_the_exact_a0_power_and_flags_a_broken_estimate():
    """Monte Carlo at the declared n for a0 (dry seed, not the study) stays inside the band; a corrupted estimate does not."""
    n = 10000
    rec = pc.run_cell(DECL, "a0", 7, n, dry=True)
    phat = {
        (7, k, d): rec["counts"][f"K{k}"][j] / n
        for k in DECL["families"]
        for j, d in enumerate(GRID)
    }
    assert pm.integrity_violations(DECL["alpha_op"], phat, DECL["integrity"]["tau"]) == []
    broken = dict(phat)
    broken[(7, 3, 4.0)] = phat[(7, 3, 4.0)] + 0.2
    assert pm.integrity_violations(DECL["alpha_op"], broken, DECL["integrity"]["tau"]) == [
        (7, 3, 4.0)
    ]
    low = dict(phat)
    low[(7, 16, 8.0)] = 0.0
    assert pm.integrity_violations(DECL["alpha_op"], low, DECL["integrity"]["tau"]) == [
        (7, 16, 8.0)
    ]


def _dry_results(n=400):
    return {(r, g): pc.run_cell(DECL, r, g, n, dry=True) for r, g in fp.cells(DECL)}


def test_void_run_produces_no_power_table_or_mde():
    res = _dry_results()
    res[("a0", 5)] = copy.deepcopy(res[("a0", 5)])
    res[("a0", 5)]["counts"]["K3"][6] = (
        0  # corrupt: planted power 0 at d = 6 where the reference is high
    )
    dec = fp.analyse_cells(DECL, res, tau=DECL["integrity"]["tau_dry"])
    assert dec["status"] == "VOID_INTEGRITY" and dec["integrity"]["violations"]
    assert "power" not in dec and "mde80" not in dec and "power_ratio_het_over_hom" not in dec
    text = fp.report(dec)
    assert "RUN VOID" in text and "| power |" not in text and "MDE80 (" not in text


def test_complete_analysis_has_every_table_and_the_frozen_counts():
    dec = fp.analyse_cells(DECL, _dry_results(), tau=DECL["integrity"]["tau_dry"])
    assert dec["status"] == "COMPLETE" and dec["integrity"]["combinations_checked"] == 108
    assert len(dec["power"]) == 324 and len(dec["mde80"]) == 27
    assert (
        len(dec["power_ratio_het_over_hom"]) == 2 * 3 * 3 * 12
        and len(dec["mde80_ratio_het_over_hom"]) == 18
    )
    assert (
        len(dec["adjacent_power_differences"]) == 9 * 3 * 11
        and len(dec["null_rejection_side_table"]) == 324
    )
    text = fp.report(dec)
    assert (
        "NON-GATING" in text
        and "positively correlated" in text
        and "cannot change the completed F3 acceptance result" in text
    )
    assert "MDE80" in text and "Wilson 95 %" in text
    for row in dec["power"]:
        assert row["wilson_lo"] <= row["power"] <= row["wilson_hi"]

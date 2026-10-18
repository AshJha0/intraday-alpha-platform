"""REL-06 known-null protocol v2 (cost-neutral size arm, planted-effect power arm)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from iap.research import known_null as kn
from iap.research.runner import render_document
from test_known_null import config, synthetic_panel

ROOT = Path(__file__).resolve().parents[2]
STATS = ["net_recentred", "gross"]


def config_v2(n_trials: int = 3, n_power: int = 3, deltas=(0.5, 3.0)) -> dict:
    c = config(n_trials=n_trials)
    c["protocol"] = "research/v118/NULL_PROTOCOL_v2.md"
    c["generators"] = {"A": {"base_seed": 20261101}, "B": {"base_seed": 20261102}}
    c["statistics"] = {"primary": "net_recentred", "also_must_pass": ["gross"], "v1": "net"}
    c["power_arm"] = {
        "generators": {"A": {"base_seed": 20261103}, "B": {"base_seed": 20261104}},
        "n_trials": n_power,
        "delta_bps_per_trip": list(deltas),
        "planted": "first hypothesis of each registered family",
        "pass_rule": None,
    }
    return c


@pytest.fixture(scope="module")
def study():
    return kn.prepare(synthetic_panel(), config_v2(), pricer=None)


def test_recentred_net_has_exactly_zero_trip_mean_when_gross_is_zero(study):
    """S1 = gross - c_j + cbar_h: the cost terms cancel exactly over a
    hypothesis's trips, so the S1 trip mean equals the gross trip mean."""
    zero = np.zeros(study.panel.n_rows)
    for t in study.trips.values():
        s1, ok = kn.trip_values(t, zero, "net_recentred")
        net, _ = kn.trip_values(t, zero, "net")
        assert ok.all() and abs(s1.mean()) < 1e-12
        assert net.mean() == pytest.approx(-t.mean_fixed_bps)  # v1's null sits at -cbar
        assert t.mean_fixed_bps > 0.5
    lab = kn.labels_real(study.panel, "1s")
    for t in study.trips.values():
        s1, ok = kn.trip_values(t, lab, "net_recentred")
        g, _ = kn.trip_values(t, lab, "gross")
        assert s1[ok].sum() == pytest.approx(
            g[ok].sum() + (t.mean_fixed_bps - t.fixed_bps[ok]).sum()
        )


def test_recentred_null_is_zero_mean_over_generator_b_draws(study):
    """Over independent generator-B draws the trial-level S1 session mean
    averages to zero (within 3 Monte Carlo SE); v1's net averages to -cbar."""
    t = study.trips["h3"]
    s1, net = [], []
    for i in range(30):
        labs = kn.trial_labels(study, "B", i, None)
        s1.append(kn.session_means(t, labs["1s"], 4, "net_recentred")[0].mean())
        net.append(kn.session_means(t, labs["1s"], 4, "net")[0].mean())
    s1a = np.asarray(s1)
    assert abs(s1a.mean()) < 3 * s1a.std(ddof=1) / np.sqrt(s1a.size)
    assert np.mean(net) == pytest.approx(s1a.mean() - t.mean_fixed_bps, abs=0.1)


def test_planted_delta_moves_the_statistic_by_exactly_delta(study):
    lab = kn.labels_real(study.panel, "1s")
    for st in STATS:
        for t in study.trips.values():
            base, ok = kn.trip_values(t, lab, st)
            up, _ = kn.trip_values(t, lab, st, planted_bps=1.25)
            filled = t.fill_frac > 0
            assert np.allclose((up - base)[filled], 1.25) and np.allclose((up - base)[~filled], 0)


S7 = [
    "2019-01-30",
    "2019-03-27",
    "2019-07-30",
    "2019-08-30",
    "2019-10-30",
    "2019-12-30",
    "2020-01-30",
]


@pytest.mark.slow
def test_planted_effect_is_detected_and_power_rises_with_delta():
    """Generator B noise on G = 7 sessions, delta planted on the first hypothesis:
    near-zero confirmations at delta = 0; power rises with delta and is high at
    twice the median analytic MDE.  The analytic MDE is the one-sided t at 6 df
    before Holm over K; the empirical power at it is lower (Holm, wild-bootstrap
    discreteness): that gap is what the power arm measures."""
    st = kn.prepare(synthetic_panel(sessions=S7), config_v2(), pricer=None)
    fam = st.variants["K3"]
    r0 = kn.run_trials_v2(st, "B", range(30), {}, statistics=["gross"], deltas=[0.0], base_seed=7)
    p0 = kn.summarise_power([r0[i]["K3"]["delta=0"]["gross"] for i in r0], fam)
    assert p0["power"]["rate"] <= 0.15
    mde = p0["median_mde_bps_80pct"]
    assert 0.0 < mde < 1.0
    ds = [mde, 2 * mde]
    r = kn.run_trials_v2(st, "B", range(30), {}, statistics=STATS, deltas=ds, base_seed=7)
    for s in STATS:
        pw = [
            kn.summarise_power([r[i]["K3"][kn.delta_key(d)][s] for i in r], fam)["power"]["rate"]
            for d in ds
        ]
        assert pw[0] <= pw[1] and pw[1] >= 0.8
        others = kn.summarise_power([r[i]["K3"][kn.delta_key(ds[1])][s] for i in r], fam)
        assert others["others_any_false_confirmation"]["rate"] <= 0.2


def test_runner_planting_equals_planting_the_label_by_hand(study):
    """The power arm's planted effect is the protocol's literal label edit
    y + side * delta / (1e4 f) on the first hypothesis's entry rows."""
    labs = kn.trial_labels(study, "B", 3, None)
    fam = study.variants["K3"]
    t = study.trips[fam[0].id]
    hand = labs["1s"].copy()
    f = t.fill_frac > 0
    hand[t.entry_row[f]] += t.side[f] * 2.0 / (1e4 * t.fill_frac[f])
    via = kn.run_trials_v2(study, "B", [3], {}, statistics=STATS, deltas=[2.0])[3]["K3"]["delta=2"]
    for s in STATS:
        x, _ = kn.session_means(t, hand, 4, s)
        assert via[s]["mean_bps"][0] == pytest.approx(kn.cluster_inference(x)["mean"], abs=1e-9)
        assert via[s]["p"][0] == pytest.approx(kn.cluster_inference(x)["p"], abs=1e-12)


def test_v2_document_is_byte_reproducible_and_chunking_invariant(study):
    cfg = study.config
    sp = kn.draw_permutations(4, 3, 20261101)
    pp = kn.draw_permutations(4, 3, 20261103)
    spm = {q["trial"]: q["permutation"] for q in sp}
    ppm = {q["trial"]: q["permutation"] for q in pp}
    deltas = cfg["power_arm"]["delta_bps_per_trip"]

    def doc(chunks):
        size = {g: {} for g in ("A", "B")}
        power = {g: {} for g in ("A", "B")}
        for g in ("A", "B"):
            for c in chunks:
                size[g].update(
                    kn.run_trials_v2(study, g, c, spm if g == "A" else {}, statistics=STATS)
                )
                power[g].update(
                    kn.run_trials_v2(
                        study,
                        g,
                        c,
                        ppm if g == "A" else {},
                        statistics=STATS,
                        deltas=deltas,
                        base_seed=cfg["power_arm"]["generators"][g]["base_seed"],
                    )
                )
        return render_document(
            kn.study_document_v2(study, size, power, sp, pp, provenance={"c": 1})
        )

    one = doc([[0, 1, 2]])
    assert one == doc([[2], [0, 1]])
    d = json.loads(one)
    assert d["complete"] is True and d["verdict"] in ("PASS", "FAIL")
    assert d["kind"] == "rel06_known_null_v2" and d["statistics"] == STATS
    assert set(d["size_arm"]["A"]["variants"]["K3"]["statistics"]) == set(STATS)
    pw = d["power_arm"]["generators"]["B"]["variants"]["K3"]["delta=3"]["gross"]["summary"]
    assert pw["planted"] == "h1" and pw["power"]["n"] == 3
    assert d["power_arm"]["generators"]["B"]["seeds"] == [20261104, 20261105, 20261106]
    assert set(d["cbar_bps"]) == {"h0", "h1", "h2", "h3"}
    assert "G = 4 session clusters" in d["small_g_statement"]


def test_v1_paths_are_unchanged(study):
    """The default statistic is v1's net, so v1 records are what they were."""
    labs = kn.trial_labels(study, "B", 0, None)
    a = kn.run_trial(study.trips, labs, study.variants["K3"], 4, alpha=0.05, eligible=True)
    b = kn.run_trial(
        study.trips, labs, study.variants["K3"], 4, alpha=0.05, eligible=True, statistic="net"
    )
    assert a == b
    with pytest.raises(ValueError):
        kn.trip_values(study.trips["h0"], labs["1s"], "bogus")


def test_identity_args_v2_bind_the_v2_config_and_arms():
    v1 = json.loads((ROOT / kn.V1_CONFIG).read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / kn.V2_CONFIG).read_text(encoding="utf-8"))
    a1, a2 = kn.identity_args(v1), kn.identity_args(v2)
    assert a1["scripts"] == list(kn.BOUND_SCRIPTS) and "statistics" not in a1["thresholds"]
    assert kn.V2_CONFIG in a2["scripts"] and kn.V1_CONFIG not in a2["scripts"]
    assert all((ROOT / s).is_file() for s in a2["scripts"])
    assert a2["thresholds"]["statistics"]["primary"] == "net_recentred"
    assert a2["thresholds"]["power_arm"]["n_trials"] == v2["power_arm"]["n_trials"]
    assert a2["thresholds"]["pass_rule"] == a1["thresholds"]["pass_rule"]
    assert a2["thresholds"]["base_seeds"] == {"A": 20261101, "B": 20261102}
    assert v2["variants"] == v1["variants"]
    assert v2.get("status") == "PARAMETERS SET, ready for signing"


def test_cli_run_all_v2_matches_the_library_and_is_worker_invariant(study):
    """tools/rel06_known_null.py drives both arms with the registered seeds."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rel06_cli", ROOT / "tools" / "rel06_known_null.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cfg = study.config
    res, perms, _ = cli.run_all(Path("."), cfg, 3, 1, study, 2)
    assert set(res) == {"size", "power"} and len(res["power"]["B"]) == 2
    assert perms["size"] == kn.draw_permutations(4, 3, 20261101)
    assert perms["power"] == kn.draw_permutations(4, 2, 20261103)
    lib = kn.run_trials_v2(
        study,
        "B",
        [0, 1],
        {},
        statistics=STATS,
        deltas=cfg["power_arm"]["delta_bps_per_trip"],
        base_seed=20261104,
    )
    assert res["power"]["B"] == lib
    doc = kn.study_document_v2(
        study, res["size"], res["power"], perms["size"], perms["power"], provenance={}
    )
    assert doc["complete"] is False  # 3 size / 2 power trials, not the registered counts


def test_binder_accepts_the_v2_config():
    import argparse
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bind_v2", ROOT / "tools" / "prereg_bind_identity.py"
    )
    assert spec and spec.loader
    bind = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bind)
    a = argparse.Namespace(study_config=ROOT / kn.V2_CONFIG)
    cfg = json.loads((ROOT / kn.V2_CONFIG).read_text(encoding="utf-8"))
    assert bind.identity_inputs(a, ROOT) == kn.identity_args(cfg)

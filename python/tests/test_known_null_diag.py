"""Synthetic self-test of the v3 residual-mean diagnostic (seeded, no real data).

Separately for the B style (independent trials) and the A0 style (dependent
derangement trials): the implemented calculation, bias-detection behaviour and the
valid-null false-flag rate.  Measured rates are recorded in
research/v118/NULL_PROTOCOL_v3.md section 3.3.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.research import known_null_diag as d


def test_calculation_independent_exact() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0])
    r = d.residual_mean_flag(x, "independent")
    sd = float(np.std(x, ddof=1))
    assert r["mean"] == pytest.approx(2.5)
    assert r["se"] == pytest.approx(sd / 2.0)
    assert r["n_used"] == 4
    assert r["t"] == pytest.approx(2.5 / (sd / 2.0))
    assert r["flag"] is False


def test_calculation_dependent_uses_neff_not_n() -> None:
    x = np.arange(1854, dtype=float)
    r = d.residual_mean_flag(x, "dependent", n_eff=7)
    assert r["n_used"] == 7
    assert r["se"] == pytest.approx(float(np.std(x, ddof=1)) / math.sqrt(7))
    assert d.residual_mean_flag(x, "independent")["se"] < r["se"]


def test_calculation_flag_threshold_and_errors() -> None:
    x = np.array([-1.0, 1.0] * 50) + 0.5  # sd ~ 1.005, n=100
    assert d.residual_mean_flag(x, "independent")["flag"] is True
    assert d.residual_mean_flag(x, "independent", flag_se=100.0)["flag"] is False
    with pytest.raises(ValueError):
        d.residual_mean_flag(x, "dependent")
    with pytest.raises(ValueError):
        d.residual_mean_flag(x, "other")
    with pytest.raises(ValueError):
        d.residual_mean_flag(np.array([1.0]), "independent")


def test_derangement_count() -> None:
    assert d.derangement_index(7).shape == (1854, 7)


IDS = [f"h{i}" for i in range(16)]


def _u(v: float) -> dict[str, float]:
    return dict.fromkeys(IDS, v)


def test_understatement_ratio_values() -> None:
    rng = np.random.default_rng(3)
    m = rng.standard_normal(5000)
    assert d.understatement_ratio(m, np.ones(5000)) == pytest.approx(1.0, abs=0.03)
    assert d.understatement_ratio(m, np.full(5000, 0.5)) == pytest.approx(2.0, abs=0.06)


def test_understatement_ratio_exceptional_values_are_nan_not_dropped() -> None:
    m = np.array([1.0, -1.0, 2.0, -2.0])
    ones = np.ones(4)
    assert math.isnan(d.understatement_ratio(np.append(m[:3], np.nan), ones))
    assert math.isnan(d.understatement_ratio(m, np.array([1.0, 1.0, np.inf, 1.0])))
    assert math.isnan(d.understatement_ratio(m, np.array([1.0, np.nan, 1.0, 1.0])))
    assert math.isnan(d.understatement_ratio(m, np.zeros(4)))  # zero denominator
    assert math.isnan(d.understatement_ratio(m[:1], ones[:1]))  # < 2 trials
    with pytest.raises(ValueError):
        d.understatement_ratio(m, ones[:3])


def test_gate_triggered_and_not() -> None:
    assert d.lost_difficulty(_u(0.9), _u(1.2), IDS)["status"] == "TRIGGERED"
    assert d.lost_difficulty(_u(1.0), _u(1.2), IDS)["lost"] is True  # <= 1.0 inclusive
    assert d.lost_difficulty(_u(1.1), _u(1.2), IDS)["status"] == "NOT_TRIGGERED"
    assert d.lost_difficulty(_u(0.9), _u(0.9), IDS)["status"] == "NOT_TRIGGERED"  # B > 1 needed
    r = d.lost_difficulty(_u(0.9), _u(1.2), IDS)
    assert r["median_u_a0"] == pytest.approx(0.9) and r["median_u_b"] == pytest.approx(1.2)


def test_gate_missing_or_nonfinite_is_undetermined_never_omitted() -> None:
    a = _u(0.9)
    a.pop("h3")
    assert d.lost_difficulty(a, _u(1.2), IDS)["invalid_ids"] == ["h3"]
    b = _u(1.2)
    b["h5"] = math.nan
    r = d.lost_difficulty(_u(0.9), b, IDS)
    assert r["status"] == "UNDETERMINED" and r["lost"] is None and r["invalid_ids"] == ["h5"]
    with pytest.raises(ValueError):
        d.lost_difficulty(_u(0.9), _u(1.2), IDS[:15])
    with pytest.raises(ValueError):
        d.lost_difficulty({**_u(0.9), "zz": 1.0}, _u(1.2), IDS)


def test_frozen_hypothesis_list_is_the_16_of_the_config() -> None:
    import json
    from pathlib import Path

    cfg = Path(__file__).resolve().parents[2] / "research/v118/known_null_v2_config.json"
    ids = [h["id"] for h in json.loads(cfg.read_text(encoding="utf-8"))["variants"]["K16"]]
    assert len(ids) == d.N_HYPOTHESES and len(set(ids)) == d.N_HYPOTHESES


# --------------------------------------------------- verdict logic (protocol 6) --

CELLS = [(s, k) for s in ("S1", "S2") for k in (3, 6, 16)]
NOT_TRIG = {"status": "NOT_TRIGGERED"}
TRIG = {"status": "TRIGGERED"}


def _v(b=60, a=55, ok=True, flag=False, gate=NOT_TRIG):
    return d.primary_verdict(dict.fromkeys(CELLS, b), dict.fromkeys(CELLS, a), ok, flag, gate)


def test_verdict_thresholds_edge_and_labels() -> None:
    assert _v()["verdict"] == "PASS" and _v()["label"] == d.PASS_LABEL_PLAIN
    assert _v(b=61)["verdict"] == "FAIL"
    assert _v(a=56)["verdict"] == "FAIL"
    assert _v(gate=TRIG)["label"] == "PASS (B-supported only; A0 non-informative)"
    assert _v(gate={"status": "UNDETERMINED"})["label"] == d.PASS_LABEL_GATE


def test_a0_rate_failure_is_fail_regardless_of_gate() -> None:
    assert _v(a=56, gate=TRIG)["verdict"] == "FAIL"
    assert _v(a=56, gate={"status": "UNDETERMINED"})["verdict"] == "FAIL"


def test_missing_cell_fails_and_b_flag_blocks_pass() -> None:
    assert (
        d.primary_verdict({}, dict.fromkeys(CELLS, 0), True, False, NOT_TRIG)["verdict"] == "FAIL"
    )
    assert _v(flag=True)["verdict"] == "FAIL"


def test_centring_failure_is_invalid_not_a_statistical_finding() -> None:
    r = _v(ok=False, a=999, b=999)
    assert r["verdict"] == "INVALID" and r["reason"] == "centring"


# ------------------------------------------------ A0 centring invariant (3.3) --


def _panel(seed: int = 0):
    rng = np.random.default_rng(seed)
    name = np.repeat([0, 1], 70)
    sess = np.tile(np.repeat(np.arange(7), 10), 2)
    raw = rng.standard_normal(140) * 3 + sess * 0.5 + name
    return raw, name, sess


def test_centring_invariant_passes_on_centred_labels() -> None:
    raw, name, sess = _panel()
    c = d.centre_labels(raw, name, sess)
    r = d.check_centring_invariant({"1s": c, "10s": c * 2}, name, sess, [0, 1], range(7))
    assert r["ok"] and r["n_groups"] == 28 and r["max_abs_mean"] < 1e-12
    d.require_centring_invariant(r)


def test_centring_invariant_rejects_uncentred_input() -> None:
    raw, name, sess = _panel()
    r = d.check_centring_invariant({"1s": raw}, name, sess, [0, 1], range(7))
    assert not r["ok"]
    with pytest.raises(d.CentringInvariantError):
        d.require_centring_invariant(r)


def test_centring_invariant_tolerance_edge() -> None:
    x = np.array([1.0, -1.0, 2.0, -2.0])  # scale 2, mean 0
    n = np.zeros(4, dtype=int)
    s = np.zeros(4, dtype=int)

    def chk(shift: float) -> bool:
        return d.check_centring_invariant({"h": x + shift}, n, s, [0], [0])["ok"]

    # shifted group: mean = shift, scale = max|x + shift|; edge at ~1e-9 * scale
    assert chk(0.0) and chk(1e-12)
    assert not chk(1e-8)


def test_centring_invariant_empty_and_invalid_groups_fail() -> None:
    raw, name, sess = _panel()
    c = d.centre_labels(raw, name, sess)
    assert not d.check_centring_invariant({"1s": c}, name, sess, [0, 1, 2], range(7))[
        "ok"
    ]  # no name 2
    allnan = c.copy()
    allnan[(name == 0) & (sess == 3)] = np.nan
    r = d.check_centring_invariant({"1s": allnan}, name, sess, [0, 1], range(7))
    assert not r["ok"] and r["failures"][0]["reason"] == "empty group"
    inf = c.copy()
    inf[0] = np.inf
    assert not d.check_centring_invariant({"1s": inf}, name, sess, [0, 1], range(7))["ok"]
    with pytest.raises(ValueError):
        d.check_centring_invariant({"1s": c[:5]}, name, sess, [0], [0])


def test_nan_rows_do_not_enter_the_mean_and_series_used_check() -> None:
    raw, name, sess = _panel()
    raw[3] = np.nan
    c = d.centre_labels(raw, name, sess)
    assert np.isnan(c[3])
    assert d.check_centring_invariant({"1s": c}, name, sess, [0, 1], range(7))["ok"]
    assert d.verify_series_used(c.copy(), c)
    assert not d.verify_series_used(raw, c)
    assert not d.verify_series_used(c[:-1], c)
    # value-equal but not bitwise identical: -0.0 vs 0.0, and different NaN payloads
    z = np.array([0.0, 1.0])
    assert d.verify_series_used(z.copy(), z)
    assert not d.verify_series_used(np.array([-0.0, 1.0]), z)
    nan2 = np.array([np.nan]).view(np.uint64) ^ np.uint64(1)
    assert not d.verify_series_used(nan2.view(np.float64), np.array([np.nan]))


def test_blocker_measure_reports_bounds_and_per_hypothesis_counts() -> None:
    r = d.measure_b_blocker(40, seed=3)
    assert r["per_hypothesis_checks_on_unbiased"] == 40 * 16
    assert 0.0 <= r["verdict_b_flag_rate"] <= r["verdict_b_flag_rate_cp95_upper"] <= 1.0
    lo, hi = r["verdict_b_flag_rate_wilson95"]
    assert 0.0 <= lo <= hi <= 1.0
    assert d.clopper_pearson_upper(0, 20000) == pytest.approx(1 - 0.05 ** (1 / 20000), rel=1e-3)


# ------------------------------------------------------------------ B style --


def test_b_valid_null_false_flag_rate_below_point_one_percent() -> None:
    # 100,000 replicates measured 1e-5 (seed 11); here a smaller seeded run.
    assert d.measure_b_style(20000, 0.0, seed=11) < 0.001


def test_b_bias_detection() -> None:
    # 4 SE at n = 2000 is a bias of 0.089 trial sd.
    assert d.measure_b_style(3000, 0.05, seed=5) < 0.15
    assert 0.3 < d.measure_b_style(3000, 0.09, seed=5) < 0.75
    assert d.measure_b_style(3000, 0.15, seed=5) > 0.98


# ----------------------------------------------------------------- A0 style --


def test_a0_bias_detection_only_large_bias() -> None:
    # n_eff = 7: a bias of 0.5 entry sd is caught <50 % of the time, 1.5 sd always.
    assert d.measure_a0_style(300, "iid", bias_sd=0.5, seed=5) < 0.7
    assert d.measure_a0_style(300, "iid", bias_sd=1.5, seed=5) > 0.99


def test_a0_valid_null_false_flag_rate_iid_dependence_is_not_below_point_one_percent() -> None:
    # Measured 0.22 % (20,000 datasets, seed 11): the "< 0.1 %" claim is NOT
    # demonstrated for A0, so the protocol does not make it.
    rate = d.measure_a0_style(3000, "iid", seed=11)
    assert rate < 0.01


def test_a0_valid_null_false_flag_rate_with_session_effects_is_not_calibrated() -> None:
    # With session effects every derangement sums every effect once, so the
    # derangement sd cannot see them: the heuristic SE is far too small.
    # Measured 34.9 % (5,000 datasets, seed 11).  The diagnostic is documented as
    # able to under-protect (false flags) under such dependence.
    rate = d.measure_a0_style(300, "effects", seed=11)
    assert rate > 0.2


def test_required_names_may_differ_per_horizon() -> None:
    raw, name, sess = _panel()
    c = d.centre_labels(raw, name, sess)
    ok = d.check_centring_invariant(
        {"1s": c, "10s": c}, name, sess, {"1s": [0, 1], "10s": [0]}, range(7)
    )
    assert ok["ok"] and ok["n_groups"] == 21

"""Regime-conditioned model and the regime gate (v1.17 C5). Synthetic frames only."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from iap.alpha import build
from iap.alpha.base import LinearAlpha
from iap.alpha.linear_v2 import linear_z_v2
from iap.alpha.regime_conditioned import (
    MODEL_RC,
    MODES,
    RegimeConditionedAlpha,
    regime_conditioned,
    regime_labels,
)
from iap.core.rng import SplitMix64
from iap.validation.metrics import HORIZONS_NS, gate_statistics
from iap.validation.regime_gate import regime_gate, regime_gate_statistics, regime_rows

NS = 1_000_000_000
T0 = 1_700_000_000 * NS
HORIZON = "5s"


def _normal(rng: SplitMix64, n: int) -> np.ndarray:
    u1 = np.array([max(rng.uniform(), 1e-12) for _ in range(n)])
    u2 = np.array([rng.uniform() for _ in range(n)])
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


class Toy(LinearAlpha):
    """One feature, as is.

    Economic rationale: none — a test double whose slope depends on the regime.
    """

    alpha_id = "TR"
    name = "toy_regime"
    asset_class = "EQUITY"
    horizon = HORIZON
    features = ("f1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return df["f1"]


def factory(aid: str):
    assert aid == "TR"
    return Toy()


def make_frames(seed=5, n=4000, slopes=(0.05, 0.4), instruments=(1, 2), unknown_every=0):
    """The label slope on f1 is ``slopes[0]`` in the low-vol regime and
    ``slopes[1]`` in the high-vol regime (flag alternates in 50-row blocks);
    ``liq_regime_flag_v1`` is independent noise."""
    rng = SplitMix64(seed)
    frames = {}
    for iid in instruments:
        f = _normal(rng, n)
        vol = ((np.arange(n) // 50) % 2).astype(float)
        liq = np.array([1.0 if rng.uniform() < 0.5 else 0.0 for _ in range(n)])
        slope = np.where(vol > 0.5, slopes[1], slopes[0])
        label = 1e-4 * (slope * f + 0.5 * _normal(rng, n))
        if unknown_every:
            vol[::unknown_every] = np.nan
        frames[iid] = pd.DataFrame(
            {
                "exchange_ts": T0 + np.arange(n, dtype=np.int64) * NS,
                "f1": f,
                "vol_regime_flag_v1": vol,
                "liq_regime_flag_v1": liq,
                f"label_mid_{HORIZON}": label,
                f"label_valid_{HORIZON}": True,
            }
        )
    return frames


# ---------------------------------------------------------------- labels


def test_regime_labels_bits_and_unknown():
    df = pd.DataFrame(
        {"vol_regime_flag_v1": [0.0, 1.0, np.nan, 1.0], "liq_regime_flag_v1": [0.0, 0.0, 1.0, 1.0]}
    )
    assert regime_labels(df).tolist() == [0, 1, -1, 1]
    assert regime_labels(df, ("vol_regime_flag_v1", "liq_regime_flag_v1")).tolist() == [0, 1, -1, 3]
    assert regime_labels(df, ("missing_v1",)).tolist() == [-1] * 4
    with pytest.raises(ValueError):
        regime_labels(df, ())


# ---------------------------------------------------------------- model


def test_identity_and_construction():
    m = RegimeConditionedAlpha("TR", member_factory=factory)
    assert m.alpha_id == "TR_rc" and m.horizon == HORIZON and m.asset_class == "EQUITY"
    assert m.features == ("f1", "vol_regime_flag_v1")
    assert type(m).economic_rationale().startswith("Economic rationale:")
    with pytest.raises(ValueError):
        RegimeConditionedAlpha("TR", mode="nope", member_factory=factory)
    with pytest.raises(ValueError):
        RegimeConditionedAlpha("TR", regime_features=(), member_factory=factory)
    with pytest.raises(ValueError):
        RegimeConditionedAlpha("EQ11")  # cross-sectional member
    real = RegimeConditionedAlpha("EQ02")
    assert real.alpha_id == "EQ02_rc" and set(build("EQ02").features) < set(real.features)
    assert build("EQ02").params()["model"] == "linear_z_v1"
    assert regime_conditioned("TR")  # the factory builds


@pytest.mark.parametrize("mode", MODES)
def test_per_regime_slopes_are_recovered(mode):
    frames = make_frames()
    m = RegimeConditionedAlpha("TR", mode, member_factory=factory)
    m.fit(frames)
    p = m.params()
    assert p["model"] == MODEL_RC and p["mode"] == mode and not p["dead"]
    assert p["hypothesis_confirmed"] and p["beta"] > 0.0
    member = Toy()
    member.fit(frames)
    # the member's z has unit scale, so the regime slopes (in label units
    # per z) are sigma * slope * 1e-4
    sig = member.sigma
    if mode == "per_regime":
        b = p["beta_by_regime"]
        assert set(b) == {"0", "1"}
        assert b["0"] == pytest.approx(0.05e-4 * sig, rel=0.25)
        assert b["1"] == pytest.approx(0.4e-4 * sig, rel=0.1)
    else:
        assert p["beta"] == pytest.approx(0.05e-4 * sig, rel=0.3)
        assert p["gamma"][0] == pytest.approx(0.35e-4 * sig, rel=0.15)
    # the pooled slope sits between the two regimes
    assert 0.05e-4 * sig < p["beta_fit"] < 0.4e-4 * sig or mode == "interaction"
    s = m.score(frames)[1]
    z = member.score(frames)[1]["expected_return"].to_numpy() / member.beta
    high = frames[1]["vol_regime_flag_v1"].to_numpy() > 0.5
    er = s["expected_return"].to_numpy()
    ok = s["confidence"].to_numpy() > 0.0
    ratio_high = np.median(np.abs(er[high & ok] / z[high & ok]))
    ratio_low = np.median(np.abs(er[~high & ok] / z[~high & ok]))
    assert ratio_high > 3.0 * ratio_low
    assert list(s.columns) == ["exchange_ts", "expected_return", "confidence"]


def test_unknown_regime_scores_at_the_pooled_slope():
    frames = make_frames(unknown_every=10)
    m = RegimeConditionedAlpha("TR", member_factory=factory)
    m.fit(frames)
    assert "-1" in m.params()["n_by_regime"] and "-1" not in m.params()["beta_by_regime"]
    s = m.score(frames)[1]
    z = m.member.score(frames)[1]["expected_return"].to_numpy() / m.member.beta
    unknown = ~np.isfinite(frames[1]["vol_regime_flag_v1"].to_numpy())
    ok = s["confidence"].to_numpy() > 0.0
    rows = unknown & ok
    assert np.allclose(s["expected_return"].to_numpy()[rows], m.beta0 * z[rows])


def test_two_flags_and_a_v2_member():
    frames = make_frames()
    m = RegimeConditionedAlpha(
        "TR", "per_regime", ("vol_regime_flag_v1", "liq_regime_flag_v1"), member_factory=factory
    )
    m.fit(frames)
    assert set(m.params()["beta_by_regime"]) == {"0", "1", "2", "3"}
    v2 = RegimeConditionedAlpha("TR", member_factory=lambda _a: linear_z_v2(Toy)())
    v2.fit(frames)
    assert v2.params()["member_params"]["model"] == "linear_z_v2" and not v2.is_dead


def test_params_round_trip_and_dead():
    frames = make_frames(n=1500)
    for mode in MODES:
        m = RegimeConditionedAlpha("TR", mode, member_factory=factory)
        m.fit(frames)
        p = json.loads(json.dumps(m.params()))
        n = RegimeConditionedAlpha("TR", mode, member_factory=factory)
        n.load_params(p)
        a, b = m.score(frames), n.score(frames)
        for iid in frames:
            assert a[iid].equals(b[iid])
        with pytest.raises(ValueError):
            RegimeConditionedAlpha(
                "TR", mode, ("liq_regime_flag_v1",), member_factory=factory
            ).load_params(p)
        with pytest.raises(ValueError):
            n.load_params({**p, "model": "linear_z_v1"})
    tiny = {k: df.head(10) for k, df in frames.items()}
    d = RegimeConditionedAlpha("TR", member_factory=factory)
    d.fit(tiny)
    assert d.is_dead and d.params()["dead"]
    for df in d.score(tiny).values():
        assert (df["confidence"] == 0.0).all()


def test_score_is_causal():
    frames = make_frames(n=1500)
    m = RegimeConditionedAlpha("TR", "interaction", member_factory=factory)
    m.fit(frames)
    full = m.score(frames)[1]
    part = m.score({k: df.head(700).reset_index(drop=True) for k, df in frames.items()})[1]
    assert np.array_equal(
        part["expected_return"].to_numpy(), full["expected_return"].to_numpy()[:700]
    )


# ---------------------------------------------------------------- gate


def test_regime_gate_statistics_match_gate_statistics_per_regime():
    frames = make_frames(n=3000)
    member = Toy()
    member.fit(frames)
    scores = member.score(frames)
    ts, x, y, r = regime_rows(frames, scores, HORIZON)
    assert ts.size == 6000 and set(np.unique(r)) == {0, 1}
    stats = regime_gate_statistics(ts, x, y, r, horizon_ns=HORIZONS_NS[HORIZON])
    assert set(stats["by_regime"]) == {"0", "1"} and stats["n_unknown"] == 0
    for rv in (0, 1):
        rows = r == rv
        exp = gate_statistics(ts[rows], x[rows], y[rows], horizon_ns=HORIZONS_NS[HORIZON])
        got = stats["by_regime"][str(rv)]
        for k in exp:
            assert got[k] == exp[k] or (got[k] != got[k] and exp[k] != exp[k]), k
        assert got["judged"] and got["flags"] == {"vol_regime_flag_v1": bool(rv)}
    assert stats["by_regime"]["1"]["ic"] > stats["by_regime"]["0"]["ic"] > 0.0
    assert stats["pooled"]["n_pairs"] == 6000
    g = regime_gate(stats)
    assert g["passed"] and g["worst_regime"] == "0" and g["failed"] == []
    # significance per regime: the weak regime has the smaller t
    strict = regime_gate(stats, min_t=stats["by_regime"]["1"]["tstat_hac"] - 1e-9)
    assert not strict["passed"] and strict["failed"] == ["regime_0_tstat"]
    # the "one regime carries it" check
    assert not regime_gate(stats, max_ic_ratio=0.9)["passed"]
    assert regime_gate(stats, max_ic_ratio=0.05)["passed"]


def test_regime_gate_edge_cases():
    frames = make_frames(n=600, unknown_every=3)
    member = Toy()
    member.fit(frames)
    ts, x, y, r = regime_rows(frames, member.score(frames), HORIZON)
    stats = regime_gate_statistics(ts, x, y, r, horizon_ns=HORIZONS_NS[HORIZON], min_pairs=100_000)
    assert stats["n_unknown"] > 0 and not any(v["judged"] for v in stats["by_regime"].values())
    g = regime_gate(stats)
    assert not g["passed"] and g["failed"] == ["no_regime_judged"] and g["worst_regime"] is None
    with pytest.raises(ValueError):
        regime_gate_statistics(ts, x, y, r[:-1], horizon_ns=NS)
    with pytest.raises(ValueError):
        regime_gate_statistics(ts, x, y, r, horizon_ns=NS, min_pairs=1)
    # a failing sign in one regime fails the gate
    bad = regime_gate_statistics(ts, -x, y, r, horizon_ns=HORIZONS_NS[HORIZON], min_pairs=50)
    assert not regime_gate(bad)["passed"]
    assert regime_gate(bad, min_regimes=3)["failed"][-1] == "too_few_regimes"

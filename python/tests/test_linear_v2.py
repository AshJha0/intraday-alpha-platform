"""``linear_z_v2`` — per-instrument causal standardisation (v1.17 C3, R-7).

Synthetic frames only (SplitMix64, no wall clock, no dataset).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.alpha import ALPHA_CLASSES, build
from iap.alpha.base import LinearAlpha
from iap.alpha.linear_v2 import (
    DEFAULT_MIN_OBS,
    DEFAULT_STD_WINDOW,
    MODEL_V2,
    STD_WINDOWS_NS,
    V2_ALPHA_CLASSES,
    V2_ALPHA_IDS,
    LinearAlphaV2,
    build_v2,
    linear_z_v2,
    rolling_standardise,
)
from iap.core.rng import SplitMix64

NS = 1_000_000_000
T0 = 1_700_000_000 * NS
HORIZON = "5s"
ROOT = Path(__file__).resolve().parents[2]


def _normal(rng: SplitMix64, n: int) -> np.ndarray:
    u1 = np.array([max(rng.uniform(), 1e-12) for _ in range(n)])
    u2 = np.array([rng.uniform() for _ in range(n)])
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


class ToyV1(LinearAlpha):
    """One feature, as is.

    Economic rationale: none — a test double with a loud and a quiet name.
    """

    alpha_id = "TV"
    name = "toy"
    asset_class = "EQUITY"
    horizon = HORIZON
    features = ("f1",)

    def raw_signal(self, df: pd.DataFrame) -> pd.Series:
        return df["f1"]


ToyV2 = linear_z_v2(ToyV1)


def make_frames(seed: int = 11, n: int = 3000, scales=(1.0, 25.0)) -> dict[int, pd.DataFrame]:
    """Two instruments with the SAME signal-to-label relation on their own
    scale; instrument 2's raw signal is 25x louder (the R-7 situation)."""
    rng = SplitMix64(seed)
    frames = {}
    for iid, scale in zip((1, 2), scales, strict=True):
        f = _normal(rng, n)
        label = 1e-4 * (0.3 * f + _normal(rng, n))
        frames[iid] = pd.DataFrame(
            {
                "exchange_ts": T0 + np.arange(n, dtype=np.int64) * NS,
                "f1": scale * f + 3.0 * scale,
                f"label_mid_{HORIZON}": label,
                f"label_valid_{HORIZON}": True,
            }
        )
    return frames


# ------------------------------------------------------------- rolling z


def test_rolling_standardise_matches_bruteforce():
    rng = SplitMix64(5)
    n = 400
    ts = T0 + np.cumsum(np.array([1 + int(rng.uniform() * 3) for _ in range(n)])) * NS
    x = _normal(rng, n)
    x[::13] = np.nan
    w = 60 * NS
    got = rolling_standardise(ts, x, w, min_obs=8)
    for i in range(n):
        rows = (ts > ts[i] - w) & (ts <= ts[i])
        v = x[rows]
        v = v[np.isfinite(v)]
        if not np.isfinite(x[i]) or v.size < 8 or not np.std(v) > 0.0:
            assert np.isnan(got[i])
        else:
            exp = (x[i] - np.mean(v)) / (np.std(v) + 1e-12)
            assert abs(got[i] - exp) < 1e-9, i


def test_rolling_standardise_is_causal():
    """Truncating the rows after i leaves z[i] bit-identical."""
    rng = SplitMix64(9)
    n = 300
    ts = T0 + np.arange(n, dtype=np.int64) * NS
    x = _normal(rng, n)
    full = rolling_standardise(ts, x, 50 * NS, min_obs=4)
    for cut in (37, 120, 299):
        part = rolling_standardise(ts[: cut + 1], x[: cut + 1], 50 * NS, min_obs=4)
        assert np.array_equal(part, full[: cut + 1], equal_nan=True)


def test_rolling_standardise_edge_cases():
    assert rolling_standardise(np.empty(0, np.int64), np.empty(0), NS).size == 0
    ts = T0 + np.arange(5, dtype=np.int64) * NS
    assert np.all(np.isnan(rolling_standardise(ts, np.full(5, np.nan), NS)))
    assert np.all(np.isnan(rolling_standardise(ts, np.ones(5), 10 * NS, min_obs=2)))  # no spread
    with pytest.raises(ValueError):
        rolling_standardise(ts[::-1].copy(), np.ones(5), NS)


# ------------------------------------------------------------- the model


def test_v2_is_a_new_identity_and_leaves_v1_alone():
    assert set(V2_ALPHA_IDS) == {
        f"{a}_z2" for a in ALPHA_CLASSES if a not in ("EQ11", "FX05", "FX06")
    }
    m = build_v2("EQ02")
    assert isinstance(m, LinearAlphaV2) and m.alpha_id == "EQ02_z2"
    assert build_v2("EQ02_z2").alpha_id == "EQ02_z2"
    assert m.horizon == build("EQ02").horizon and m.features == build("EQ02").features
    assert type(m).economic_rationale().startswith("Economic rationale:")
    assert build("EQ02").params()["model"] == "linear_z_v1"  # the registered one is untouched
    with pytest.raises(ValueError):
        build_v2("EQ11")  # cross-sectional: no per-instrument twin
    with pytest.raises(ValueError):
        linear_z_v2(ToyV1, std_window="3h")
    assert linear_z_v2(ToyV1, std_window="1h").alpha_id == "TV_z2_1h"


def test_per_instrument_z_is_comparable_across_a_loud_and_a_quiet_name():
    frames = make_frames()
    v1, v2 = ToyV1(), ToyV2()
    v1.fit(frames)
    v2.fit(frames)
    s1, s2 = v1.score(frames), v2.score(frames)
    # v1: the loud name sets the pooled sigma, so the quiet name never reaches conf >= 0.5
    assert np.mean(s1[1]["confidence"].to_numpy() >= 0.5) < 0.05
    assert np.mean(s1[2]["confidence"].to_numpy() >= 0.5) > 0.2
    # v2: both names get the same share of confident rows (within noise)
    c1 = np.mean(s2[1]["confidence"].to_numpy() >= 0.5)
    c2 = np.mean(s2[2]["confidence"].to_numpy() >= 0.5)
    assert abs(c1 - c2) < 0.05 and c1 > 0.2
    assert v2.beta > 0.0 and v2.params()["hypothesis_confirmed"]
    # the warmup rows of each instrument have no opinion
    for iid in (1, 2):
        head = s2[iid].head(DEFAULT_MIN_OBS - 1)
        assert (head["confidence"] == 0.0).all() and (head["expected_return"] == 0.0).all()


def test_score_is_causal_and_output_contract_holds():
    frames = make_frames(n=1200)
    m = ToyV2()
    m.fit(frames)
    full = m.score(frames)
    cut = {iid: df.head(700).reset_index(drop=True) for iid, df in frames.items()}
    part = m.score(cut)
    for iid in frames:
        assert list(full[iid].columns) == ["exchange_ts", "expected_return", "confidence"]
        assert np.array_equal(
            part[iid]["expected_return"].to_numpy(), full[iid]["expected_return"].to_numpy()[:700]
        )
        conf = full[iid]["confidence"].to_numpy()
        assert np.all((conf >= 0.0) & (conf <= 1.0))
        er = full[iid]["expected_return"].to_numpy()
        assert np.all(er[conf == 0.0] == 0.0)


def test_params_round_trip_and_loader_contract():
    frames = make_frames(n=800)
    m = ToyV2()
    m.fit(frames)
    p = m.params()
    assert p["model"] == MODEL_V2 and p["std_window"] == DEFAULT_STD_WINDOW
    assert p["std_window_ns"] == STD_WINDOWS_NS[DEFAULT_STD_WINDOW]
    assert p["mu"] == 0.0 and p["sigma"] == 1.0 and not p["dead"]
    json.dumps(p)
    n = ToyV2()
    n.load_params(p)
    a, b = m.score(frames), n.score(frames)
    for iid in frames:
        assert a[iid].equals(b[iid])
    with pytest.raises(ValueError):
        ToyV1().load_params(p)  # a v1 loader rejects the v2 model
    with pytest.raises(ValueError):
        n.load_params({**p, "model": "linear_z_v1"})
    with pytest.raises(ValueError):
        n.load_params({**p, "std_window": "1h"})


def test_dead_when_too_few_pairs():
    frames = {k: df.head(40) for k, df in make_frames(n=40).items()}
    m = ToyV2()
    m.fit(frames)
    assert m.is_dead and m.params()["dead"]
    for df in m.score(frames).values():
        assert (df["confidence"] == 0.0).all() and (df["expected_return"] == 0.0).all()


@pytest.mark.parametrize("aid", V2_ALPHA_IDS)
def test_every_v2_twin_builds_and_declares_its_base(aid):
    m = V2_ALPHA_CLASSES[aid]()
    assert m.alpha_id == aid and m.base_alpha_id == aid.removesuffix("_z2")
    assert m.std_window == DEFAULT_STD_WINDOW


def test_prereg_draft_is_unsigned():
    doc = json.loads((ROOT / "research" / "preregs" / "linear_z_v2_prereg_DRAFT.json").read_text())
    assert doc["signed"] is False and doc["signature"] is None
    assert doc["status"].startswith("DRAFT")
    assert doc["model"] == MODEL_V2
    assert set(doc["candidates"]) <= set(V2_ALPHA_IDS)

"""Interface compliance for the 24 flagship alphas (spec §§11-12)."""

from __future__ import annotations

import numpy as np
import pytest

from iap.alpha import (
    ALPHA_CLASSES,
    ALPHA_IDS,
    EQ_IDS,
    ETF_ID,
    FX_IDS,
    FX_REF_ID,
    VALID_HORIZONS,
    LinearAlpha,
    build,
    build_all,
    currency_exposures,
    exposure_matrix,
    free_exposure_matrix,
    solve_factor_returns,
)
from iap.alpha.data import load_features
from iap.features.registry import build_registry

from conftest import REPO_ROOT

FEATURES_DIR = REPO_ROOT / "data" / "features"

EXPECTED_IDS = [f"EQ{i:02d}" for i in range(1, 13)] + [
    f"FX{i:02d}" for i in range(1, 13)
]


@pytest.fixture(scope="module")
def registry_names():
    return {s.name for s in build_registry()}


@pytest.fixture(scope="module")
def small_frames():
    """First 2500 rows of every bundled instrument frame (real columns)."""
    if not FEATURES_DIR.is_dir() or not list(FEATURES_DIR.glob("features_*.parquet")):
        pytest.skip("data/features missing — regenerate via python3 -m iap.features")
    frames = load_features(FEATURES_DIR)
    return {iid: df.head(2500).reset_index(drop=True) for iid, df in frames.items()}


@pytest.fixture(scope="module")
def fitted_models(small_frames):
    models = build_all()
    for m in models.values():
        m.fit(small_frames)
    return models


def test_registry_has_exactly_the_24_spec_ids():
    assert ALPHA_IDS == EXPECTED_IDS
    assert set(ALPHA_CLASSES) == set(EXPECTED_IDS)


def test_build_unknown_id_raises():
    with pytest.raises(ValueError):
        build("EQ99")


@pytest.mark.parametrize("aid", EXPECTED_IDS)
def test_identity_and_rationale(aid):
    m = build(aid)
    assert m.alpha_id == aid
    assert m.name
    assert m.asset_class in ("EQUITY", "FX")
    assert m.horizon in VALID_HORIZONS
    assert m.features, f"{aid} declares no feature dependencies"
    rationale = type(m).economic_rationale()
    assert rationale.startswith("Economic rationale:")
    assert len(rationale) > 80, f"{aid} rationale is not a real hypothesis"


@pytest.mark.parametrize("aid", EXPECTED_IDS)
def test_declared_features_exist_in_registry(aid, registry_names):
    m = build(aid)
    missing = set(m.features) - registry_names
    assert not missing, f"{aid} depends on unregistered features {missing}"


def test_class_without_rationale_cannot_exist():
    with pytest.raises(TypeError):
        class NoRationale(LinearAlpha):  # noqa: F811 - deliberate
            """No hypothesis here."""

            alpha_id = "XX01"
            name = "bad"
            asset_class = "EQUITY"
            horizon = "1s"
            features = ("mid_price_v1",)

            def raw_signal(self, df):
                return df["mid_price_v1"]


def test_score_before_fit_raises(small_frames):
    m = build("EQ01")
    with pytest.raises(RuntimeError):
        m.score(small_frames)


def test_universes_pinned():
    assert build("EQ01").universe(list(EQ_IDS) + list(FX_IDS)) == list(EQ_IDS)
    assert build("FX01").universe(list(EQ_IDS) + list(FX_IDS)) == list(FX_IDS)
    assert ETF_ID not in build("EQ10").universe(list(EQ_IDS))
    assert ETF_ID not in build("EQ09").universe(list(EQ_IDS))
    assert ETF_ID not in build("EQ11").universe(list(EQ_IDS))
    assert FX_REF_ID not in build("FX07").universe(list(FX_IDS))


@pytest.mark.parametrize("aid", EXPECTED_IDS)
def test_fit_score_contract(aid, small_frames, fitted_models):
    m = fitted_models[aid]
    out = m.score(small_frames)
    assert sorted(out) == m.universe(list(small_frames))
    for iid, sc in out.items():
        df = small_frames[iid]
        assert list(sc.columns) == ["exchange_ts", "expected_return", "confidence"]
        assert len(sc) == len(df)
        assert np.array_equal(
            sc["exchange_ts"].to_numpy(), df["exchange_ts"].to_numpy()
        )
        er = sc["expected_return"].to_numpy()
        conf = sc["confidence"].to_numpy()
        assert np.isfinite(er).all(), f"{aid}/{iid}: non-finite expected_return"
        assert np.isfinite(conf).all()
        assert (conf >= 0.0).all() and (conf <= 1.0).all()
        assert np.all(er[conf == 0.0] == 0.0), f"{aid}: er != 0 where conf == 0"


@pytest.mark.parametrize("aid", EXPECTED_IDS)
def test_score_uses_only_declared_features(aid, small_frames, fitted_models):
    """Scoring must be invariant to every column it does not declare
    (label columns included — this is also a leakage guarantee)."""
    m = fitted_models[aid]
    full = m.score(small_frames)
    keep = set(m.features) | {"instrument_id", "exchange_ts"}
    restricted = {
        iid: df[[c for c in df.columns if c in keep]].copy()
        for iid, df in small_frames.items()
    }
    out = m.score(restricted)
    for iid in full:
        assert np.array_equal(
            full[iid]["expected_return"].to_numpy(),
            out[iid]["expected_return"].to_numpy(),
            equal_nan=True,
        ), f"{aid}/{iid}: score depends on undeclared columns"
        assert np.array_equal(
            full[iid]["confidence"].to_numpy(),
            out[iid]["confidence"].to_numpy(),
            equal_nan=True,
        )


@pytest.mark.parametrize("aid", ["EQ01", "EQ08", "EQ11", "FX05", "FX11"])
def test_fit_and_score_deterministic(aid, small_frames):
    m1, m2 = build(aid), build(aid)
    m1.fit(small_frames)
    m2.fit(small_frames)
    assert m1.params() == m2.params()
    s1, s2 = m1.score(small_frames), m2.score(small_frames)
    for iid in s1:
        assert np.array_equal(
            s1[iid]["expected_return"].to_numpy(),
            s2[iid]["expected_return"].to_numpy(),
            equal_nan=True,
        )


@pytest.mark.parametrize("aid", EXPECTED_IDS)
def test_params_roundtrip(aid, small_frames, fitted_models):
    m = fitted_models[aid]
    blob = m.params()
    assert blob["fitted"] is True
    assert "hypothesis_confirmed" in blob
    fresh = build(aid)
    fresh.load_params(blob)
    a = m.score(small_frames)
    b = fresh.score(small_frames)
    for iid in a:
        assert np.array_equal(
            a[iid]["expected_return"].to_numpy(),
            b[iid]["expected_return"].to_numpy(),
            equal_nan=True,
        )


def test_load_params_wrong_alpha_raises(fitted_models):
    blob = fitted_models["EQ01"].params()
    with pytest.raises(ValueError):
        build("EQ02").load_params(blob)


# -- currency exposure machinery (spec §12) -----------------------------


def test_exposure_matrix_rows_sum_to_zero():
    a = exposure_matrix(list(FX_IDS))
    assert a.shape == (8, 8)
    assert np.array_equal(a.sum(axis=1), np.zeros(8))
    # EUR/USD row: +EUR, -USD
    assert a[0].tolist() == [0, 0, 0, 1, 0, 0, 0, -1]


def test_currency_exposures_hand_calc():
    # long 5 EUR/USD, short 3 EUR/GBP, long 2 USD/JPY
    exp = currency_exposures({101: 5, 108: -3, 103: 2})
    assert exp["EUR"] == 2.0    # +5 - 3
    assert exp["GBP"] == 3.0    # -(-3)
    assert exp["USD"] == -3.0   # -5 + 2
    assert exp["JPY"] == -2.0
    assert exp["AUD"] == 0.0
    # exposures conserve: total base+quote nets to zero
    assert sum(exp.values()) == 0.0


def test_factor_solve_recovers_exact_factors():
    f_true = np.array([0.001, -0.002, 0.0005, 0.003, -0.001, 0.002, 0.0])
    a = free_exposure_matrix(list(FX_IDS))
    r = a @ f_true
    f, fitted = solve_factor_returns(list(FX_IDS), r)
    assert np.allclose(f, f_true, atol=1e-12)
    assert np.allclose(fitted, r, atol=1e-12)


def test_factor_solve_single_pair_degrades_to_zero_residual():
    r = np.full(8, np.nan)
    r[0] = 0.004  # only EUR/USD observed
    f, fitted = solve_factor_returns(list(FX_IDS), r)
    assert np.isclose(fitted[0], r[0], atol=1e-12)  # residual exactly 0


# -- round-3: dead alphas and parameter provenance ------------------------


def test_dead_alpha_scores_confidence_zero():
    """sigma == 0 (or beta == 0) means the fit found no evidence: every row
    scores (0, 0), never confidence 1.0."""
    import numpy as np
    import pandas as pd

    from iap.alpha import build

    m = build("EQ01")
    m.load_params({
        "alpha_id": "EQ01", "model": "linear_z_v1", "horizon": m.horizon,
        "features": list(m.features), "mu": 0.0, "sigma": 0.0, "beta": 0.0,
        "beta_fit": 0.0, "z_clip": 4.0, "conf_scale": 2.0, "n_train": 3,
        "fitted": True,
    })
    assert m.is_dead
    n = 16
    df = pd.DataFrame({
        "exchange_ts": np.arange(n, dtype=np.int64) * 1_000_000_000,
        **{f: np.linspace(-3.0, 3.0, n) for f in m.features},
    })
    sc = m.score({1: df})[1]
    assert np.all(sc["expected_return"].to_numpy() == 0.0)
    assert np.all(sc["confidence"].to_numpy() == 0.0)


def test_load_params_rejects_edited_or_impossible_files():
    from iap.alpha import build

    m = build("EQ01")
    good = {
        "alpha_id": "EQ01", "model": "linear_z_v1", "horizon": m.horizon,
        "features": list(m.features), "mu": 0.0, "sigma": 1.0, "beta": 1e-4,
        "beta_fit": 1e-4, "z_clip": 4.0, "conf_scale": 2.0, "n_train": 100,
        "fitted": True,
    }
    m.load_params(dict(good))  # baseline: loads

    for key, val, msg in (
        ("z_clip", 3.0, "pinned"),
        ("conf_scale", 1.0, "pinned"),
        ("model", "other_v1", "unsupported model"),
        ("horizon", "15m", "horizon"),
        ("sigma", 0.0, "dead alpha"),
        ("sigma", float("nan"), "non-finite"),
    ):
        blob = dict(good)
        blob[key] = val
        with pytest.raises(ValueError, match=msg):
            build("EQ01").load_params(blob)

    blob = dict(good)
    blob["features"] = ["not_a_feature_v1"]
    with pytest.raises(ValueError, match="features"):
        build("EQ01").load_params(blob)


def test_params_file_carries_provenance_and_rejects_a_foreign_registry(tmp_path):
    from iap.alpha import build, load_params_file, save_params
    from iap.features.registry import registry_hash

    m = build("EQ01")
    m.load_params({
        "alpha_id": "EQ01", "model": "linear_z_v1", "horizon": m.horizon,
        "features": list(m.features), "mu": 0.0, "sigma": 1.0, "beta": 1e-4,
        "beta_fit": 1e-4, "z_clip": 4.0, "conf_scale": 2.0, "n_train": 100,
        "fitted": True,
    })
    path = tmp_path / "alpha_params.json"
    blob = save_params({"EQ01": m}, path)
    assert blob["x-version"] == 2
    assert blob["feature_version"] == registry_hash()
    assert len(blob["data_version"]) >= 16
    assert "git_commit" in blob and "git_dirty" in blob
    # loads against the running registry
    assert "EQ01" in load_params_file(path)
    # and is rejected against a different one
    with pytest.raises(ValueError, match="feature_version"):
        load_params_file(path, expected_feature_version="0" * 64)


def test_fx05_universe_excludes_singleton_currencies():
    """A currency seen in ONE pair has its factor absorb that pair's whole
    return: the residual is 0 by construction, so the pair must score NaN
    rather than a constant."""
    import numpy as np

    from iap.alpha.fx_exposure import (
        FX05CrossPairRelativeValue,
        identified_pairs,
        solve_factor_returns,
    )

    ids = [101, 102, 103, 104, 105, 106, 107, 108]
    ident = identified_pairs(ids, [True] * 8)
    # only the EUR/USD - GBP/USD - EUR/GBP triangle is identified
    assert list(ident) == [True, True, False, False, False, False, False, True]
    assert list(identified_pairs([101, 102, 108], [True] * 3)) == [True] * 3
    assert list(identified_pairs([101, 103], [True] * 2)) == [False, False]

    # the singleton pairs really do have a zero residual
    r = np.array([0.001, 0.0005, 0.002, -0.001, 0.0, 0.0007, -0.0003, 0.0004])
    _, fitted = solve_factor_returns(ids, r)
    for i in (2, 3, 4, 5, 6):
        assert abs(r[i] - fitted[i]) < 1e-12

    m = FX05CrossPairRelativeValue()
    m._pair_ids = ids
    mat = np.tile(r.reshape(-1, 1), (1, 4))
    sig = m.grid_signals(mat)
    assert np.all(np.isnan(sig[2:7, :])), "singleton pairs must score NaN"
    assert np.all(np.isfinite(sig[[0, 1, 7], :]))

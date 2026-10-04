"""Signal combination (iap.combine).

Covers: the four weight estimators and their building blocks (standardise,
Ledoit-Wolf shrinkage, the nested ridge penalty search, pairwise signal
correlation, effective independent bets, the row-count-independent blend);
``CombinedAlpha`` as an ``AlphaModel`` (fit / score / params round trip,
dead combinations, the member purge); the LEAKAGE proofs — the fitted
parameters of every walk-forward fold are bit-identical when the rows from
the test start onwards are truncated away, garbled or shifted, a combiner
that does read them is caught by the same test, and the platform's own
leakage detectors pass on the combination; the look accounting and the
experiment identity; the committed ``research/combination`` artefacts; and
one run of the report pipeline on the bundled data.

The unit tests use toy member alphas over synthetic frames (SplitMix64, no
wall clock), so they run without the dataset.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from iap.alpha import ALPHA_IDS
from iap.alpha.base import LinearAlpha
from iap.combine import (
    DEFAULT_METHOD,
    INNER_FOLDS,
    METHODS,
    RIDGE_PENALTIES,
    CombinedAlpha,
    combination_horizon,
    correlation_matrix,
    effective_bets,
    fit_weights,
    ledoit_wolf,
    member_purged_train,
    standardise,
)
from iap.combine import report as combine_report
from iap.combine.weights import apply_standardisation, blend
from iap.core.rng import SplitMix64
from iap.research.__main__ import main as research_main
from iap.validation.leakage import LeakageTester
from iap.validation.ledger import ExperimentLedger
from iap.validation.metrics import HORIZONS_NS, ic
from iap.validation.splits import WalkForwardSplitter
from iap.validation.validate import looks_per_validation

ROOT = Path(__file__).resolve().parents[2]
COMBINATION_DIR = ROOT / "research" / "combination"
HAS_DATA = (ROOT / "data" / "features" / "features_1.parquet").is_file()
NS = 1_000_000_000
T0 = 1_700_000_000 * NS
HORIZON = "5s"
N_FEATURES = 4


def _normal(rng: SplitMix64, n: int) -> np.ndarray:
    """Standard normals from the pinned RNG (Box-Muller)."""
    u1 = np.array([max(rng.uniform(), 1e-12) for _ in range(n)])
    u2 = np.array([rng.uniform() for _ in range(n)])
    return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)


def _toy(alpha_id: str, feature: str, horizon: str = HORIZON) -> type[LinearAlpha]:
    class Toy(LinearAlpha):
        """A toy member: one feature column, as is.

        Economic rationale: none — a test double for the combination tests.
        """

        def raw_signal(self, df: pd.DataFrame) -> pd.Series:
            return df[feature]

    Toy.alpha_id = alpha_id
    Toy.name = f"toy {feature}"
    Toy.asset_class = "EQUITY"
    Toy.horizon = horizon
    Toy.features = (feature,)
    return Toy


TOYS = {f"T{k + 1}": _toy(f"T{k + 1}", f"f{k + 1}") for k in range(N_FEATURES)}
TOYS["TL"] = _toy("TL", "f1", "1m")  # a member with a longer label horizon


def toy_factory(alpha_id: str) -> LinearAlpha:
    return TOYS[alpha_id]()


def make_frames(seed: int = 7, n: int = 1500, instruments=(1, 2, 3)) -> dict[int, pd.DataFrame]:
    """Synthetic feature frames: four features, of which f1 and f2 carry a
    weak signal for the 5 s label, f3 is a noisy copy of f1 and f4 is noise;
    one row per second."""
    rng = SplitMix64(seed)
    frames = {}
    for iid in instruments:
        f = [_normal(rng, n) for _ in range(N_FEATURES)]
        f[2] = 0.9 * f[0] + 0.1 * f[2]
        noise = _normal(rng, n)
        label = 1e-4 * (0.15 * f[0] + 0.10 * f[1] + noise)
        f[3][::7] = np.nan  # the fourth member has no opinion on some rows
        frame = {"exchange_ts": T0 + np.arange(n, dtype=np.int64) * NS}
        for k in range(N_FEATURES):
            frame[f"f{k + 1}"] = f[k]
        for h in (HORIZON, "1m"):
            frame[f"label_mid_{h}"] = label
            frame[f"label_valid_{h}"] = True
        frame["spread_ticks_v1"] = 1.0
        frames[iid] = pd.DataFrame(frame)
    return frames


def combo(method: str = DEFAULT_METHOD, members=("T1", "T2", "T3", "T4"), **kw) -> CombinedAlpha:
    return CombinedAlpha(list(members), method, horizon=HORIZON, member_factory=toy_factory, **kw)


def _fitted_json(model: CombinedAlpha) -> str:
    return json.dumps(model.params(), sort_keys=True)


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------


def _stack(seed: int = 3, n: int = 900):
    rng = SplitMix64(seed)
    z = np.column_stack([_normal(rng, n) for _ in range(4)])
    z[:, 2] = z[:, 0] + 0.05 * z[:, 2]  # a near-duplicate of member 0
    y = 0.2 * z[:, 0] + 0.1 * z[:, 1] - 0.1 * z[:, 3] + _normal(rng, n)
    folds = np.repeat([1, 2, 3], n // 3)
    ts = np.arange(n, dtype=np.int64) * NS
    return z, y, folds, ts


def test_methods_and_default_are_pinned():
    assert METHODS == ("equal_weight", "ic_weighted", "ridge", "shrinkage_mv")
    assert DEFAULT_METHOD == "equal_weight"
    assert RIDGE_PENALTIES == (0.01, 0.1, 1.0, 10.0, 100.0)
    assert INNER_FOLDS == 3
    with pytest.raises(ValueError, match="unknown combination method"):
        fit_weights("median", np.zeros((40, 2)), np.zeros(40))
    with pytest.raises(ValueError, match="unknown combination method"):
        combo("median")


def test_standardise_uses_finite_rows_and_marks_inactive_members():
    z, *_ = _stack()
    z[:100, 1] = np.nan
    z[:, 3] = 5.0  # no variance
    short = np.full(len(z), np.nan)
    short[:10] = np.arange(10.0)  # fewer than MIN_OBS finite rows
    std = standardise(np.column_stack([z, short]))
    assert std.active.tolist() == [True, True, True, False, False]
    assert np.all(std.values[:100, 1] == 0.0) and np.all(std.values[:, 3:] == 0.0)
    ok = np.isfinite(z[:, 1])
    assert abs(float(np.mean(std.values[ok, 1]))) < 1e-12
    assert abs(float(np.std(std.values[ok, 1])) - 1.0) < 1e-12
    # the stored mean / scale reproduce the standardisation on other rows
    again = apply_standardisation(z[:50], std.mean[:4], std.scale[:4])
    assert np.array_equal(again[:, 0], std.values[:50, 0])


def test_equal_weight_never_reads_the_label():
    z, y, folds, ts = _stack()
    a, _ = fit_weights("equal_weight", z, y)
    b, _ = fit_weights("equal_weight", z, -y[::-1])
    assert np.array_equal(a.weights, b.weights) and np.allclose(a.weights, 0.25)
    z[:, 1] = 1.0  # an inactive member gets no weight; the rest share it
    c, _ = fit_weights("equal_weight", z, y)
    assert c.weights[1] == 0.0 and np.allclose(c.weights[[0, 2, 3]], 1.0 / 3.0)


def test_ic_weighted_gives_no_weight_to_a_member_that_contradicts_its_hypothesis():
    z, y, folds, ts = _stack()
    fit, _ = fit_weights("ic_weighted", z, y)
    ics = [ic(z[:, k], y) for k in range(4)]
    assert ics[3] < 0.0 and fit.weights[3] == 0.0
    assert all(fit.weights[k] > 0.0 for k in (0, 1, 2))
    assert abs(float(np.sum(np.abs(fit.weights))) - 1.0) < 1e-12
    assert fit.weights[0] > fit.weights[1]  # the stronger member weighs more
    # every member against its hypothesis: nothing to weight
    dead, _ = fit_weights("ic_weighted", z[:, [3]], y)
    assert np.all(dead.weights == 0.0)


def test_correlation_aware_methods_split_the_weight_of_duplicates():
    """Members 0 and 2 are near-duplicates.  ``equal_weight`` gives the pair
    half of the total; ``ridge`` and ``shrinkage_mv`` give the pair about
    what one member would get, and may short the contradicting member."""
    z, y, folds, ts = _stack()
    purge = HORIZONS_NS[HORIZON]
    equal, _ = fit_weights("equal_weight", z, y)
    assert abs(equal.weights[0] + equal.weights[2] - 0.5) < 1e-12
    for method in ("ridge", "shrinkage_mv"):
        fit, _ = fit_weights(method, z, y, folds, ts, purge)
        assert abs(float(np.sum(np.abs(fit.weights))) - 1.0) < 1e-12
        assert fit.weights[3] < 0.0, method
        solo, _ = fit_weights(method, z[:, [0, 1, 3]], y, folds, ts, purge)
        pair = fit.weights[0] + fit.weights[2]
        assert abs(pair - solo.weights[0]) < 0.08, (method, pair, solo.weights[0])


def test_ridge_penalty_is_chosen_inside_the_stack_by_forward_cv():
    z, y, folds, ts = _stack()
    fit, _ = fit_weights("ridge", z, y, folds, ts, HORIZONS_NS[HORIZON])
    assert fit.detail["ridge_penalty"] in RIDGE_PENALTIES
    assert fit.detail["cv_splits"] == 2  # folds 2 and 3 are scored
    sse = fit.detail["cv_sse"]
    assert sorted(sse) == sorted(f"{p:g}" for p in RIDGE_PENALTIES)
    assert sse[f"{fit.detail['ridge_penalty']:g}"] == min(sse.values())
    # no fold structure: the largest penalty, and the detail says so
    alone, _ = fit_weights("ridge", z, y)
    assert alone.detail["ridge_penalty"] == RIDGE_PENALTIES[-1] and alone.detail["cv_splits"] == 0
    one, _ = fit_weights("ridge", z, y, np.ones(len(y), dtype=int), ts, 0)
    assert one.detail["cv_splits"] == 0
    # deterministic
    again, _ = fit_weights("ridge", z, y, folds, ts, HORIZONS_NS[HORIZON])
    assert np.array_equal(fit.weights, again.weights)


def test_ledoit_wolf_shrinks_towards_a_scaled_identity():
    rng = SplitMix64(11)
    x = np.column_stack([_normal(rng, 400) for _ in range(5)])
    x[:, 1] = 0.8 * x[:, 0] + 0.6 * x[:, 1]  # a correlated pair: not a scaled identity
    x[:, 4] = 3.0 * x[:, 4]
    sigma, delta, mu = ledoit_wolf(x)
    xc = x - x.mean(axis=0)
    s = xc.T @ xc / len(x)
    assert 0.0 < delta < 1.0 and abs(mu - np.trace(s) / 5) < 1e-12
    assert np.allclose(sigma, delta * mu * np.eye(5) + (1 - delta) * s)
    assert np.allclose(sigma, sigma.T)
    # fewer rows -> more shrinkage; a singular sample covariance becomes invertible
    _, delta_small, _ = ledoit_wolf(x[:20])
    assert delta_small > delta
    dup = np.column_stack([x[:, 0], x[:, 0], x[:, 1]])
    sigma_dup, delta_dup, _ = ledoit_wolf(dup)
    assert delta_dup > 0.0 and np.linalg.cond(sigma_dup) < 1e12
    eye, delta_eye, _ = ledoit_wolf(np.array([[1.0, 1.0], [-1.0, -1.0], [1.0, -1.0], [-1.0, 1.0]]))
    assert delta_eye == 1.0 and np.allclose(eye, np.eye(2))
    with pytest.raises(ValueError):
        ledoit_wolf(np.zeros((1, 3)))


def test_correlation_matrix_is_pairwise_complete():
    z, *_ = _stack()
    z[:880, 3] = np.nan  # 20 finite rows: below MIN_OBS
    corr, n = correlation_matrix(z)
    assert corr[0, 0] == 1.0 and np.isnan(corr[3, 3]) and np.isnan(corr[0, 3])
    assert n[0, 3] == 20 and n[0, 1] == len(z)
    assert corr[0, 2] > 0.99 and corr[0, 2] == corr[2, 0]
    assert abs(corr[0, 1] - np.corrcoef(z[:, 0], z[:, 1])[0, 1]) < 1e-12


def test_effective_bets_is_the_participation_ratio():
    assert effective_bets(np.eye(6))["n_effective"] == pytest.approx(6.0)
    assert effective_bets(np.ones((6, 6)))["n_effective"] == pytest.approx(1.0)
    rho = 0.5
    c = np.full((4, 4), rho) + (1 - rho) * np.eye(4)
    bets = effective_bets(c)
    # eigenvalues 1 + 3 rho and three times 1 - rho
    assert bets["eigenvalues"] == pytest.approx([2.5, 0.5, 0.5, 0.5])
    assert bets["n_effective"] == pytest.approx(16.0 / (2.5**2 + 3 * 0.25))
    assert bets["mean_offdiag"] == pytest.approx(rho)
    # a member with no defined correlation is dropped, not counted as a bet
    c2 = np.eye(3)
    c2[2, :] = c2[:, 2] = np.nan
    assert effective_bets(c2)["n_members"] == 2
    assert effective_bets(np.full((2, 2), np.nan))["n_effective"] == 0.0


def test_blend_does_not_depend_on_the_number_of_rows():
    """The score of a row must be bit-identical whatever rows follow it (the
    truncation probe); a BLAS product does not guarantee that."""
    rng = SplitMix64(5)
    values = np.column_stack([_normal(rng, 257) for _ in range(12)])
    weights = _normal(rng, 12)
    weights[4] = 0.0
    full = blend(values, weights)
    for n in (1, 2, 7, 64, 255):
        assert np.array_equal(blend(values[:n], weights), full[:n])
    assert np.allclose(full, values @ weights)


# --------------------------------------------------------------------------
# CombinedAlpha
# --------------------------------------------------------------------------


def test_combined_alpha_identity_and_horizon_rule():
    model = combo()
    assert model.alpha_id == "COMB_EQUITY" and model.asset_class == "EQUITY"
    assert model.member_ids == ("T1", "T2", "T3", "T4") and model.horizon == HORIZON
    assert model.features == ("f1", "f2", "f3", "f4")
    assert "Economic rationale:" in CombinedAlpha.economic_rationale()
    assert combination_horizon(["1s", "5s", "10s"]) == "5s"
    assert combination_horizon(["1s", "5s", "10s", "15m"]) == "5s"  # the LOWER median
    assert combination_horizon(["5m", "1m", "500ms"]) == "1m"
    with pytest.raises(ValueError):
        combination_horizon([])
    with pytest.raises(ValueError, match="non-empty and unique"):
        combo(members=("T1", "T1"))
    with pytest.raises(ValueError, match="inner_folds"):
        combo(inner_folds=1)
    with pytest.raises(RuntimeError, match="before fit"):
        combo().score(make_frames())


def test_default_combinations_use_every_alpha_at_the_median_horizon():
    """No member selection: all twelve alphas of an asset class, whatever
    their verdict; the horizon is the members' lower-median horizon."""
    eq = combine_report.default_members("EQUITY")
    fx = combine_report.default_members("FX")
    assert sorted(eq + fx) == sorted(ALPHA_IDS) and len(eq) == len(fx) == 12
    assert CombinedAlpha(eq).horizon == "5s" and CombinedAlpha(fx).horizon == "1m"
    with pytest.raises(ValueError, match="span asset classes"):
        CombinedAlpha(["EQ01", "FX01"])


@pytest.mark.parametrize("method", METHODS)
def test_fit_score_and_params_round_trip(method):
    frames = make_frames()
    train = {i: df.iloc[:1000].reset_index(drop=True) for i, df in frames.items()}
    test = {i: df.iloc[1000:].reset_index(drop=True) for i, df in frames.items()}
    model = combo(method)
    model.fit(train)
    params = model.params()
    assert params["fitted"] and not params["dead"] and params["method"] == method
    assert params["n_stack_rows"] > 0 and params["beta"] > 0.0 and params["hypothesis_confirmed"]
    assert abs(sum(abs(w) for w in params["weights"].values()) - 1.0) < 1e-12
    scores = model.score(test)
    assert sorted(scores) == [1, 2, 3]
    z, y = [], []
    for iid, sc in scores.items():
        assert list(sc.columns) == ["exchange_ts", "expected_return", "confidence"]
        assert len(sc) == len(test[iid])
        conf = sc["confidence"].to_numpy()
        assert np.all((conf >= 0.0) & (conf <= 1.0))
        z.append(sc["expected_return"].to_numpy() / params["beta"])
        y.append(test[iid][f"label_mid_{HORIZON}"].to_numpy())
    # the planted signal is found out of sample, and beats the best member alone
    assert ic(np.concatenate(z), np.concatenate(y)) > 0.1
    # JSON round trip reproduces the scores bit for bit
    clone = combo(method)
    clone.load_params(json.loads(json.dumps(params)))
    again = clone.score(test)
    for iid in scores:
        assert scores[iid].equals(again[iid])
    with pytest.raises(ValueError, match="members differ"):
        combo(method, members=("T1", "T2")).load_params(params)


def test_a_window_too_short_to_stack_gives_a_dead_combination():
    frames = make_frames(n=40)
    model = combo("ridge")
    model.fit(frames)
    assert model.is_dead and model.params()["n_stack_rows"] == 0
    scores = model.score(frames)
    for sc in scores.values():
        assert np.all(sc["expected_return"] == 0.0) and np.all(sc["confidence"] == 0.0)


def test_member_purge_drops_the_rows_a_longer_label_would_reach_past():
    frames = make_frames(n=300)
    same = member_purged_train(frames, HORIZON, HORIZON)
    assert all(len(same[i]) == 300 for i in frames)
    purged = member_purged_train(frames, "1m", HORIZON)
    end = T0 + 299 * NS
    for df in purged.values():
        assert int(df["exchange_ts"].iloc[-1]) <= end - (HORIZONS_NS["1m"] - HORIZONS_NS[HORIZON])
        assert len(df) == 300 - 55
    # a combination with the long-horizon member fits it on the purged rows
    model = combo(members=("T1", "T2", "TL"))
    model.fit(frames)
    assert model.params()["member_params"]["TL"]["n_train"] == 3 * (300 - 55)
    assert model.params()["member_params"]["T1"]["n_train"] == 3 * 300


# --------------------------------------------------------------------------
# Leakage: no fitted number is a function of a test row
# --------------------------------------------------------------------------


def _fold_params(frames, method: str) -> list[str]:
    """The fitted parameters of the combination in every walk-forward fold."""
    splitter = WalkForwardSplitter(n_folds=4, embargo_ns=60 * NS)
    out = []
    for _fold, train, _test in splitter.split_frames(frames, HORIZONS_NS[HORIZON]):
        model = combo(method)
        model.fit(train)
        out.append(_fitted_json(model))
    return out


def _folds(frames):
    splitter = WalkForwardSplitter(n_folds=4, embargo_ns=60 * NS)
    return [f for f, _, _ in splitter.split_frames(frames, HORIZONS_NS[HORIZON])]


def _garble_from(frames, start_ts: int, what: str):
    out = {}
    for iid, df in frames.items():
        df = df.copy()
        late = df["exchange_ts"].to_numpy() >= start_ts
        if what == "truncate":
            df = df[~late].reset_index(drop=True)
        elif what == "labels":
            for h in (HORIZON, "1m"):
                df.loc[late, f"label_mid_{h}"] = 0.12345
        elif what == "shift":  # label_t <- label_{t+1} on the late rows
            for h in (HORIZON, "1m"):
                col = df[f"label_mid_{h}"].to_numpy().copy()
                col[late] = np.roll(col, -1)[late]
                df[f"label_mid_{h}"] = col
        elif what == "features":
            for k in range(N_FEATURES):
                df.loc[late, f"f{k + 1}"] = df.loc[late, f"f{k + 1}"] * 1000.0 + 7.0
        else:
            raise AssertionError(what)
        out[iid] = df
    return out


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("what", ["labels", "shift", "features"])
def test_fold_parameters_ignore_everything_from_the_test_start_on(method, what):
    """Shift / garble test: for every outer fold, corrupt the labels (or
    shift them by one row, or rescale the features) of every row from that
    fold's test start onwards.  Weights, standardisation, ridge penalty,
    shrinkage and scale of that fold must be bit-identical."""
    frames = make_frames()
    reference = _fold_params(frames, method)
    for k, fold in enumerate(_folds(frames)):
        corrupted = _fold_params(_garble_from(frames, fold.test_start, what), method)
        assert corrupted[k] == reference[k], (method, what, fold.index)


@pytest.mark.parametrize("method", METHODS)
def test_fold_parameters_survive_truncating_the_data_at_the_test_start(method):
    """Truncation test: fitting on the training rows the validation hands
    over equals fitting on the same rows of a dataset that simply ENDS where
    the test segment starts — the future does not exist for the fit."""
    frames = make_frames()
    splitter = WalkForwardSplitter(n_folds=4, embargo_ns=60 * NS)
    horizon_ns = HORIZONS_NS[HORIZON]
    for fold, train, _test in splitter.split_frames(frames, horizon_ns):
        model = combo(method)
        model.fit(train)
        cut = _garble_from(frames, fold.test_start, "truncate")
        rows = {
            i: df[fold.train_mask(df["exchange_ts"].to_numpy(), horizon_ns, 60 * NS)].reset_index(
                drop=True
            )
            for i, df in cut.items()
        }
        truncated = combo(method)
        truncated.fit(rows)
        assert _fitted_json(truncated) == _fitted_json(model), (method, fold.index)


@pytest.mark.parametrize("method", ["ic_weighted", "ridge", "shrinkage_mv"])
def test_the_leakage_test_has_power_against_a_combiner_that_reads_test_rows(method):
    """Negative control: fit the same combination on train + test rows (the
    mistake the protocol exists to prevent) and the garbled labels DO move
    the parameters — so the tests above would catch it."""
    frames = make_frames()
    fold = _folds(frames)[-1]
    honest, leaky = combo(method), combo(method)
    honest.fit(frames)
    leaky.fit(_garble_from(frames, fold.test_start, "labels"))
    assert _fitted_json(honest) != _fitted_json(leaky)


@pytest.mark.parametrize("method", METHODS)
def test_platform_leakage_detectors_pass_on_the_combination(method):
    """The label guard and the truncation probe of ``iap.validation.leakage``
    on a fitted combination: scoring reads no label and no later row.  (The
    shift detector is not asserted here: the toy features are independent
    from row to row with an IC of 0.2, which is exactly the signature it
    exists to flag; on the bundled data it passes — the committed report.)"""
    frames = make_frames()
    train = {i: df.iloc[:1000].reset_index(drop=True) for i, df in frames.items()}
    test = {i: df.iloc[1000:].reset_index(drop=True) for i, df in frames.items()}
    model = combo(method)
    model.fit(train)
    result = LeakageTester().run(model, test)
    assert result.label_guard_ok and result.truncation_ok
    assert result.recompute_ok is None  # no raw events behind synthetic frames


def test_standardisation_of_test_rows_uses_training_statistics_only():
    frames = make_frames()
    train = {i: df.iloc[:1000].reset_index(drop=True) for i, df in frames.items()}
    model = combo("equal_weight")
    model.fit(train)
    test = {i: df.iloc[1000:].reset_index(drop=True) for i, df in frames.items()}
    base = model.score(test)
    # append wildly scaled rows AFTER the scored ones: earlier scores are unchanged
    extended = {}
    for i, df in test.items():
        tail = df.copy()
        tail["exchange_ts"] = tail["exchange_ts"] + len(df) * NS
        for k in range(N_FEATURES):
            tail[f"f{k + 1}"] = tail[f"f{k + 1}"] * 1e6
        extended[i] = pd.concat([df, tail], ignore_index=True)
    longer = model.score(extended)
    for i in base:
        assert base[i].equals(longer[i].iloc[: len(base[i])].reset_index(drop=True))


# --------------------------------------------------------------------------
# Looks, identity, report
# --------------------------------------------------------------------------


def test_a_combination_costs_the_validation_chain_plus_one_look_per_member():
    assert combine_report.combination_looks(12) == looks_per_validation(4) + 12 == 95
    assert combine_report.combination_looks(3) == 86
    with pytest.raises(ValueError):
        combine_report.combination_looks(0)
    a = combine_report.experiment_identity("EQUITY", ["EQ03", "EQ01"], "ridge", "5s")
    assert a["members"] == ["EQ01", "EQ03"] and a["method"] == "ridge"
    assert a["ridge_penalties"] == list(RIDGE_PENALTIES) and a["methods"] == "v2"
    keys = {
        ExperimentLedger.experiment_key("COMB_EQ", combine_report.LEDGER_KIND, cfg, "d" * 64)
        for cfg in (
            a,
            combine_report.experiment_identity("EQUITY", ["EQ01", "EQ03"], "ridge", "5s"),
            combine_report.experiment_identity("EQUITY", ["EQ01", "EQ03"], "equal_weight", "5s"),
            combine_report.experiment_identity("EQUITY", ["EQ01", "EQ02"], "ridge", "5s"),
            combine_report.experiment_identity("EQUITY", ["EQ01", "EQ03"], "ridge", "10s"),
        )
    }
    # member order is not identity; members, method and horizon are
    assert len(keys) == 4


def test_member_pass_on_toy_members_matches_direct_computation():
    frames = make_frames()

    class _NoTrades:
        """A backtester stand-in: the member pass only needs bars and counts."""

        def for_horizon(self, horizon):
            return self

        def run(self, frames, scores, asset_class):
            class _Result:
                per_instrument: dict = {}
                trade_count = 0
                total_pnl = 0.0

            return _Result()

    ids = ["T1", "T2", "T3", "T4"]
    mp = combine_report.member_pass(
        frames,
        ids,
        HORIZON,
        "EQUITY",
        _NoTrades(),
        METHODS,
        ic_rows="valid_only",
        member_factory=toy_factory,
    )
    n_test = sum(
        int(f.test_mask(frames[1]["exchange_ts"].to_numpy()).sum()) for f in _folds(frames)
    )
    assert mp["z"].shape == (3 * n_test, 4) and mp["y"].shape == (3 * n_test,)
    corr, _ = correlation_matrix(mp["z"])
    assert corr[0, 2] > 0.95 and abs(corr[0, 1]) < 0.2
    bets = effective_bets(corr)
    assert 2.0 < bets["n_effective"] < 4.0  # four members, two of them one bet
    for method in METHODS:
        folds = mp["weights_by_fold"][method]
        assert [f["fold"] for f in folds] == [1, 2, 3, 4]
        assert ic(mp["combined"][method], mp["y"]) > 0.05
        # the per-fold weights are the ones a fresh fit of that fold gives
        assert [json.dumps(f["weights"], sort_keys=True) for f in folds] == [
            json.dumps(json.loads(p)["weights"], sort_keys=True)
            for p in _fold_params(frames, method)
        ]
    equal = mp["weights_by_fold"]["equal_weight"]
    assert all(w == 0.25 for f in equal for w in f["weights"].values())


def test_combine_cli_rejects_unknown_methods_and_member_lists(tmp_path, capsys):
    args = ["combine", "--dry-run", "--ledger", str(tmp_path / "ledger.json")]
    assert research_main([*args, "--method", "median", "--features-dir", str(tmp_path)]) == 1
    assert "unknown combination method" in capsys.readouterr().err
    assert research_main([*args, "--members", "EQ01,EQ02"]) == 1
    assert "--members needs one --asset-class" in capsys.readouterr().err
    assert not (tmp_path / "ledger.json").exists()


@pytest.fixture(scope="module")
def committed():
    return json.loads((COMBINATION_DIR / "COMBINATION.json").read_text(encoding="utf-8"))


def test_committed_report_evaluates_eight_experiments_and_promotes_none(committed):
    assert committed["x-version"] == combine_report.COMBINATION_X_VERSION
    assert committed["default_method"] == DEFAULT_METHOD and committed["methods_bundle"] == "v2"
    assert sorted(committed["combinations"]) == ["EQUITY", "FX"]
    looks = committed["looks"]
    assert looks["experiments"] == 8 and looks["declared_by_this_report"] == 8 * 95
    assert looks["gate_looks"] == looks["ledger_before"] + looks["declared_by_this_report"]
    assert looks["tstat_threshold"] == pytest.approx(
        ExperimentLedger.bonferroni_t_threshold_at(looks["gate_looks"])
    )
    for ac, block in committed["combinations"].items():
        assert block["members"] == combine_report.default_members(ac)
        assert sorted(block["methods"]) == sorted(METHODS)
        assert all(block["weights_ignore_test_rows"].values()), ac
        for name, m in block["methods"].items():
            # honest result: nothing is promotable, and the reason is costs
            assert m["verdict"] != "PROMOTE", (ac, name)
            assert not m["promote_gates"]["cost"], (ac, name)
            assert m["leakage"]["passed"] and m["leakage"]["recompute_ok"] is True, (ac, name)
            assert m["tstat_threshold"] == looks["tstat_threshold"]
            # the member pass and the validation chain measure the same signal
            assert m["member_pass_check"]["gate_ic"] == pytest.approx(m["gate_ic"], abs=1e-12)
            report = json.loads(
                (COMBINATION_DIR / "reports" / f"{block['combination_id']}.{name}.json").read_text(
                    encoding="utf-8"
                )
            )
            assert report["verdict"] == m["verdict"] and report["gate_ic"] == m["gate_ic"]
            assert report["experiment_id"] == m["experiment_id"]
            assert report["combination"]["members"] == block["members"]
            assert len(m["weights_by_fold"]) == 4
        equal = block["methods"]["equal_weight"]
        assert equal["weight_stability"]["mean_pairwise_cosine"] == pytest.approx(1.0)
        bets = block["effective_bets"]
        assert 1.0 <= bets["n_effective"] <= bets["n_members"] <= 12
        assert block["breadth"]["measured_ic_equal_weight"] == equal["gate_ic"]


def test_committed_ledger_holds_the_eight_combination_entries(committed):
    ledger = json.loads((ROOT / "research" / "experiments.json").read_text(encoding="utf-8"))
    entries = [e for e in ledger["entries"] if e["kind"] == combine_report.LEDGER_KIND]
    assert len(entries) == 8 and {e["alpha_id"] for e in entries} == {"COMB_EQ", "COMB_FX"}
    assert {e["count"] for e in entries} == {95}
    assert {e["gate_looks"] for e in entries} == {committed["looks"]["gate_looks"]}
    assert {e["config"]["method"] for e in entries} == set(METHODS)
    assert all(len(e["config"]["members"]) == 12 for e in entries)
    ids = {
        m["experiment_id"]
        for block in committed["combinations"].values()
        for m in block["methods"].values()
    }
    assert {e["result"]["experiment_id"] for e in entries} == ids and len(ids) == 8


def test_committed_correlation_document_feeds_the_lifecycle_gate(committed):
    doc = json.loads((COMBINATION_DIR / "signal_correlation.json").read_text(encoding="utf-8"))
    assert doc["x-version"] == combine_report.CORRELATION_X_VERSION
    assert doc["alpha_ids"] == sorted(ALPHA_IDS)
    assert doc == json.loads(
        json.dumps(combine_report.correlation_document(committed), sort_keys=True)
    )
    corr = doc["correlation"]
    for a in ALPHA_IDS:
        assert sorted(corr[a]) == sorted(x for x in ALPHA_IDS if x != a)
        for b, rho in corr[a].items():
            assert -1.0 <= rho <= 1.0 and corr[b][a] == rho
            if a[:2] != b[:2]:  # an equity and an FX alpha share no row
                assert rho == 0.0 and doc["n_common"][a][b] == 0
    # the three order-flow alphas are one bet: the gate would hold two of them
    assert corr["EQ02"]["EQ12"] > 0.7 and corr["EQ02"]["EQ03"] > 0.7
    assert "EQ02/EQ03" in (COMBINATION_DIR / "REPORT.md").read_text(encoding="utf-8")


@pytest.mark.skipif(not HAS_DATA, reason="bundled dataset not generated")
def test_report_pipeline_on_the_bundled_data(tmp_path):
    """Three equity members under the default method through the whole
    chain: the looks are debited once, a rerun adds none and reproduces the
    document, and the result is not promotable."""
    ledger_path = tmp_path / "experiments.json"
    kwargs = dict(
        asset_classes=["EQUITY"],
        method_names=[DEFAULT_METHOD],
        members={"EQUITY": ["EQ06", "EQ01", "EQ03"]},
        ledger_path=ledger_path,
    )
    first = combine_report.run_combination(ROOT, **kwargs)
    doc = first["document"]
    assert first["correlation"] is None  # not the full member lists: no gate document
    assert doc["looks"]["declared_by_this_report"] == 86 == doc["looks"]["gate_looks"]
    block = doc["combinations"]["EQUITY"]
    assert block["members"] == ["EQ01", "EQ03", "EQ06"] and block["horizon"] == "5s"
    result = block["methods"][DEFAULT_METHOD]
    assert result["verdict"] != "PROMOTE" and result["leakage"]["passed"]
    assert block["weights_ignore_test_rows"] == {DEFAULT_METHOD: True}
    ledger = ExperimentLedger(ledger_path)
    assert ledger.total_experiments == 86 and len(ledger.entries) == 1
    second = combine_report.run_combination(ROOT, **kwargs)
    assert ExperimentLedger(ledger_path).total_experiments == 86
    assert json.dumps(second["document"], sort_keys=True) == json.dumps(doc, sort_keys=True)
    paths = combine_report.write_reports(first, tmp_path / "out", 0.7)
    assert paths["md"].read_text(encoding="utf-8").startswith("# Signal combination report")
    assert "correlation" not in paths
    assert (tmp_path / "out" / "reports" / "COMB_EQ.equal_weight.json").is_file()

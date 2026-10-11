"""v1.13 research-validity items (quality review 2026-10-11): R-P1-2
(frozen-parameter scoring, pre-registered threshold), R-P1-4 (auction
orientation), R-P1-5 (study summariser), R-P1-6 (post-block embargo),
R-P2-9 (event calendar file), R-P2-1 (beta sign consistency), R-P2-7
(DSR trials = registered hypotheses)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from conftest import CONFIGS_DIR, GOLDEN_DIR
from iap.research.golden import golden_frames, golden_spec
from iap.research.runner import ExperimentRunner
from iap.research.study_stats import MIN_SESSIONS_FOR_CI, mde, summarise_study
from iap.validation import sessions as sess
from iap.validation.cpcv import CombinatorialPurgedSplitter
from iap.validation.deflated import (
    TRIALS_BASIS_REGISTERED,
    registered_hypotheses_trials,
)
from iap.validation.metrics import HORIZONS_NS
from iap.validation.splits import (
    Fold,
    WalkForwardSplitter,
    longest_feature_window_ns,
    post_block_embargo_ns,
)
from iap.validation.validate import beta_sign_consistency, effective_gates, frozen_params_block
from test_research_validity_v19 import _multi_day_frames, _TwoInstrumentAlpha, _validate

NS_S = 1_000_000_000

# ------------------------------------------------------------ R-P1-2 (a)


def _fitted_params(frames):
    m = _TwoInstrumentAlpha()
    m.fit(frames)
    return m.params()


def test_frozen_params_scores_without_refit_beside_refit_stats():
    frames = _multi_day_frames(per_day=400)
    base = _validate(frames)
    assert "frozen_ic" not in base and "frozen" not in base  # opt-in
    rep = _validate(frames, frozen_params=_fitted_params(frames))
    assert rep["frozen"]["refit"] is False and rep["frozen"]["mode"] == "frozen_params"
    # the backwards fixture is backwards with frozen params too
    assert rep["frozen_ic"] < 0 and rep["frozen_t"] < 0
    # the refit statistics are untouched
    assert rep["gate_ic"] == base["gate_ic"] and rep["verdict"] == base["verdict"]


def test_frozen_params_block_never_calls_fit():
    frames = _multi_day_frames(per_day=300)
    params = _fitted_params(frames)

    class NoFit(_TwoInstrumentAlpha):
        def fit(self, train):  # pragma: no cover - must not run
            raise AssertionError("frozen scoring refit the model")

    probe = NoFit()
    out = frozen_params_block(NoFit, frames, params, probe.horizon, HORIZONS_NS[probe.horizon])
    assert out["frozen_ic"] is not None and out["n_pairs"] > 0
    flipped = dict(params, beta=-params["beta"], beta_fit=-params["beta_fit"])
    out2 = frozen_params_block(NoFit, frames, flipped, probe.horizon, HORIZONS_NS[probe.horizon])
    # z = er / beta is invariant to the stored beta's sign: same IC
    assert out2["frozen_ic"] == pytest.approx(out["frozen_ic"])
    assert out2["params_sha256"] != out["params_sha256"]


# ------------------------------------------------------------ R-P1-2 (b)


def test_prereg_threshold_policy_ignores_the_ledger_count():
    g = effective_gates("prereg", 4.2)
    assert g["min_nw_tstat"] == 4.2
    assert effective_gates("prereg", 1.0)["min_nw_tstat"] == 3.0  # never looser than fixed
    with pytest.raises(ValueError, match="prereg"):
        effective_gates("prereg", None)
    frames = _multi_day_frames(per_day=300)
    rep = _validate(frames, prereg_t_threshold=6.5)  # ledger_t_threshold=3.5 is ignored
    assert rep["gates"]["min_nw_tstat"] == 6.5
    assert rep["tstat_threshold_policy"] == "prereg" and rep["prereg_t_threshold"] == 6.5
    assert "ledger_t_threshold" not in rep


def test_runner_prereg_threshold_and_frozen_params(tmp_path):
    frames = golden_frames(GOLDEN_DIR, CONFIGS_DIR)
    spec = golden_spec(GOLDEN_DIR, frames)
    from iap.alpha import build

    model = build(spec.alpha_id)
    model.horizon = spec.horizon
    model.fit({i: frames[i] for i in model.universe(sorted(frames))})
    runner = ExperimentRunner(
        None,
        tmp_path / "experiments.json",
        tmp_path / "experiments",
        CONFIGS_DIR,
        frames=frames,
        dry_run=True,
    )
    runner.run(spec, frozen_params=model.params(), prereg_t_threshold=5.5)
    rep = runner.last_report
    assert rep["gates"]["min_nw_tstat"] == 5.5
    assert rep["tstat_threshold_policy"] == "prereg"
    assert rep["frozen"]["refit"] is False and "frozen_ic" in rep


# ------------------------------------------------------------ R-P2-1


def test_beta_sign_consistency_is_additive():
    assert beta_sign_consistency([{"beta_fit": 1.0}, {"beta_fit": -2.0}]) == 0.5
    assert beta_sign_consistency([{"beta_fit": None}]) is None
    rep = _validate(_multi_day_frames(per_day=300))
    assert rep["beta_sign_consistency"] == 0.0  # backwards fixture, every fold
    assert "hypothesis_confirmed" in rep


# ------------------------------------------------------------ R-P2-7


def test_dsr_trials_can_be_the_registered_hypotheses():
    entries = [
        {"kind": "prereg", "body": {"prereg_id": "a", "hypothesis": "XNAS ITCH 2026 x"}},
        {"kind": "prereg", "body": {"prereg_id": "a", "hypothesis": "XNAS ITCH 2026 x"}},
        {"kind": "prereg", "body": {"prereg_id": "b", "data_family": "itch2026"}},
        {"kind": "prereg", "body": {"prereg_id": "c", "data_family": "other"}},
        {"kind": "result", "body": {"prereg_id": "d", "data_family": "itch2026"}},
    ]
    assert registered_hypotheses_trials(entries, "itch2026") == 1
    assert registered_hypotheses_trials(entries, "XNAS ITCH 2026") == 1
    assert registered_hypotheses_trials([], "x") == 1
    frames = _multi_day_frames(per_day=300)
    rep = _validate(
        frames,
        fold_diagnostics=True,
        deflated_sharpe=True,
        deflated_sharpe_trials=6,
        deflated_sharpe_trials_basis=TRIALS_BASIS_REGISTERED,
    )
    d = rep["deflated_sharpe"]
    assert d["n_trials"] == 6 and d["trials_basis"] == TRIALS_BASIS_REGISTERED
    assert "dsr_at_looks" in d


# ------------------------------------------------------------ R-P1-6


def test_post_block_embargo_covers_the_longest_feature_window():
    assert longest_feature_window_ns() == 300 * NS_S
    assert post_block_embargo_ns(60 * NS_S, None) == 60 * NS_S
    assert post_block_embargo_ns(60 * NS_S, "registry") == 300 * NS_S
    assert post_block_embargo_ns(600 * NS_S, 300 * NS_S) == 600 * NS_S
    with pytest.raises(ValueError):
        post_block_embargo_ns(0, "5m")
    fold = Fold(index=0, train_end=0, test_start=1000 * NS_S, test_end=2000 * NS_S, two_sided=True)
    ts = np.array([2000 + 100, 2000 + 299, 2000 + 300], dtype=np.int64) * NS_S
    assert fold.train_mask(ts, 0, 60 * NS_S).tolist() == [True, True, True]
    assert fold.train_mask(ts, 0, 60 * NS_S, 300 * NS_S).tolist() == [False, False, True]
    assert WalkForwardSplitter(embargo_ns=60 * NS_S).post_embargo_ns == 60 * NS_S
    assert (
        WalkForwardSplitter(embargo_ns=60 * NS_S, feature_window_ns="registry").post_embargo_ns
        == 300 * NS_S
    )
    c = CombinatorialPurgedSplitter(embargo_ns=60 * NS_S, feature_window_ns="registry")
    assert c.post_embargo_ns == 300 * NS_S


# ------------------------------------------------------------ R-P2-9


def test_event_calendar_file_has_sources_and_fails_closed(tmp_path):
    doc = sess.load_event_calendar()
    for year in ("2019", "2020", "2021", "2025", "2026"):
        e = doc["years"][year]
        assert e["source"] and e["as_of"] and e["fomc"]
    assert sess.day_tags("2019-01-30") == {"fomc"}
    assert sess.day_tags("2025-12-10") == {"fomc"}
    assert sess.day_tags("2026-12-24") == {"holiday_thin"}
    assert sess.day_tags("2019-05-30") == frozenset()
    with pytest.raises(sess.EventCalendarError, match="does not cover 2023"):
        sess.event_calendar_for_year(2023)
    assert sess.event_calendar_for_year(2026)["fomc"][0] == "2026-01-28"
    with pytest.raises(sess.EventCalendarError, match="not found"):
        sess.load_event_calendar(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"years": {"2019": {"fomc": [], "holiday_thin": []}}}))
    with pytest.raises(sess.EventCalendarError, match="source"):
        sess.load_event_calendar(bad)


# ------------------------------------------------------------ R-P1-4


def test_auction_orientation_is_the_registered_sign_by_default(tmp_path):
    from iap.auction import (
        AuctionStrategyConfig,
        AuctionStream,
        auction_features,
        auction_targets,
        extract_auction_stream,
        walk_forward,
    )
    from iap.auction.strategy import DEFAULT_THRESHOLDS
    from test_auction import SYMBOLS, _days, _session

    parts = [
        extract_auction_stream(
            _session(100 + i).write(tmp_path / f"{d}.itch"), symbols=SYMBOLS, date=d
        )
        for i, d in enumerate(_days(10))
    ]
    stream = AuctionStream.concat(parts)
    lab = auction_targets(auction_features(stream.noii, "C"), stream.crosses)
    n_thr = len(DEFAULT_THRESHOLDS)
    # orientation logic only: keep the pre-v1.13 cross exit at 300 s so the
    # X-P1-5 MOC-cutoff taker repricing does not change what the fit selects
    cfg = {"decision_s": 300, "orientation": -1, "enforce_entry_cutoff": False}
    fade = walk_forward(lab, AuctionStrategyConfig(**cfg), n_folds=4)
    assert all(f["orientation"] in (-1, None) for f in fade.folds)
    assert fade.looks_consumed == 4 * n_thr
    assert fade.to_dict()["looks_consumed"] == 4 * n_thr
    fitted = walk_forward(lab, AuctionStrategyConfig(**cfg), n_folds=4, fit_orientation=True)
    assert all(f["orientation"] == 1 for f in fitted.folds)
    assert fitted.looks_consumed == 4 * 2 * n_thr


# ------------------------------------------------------------ R-P1-5


def test_study_summary_counts_zero_trip_sessions_and_refuses_tiny_ci():
    trips = [
        {"session": "d1", "net_usd": 10.0, "net_bps": 5.0},
        {"session": "d1", "net_usd": 2.0, "net_bps": 1.0},
        {"session": "d3", "net_usd": -4.0, "net_bps": -2.0},
    ]
    s = summarise_study(trips, ["d1", "d2", "d3", "d4"], registered_t=4.8)
    assert s["per_session_usd"] == {"d1": 12.0, "d2": 0.0, "d3": -4.0, "d4": 0.0}
    assert s["n_sessions"] == 4 and s["n_sessions_without_trips"] == 2
    assert s["mean_usd"] == pytest.approx(2.0)
    assert s["ci_estimable"] and s["ci95_usd"][0] < 2.0 < s["ci95_usd"][1]
    assert s["per_trip"]["descriptive_only"] and s["per_trip"]["n_trips"] == 3
    assert s["power"]["mde_usd"] == pytest.approx(mde(s["sd_usd"], 4))
    assert "n=4" in s["power"]["statement"]
    assert s["power"]["registered_rule"]["reachable_at_observed_mean"] is False
    two = summarise_study(trips, ["d1", "d3"])
    assert two["n_sessions"] < MIN_SESSIONS_FOR_CI
    assert two["ci95_usd"] is None and two["t_stat"] is None and not two["ci_estimable"]
    with pytest.raises(ValueError, match="not in sessions"):
        summarise_study(trips, ["d1"])

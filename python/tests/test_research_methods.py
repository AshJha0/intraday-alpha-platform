"""The corrected research methods: square-root impact, L1 fill cap and
edge-based capacity; the two-sample HAC drift z and the CUSUM retirement
rule; the recompute leakage probe; per-fold diagnostics and the stationary
bootstrap; blackout-aware IC and blocked rows; scale-free ICs.

v1.3.0 / v1.4.0 added them as opt-ins; since v1.5.0 they ARE the defaults and
every rule they replaced stays selectable under an explicit legacy name
(``CostModel.with_linear_impact``, ``BacktestConfig.legacy``,
``compute_labels(blackout_reopen=False)``, ``ic_z_method="legacy"`` /
``rolling_ic_z``, ``LifecycleConfig.legacy``, ``methods("legacy_v1")``).
Each test group asserts the new default first, then that the legacy name
still gives the old numbers; a hand calculation made under an old rule
selects that rule by name."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
from conftest import CONFIGS_DIR, GOLDEN_DIR
from iap.adaptive.drift import (
    DEFAULT_IC_Z_METHOD,
    IC_Z_METHODS,
    LEGACY_IC_Z_METHOD,
    ICBaseline,
    rolling_ic_z,
    rolling_ic_z_hac,
    two_sample_hac_z,
)
from iap.adaptive.lifecycle import (
    ACTIVE,
    RETIRED,
    WATCH,
    LifecycleConfig,
    LifecycleTracker,
)
from iap.alpha.base import LinearAlpha
from iap.backtest import BacktestConfig, Backtester, CostModel
from iap.backtest.engine import cost_aware_targets
from iap.core.codec import read_jsonl
from iap.labels.labels import LabelReason, MidSeries, compute_labels
from iap.validation.diagnostics import fold_diagnostics, stationary_bootstrap_ci
from iap.validation.leakage import LeakageTester, RecomputeSource, engine_frame_builder
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import methods
from iap.validation.metrics import (
    bucket_ics_with_counts,
    capacity_breakeven,
    hac_mean_variance,
    ic,
    ic_with_blackout_reopen,
    instrument_ics,
    newey_west_tstat,
)
from iap.validation.validate import validate_alpha
from test_validation_framework import _backwards_frames, _BackwardsAlpha

NS_S = 1_000_000_000
META = {
    1: {
        "tick_size": 0.01,
        "lot_size": 100,
        "adv": 1_000_000.0,
        "asset_class": "EQUITY",
        "ref_price": 25.0,
    }
}


def _cost_model(**kw) -> CostModel:
    """The default cost model: square-root impact, 100 bps at one ADV."""
    base = dict(
        impact_coeff_bps_per_pct_adv=2.0,
        equity_taker_fee_per_share=0.003,
        fx_commission_per_million=2.5,
    )
    base.update(kw)
    return CostModel(**base)


def _linear_cost_model(**kw) -> CostModel:
    """The same coefficients under the LEGACY linear impact rule, named."""
    return _cost_model(**kw).with_linear_impact()


# ---------------------------------------------------------------------------
# 13. impact, fill cap, edge-based capacity
# ---------------------------------------------------------------------------


def test_impact_model_defaults_to_sqrt_and_linear_is_the_named_legacy(tmp_path):
    path = CONFIGS_DIR / "execution" / "execution.json"
    cm = CostModel.load(path)
    assert cm.impact_model == "sqrt" and cm.sqrt_impact_coeff_bps == 100.0
    assert cm.with_multiplier(2.0).impact_model == "sqrt"
    # a model built without naming a rule is the default one
    assert _cost_model().impact_model == "sqrt"
    assert _cost_model() == _cost_model().with_sqrt_impact(100.0)
    # the legacy rule, by name: nothing but the rule changes
    legacy = cm.with_linear_impact()
    assert legacy.impact_model == "linear" and legacy.with_multiplier(2.0).impact_model == "linear"
    assert legacy.impact_coeff_bps_per_pct_adv == cm.impact_coeff_bps_per_pct_adv == 2.0
    assert legacy == _linear_cost_model()
    assert _cost_model(impact_model="linear") == _linear_cost_model()
    with pytest.raises(ValueError, match="unknown impact_model"):
        _cost_model(impact_model="cubic")
    with pytest.raises(ValueError):
        _cost_model(sqrt_impact_coeff_bps=-1.0)

    # the pinned config must NAME its rule: a v1.4.0 block is rejected, not
    # silently priced under the new default ...
    blob = json.loads(path.read_text())
    assert blob["cost_model"]["impact_model"] == "sqrt"
    v140 = {
        k: v
        for k, v in blob["cost_model"].items()
        if k not in ("impact_model", "sqrt_impact_coeff_bps")
    }
    old = tmp_path / "execution_v140.json"
    old.write_text(json.dumps({"cost_model": v140}))
    with pytest.raises(ValueError, match="names no 'impact_model'"):
        CostModel.load(old)
    # ... and saying "linear" keeps the rule it was written for
    old.write_text(json.dumps({"cost_model": {**v140, "impact_model": "linear"}}))
    assert CostModel.load(old) == legacy
    old.write_text(json.dumps({"cost_model": {**v140, "impact_model": "sqrt"}}))
    with pytest.raises(ValueError, match="needs 'sqrt_impact_coeff_bps'"):
        CostModel.load(old)


def test_legacy_linear_costs_are_unchanged_by_the_new_fields():
    q, mid, hs = np.array([1000.0, -250.0]), np.array([25.0, 25.0]), np.array([0.01, 0.01])
    c = _linear_cost_model().cost_components(q, mid, hs, "EQUITY", adv=1_000_000.0, lot_size=100)
    assert c["impact"][0] == pytest.approx((2.0 * 0.1) * 1e-4 * 1000 * 25.0, abs=1e-12)
    assert c["impact"][1] == pytest.approx((2.0 * 0.025) * 1e-4 * 250 * 25.0, abs=1e-12)
    # the default prices the same two children under the square root:
    # 100 bps * sqrt(1000 / 1e6) = 3.1623 bps and 100 * sqrt(250 / 1e6) = 1.5811 bps
    d = _cost_model().cost_components(q, mid, hs, "EQUITY", adv=1_000_000.0, lot_size=100)
    assert d["impact"][0] == pytest.approx(100.0 * math.sqrt(1e-3) * 1e-4 * 1000 * 25.0)
    assert d["impact"][1] == pytest.approx(100.0 * math.sqrt(2.5e-4) * 1e-4 * 250 * 25.0)
    assert np.array_equal(d["spread"], c["spread"]) and np.array_equal(d["fee"], c["fee"])


def test_sqrt_impact_hand_calc_and_concavity():
    cm = _cost_model().with_sqrt_impact(100.0)  # 100 bps at one ADV
    assert cm.impact_model == "sqrt" and cm.with_multiplier(2.0).impact_model == "sqrt"
    assert cm == _cost_model()  # the default model IS this one
    q = np.array([10_000.0, 40_000.0])  # 1 % and 4 % of ADV
    c = cm.cost_components(
        q, np.array([25.0, 25.0]), np.array([0.01, 0.01]), "EQUITY", adv=1_000_000.0, lot_size=100
    )
    assert c["impact"][0] == pytest.approx(100.0 * 0.1 * 1e-4 * 10_000 * 25.0)  # 10 bps
    assert c["impact"][1] == pytest.approx(100.0 * 0.2 * 1e-4 * 40_000 * 25.0)  # 20 bps
    # 4x the size costs 2x the bps (sqrt), not 4x (linear)
    assert cm.impact_bps(np.array([0.04]))[0] == pytest.approx(
        2.0 * cm.impact_bps(np.array([0.01]))[0]
    )
    lin = _linear_cost_model()
    assert lin.impact_bps(np.array([0.04]))[0] == pytest.approx(
        4.0 * lin.impact_bps(np.array([0.01]))[0]
    )
    # spread and fee are the same under both models
    lc = lin.cost_components(
        q, np.array([25.0, 25.0]), np.array([0.01, 0.01]), "EQUITY", adv=1_000_000.0, lot_size=100
    )
    assert np.array_equal(lc["spread"], c["spread"]) and np.array_equal(lc["fee"], c["fee"])


def test_breakeven_size_is_where_edge_equals_cost():
    for cm in (
        _linear_cost_model(),  # the legacy rule
        _cost_model(),  # the default: square root, 100 bps at one ADV
        _cost_model().with_sqrt_impact(100.0),
        _cost_model(multiplier=2.0).with_sqrt_impact(60.0),
    ):
        edge = 30e-4  # 30 bps per round trip
        q = cm.breakeven_size(edge, 25.0, 0.01, "EQUITY", 1_000_000.0, 100)
        assert 0.0 < q < float("inf")
        comp = cm.cost_components(
            np.array([q]), np.array([25.0]), np.array([0.01]), "EQUITY", 1_000_000.0, 100
        )
        round_trip = 2.0 * float(comp["spread"][0] + comp["fee"][0] + comp["impact"][0])
        assert round_trip == pytest.approx(edge * q * 25.0, rel=1e-9)
        # one share more loses money, one less makes it
        assert cm.breakeven_size(edge * 1.1, 25.0, 0.01, "EQUITY", 1e6, 100) > q


@pytest.mark.parametrize("impact_model", ["sqrt", "linear"])
def test_breakeven_size_edge_cases(impact_model):
    """Under the default (sqrt) and under the named legacy (linear) rule."""
    if impact_model == "sqrt":
        cm = _cost_model()
        free = _cost_model(sqrt_impact_coeff_bps=0.0)
    else:
        cm = _linear_cost_model()
        free = _linear_cost_model(impact_coeff_bps_per_pct_adv=0.0)
    assert cm.impact_model == free.impact_model == impact_model
    fixed = (2 * 0.01 + 2 * 0.003) / 25.0
    assert cm.breakeven_size(fixed, 25.0, 0.01, "EQUITY", 1e6, 100) == 0.0
    assert cm.breakeven_size(-1e-4, 25.0, 0.01, "EQUITY", 1e6, 100) == 0.0
    assert free.breakeven_size(1e-2, 25.0, 0.01, "EQUITY", 1e6, 100) == float("inf")
    with pytest.raises(ValueError):
        cm.breakeven_size(float("nan"), 25.0, 0.01, "EQUITY", 1e6, 100)
    with pytest.raises(ValueError):
        cm.breakeven_size(1e-3, float("nan"), 0.01, "EQUITY", 1e6, 100)
    # FX sizes are in lots
    fx = cm.breakeven_size(5e-4, 1.1, 2e-5, "FX", 4e9, 1000)
    out = capacity_breakeven(cm, 5e-4, 1.1, 2e-5, "FX", 4e9, 1000)
    assert out["units"] == fx and out["notional"] == pytest.approx(fx * 1000 * 1.1)
    assert out["participation"] == pytest.approx(fx * 1000 / 4e9)


def test_breakeven_size_hand_calc_under_both_impact_rules():
    """A 100 bp edge at mid 25, half-spread 0.01, fee 0.003, ADV 1e6:
    room = 0.01 - (2 * 0.01 + 2 * 0.003) / 25 = 0.00896, i.e. 44.8 bps of
    impact per leg.  Default sqrt, 100 bps at one ADV: participation
    (44.8 / 100)^2 = 0.200704 -> 200 704 shares.  Legacy linear, 2 bps per
    1 % of ADV: 44.8 / 200 = 0.224 -> 224 000 shares."""
    assert _cost_model().breakeven_size(1e-2, 25.0, 0.01, "EQUITY", 1e6, 100) == pytest.approx(
        200_704.0, rel=1e-12
    )
    assert _linear_cost_model().breakeven_size(
        1e-2, 25.0, 0.01, "EQUITY", 1e6, 100
    ) == pytest.approx(224_000.0, rel=1e-12)
    # zeroing the LINEAR coefficient does not free the default model: it
    # reads the square-root coefficient
    assert _cost_model(impact_coeff_bps_per_pct_adv=0.0).breakeven_size(
        1e-2, 25.0, 0.01, "EQUITY", 1e6, 100
    ) == pytest.approx(200_704.0, rel=1e-12)


def test_capacity_breakeven_grows_with_the_edge_unlike_the_participation_proxy():
    cm = _cost_model().with_sqrt_impact(100.0)
    small = capacity_breakeven(cm, 15e-4, 25.0, 0.01, "EQUITY", 1e6)
    large = capacity_breakeven(cm, 30e-4, 25.0, 0.01, "EQUITY", 1e6)
    none = capacity_breakeven(cm, 1e-4, 25.0, 0.01, "EQUITY", 1e6)
    assert none == {"units": 0.0, "notional": 0.0, "participation": 0.0}
    assert 0.0 < small["notional"] < large["notional"]


def _l1_frame(n, bid_size, ask_size, mids=None, valid=None):
    ts = NS_S + np.arange(n, dtype=np.int64) * NS_S
    frame = pd.DataFrame(
        {
            "exchange_ts": ts,
            "mid_price_v1": np.full(n, 25.0) if mids is None else np.asarray(mids, float),
            "spread_ticks_v1": np.full(n, 2.0),
            "depth_bid_l1_v1": np.asarray(bid_size, dtype=float),
            "depth_ask_l1_v1": np.asarray(ask_size, dtype=float),
        }
    )
    if valid is not None:
        frame["label_valid_1s"] = np.asarray(valid, dtype=bool)
    return frame


def _scores(er):
    n = len(er)
    return pd.DataFrame(
        {
            "exchange_ts": NS_S + np.arange(n, dtype=np.int64) * NS_S,
            "expected_return": np.asarray(er, dtype=float),
            "confidence": np.ones(n),
        }
    )


def test_fill_cap_is_the_default_and_limits_each_fill_to_the_displayed_size():
    """The cap is ON by default (v1.5.0) and OFF under ``BacktestConfig.legacy``.
    The positions below are hand-computed under the legacy SIGN policy (named,
    so the target is 1000 on every row) with the cap switched on and off."""
    assert BacktestConfig().cap_fills_at_l1 is True
    assert BacktestConfig.legacy().cap_fills_at_l1 is False
    er = [1e-3] * 6
    frame = _l1_frame(6, bid_size=[500] * 6, ask_size=[300, 300, 300, np.nan, 300, 300])
    cfg = dict(max_pos_qty=1000, latency_rows=0)
    full = Backtester(_cost_model(), META, BacktestConfig.legacy(**cfg)).run_instrument(
        1, frame, _scores(er)
    )
    capped = Backtester(
        _cost_model(), META, BacktestConfig.legacy(cap_fills_at_l1=True, **cfg)
    ).run_instrument(1, frame, _scores(er))
    assert full.positions.tolist() == [1000] * 6  # fills 1000 at a 300 touch
    assert capped.positions.tolist() == [300, 600, 900, 900, 1000, 1000]
    assert capped.trade_count == 4 and capped.traded_qty == 1000
    # selling takes the BID size
    down = Backtester(
        _cost_model(), META, BacktestConfig.legacy(cap_fills_at_l1=True, **cfg)
    ).run_instrument(1, frame, _scores([-1e-3] * 6))
    assert down.positions.tolist() == [-500, -1000, -1000, -1000, -1000, -1000]


def test_default_backtest_config_hand_calc_cost_aware_capped_and_row_blocked():
    """The v1.5.0 default configuration, nothing named, on six 1 s rows.

    mid 25, half-spread 0.01 (2 ticks of 0.01), fee 0.003/share: the entry
    threshold is the round-trip cost (2 * 0.01 + 2 * 0.003) / 25 = 1.04e-3
    and the renewal threshold half of it, 5.2e-4.  Horizon 1 s, max 1000,
    ask shows 300, bid shows 500, latency 0, last row has no valid label.

    row  er      cost-aware target                         fill (capped)   pos
    0    2e-3    flat, clears entry          -> +1000      buy 300          300
    1    6e-4    1 s elapsed, clears renewal -> +1000      buy 300          600
    2    1e-4    1 s elapsed, no renewal     ->     0      sell 500         100
    3   -2e-3    flat, clears entry          -> -1000      sell 500        -400
    4    0       1 s elapsed, no signal      ->     0      buy 300         -100
    5    0       blocked (invalid label): no decision                      -100
    """
    assert BacktestConfig().position_policy == "cost_aware"
    assert BacktestConfig().block_rows_column == "auto"
    er = [2e-3, 6e-4, 1e-4, -2e-3, 0.0, 0.0]
    valid = [True, True, True, True, True, False]
    frame = _l1_frame(6, bid_size=[500] * 6, ask_size=[300] * 6, valid=valid)
    frame["label_mid_1s"] = 0.0
    cfg = BacktestConfig(max_pos_qty=1000, latency_rows=0)
    bt = Backtester(_cost_model(), META, cfg)
    with pytest.raises(ValueError, match="needs horizon_ns"):
        bt.run_instrument(1, frame, _scores(er))
    threshold = _cost_model().round_trip_cost_return(
        frame["mid_price_v1"].to_numpy(), np.full(6, 0.01), "EQUITY"
    )
    assert threshold == pytest.approx(np.full(6, 1.04e-3), rel=1e-12)
    ts = frame["exchange_ts"].to_numpy()
    targets = cost_aware_targets(ts, np.asarray(er), np.ones(6), threshold, 0.5, NS_S, 0.5)
    assert targets.tolist() == [1.0, 1.0, 0.0, -1.0, 0.0, 0.0]
    res = bt.for_horizon("1s").run_instrument(1, frame, _scores(er))
    assert res.positions.tolist() == [300, 600, 100, -400, -100, -100]
    assert res.trade_count == 5 and res.traded_qty == 300 + 300 + 500 + 500 + 300
    # square-root impact on each child: 100 bps * sqrt(q / 1e6) of q * 25
    want_impact = sum(
        100.0 * math.sqrt(q / 1e6) * 1e-4 * q * 25.0 for q in (300, 300, 500, 500, 300)
    )
    assert res.impact_cost == pytest.approx(want_impact, rel=1e-12)
    assert res.spread_cost == pytest.approx(1900 * 0.01) and res.fee_cost == pytest.approx(5.7)
    # the legacy rules on the same inputs: sign of every row, any size at the
    # touch, every row traded (er = 0 is flat), linear impact
    old = Backtester(
        _linear_cost_model(), META, BacktestConfig.legacy(max_pos_qty=1000, latency_rows=0)
    ).run_instrument(1, frame, _scores(er))
    assert old.positions.tolist() == [1000, 1000, 1000, -1000, 0, 0]
    assert old.impact_cost == pytest.approx(
        sum(2.0 * (q / 1e6 * 100.0) * 1e-4 * q * 25.0 for q in (1000, 2000, 1000)), rel=1e-12
    )


def test_fill_cap_keeps_the_accounting_identity_and_exempts_the_session_flatten():
    day = 86_400 * NS_S
    n = 8
    frame = _l1_frame(n, bid_size=[100] * n, ask_size=[400] * n, mids=25.0 + 0.01 * np.arange(n))
    ts = frame["exchange_ts"].to_numpy().copy()
    ts[4:] += day  # two sessions of four rows
    frame["exchange_ts"] = ts
    scores = _scores([1e-3] * n)
    scores["exchange_ts"] = ts
    # the legacy SIGN policy, named (target 1000 on every row), with the cap on
    res = Backtester(
        _cost_model(),
        META,
        BacktestConfig.legacy(
            max_pos_qty=1000, latency_rows=0, cap_fills_at_l1=True, flatten_at_session_end=True
        ),
    ).run_instrument(1, frame, scores)
    # builds 400 a row, then is flat at each close although the bid shows 100
    assert res.positions.tolist() == [400, 800, 1000, 0, 400, 800, 1000, 0]
    mark = frame["mid_price_v1"].to_numpy()
    gross = float(np.sum(res.positions[:-1] * np.diff(mark)))
    assert res.total_pnl == pytest.approx(gross - res.total_costs, abs=1e-9)
    with pytest.raises(ValueError, match="cap_fills_at_l1 needs"):
        Backtester(_cost_model(), META, BacktestConfig.legacy(cap_fills_at_l1=True)).run_instrument(
            1, frame.drop(columns=["depth_ask_l1_v1"]), scores
        )
    # the DEFAULT config caps too, so it needs the displayed sizes as well
    labelled = frame.assign(label_mid_1s=0.0, label_valid_1s=True)
    with pytest.raises(ValueError, match="cap_fills_at_l1 needs"):
        Backtester(_cost_model(), META, BacktestConfig()).for_horizon("1s").run_instrument(
            1, labelled.drop(columns=["depth_ask_l1_v1"]), scores
        )


# ---------------------------------------------------------------------------
# 17. blocked rows and the blackout-aware IC
# ---------------------------------------------------------------------------


def test_blocked_rows_make_no_decision_and_legacy_trades_every_row():
    """The default blocks the rows the IC does not score (``"auto"``); the
    legacy config blocks nothing.  Positions are hand-computed under the
    legacy SIGN policy, named, with the block off, on a named column and on
    the default ``"auto"`` mask."""
    assert BacktestConfig().block_rows_column == "auto"
    assert BacktestConfig.legacy().block_rows_column is None
    er = [1e-3, 1e-3, -1e-3, -1e-3, 1e-3, 1e-3]
    valid = [True, True, False, False, True, True]
    frame = _l1_frame(6, [1e9] * 6, [1e9] * 6, valid=valid)
    base = Backtester(
        _cost_model(), META, BacktestConfig.legacy(max_pos_qty=100, latency_rows=0)
    ).run_instrument(1, frame, _scores(er))
    assert base.positions.tolist() == [100, 100, -100, -100, 100, 100]
    blocked = Backtester(
        _cost_model(),
        META,
        BacktestConfig.legacy(max_pos_qty=100, latency_rows=0, block_rows_column="label_valid_1s"),
    ).run_instrument(1, frame, _scores(er))
    assert blocked.positions.tolist() == [100] * 6  # the two rows are skipped
    assert blocked.trade_count == 1 and base.trade_count == 3
    with pytest.raises(ValueError, match="block_rows_column"):
        Backtester(
            _cost_model(), META, BacktestConfig.legacy(block_rows_column="label_valid_5s")
        ).run_instrument(1, frame, _scores(er))
    # the default mask, "auto" = the rows the IC scores at the horizon: here
    # exactly the valid-label rows, so it blocks the same two decisions
    auto_cfg = BacktestConfig.legacy(max_pos_qty=100, latency_rows=0, block_rows_column="auto")
    assert auto_cfg.block_rows_column == BacktestConfig().block_rows_column
    labelled = frame.assign(label_mid_1s=0.0)
    auto = Backtester(_cost_model(), META, auto_cfg).for_horizon("1s")
    assert auto.config.block_rows_label() == "scored_rows:1s"
    assert auto.run_instrument(1, labelled, _scores(er)).positions.tolist() == [100] * 6
    # it needs the horizon and that horizon's label columns — never a guess
    with pytest.raises(ValueError, match="block_rows_column='auto' needs horizon_ns"):
        Backtester(_cost_model(), META, auto_cfg).run_instrument(1, labelled, _scores(er))
    with pytest.raises(ValueError, match="needs frame column 'label_mid_1s'"):
        auto.run_instrument(1, frame, _scores(er))


@pytest.mark.parametrize(
    "latency",
    [
        dict(latency_rows=1),
        dict(latency_rows=2, max_decision_age_ns=10 * NS_S),
        dict(latency_ns=NS_S),
        dict(latency_ns=NS_S, max_decision_age_ns=10 * NS_S),
    ],
)
def test_blocking_applies_at_the_decision_row_under_every_latency_mode(latency):
    rng = np.random.default_rng(3)  # test-only fixture data
    n = 60
    er = rng.standard_normal(n) * 1e-3
    valid = rng.uniform(size=n) > 0.4
    frame = _l1_frame(n, [1e9] * n, [1e9] * n, valid=valid)
    # the legacy SIGN policy, named: a target on every row, so the block is
    # the only thing that differs between the runs
    blocked = Backtester(
        _cost_model(),
        META,
        BacktestConfig.legacy(max_pos_qty=100, block_rows_column="label_valid_1s", **latency),
    ).run_instrument(1, frame, _scores(er))
    # reference: a backtest of scores whose blocked rows carry "no decision"
    # is exactly a backtest in which those rows repeat the previous target
    all_true = frame.assign(label_valid_1s=True)
    same = Backtester(
        _cost_model(),
        META,
        BacktestConfig.legacy(max_pos_qty=100, block_rows_column="label_valid_1s", **latency),
    ).run_instrument(1, all_true, _scores(er))
    plain = Backtester(
        _cost_model(), META, BacktestConfig.legacy(max_pos_qty=100, **latency)
    ).run_instrument(1, frame, _scores(er))
    # nothing blocked = the legacy every-row run
    assert np.array_equal(same.positions, plain.positions)
    assert not np.array_equal(blocked.positions, plain.positions)
    assert blocked.trade_count <= plain.trade_count


def _halt_series():
    """1 s samples; a halt (non-tradable) at t = 10..14, reopening 2 % lower."""
    s = MidSeries()
    for t in range(30):
        if 10 <= t < 15:
            s.append(t * NS_S, float("nan"), float("nan"), False)
        else:
            s.append(t * NS_S, 100.0 if t < 10 else 98.0, 0.01, True)
    return s


def test_blackout_reopen_return_is_the_default_and_scores_the_reopen_price():
    series = _halt_series()
    anchors = [t * NS_S for t in range(25)]
    # legacy (up to v1.4.0), by name: the reopen return is not computed
    plain = compute_labels(anchors, series, 29 * NS_S, horizons=("5s",), blackout_reopen=False)[
        "5s"
    ]
    assert plain.reopen_mid == []  # legacy: untouched
    # default (v1.5.0): computed without being asked for
    lab = compute_labels(anchors, series, 29 * NS_S, horizons=("5s",))["5s"]
    assert len(lab.reopen_mid) == len(anchors)
    named = compute_labels(anchors, series, 29 * NS_S, horizons=("5s",), blackout_reopen=True)["5s"]
    assert np.array_equal(named.reopen_mid, lab.reopen_mid, equal_nan=True)
    assert lab.mid == plain.mid or all(
        (a == b) or (a != a and b != b) for a, b in zip(lab.mid, plain.mid, strict=False)
    )
    assert lab.valid == plain.valid and lab.reason == plain.reason
    # anchors 5..9: the 5 s horizon ends inside or just after the halt
    for t in range(5, 10):
        assert not lab.valid[t] and lab.reason[t] == LabelReason.BLACKOUT
        assert lab.reopen_mid[t] == pytest.approx(98.0 / 100.0 - 1.0)
    # anchors before the halt's reach are valid and carry no reopen value
    assert lab.valid[2] and math.isnan(lab.reopen_mid[2])
    # anchors INSIDE the halt are not tradable at the anchor: not rescued
    assert lab.reason[12] & LabelReason.ANCHOR_NOT_TRADABLE
    assert math.isnan(lab.reopen_mid[12])


def test_blackout_reopen_needs_a_reopen():
    s = MidSeries()
    for t in range(12):
        s.append(t * NS_S, 100.0 if t < 8 else float("nan"), 0.01, t < 8)
    lab = compute_labels([5 * NS_S], s, 11 * NS_S, horizons=("5s",))["5s"]
    assert lab.reason[0] == LabelReason.BLACKOUT and math.isnan(lab.reopen_mid[0])


def test_ic_with_blackout_reopen_removes_the_survivor_selection():
    """A signal that is right on ordinary rows and wrong into every halt:
    the valid-only IC never sees the halts; the blackout-aware IC does."""
    rng = np.random.default_rng(21)
    n = 2000
    x = rng.standard_normal(n)
    label = 0.3 * x + rng.standard_normal(n)
    valid = np.ones(n, dtype=bool)
    reason = np.zeros(n, dtype=np.int64)
    reopen = np.full(n, np.nan)
    halted = np.arange(n) % 5 == 0
    valid[halted] = False
    reason[halted] = LabelReason.BLACKOUT
    reopen[halted] = -3.0 * x[halted]  # wrong-footed by the reopen
    other = np.arange(n) % 97 == 1  # invalid for another reason
    valid[other] = False
    reason[other] = LabelReason.BLACKOUT | LabelReason.FORWARD_STALE
    reopen[other] = 50.0
    out = ic_with_blackout_reopen(x, label, valid, reason, reopen)
    assert out["ic_valid_only"] == pytest.approx(ic(x, np.where(valid, label, np.nan)))
    assert out["ic_valid_only"] > 0.2
    assert out["ic"] < 0.0  # the sign flips
    assert out["n_blackout_scored"] == int((halted & ~other).sum())  # BLACKOUT alone
    assert out["n_valid"] == int(valid.sum())
    # with nothing to rescue the two agree exactly
    none = ic_with_blackout_reopen(x, label, valid, reason, np.full(n, np.nan))
    assert none["ic"] == none["ic_valid_only"] and none["n_blackout_scored"] == 0
    with pytest.raises(ValueError):
        ic_with_blackout_reopen(x, label[:-1], valid, reason, reopen)


# ---------------------------------------------------------------------------
# 18. scale-free ICs
# ---------------------------------------------------------------------------


def test_instrument_ics_hand_calc():
    rng = np.random.default_rng(31)
    ids = np.repeat([1, 2, 3], 400)
    x = rng.standard_normal(1200)
    y = 0.2 * x + rng.standard_normal(1200)
    out = instrument_ics(ids, x, y)
    want = {str(i): float(np.corrcoef(x[ids == i], y[ids == i])[0, 1]) for i in (1, 2, 3)}
    assert out["by_instrument"] == pytest.approx(want)
    assert out["instrument_mean"] == pytest.approx(np.mean(list(want.values())))
    assert out["n_instruments"] == 3 and out["n_skipped"] == 0
    zx = np.concatenate([(x[ids == i] - x[ids == i].mean()) / x[ids == i].std() for i in (1, 2, 3)])
    zy = np.concatenate([(y[ids == i] - y[ids == i].mean()) / y[ids == i].std() for i in (1, 2, 3)])
    assert out["vol_scaled"] == pytest.approx(float(np.corrcoef(zx, zy)[0, 1]))


def test_pooled_ic_is_dominated_by_the_volatile_instrument_and_the_scaled_ones_are_not():
    """Nine quiet instruments with a real signal and one 50x more volatile
    instrument with none: the pooled IC is ~0, the scale-free ICs are not."""
    rng = np.random.default_rng(32)
    ids, xs, ys = [], [], []
    for i in range(1, 10):
        x = rng.standard_normal(500)
        ids.append(np.full(500, i))
        xs.append(x)
        ys.append((0.3 * x + rng.standard_normal(500)) * 1e-4)
    ids.append(np.full(500, 10))
    xs.append(rng.standard_normal(500))
    ys.append(rng.standard_normal(500) * 50e-4)
    ids, x, y = np.concatenate(ids), np.concatenate(xs), np.concatenate(ys)
    pooled = ic(x, y)
    out = instrument_ics(ids, x, y)
    assert abs(pooled) < 0.05
    assert out["instrument_mean"] > 0.2 and out["vol_scaled"] > 0.2


def test_instrument_ics_removes_between_instrument_level_differences():
    """Different mean score AND mean label per instrument look like signal
    in the pool; no row of any instrument carries it."""
    rng = np.random.default_rng(33)
    ids = np.repeat([1, 2], 1000)
    x = rng.standard_normal(2000) + np.where(ids == 1, 3.0, -3.0)
    y = rng.standard_normal(2000) + np.where(ids == 1, 3.0, -3.0)
    assert ic(x, y) > 0.8
    out = instrument_ics(ids, x, y)
    assert abs(out["instrument_mean"]) < 0.08 and abs(out["vol_scaled"]) < 0.08


def test_instrument_ics_degenerate_inputs():
    ids = np.repeat([1, 2], 40)
    x = np.concatenate([np.arange(40.0), np.ones(40)])  # instrument 2 is flat
    y = np.concatenate([np.arange(40.0) ** 2, np.arange(40.0)])
    out = instrument_ics(ids, x, y)
    assert out["n_instruments"] == 1 and out["n_skipped"] == 1
    empty = instrument_ics(np.array([1, 1]), np.array([1.0, 2.0]), np.array([1.0, 2.0]))
    assert math.isnan(empty["instrument_mean"]) and empty["by_instrument"] == {}
    with pytest.raises(ValueError):
        instrument_ics(ids[:-1], x, y)


def _validate_backwards(bundle_name="v2", **kw):
    """``(frames, backtester, report)`` of the backwards fixture under a
    method bundle: ``"v2"`` is the default chain, ``"legacy_v1"`` names every
    rule up to v1.4.0 (sign positions, uncapped fills, linear impact, the
    fixed 3.0 gate, last-fold-only diagnostics)."""
    bundle = methods(bundle_name)
    frames = _backwards_frames()
    meta = {
        1: {
            "tick_size": 0.01,
            "lot_size": 1,
            "adv": 1_000_000.0,
            "asset_class": "EQUITY",
            "ref_price": 25.0,
        }
    }
    bt = Backtester(
        bundle.cost_model(_cost_model()),
        meta,
        bundle.backtest_config(max_pos_qty=100, latency_rows=1),
    )
    kwargs = bundle.validate_kwargs()
    if bundle.tstat_threshold == "ledger":
        # the caller's part of the default policy: the Bonferroni |t| at the
        # look count of a ledger holding this one run
        looks = bundle.looks(4)
        kwargs["ledger_t_threshold"] = ExperimentLedger.bonferroni_t_threshold_at(looks)
        kwargs["ledger_looks"] = looks
    kwargs.update(kw)
    return (
        frames,
        bt,
        validate_alpha(
            _BackwardsAlpha,
            frames,
            bt,
            {1: {"adv": 1_000_000.0, "ref_price": 25.0}},
            0.1,
            n_folds=4,
            embargo_ns=NS_S,
            **kwargs,
        ),
    )


@pytest.mark.parametrize("bundle_name", ["v2", "legacy_v1"])
def test_validation_report_carries_the_scale_free_ics(bundle_name):
    """Headline report fields under the default chain and the legacy one."""
    _, _, report = _validate_backwards(bundle_name)
    assert report["methods"]["significance"] == (
        "pooled_slope" if bundle_name == "v2" else "within_bucket"
    )
    # one instrument: both scale-free ICs are that instrument's IC = the pooled IC
    assert report["oos_ic_instrument_mean"] == pytest.approx(report["oos_ic"], rel=1e-9)
    assert report["oos_ic_vol_scaled"] == pytest.approx(report["oos_ic"], rel=1e-9)
    assert list(report["oos_ic_by_instrument"]) == ["1"]
    # ... and on the uncrossed rows the gate reads (no row is crossed here)
    assert report["oos_ic_vol_scaled_uncrossed"] == pytest.approx(report["gate_ic"], rel=1e-9)
    assert report["ic_scale_consistent"] is True
    json.dumps(report, allow_nan=False)


# ---------------------------------------------------------------------------
# 14. drift: two-sample HAC z and the CUSUM rule
# ---------------------------------------------------------------------------


def test_hac_mean_variance_is_the_newey_west_tstat_decomposed():
    rng = np.random.default_rng(41)
    s = rng.standard_normal(60) * 0.05 + 0.02
    w = rng.integers(50, 3000, size=60).astype(float)
    for lags in (0, 2, 4):
        m, var, n = hac_mean_variance(s, lags=lags, weights=w)
        assert n == 60
        assert m / math.sqrt(var) == pytest.approx(
            newey_west_tstat(s, lags=lags, weights=w), rel=1e-12
        )
    m, var, n = hac_mean_variance(np.array([0.1]), lags=2)
    assert (m, n) == (0.1, 1) and math.isnan(var)
    assert hac_mean_variance(np.array([]))[2] == 0


def _ic_window(seed, n_buckets, slope, per=200, bucket_ns=300 * NS_S):
    rng = np.random.default_rng(seed)
    ts = np.repeat(np.arange(n_buckets, dtype=np.int64) * bucket_ns, per) + np.tile(
        np.arange(per, dtype=np.int64) * NS_S, n_buckets
    )
    x = rng.standard_normal(n_buckets * per)
    y = slope * x + rng.standard_normal(n_buckets * per)
    return ts, x, y


def _baseline(ic_mean, ic_std, n):
    return ICBaseline(
        name="b",
        alpha_id="A",
        source="t",
        ic_mean=ic_mean,
        ic_std=ic_std,
        n_buckets_baseline=n,
        bucket_ns=300 * NS_S,
        horizon="1s",
    )


def test_two_sample_hac_z_hand_calc_and_guards():
    assert two_sample_hac_z(0.01, 4e-6, 0.03, 12e-6) == pytest.approx(-0.02 / 0.004)
    assert two_sample_hac_z(0.01, float("nan"), 0.03, 1e-6) is None
    assert two_sample_hac_z(0.01, 0.0, 0.03, 0.0) is None
    assert two_sample_hac_z(0.01, -1e-6, 0.03, 1e-6) is None
    # the two-sample HAC z is the default; the z up to v1.4.0 is "legacy"
    assert IC_Z_METHODS == ("hac", "legacy")
    assert DEFAULT_IC_Z_METHOD == "hac" and LEGACY_IC_Z_METHOD == "legacy"
    pinned = json.loads((CONFIGS_DIR / "strategies" / "strategies.json").read_text())
    assert pinned["adaptive"]["ic_z_method"] == "hac"


def test_hac_z_accounts_for_the_baseline_mean_error():
    """Live and baseline drawn from the SAME process: the legacy z
    (``rolling_ic_z``) treats the baseline mean as exact and is inflated by
    its sampling error; the default two-sample z is not.  Averaged over many
    draws the legacy z has variance ~ 1 + n_live / n_base, the HAC z ~ 1."""
    pinned, hac = [], []  # ``pinned`` = the legacy z (its name up to v1.4.0)
    for seed in range(60):
        bts, bx, by = _ic_window(1000 + seed, 8, 0.05)  # a SHORT baseline
        bics, bcounts = bucket_ics_with_counts(bts, bx, by)
        baseline = _baseline(float(bics.mean()), float(bics.std()), int(bics.size))
        ts, x, y = _ic_window(5000 + seed, 24, 0.05)  # a 3x longer live window
        pinned.append(rolling_ic_z(baseline, ts, x, y).z)
        got = rolling_ic_z_hac(baseline, ts, x, y, lags=1)
        hac.append(got.z)
        assert got.n_buckets == 24 and len(got.bucket_ics) == 24
    assert np.var(pinned) > 2.5  # ~ 1 + 24/8, and more
    assert np.var(hac) < 1.6
    assert np.mean(np.abs(np.asarray(pinned)) > 2.0) > 3 * np.mean(np.abs(np.asarray(hac)) > 2.0)


def test_hac_z_still_detects_a_real_shift_and_uses_the_baseline_series():
    bts, bx, by = _ic_window(7, 60, 0.20)
    bics, bcounts = bucket_ics_with_counts(bts, bx, by)
    baseline = _baseline(float(bics.mean()), float(bics.std()), int(bics.size))
    ts, x, y = _ic_window(8, 30, 0.0)  # the signal is gone
    summary = rolling_ic_z_hac(baseline, ts, x, y)
    series = rolling_ic_z_hac(
        baseline, ts, x, y, baseline_bucket_ics=bics, baseline_bucket_counts=bcounts
    )
    assert summary.z < -5.0 and series.z < -5.0
    assert summary.rolling_ic == pytest.approx(series.rolling_ic)
    # too few live buckets: silence, as in the legacy monitor
    few = rolling_ic_z_hac(baseline, ts[:400], x[:400], y[:400], min_buckets=4)
    assert few.z is None and few.rolling_ic is None and few.n_buckets == 2


def test_hac_z_widens_under_autocorrelated_bucket_ics():
    """Bucket ICs that move in long runs carry fewer independent readings
    than their count: the HAC variance must exceed the iid one."""
    rng = np.random.default_rng(43)
    n = 200
    e = rng.standard_normal(n)
    ar = np.empty(n)
    ar[0] = e[0]
    for i in range(1, n):
        ar[i] = 0.8 * ar[i - 1] + e[i]
    _, var_iid, _ = hac_mean_variance(ar, lags=0)
    _, var_hac, _ = hac_mean_variance(ar, lags=8)
    assert var_hac > 3.0 * var_iid


def _tracker(**kw):
    """A tracker over ``LifecycleConfig(**gates, **kw)``: with nothing named
    that is the default CUSUM rule (k 0.0025, h 0.01)."""
    cfg = dict(
        watch_ic_gate=0.0, reactivate_ic_gate=0.01, retire_breach_evals=3, reactivate_evals=2
    )
    cfg.update(kw)
    return LifecycleTracker(alpha_id="A", config=LifecycleConfig(**cfg))


def test_lifecycle_config_defaults_to_the_cusum_rule_and_legacy_names_the_consecutive_one():
    cfg = LifecycleConfig(0.0, 0.01, 3, 2)
    assert (cfg.breach_rule, cfg.cusum_k, cfg.cusum_h) == ("cusum", 0.0025, 0.01)
    assert _tracker().config == cfg
    legacy = LifecycleConfig.legacy(0.0, 0.01, 3, 2)
    assert legacy.breach_rule == "consecutive"
    assert legacy == LifecycleConfig(0.0, 0.01, 3, 2, breach_rule="consecutive")
    pinned = json.loads((CONFIGS_DIR / "strategies" / "strategies.json").read_text())
    block = pinned["adaptive"]["lifecycle"]
    # the pinned block NAMES the default rule and its two parameters
    assert block["breach_rule"] == "cusum"
    assert (block["cusum_k"], block["cusum_h"]) == (0.0025, 0.01)
    assert LifecycleConfig.from_config(block).breach_rule == "cusum"
    assert LifecycleConfig.from_config(block).to_dict() == block
    assert (
        LifecycleConfig.from_config({**block, "breach_rule": "cusum", "cusum_h": 0.05}).cusum_h
        == 0.05
    )
    # a block written for v1.4.0 names no rule: rejected, not read under the
    # other one; saying "consecutive" keeps the rule it was written for
    v140 = {k: v for k, v in block.items() if k not in ("breach_rule", "cusum_k", "cusum_h")}
    with pytest.raises(ValueError, match="names no 'breach_rule'"):
        LifecycleConfig.from_config(v140)
    old = LifecycleConfig.from_config({**v140, "breach_rule": "consecutive"})
    assert old.breach_rule == "consecutive"
    assert old == LifecycleConfig.legacy(
        block["watch_ic_gate"],
        block["reactivate_ic_gate"],
        block["retire_breach_evals"],
        block["reactivate_evals"],
    )
    for missing in ("cusum_k", "cusum_h"):
        partial = {k: v for k, v in block.items() if k != missing}
        with pytest.raises(ValueError, match=f"needs '{missing}'"):
            LifecycleConfig.from_config(partial)
    with pytest.raises(ValueError, match="unknown breach_rule"):
        LifecycleConfig(0.0, 0.01, 3, 2, breach_rule="ewma")
    with pytest.raises(ValueError, match="cusum_h"):
        LifecycleConfig(0.0, 0.01, 3, 2, breach_rule="cusum", cusum_h=0.0)
    with pytest.raises(ValueError, match="cusum_k"):
        LifecycleConfig(0.0, 0.01, 3, 2, cusum_k=-1.0)


def test_legacy_consecutive_rule_is_unchanged_and_ignores_new_fraction():
    a, b = _tracker(breach_rule="consecutive"), _tracker(breach_rule="consecutive")
    readings = [0.02, -0.01, -0.01, 0.005, -0.01, -0.01, -0.01, 0.02, 0.02, 0.02]
    for i, r in enumerate(readings):
        a.update(i, r)
        b.update(i, r, new_fraction=0.1)
    assert [t.to_dict() for t in a.transitions] == [t.to_dict() for t in b.transitions]
    assert [t.to_state for t in a.transitions] == [WATCH, RETIRED, WATCH]
    assert a.cusum == 0.0 and b.cusum == 0.0
    # the DEFAULT rule on the same readings is another machine.  Gate 0,
    # k 0.0025, h 0.01, disjoint readings: 0.02 keeps S at 0; -0.01 enters
    # WATCH with S = 0.0075; the next -0.01 makes S = 0.015 >= 0.01 and
    # retires on reading 3 (the legacy rule waits for reading 7); 0.02 twice
    # (readings 8, 9) recovers to WATCH and the 10th starts the next recovery.
    c = _tracker()
    states = [c.update(i, r) for i, r in enumerate(readings)]
    assert states == [ACTIVE, WATCH, RETIRED] + [RETIRED] * 5 + [WATCH, WATCH]
    assert [t.eval_index for t in c.transitions] == [2, 3, 9]
    assert [t.eval_index for t in a.transitions] == [2, 7, 9]


def test_cusum_does_not_retire_on_one_bad_stretch_seen_through_overlapping_windows():
    """Three consecutive readings of a mostly shared window: the consecutive
    rule retires; the CUSUM rule, told each reading is 1/6 new, does not —
    and does retire once the same evidence is genuinely new."""
    readings = [-0.02, -0.02, -0.02]
    consecutive = _tracker(breach_rule="consecutive")  # the legacy rule, named
    # no slack (cusum_k = 0), so S is exactly the weighted shortfall
    overlapping = _tracker(cusum_k=0.0, cusum_h=0.05)
    disjoint = _tracker(cusum_k=0.0, cusum_h=0.05)
    assert overlapping.config.breach_rule == disjoint.config.breach_rule == "cusum"
    for i, r in enumerate(readings):
        consecutive.update(i, r)
        overlapping.update(i, r, new_fraction=1.0 / 6.0)
        disjoint.update(i, r, new_fraction=1.0)
    assert consecutive.state == RETIRED
    assert overlapping.state == WATCH
    assert overlapping.cusum == pytest.approx(3 * 0.02 / 6.0)
    assert disjoint.state == RETIRED
    assert "CUSUM" in disjoint.transitions[-1].reason
    assert disjoint.cusum == 0.0  # reset by the verdict
    # the overlapping tracker retires after the evidence has accumulated
    for i in range(3, 20):
        if overlapping.update(i, -0.02, new_fraction=1.0 / 6.0) == RETIRED:
            break
    assert overlapping.state == RETIRED and i == 14  # 15 readings * 0.02 / 6


def test_cusum_slack_drains_and_recovery_is_unchanged():
    t = _tracker(breach_rule="cusum", cusum_h=0.05, cusum_k=0.005)
    assert t.update(0, -0.02) == WATCH  # entering WATCH: as before
    assert t.cusum == pytest.approx(0.015)
    t.update(1, -0.004)  # under the gate, inside the slack
    assert t.cusum == pytest.approx(0.014) and t.state == WATCH
    t.update(2, None)  # silence moves nothing
    t.update(3, -0.5, informative=False)
    assert t.cusum == pytest.approx(0.014)
    t.update(4, 0.02)
    assert t.state == WATCH and t.cusum == 0.0  # drained by a good reading
    assert t.update(5, 0.02) == ACTIVE  # two recoveries re-activate
    with pytest.raises(ValueError, match="new_fraction"):
        t.update(6, 0.0, new_fraction=0.0)
    # a retired alpha recovers through WATCH exactly as under the legacy rule
    r = _tracker(breach_rule="cusum", cusum_h=0.01)
    r.update(0, -0.05)
    assert r.update(1, -0.05) == RETIRED
    r.update(2, 0.02)
    assert r.update(3, 0.02) == WATCH and r.cusum == 0.0


# ---------------------------------------------------------------------------
# 15. recompute leakage probe
# ---------------------------------------------------------------------------


class _Ev:
    def __init__(self, ts, px):
        self.exchange_ts, self.px = ts, px


def _toy_builder(events):
    """Features from raw events: ``causal`` uses the past, ``centred`` is a
    centred 3-event mean — it reads the NEXT event (feature look-ahead)."""
    px = np.array([e.px for e in events], dtype=float)
    n = len(px)
    causal = np.array([px[max(0, i - 2) : i + 1].mean() for i in range(n)])
    centred = np.array([px[max(0, i - 1) : i + 2].mean() for i in range(n)])
    return {
        1: pd.DataFrame(
            {
                "exchange_ts": np.array([e.exchange_ts for e in events], dtype=np.int64),
                "causal": causal - px,
                "centred": centred - px,
                "label_mid_1s": np.concatenate((px[1:] / px[:-1] - 1.0, [np.nan])),
                "label_valid_1s": np.concatenate((np.ones(n - 1, dtype=bool), [False])),
            }
        )
    }


class _ToyAlpha(LinearAlpha):
    """Toy alpha over one engine feature.

    Economic rationale: fixture — scores a single pre-built feature column so
    the leakage probes can be pointed at a causal or a leaky feature.
    """

    alpha_id = "TSTL"
    name = "toy_feature"
    asset_class = "EQUITY"
    horizon = "1s"
    column = "causal"

    def universe(self, ids):
        return sorted(ids)

    def raw_signal(self, df):
        return df[self.column]


class _LeakyFeatureAlpha(_ToyAlpha):
    """Toy alpha over the look-ahead feature.

    Economic rationale: fixture — reads the centred (look-ahead) feature;
    its scoring path itself is causal, the FEATURE is not.
    """

    alpha_id = "TSTM"
    column = "centred"


def _toy_events(n=400, seed=51):
    rng = np.random.default_rng(seed)
    px = 100.0 + np.cumsum(rng.standard_normal(n)) * 0.05
    return [_Ev(NS_S * (i + 1), float(p)) for i, p in enumerate(px)]


def test_recompute_probe_catches_feature_lookahead_the_frame_probe_cannot_see():
    events = _toy_events()
    frames = _toy_builder(events)
    tester = LeakageTester()
    for cls in (_ToyAlpha, _LeakyFeatureAlpha):
        model = cls()
        model.fit(frames)
        # the frame-truncation probe passes BOTH: the leak is baked into the
        # precomputed feature column, and truncating the frame cannot undo it
        assert tester.truncation_probe(model, frames)
        assert tester.label_guard(model, frames)
    clean = _ToyAlpha()
    clean.fit(frames)
    leaky = _LeakyFeatureAlpha()
    leaky.fit(frames)
    # the leaky alpha is far "better" — which is the whole danger
    assert (
        abs(
            ic(
                leaky.score(frames)[1]["expected_return"].to_numpy(),
                frames[1]["label_mid_1s"].to_numpy(),
            )
        )
        > 0.3
    )

    got = tester.recompute_probe(leaky, events, _toy_builder, n_probes=3)
    assert not got.ok and got.n_anchors == 3
    assert got.leaky_columns == ["centred", "confidence", "expected_return"]
    assert {m["instrument_id"] for m in got.mismatches} == {1}
    json.dumps(got.to_dict(), allow_nan=False)

    # the SAME pipeline scored by the causal alpha: the feature leak is still
    # reported (the probe checks every feature column), its score is clean
    mixed = tester.recompute_probe(clean, events, _toy_builder, n_probes=3)
    assert not mixed.ok and mixed.leaky_columns == ["centred"]

    def causal_only(evs):
        return {1: _toy_builder(evs)[1].drop(columns=["centred"])}

    ok = tester.recompute_probe(clean, events, causal_only, n_probes=3)
    assert ok.ok and ok.mismatches == [] and ok.n_anchors == 3
    assert tester.recompute_probe(None, events, causal_only, n_probes=2).ok

    # since v1.5.0 the probe is part of the standard run whenever the raw
    # events are handed in, and a failure fails the leakage verdict ...
    caught = tester.run(leaky, frames, recompute=RecomputeSource(events, _toy_builder, 3))
    assert caught.recompute_ok is False and caught.passed is False
    assert caught.recompute_n_anchors == 3 and caught.recompute_mismatches
    causal_frames = causal_only(events)
    with_probe = tester.run(clean, causal_frames, recompute=RecomputeSource(events, causal_only, 3))
    assert with_probe.recompute_ok is True and with_probe.recompute_n_anchors == 3
    # ... and without a source it does not run and says so (None, never a
    # fabricated pass); the other detectors give the same verdict either way
    without = tester.run(clean, causal_frames)
    assert without.recompute_ok is None and without.recompute_n_anchors == 0
    assert without.passed == with_probe.passed
    assert tester.run(leaky, frames).recompute_ok is None


def test_recompute_probe_argument_checks():
    tester = LeakageTester()
    with pytest.raises(ValueError):
        tester.recompute_probe(None, _toy_events(1), _toy_builder)
    with pytest.raises(ValueError):
        tester.recompute_probe(None, _toy_events(10), _toy_builder, n_probes=0)
    assert tester.recompute_probe(None, _toy_events(2), _toy_builder, n_probes=5).n_anchors == 1


def test_reference_feature_engine_passes_the_recompute_probe():
    """The real FeatureEngine on the bundled equity vector: features rebuilt
    from truncated raw events equal the full-run features at every anchor."""
    events = read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")[:600]
    build = engine_frame_builder(CONFIGS_DIR, cadence_ns=0)
    frames = build(events)
    assert list(frames) == [1] and len(frames[1]) == 600
    assert not [c for c in frames[1].columns if c.startswith("label_")]
    got = LeakageTester().recompute_probe(None, events, build, n_probes=2)
    assert got.ok, got.leaky_columns
    assert got.n_anchors == 2


# ---------------------------------------------------------------------------
# 16. per-fold diagnostics and the stationary bootstrap
# ---------------------------------------------------------------------------


def test_stationary_bootstrap_is_seeded_and_brackets_the_estimate():
    rng = np.random.default_rng(61)
    v = rng.standard_normal(500) + 0.3
    a = stationary_bootstrap_ci(v, seed=7, n_boot=400)
    b = stationary_bootstrap_ci(v, seed=7, n_boot=400)
    c = stationary_bootstrap_ci(v, seed=8, n_boot=400)
    assert a == b and a != c
    assert a["estimate"] == pytest.approx(float(v.sum()))
    assert a["ci_low"] < a["estimate"] < a["ci_high"]
    assert a["ci_low"] > 0.0 and a["frac_resamples_le_zero"] < 0.01
    assert a["mean_block"] == 8.0 and a["n"] == 500 and a["seed"] == 7
    # iid case: the interval is close to estimate +- 1.96 * sqrt(n) * sd
    half = 1.96 * math.sqrt(500) * float(v.std())
    assert a["ci_high"] - a["ci_low"] == pytest.approx(2 * half, rel=0.25)
    json.dumps(a, allow_nan=False)


def test_stationary_bootstrap_widens_for_dependent_series():
    """Positively autocorrelated P&L: block resampling keeps the dependence
    and gives a wider interval than resampling single points."""
    rng = np.random.default_rng(62)
    e = rng.standard_normal(1500)
    ar = np.empty(1500)
    ar[0] = e[0]
    for i in range(1, 1500):
        ar[i] = 0.85 * ar[i - 1] + e[i]
    iid = stationary_bootstrap_ci(ar, seed=1, n_boot=400, mean_block=1.0)
    blocks = stationary_bootstrap_ci(ar, seed=1, n_boot=400, mean_block=40.0)
    assert (blocks["ci_high"] - blocks["ci_low"]) > 2.5 * (iid["ci_high"] - iid["ci_low"])


def test_stationary_bootstrap_degenerate_and_invalid_inputs():
    short = stationary_bootstrap_ci([1.0, 2.0, float("nan")], seed=1)
    assert short["estimate"] == 3.0 and short["ci_low"] is None and short["n"] == 2
    empty = stationary_bootstrap_ci([], seed=1)
    assert empty["estimate"] == 0.0 and empty["ci_high"] is None
    const = stationary_bootstrap_ci([2.0] * 50, seed=1, n_boot=50)
    assert const["ci_low"] == const["ci_high"] == 100.0
    for bad in (dict(n_boot=0), dict(level=1.0), dict(mean_block=0.5)):
        with pytest.raises(ValueError):
            stationary_bootstrap_ci(np.ones(20), seed=1, **bad)


def test_validate_alpha_reports_the_fold_diagnostics_by_default():
    """v1.5.0: every-fold diagnostics, the pooled net P&L and its bootstrap
    interval are fields of the default report — exactly the standalone
    ``fold_diagnostics`` block — and the legacy chain leaves them out."""
    frames, bt, report = _validate_backwards(seed=20_260_919, n_boot=200)
    assert report["methods"]["fold_diagnostics"] is True
    diag = fold_diagnostics(
        _BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S, seed=20_260_919, n_boot=200
    )
    assert report["fold_diagnostics"] == diag["folds"] and len(diag["folds"]) == 4
    assert report["n_folds_survive_1x_cost"] == diag["n_folds_survive_1x_cost"] == 0
    assert report["net_pnl_1x_pooled"] == diag["net_pnl_1x_pooled"]
    assert report["net_pnl_bootstrap"] == diag["net_pnl_bootstrap"]
    assert report["net_pnl_bootstrap"]["seed"] == 20_260_919
    # the fixture's forecast is |beta * z| ~ 2.5e-5 * |z| against a round-trip
    # cost of (2 * 0.005 + 2 * 0.003) / 25 = 6.4e-4: under the default
    # cost-aware policy it never clears the entry threshold, so no fold
    # trades and every net P&L is exactly 0 — which does not survive costs
    for f in report["fold_diagnostics"]:
        assert f["trade_count_1x"] == 0
        assert f["net_pnl_by_cost"] == {"x0.5": 0.0, "x1": 0.0, "x2": 0.0}
        assert f["survives_1x_cost"] is False
        assert f["decay_ic_by_horizon"]["1s"] < 0.0  # backwards in EVERY fold
    assert report["trade_count_1x_cost"] == 0 and report["net_pnl_1x_cost"] == 0.0
    assert report["promote_gates"]["cost"] is False
    # no trade, no realised edge: the default (breakeven) capacity is 0 where
    # the legacy participation proxy is 0.1 * ADV * price whatever the edge
    assert report["capacity_usd_by_instrument"] == {"1": 0.0}
    assert report["capacity_proxy_usd_by_instrument"] == {"1": 0.1 * 1_000_000.0 * 25.0}
    _, _, legacy = _validate_backwards("legacy_v1")
    assert legacy["methods"]["fold_diagnostics"] is False
    for key in ("fold_diagnostics", "n_folds_survive_1x_cost", "net_pnl_bootstrap"):
        assert key not in legacy
    assert legacy["capacity_usd_by_instrument"] == {"1": 0.1 * 1_000_000.0 * 25.0}


def test_fold_diagnostics_reports_every_fold_and_matches_the_last_fold_of_legacy_validate():
    """Under the LEGACY backtest rules, named (sign positions, uncapped
    fills, linear impact): the fixture trades every row and loses money in
    every fold, so the cost grid is strictly ordered."""
    frames, bt, report = _validate_backwards("legacy_v1")
    assert bt.config == BacktestConfig.legacy(max_pos_qty=100, latency_rows=1)
    assert bt.cost_model == _linear_cost_model()
    diag = fold_diagnostics(
        _BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S, seed=20_260_919, n_boot=200
    )
    assert diag["n_folds_run"] == 4 == len(diag["folds"])
    assert [f["fold"] for f in diag["folds"]] == [r["fold"] for r in report["folds"]]
    for f, r in zip(diag["folds"], report["folds"], strict=False):
        assert f["n_test_pairs"] == r["n_test_pairs"] and f["degenerate"] == r["degenerate"]
        assert sorted(f["net_pnl_by_cost"]) == ["x0.5", "x1", "x2"]
        assert (
            f["net_pnl_by_cost"]["x0.5"] > f["net_pnl_by_cost"]["x1"] > f["net_pnl_by_cost"]["x2"]
        )
        assert f["survives_1x_cost"] == (f["net_pnl_by_cost"]["x1"] > 0.0)
        assert set(f["regime"]) == {"ic_high_vol", "ic_low_vol"}
        assert f["decay_ic_by_horizon"]["1s"] < 0.0  # backwards in EVERY fold
    # the last fold is exactly what validate_alpha reports as its one view
    last = diag["folds"][-1]
    assert last["net_pnl_by_cost"]["x1"] == pytest.approx(report["net_pnl_1x_cost"])
    assert last["regime"] == pytest.approx(report["stress"]["regime"])
    assert last["decay_ic_by_horizon"]["1s"] == pytest.approx(report["decay_ic_by_horizon"]["1s"])
    assert diag["n_folds_survive_1x_cost"] == 0
    boot = diag["net_pnl_bootstrap"]
    assert boot["seed"] == 20_260_919 and boot["n_boot"] == 200
    assert boot["estimate"] == pytest.approx(diag["net_pnl_1x_pooled"])
    assert diag["net_pnl_1x_pooled"] == pytest.approx(
        sum(f["net_pnl_by_cost"]["x1"] for f in diag["folds"])
    )
    assert boot["ci_high"] < 0.0  # loses money, reliably
    again = fold_diagnostics(
        _BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S, seed=20_260_919, n_boot=200
    )
    assert again == diag
    json.dumps(diag, allow_nan=False)
    with pytest.raises(ValueError, match="multipliers must include 1.0"):
        fold_diagnostics(
            _BackwardsAlpha, frames, bt, n_folds=4, embargo_ns=NS_S, multipliers=(0.5, 2.0)
        )

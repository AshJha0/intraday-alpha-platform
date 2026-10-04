"""Research-validity regressions: stress configs differ from the base in the
stressed parameter only, the pooled-slope HAC t-stat, the ledger-derived
t-threshold, the cost-aware position policy and the meta-label AUC.

Since v1.5.0 the corrected methods are the DEFAULTS; every test here that
pins a pre-v1.5.0 rule selects it by its legacy name."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest
from iap.backtest import BacktestConfig, Backtester, CostModel
from iap.backtest.engine import POSITION_POLICIES, cost_aware_targets
from iap.labels.labels import HORIZONS_NS
from iap.models.metalabel import META_CONTEXT_COLUMNS, build_meta_features
from iap.validation.ledger import ExperimentLedger
from iap.validation.methods import methods
from iap.validation.metrics import (
    bucket_ics_with_counts,
    newey_west_tstat,
    pooled_slope_hac_tstat,
)
from iap.validation.stress import (
    DEFAULT_STRESS_VERSION,
    STRESS_VERSION_CARRY,
    STRESS_VERSION_LEGACY,
    cost_stress,
    latency_stress,
    latency_stress_time,
)
from iap.validation.validate import GATES, effective_gates, validate_alpha
from test_validation_framework import _backwards_frames, _BackwardsAlpha

NS_S = 1_000_000_000
NS_DAY = 86_400 * NS_S

META = {
    1: {
        "tick_size": 0.01,
        "lot_size": 100,
        "adv": 1_000_000.0,
        "asset_class": "EQUITY",
        "ref_price": 25.0,
    }
}


def _cost_model(multiplier: float = 1.0) -> CostModel:
    return CostModel(
        impact_coeff_bps_per_pct_adv=2.0,
        equity_taker_fee_per_share=0.003,
        fx_commission_per_million=2.5,
        multiplier=multiplier,
    )


def _frame(ts, mids, spread_ticks=2.0) -> pd.DataFrame:
    """A frame the DEFAULT backtest rules can run on: displayed L1 size well
    above every test position (the fill cap never binds) and a valid label
    at every pinned horizon (the scored-rows block lets every row decide)."""
    n = len(mids)
    cols = {
        "exchange_ts": np.asarray(ts, dtype=np.int64),
        "mid_price_v1": np.asarray(mids, dtype=float),
        "spread_ticks_v1": np.full(n, spread_ticks, dtype=float),
        "depth_bid_l1_v1": np.full(n, 1000.0),
        "depth_ask_l1_v1": np.full(n, 1000.0),
    }
    for h in HORIZONS_NS:
        cols[f"label_mid_{h}"] = np.zeros(n)
        cols[f"label_valid_{h}"] = np.ones(n, dtype=bool)
    return pd.DataFrame(cols)


def _scores(ts, er, conf=None) -> pd.DataFrame:
    n = len(er)
    return pd.DataFrame(
        {
            "exchange_ts": np.asarray(ts, dtype=np.int64),
            "expected_return": np.asarray(er, dtype=float),
            "confidence": np.ones(n) if conf is None else np.asarray(conf, dtype=float),
        }
    )


# ---------------------------------------------------------------------------
# 1. stress: the stressed backtest differs from the base in ONE parameter
# ---------------------------------------------------------------------------


class _RecordingBacktester(Backtester):
    """Records every config a stress function builds from it."""

    built: list = []

    def __init__(self, cost_model, meta, config=None, reporting_ccy="USD"):
        super().__init__(cost_model, meta, config, reporting_ccy=reporting_ccy)
        _RecordingBacktester.built.append((self.config, self.reporting_ccy))


@pytest.fixture()
def recorded(monkeypatch):
    import iap.validation.stress as stress

    _RecordingBacktester.built = []
    monkeypatch.setattr(stress, "Backtester", _RecordingBacktester)
    return _RecordingBacktester.built


def _two_session_inputs():
    """Two sessions; the signal is long into the first close.

    The forecast is 3e-3: above the round-trip hurdle of the default
    cost-aware policy at every cost multiplier used here (at 2x and mid 25:
    2 * (2 * 0.01 + 2 * 0.003) / 25 = 2.08e-3), so the default base strategy
    trades, and trades the same rows at every multiplier."""
    day = [NS_DAY + 14 * 3600 * NS_S + i * NS_S for i in range(40)]
    ts = day + [t + NS_DAY for t in day]
    mids = np.concatenate([np.full(40, 25.0), np.full(40, 30.0)])
    frames = {1: _frame(ts, mids)}
    scores = {1: _scores(ts, np.full(80, 3e-3))}
    return frames, scores


#: The base strategy under the DEFAULT rules (cost-aware positions, L1 fill
#: cap, scored-rows block) with the label horizon set.
BASE = BacktestConfig(
    max_pos_qty=100,
    conf_min=0.5,
    latency_rows=1,
    max_decision_age_ns=60 * NS_S,
    flatten_at_session_end=True,
    session_gap_ns=45 * 60 * NS_S,
).for_horizon("1s")
#: The same base strategy under the LEGACY (v1.4.0) backtest rules.
BASE_LEGACY = BacktestConfig.legacy(
    max_pos_qty=100,
    conf_min=0.5,
    latency_rows=1,
    max_decision_age_ns=60 * NS_S,
    flatten_at_session_end=True,
    session_gap_ns=45 * 60 * NS_S,
)


def test_base_configs_are_the_default_and_the_legacy_rules():
    assert BASE.position_policy == "cost_aware" and BASE.horizon_ns == NS_S
    assert BASE.cap_fills_at_l1 is True and BASE.block_rows_column == "auto"
    assert BASE_LEGACY.position_policy == "sign"
    assert BASE_LEGACY.cap_fills_at_l1 is False and BASE_LEGACY.block_rows_column is None


def test_time_latency_stress_changes_only_latency_ns(recorded):
    frames, scores = _two_session_inputs()
    base = Backtester(_cost_model(), META, BASE, reporting_ccy="EUR")
    latency_stress_time(base, frames, scores, "EQUITY", "1s", latencies_ns=(NS_S,))
    ((cfg, ccy),) = recorded
    assert ccy == "EUR"
    assert cfg == dataclasses.replace(BASE, latency_ns=NS_S)


def test_cost_stress_carries_config_and_reporting_currency(recorded):
    frames, scores = _two_session_inputs()
    base = Backtester(_cost_model(), META, BASE)
    out = cost_stress(base, frames, scores, "EQUITY", multipliers=(0.5, 2.0))
    assert [c for c, _ in recorded] == [BASE, BASE]
    assert {ccy for _, ccy in recorded} == {"USD"}
    assert out["x0.5"]["total_costs"] * 4 == pytest.approx(out["x2"]["total_costs"])
    # the default strategy trades here (in, flat at the close, in, flat)
    assert out["x0.5"]["trade_count"] == out["x2"]["trade_count"] == 4
    assert out["x2"]["total_costs"] > 0.0


def test_cost_stress_carries_the_legacy_config_too(recorded):
    frames, scores = _two_session_inputs()
    base = Backtester(_cost_model().with_linear_impact(), META, BASE_LEGACY)
    out = cost_stress(base, frames, scores, "EQUITY", multipliers=(0.5, 2.0))
    assert [c for c, _ in recorded] == [BASE_LEGACY, BASE_LEGACY]
    assert out["x0.5"]["total_costs"] * 4 == pytest.approx(out["x2"]["total_costs"])
    assert out["x2"]["total_costs"] > 0.0


def test_row_latency_stress_legacy_v1_is_the_historic_four_field_rebuild(recorded):
    """Version 1 (legacy, selected by name): four fields of the base and
    every other field at its v1.4.0 default — i.e. the legacy backtest rules,
    whatever the base strategy's rules are."""
    frames, scores = _two_session_inputs()
    for base_cfg in (BASE, BASE_LEGACY):
        recorded.clear()
        base = Backtester(_cost_model(), META, base_cfg)
        latency_stress(
            base, frames, scores, "EQUITY", "1s", shifts=(0, 2), version=STRESS_VERSION_LEGACY
        )
        assert [c for c, _ in recorded] == [
            BacktestConfig.legacy(max_pos_qty=100, conf_min=0.5, latency_rows=1),
            BacktestConfig.legacy(max_pos_qty=100, conf_min=0.5, latency_rows=3),
        ]


def test_row_latency_stress_default_version_is_the_carry(recorded):
    assert DEFAULT_STRESS_VERSION == STRESS_VERSION_CARRY == 2
    assert STRESS_VERSION_LEGACY == 1
    frames, scores = _two_session_inputs()
    base = Backtester(_cost_model(), META, BASE)
    latency_stress(base, frames, scores, "EQUITY", "1s", shifts=(0, 2))
    assert [c for c, _ in recorded] == [
        dataclasses.replace(BASE, latency_rows=1),
        dataclasses.replace(BASE, latency_rows=3),
    ]


def test_row_latency_stress_v2_carries_every_base_field(recorded):
    frames, scores = _two_session_inputs()
    timed = dataclasses.replace(BASE, latency_ns=NS_S)
    base = Backtester(_cost_model(), META, timed)
    latency_stress(
        base, frames, scores, "EQUITY", "1s", shifts=(0, 2), version=STRESS_VERSION_CARRY
    )
    assert [c for c, _ in recorded] == [
        dataclasses.replace(BASE, latency_rows=1, latency_ns=None),
        dataclasses.replace(BASE, latency_rows=3, latency_ns=None),
    ]
    # the grid is defined in rows: a carried latency_ns would override it
    assert all(c.latency_ns is None for c, _ in recorded)


@pytest.mark.parametrize("base_cfg", [BASE, BASE_LEGACY], ids=["default", "legacy"])
def test_row_latency_stress_v2_keeps_the_base_strategy_flat_overnight(base_cfg):
    """The defect v2 fixes, as P&L: the base strategy is flat overnight, the
    legacy v1 stressed strategy is not and books the overnight jump."""
    frames, scores = _two_session_inputs()
    base = Backtester(_cost_model(), META, base_cfg)
    v1 = latency_stress(
        base, frames, scores, "EQUITY", "1s", shifts=(0,), version=STRESS_VERSION_LEGACY
    )["+0ev"]["total_pnl"]
    v2 = latency_stress(
        base, frames, scores, "EQUITY", "1s", shifts=(0,), version=STRESS_VERSION_CARRY
    )["+0ev"]["total_pnl"]
    assert v1 > 400.0  # 100 shares * the 5.00 overnight gap
    assert v2 < 0.0  # flat overnight: only costs remain
    assert v2 == pytest.approx(base.run(frames, scores, "EQUITY").total_pnl)
    # and version 2 is what a caller gets without naming a version
    default = latency_stress(base, frames, scores, "EQUITY", "1s", shifts=(0,))
    assert default["+0ev"]["total_pnl"] == v2


def test_row_latency_stress_rejects_unknown_version():
    frames, scores = _two_session_inputs()
    base = Backtester(_cost_model(), META, BASE)
    with pytest.raises(ValueError, match="unknown stress version"):
        latency_stress(base, frames, scores, "EQUITY", "1s", version=3)


def test_row_latency_ic_is_null_not_nan():
    """A row-grid IC that cannot be computed is None (JSON null): a raw NaN
    is not valid JSON and bypassed the report's own NaN policy."""
    frames, scores = _two_session_inputs()  # constant label: IC undefined
    base = Backtester(_cost_model(), META, BASE)
    for version in (STRESS_VERSION_LEGACY, STRESS_VERSION_CARRY):
        out = latency_stress(base, frames, scores, "EQUITY", "1s", version=version)
        assert all(row["ic"] is None for row in out.values())
        json.dumps(out, allow_nan=False)


# ---------------------------------------------------------------------------
# 4. pooled-slope HAC t-stat
# ---------------------------------------------------------------------------


def test_pooled_slope_hac_tstat_hand_calc():
    """Two-row buckets, lag 1, against the formula written out."""
    rng = np.random.default_rng(5)  # test-only fixture data
    n_buckets, per = 12, 2
    ts = np.repeat(np.arange(n_buckets, dtype=np.int64) * 300 * NS_S, per) + NS_S
    x = rng.standard_normal(n_buckets * per)
    y = 0.3 * x + rng.standard_normal(n_buckets * per)
    dx, dy = x - x.mean(), y - y.mean()
    sxx = float(np.sum(dx * dx))
    b = float(np.sum(dx * dy)) / sxx
    s = (dx * (dy - b * dx)).reshape(n_buckets, per).sum(axis=1)
    lrv = float(np.sum(s * s)) + 2.0 * 0.5 * float(np.sum(s[1:] * s[:-1]))
    want = b * sxx / np.sqrt(lrv)
    assert pooled_slope_hac_tstat(ts, x, y, lags=1) == pytest.approx(want, rel=1e-12)


def test_pooled_slope_hac_tstat_is_scale_and_shift_invariant_and_signed():
    rng = np.random.default_rng(6)
    n = 4000
    ts = np.arange(n, dtype=np.int64) * 10 * NS_S
    x = rng.standard_normal(n)
    y = 0.1 * x + rng.standard_normal(n)
    t = pooled_slope_hac_tstat(ts, x, y, lags=2)
    assert t > 3.0
    assert pooled_slope_hac_tstat(ts, 7.0 * x + 3.0, 1e-4 * y - 2.0, lags=2) == pytest.approx(
        t, rel=1e-9
    )
    assert pooled_slope_hac_tstat(ts, -x, y, lags=2) == pytest.approx(-t, rel=1e-9)


def test_pooled_slope_hac_tstat_keeps_the_between_bucket_signal():
    """A signal that is CONSTANT inside each bucket and predicts the
    bucket's mean return: the pooled IC is large, every within-bucket
    Pearson is undefined (so the within-bucket t sees nothing), and the
    pooled-slope t reports it."""
    rng = np.random.default_rng(8)
    n_buckets, per = 60, 50
    level = rng.standard_normal(n_buckets)
    ts = np.repeat(np.arange(n_buckets, dtype=np.int64) * 300 * NS_S, per) + np.tile(
        np.arange(per, dtype=np.int64) * NS_S, n_buckets
    )
    x = np.repeat(level, per)
    y = 0.5 * x + rng.standard_normal(n_buckets * per)
    bics, counts = bucket_ics_with_counts(ts, x, y)
    assert bics.size == 0  # nothing to average
    assert np.isnan(newey_west_tstat(bics, lags=2, weights=counts))
    assert pooled_slope_hac_tstat(ts, x, y, lags=2) > 5.0


def test_pooled_slope_hac_tstat_is_not_fooled_by_within_bucket_duplication():
    """Rows that are copies of each other inside a bucket carry one bucket's
    worth of evidence: the statistic must not grow with the copy count."""
    rng = np.random.default_rng(9)
    n_buckets = 80
    xb = rng.standard_normal(n_buckets)
    yb = 0.2 * xb + rng.standard_normal(n_buckets)
    base_ts = np.arange(n_buckets, dtype=np.int64) * 300 * NS_S
    t1 = pooled_slope_hac_tstat(base_ts, xb, yb, lags=0)
    copies = 25
    t25 = pooled_slope_hac_tstat(
        np.repeat(base_ts, copies), np.repeat(xb, copies), np.repeat(yb, copies), lags=0
    )
    assert t25 == pytest.approx(t1, rel=1e-9)


def test_pooled_slope_hac_tstat_degenerate_inputs():
    ts = np.arange(100, dtype=np.int64) * 300 * NS_S
    x = np.arange(100, dtype=float)
    assert np.isnan(pooled_slope_hac_tstat(ts[:5], x[:5], x[:5] ** 2))  # < 8 buckets
    assert np.isnan(pooled_slope_hac_tstat(ts, np.ones(100), x))  # flat score
    assert np.isnan(pooled_slope_hac_tstat(ts, x, np.full(100, np.nan)))
    with pytest.raises(ValueError):
        pooled_slope_hac_tstat(ts, x[:-1], x)
    with pytest.raises(ValueError):
        pooled_slope_hac_tstat(ts, x, x, lags=-1)


#: Stand-in for the ledger's Bonferroni |t| at the run's gate look count.
LEDGER_T = 3.5
LEDGER_LOOKS = 84


def _with_depth(frames):
    """The frames with a displayed L1 size (the default fill cap needs the
    columns; 1000 never binds on the 100-share test positions)."""
    out = {}
    for iid, df in frames.items():
        df = df.copy()
        for name in ("depth_bid_l1_v1", "depth_ask_l1_v1"):
            if name not in df.columns:
                df[name] = 1000.0
        out[iid] = df
    return out


def _validate(bundle="v2", **kwargs):
    """``validate_alpha`` on the backwards fixture under a method bundle:
    ``"v2"`` = every default (the ledger threshold is the caller's),
    ``"legacy_v1"`` = every legacy rule, by name."""
    m = methods(bundle)
    frames = _with_depth(_backwards_frames())
    meta = {
        1: {
            "tick_size": 0.01,
            "lot_size": 1,
            "adv": 1_000_000.0,
            "asset_class": "EQUITY",
            "ref_price": 25.0,
        }
    }
    if bundle == "v2":
        # the defaults, spelled by NOT passing them
        bt = Backtester(_cost_model(), meta, BacktestConfig(max_pos_qty=100, latency_rows=1))
        method_kwargs = {"ledger_t_threshold": LEDGER_T, "ledger_looks": LEDGER_LOOKS}
    else:
        bt = Backtester(
            m.cost_model(_cost_model()), meta, m.backtest_config(max_pos_qty=100, latency_rows=1)
        )
        method_kwargs = m.validate_kwargs()
    method_kwargs.update(kwargs)
    return validate_alpha(
        _BackwardsAlpha,
        frames,
        bt,
        {1: {"adv": 1_000_000.0, "ref_price": 25.0}},
        0.1,
        n_folds=4,
        embargo_ns=NS_S,
        **method_kwargs,
    )


@pytest.fixture(scope="module")
def default_report():
    return _validate()


@pytest.fixture(scope="module")
def legacy_report():
    return _validate("legacy_v1")


def test_report_carries_the_pooled_tstat_beside_the_gate_input(default_report, legacy_report):
    r = default_report
    assert r["nw_tstat_pooled"] is not None and r["nw_tstat_pooled_uncrossed"] is not None
    # the backwards fixture is strongly NEGATIVE on z in both statistics
    assert r["nw_tstat"] < -3.0 and r["nw_tstat_pooled"] < -3.0
    # the DEFAULT report names every method, reads the pooled-slope t and
    # carries the ledger policy keys and the ledger-tightened gate
    assert r["methods"] == {
        "ic_rows": "blackout_reopen",
        "significance": "pooled_slope",
        "tstat_threshold": "ledger",
        "stress_version": 2,
        "capacity": "breakeven",
        "fold_diagnostics": True,
        "recompute_probe": False,
        "position_policy": "cost_aware",
        "cap_fills_at_l1": True,
        "block_rows": "scored_rows:1s",
        "impact_model": "sqrt",
    }
    assert r["gate_tstat"] == r["nw_tstat_pooled_uncrossed"]
    assert r["gates"] == {**GATES, "min_nw_tstat": LEDGER_T}
    assert r["tstat_threshold_policy"] == "ledger"
    assert r["ledger_t_threshold"] == LEDGER_T
    assert r["ledger_looks"] == LEDGER_LOOKS
    assert len(r["fold_diagnostics"]) == r["n_folds_run"]
    # the LEGACY bundle, by name: no policy keys, the pinned gates, the
    # within-bucket t — and the same statistics on the same rows
    old = legacy_report
    assert old["gates"] is GATES
    assert not {"tstat_threshold_policy", "ledger_t_threshold", "stress_version"} & set(old)
    assert not {"fold_diagnostics", "net_pnl_bootstrap", "net_pnl_1x_pooled"} & set(old)
    assert old["methods"] == {
        "ic_rows": "valid_only",
        "significance": "within_bucket",
        "tstat_threshold": "fixed",
        "stress_version": 1,
        "capacity": "participation",
        "fold_diagnostics": False,
        "recompute_probe": False,
        "position_policy": "sign",
        "cap_fills_at_l1": False,
        "block_rows": None,
        "impact_model": "linear",
    }
    assert old["gate_tstat"] == old["nw_tstat_uncrossed"]
    for key in ("nw_tstat", "nw_tstat_pooled", "nw_tstat_uncrossed", "gate_ic", "oos_ic"):
        assert old[key] == r[key]


# ---------------------------------------------------------------------------
# 5. ledger-derived t threshold (the default; the fixed 3.0 is the legacy)
# ---------------------------------------------------------------------------


def test_effective_gates_legacy_fixed_is_the_pinned_table():
    assert effective_gates("fixed") is GATES
    assert effective_gates("fixed", 9.0) is GATES
    assert GATES["min_nw_tstat"] == 3.0


def test_effective_gates_default_is_the_ledger_policy():
    # the default policy has no silent fall-back to the fixed gate
    with pytest.raises(ValueError, match="ledger_t_threshold"):
        effective_gates()
    gates = effective_gates(ledger_t_threshold=4.07)
    assert gates == effective_gates("ledger", 4.07)
    assert gates is not GATES
    assert gates == {**GATES, "min_nw_tstat": 4.07}


def test_effective_gates_ledger_never_loosens_and_needs_a_threshold():
    assert effective_gates("ledger", 4.07)["min_nw_tstat"] == 4.07
    assert effective_gates("ledger", 1.96)["min_nw_tstat"] == 3.0  # never looser
    assert GATES["min_nw_tstat"] == 3.0  # table untouched
    for bad in (None, float("nan"), 0.0, -1.0):
        with pytest.raises(ValueError, match="ledger_t_threshold"):
            effective_gates("ledger", bad)
    with pytest.raises(ValueError, match="unknown tstat_threshold"):
        effective_gates("bonferroni", 4.0)


def test_ledger_threshold_helpers(tmp_path):
    led = ExperimentLedger(tmp_path / "experiments.json")
    led.record("EQ01", "k", {"a": 1}, count=100)
    assert led.bonferroni_t_threshold_at(100) == led.bonferroni_t_threshold()
    assert led.bonferroni_t_threshold_at(1000) > led.bonferroni_t_threshold()
    assert led.would_add("EQ01", "k", {"a": 1}, 28) == 0  # a rerun
    assert led.would_add("EQ01", "k", {"a": 2}, 28) == 28


class _PromotableAlpha(_BackwardsAlpha):
    """Fixture whose stated direction the data confirms.

    Economic rationale: fixture — the backwards fixture's signal with the
    orientation flipped, so the fit confirms the hypothesis.  Exists only
    inside the test suite.
    """

    alpha_id = "TST5"

    def raw_signal(self, df):
        return -df["sig_feature"]


@pytest.mark.parametrize(
    ("bundle", "t_key"),
    [("v2", "nw_tstat_pooled_uncrossed"), ("legacy_v1", "nw_tstat_uncrossed")],
)
def test_ledger_policy_changes_only_the_tstat_gate(bundle, t_key):
    """Under either bundle the ledger threshold moves the t gate and nothing
    else; ``t_key`` is the t-statistic that bundle's gate reads."""
    m = methods(bundle)
    df = _with_depth(_backwards_frames())[1]
    # Make the fixture tradable: the mid realises each row's label over the
    # NEXT row, and the book has no spread, so a correct sign earns money.
    label = df["label_mid_1s"].to_numpy()
    df["mid_price_v1"] = 25.0 * (1.0 + np.concatenate(([0.0], np.cumsum(label)[:-1])))
    df["spread_ticks_v1"] = 0.0
    frames = {1: df}
    meta = {
        1: {
            "tick_size": 0.01,
            "lot_size": 1,
            "adv": 1_000_000.0,
            "asset_class": "EQUITY",
            "ref_price": 25.0,
        }
    }
    free = CostModel(
        impact_coeff_bps_per_pct_adv=0.0,
        equity_taker_fee_per_share=0.0,
        fx_commission_per_million=0.0,
        sqrt_impact_coeff_bps=0.0,  # free under either impact rule
    )
    bt = Backtester(m.cost_model(free), meta, m.backtest_config(max_pos_qty=100, latency_rows=0))
    args = (_PromotableAlpha, frames, bt, {1: {"adv": 1_000_000.0, "ref_price": 25.0}}, 0.1)
    kw = {**m.validate_kwargs(), "n_folds": 4, "embargo_ns": NS_S}
    fixed = validate_alpha(*args, **{**kw, "tstat_threshold": "fixed"})
    assert fixed["methods"]["tstat_threshold"] == "fixed" and fixed["gates"] is GATES
    t = fixed[t_key]
    assert fixed["gate_tstat"] == t
    assert fixed["verdict"] == "PROMOTE" and t > 3.0
    ledger = {**kw, "tstat_threshold": "ledger"}
    below = validate_alpha(*args, **ledger, ledger_t_threshold=t - 0.5)
    above = validate_alpha(*args, **ledger, ledger_t_threshold=t + 0.5)
    assert below["verdict"] == "PROMOTE"
    assert above["verdict"] == "ITERATE"  # evidence, not promotion
    assert above["gates"]["min_nw_tstat"] == pytest.approx(t + 0.5)
    assert above["tstat_threshold_policy"] == "ledger"
    assert above["ledger_t_threshold"] == pytest.approx(t + 0.5)
    for key in ("gate_ic", "gate_tstat", "nw_tstat", "nw_tstat_pooled", "fold_sign_consistency"):
        assert above[key] == fixed[key]
    with pytest.raises(ValueError, match="ledger_t_threshold"):
        validate_alpha(*args, **ledger)
    # "ledger" is the default policy: not naming one is the same error
    with pytest.raises(ValueError, match="ledger_t_threshold"):
        validate_alpha(*args, n_folds=4, embargo_ns=NS_S)


def test_stress_version_is_always_reported_and_defaults_to_carry(default_report):
    # the default report names version 2 (under ``methods``) without opting in
    assert default_report["methods"]["stress_version"] == 2
    carried = _validate(stress_version=STRESS_VERSION_CARRY)
    assert carried["methods"]["stress_version"] == 2
    assert carried["stress"] == default_report["stress"]
    assert carried["verdict"] == default_report["verdict"]
    # the legacy grid is selected by name and reported as such
    old = _validate(stress_version=STRESS_VERSION_LEGACY)
    assert old["methods"]["stress_version"] == 1
    assert old["verdict"] == default_report["verdict"]
    for key in ("cost", "latency_time", "regime"):
        assert old["stress"][key] == default_report["stress"][key]


# ---------------------------------------------------------------------------
# 3. cost-aware position policy
# ---------------------------------------------------------------------------


def test_position_policy_defaults_and_validation():
    assert BacktestConfig().position_policy == "cost_aware"
    assert BacktestConfig.legacy().position_policy == "sign"
    assert POSITION_POLICIES == ("cost_aware", "sign")
    with pytest.raises(ValueError, match="unknown position_policy"):
        BacktestConfig(position_policy="kelly")
    # the horizon is checked when the policy RUNS (Backtester.for_horizon sets
    # it): a cost-aware config without one is an error, never a fall-back
    assert BacktestConfig(position_policy="cost_aware").horizon_ns is None
    with pytest.raises(ValueError, match="needs horizon_ns"):
        _policy_backtest(np.zeros(4), BacktestConfig(position_policy="cost_aware"))
    with pytest.raises(ValueError, match="needs horizon_ns"):
        _policy_backtest(np.zeros(4), BacktestConfig())
    with pytest.raises(ValueError, match="horizon_ns must be positive"):
        BacktestConfig(position_policy="cost_aware", horizon_ns=0)
    with pytest.raises(ValueError, match="hysteresis"):
        BacktestConfig(position_policy="cost_aware", horizon_ns=NS_S, hysteresis=1.5)


def test_round_trip_cost_return_hand_calc():
    cm = _cost_model()
    eq = cm.round_trip_cost_return(
        np.array([25.0, np.nan, 25.0]), np.array([0.01, 0.01, -0.01]), "EQUITY"
    )
    assert eq[0] == pytest.approx((2 * 0.01 + 2 * 0.003) / 25.0)
    assert np.isnan(eq[1]) and np.isnan(eq[2])
    fx = _cost_model(multiplier=2.0).round_trip_cost_return(np.array([1.1]), np.array([2e-5]), "FX")
    assert fx[0] == pytest.approx(2.0 * (2 * 2e-5 / 1.1 + 2 * 2.5e-6))
    with pytest.raises(ValueError):
        cm.round_trip_cost_return(np.array([1.0]), np.array([0.1]), "BOND")


def _targets(er, thr=1.0, conf=None, horizon=3, hysteresis=0.5, ts=None):
    n = len(er)
    ts = np.arange(n, dtype=np.int64) if ts is None else np.asarray(ts, dtype=np.int64)
    return cost_aware_targets(
        ts,
        np.asarray(er, dtype=float),
        np.ones(n) if conf is None else np.asarray(conf, dtype=float),
        np.full(n, thr) if np.isscalar(thr) else np.asarray(thr, dtype=float),
        0.5,
        horizon,
        hysteresis,
    ).tolist()


def test_cost_aware_enters_only_above_the_cost_hurdle():
    assert _targets([0.9, 1.0, -1.0, 0.0]) == [0, 0, 0, 0]  # never strictly above
    assert _targets([0.9, 1.1, 0.0, 0.0])[:2] == [0, 1]
    assert _targets([-1.1, 0.0])[0] == -1
    # low confidence, NaN forecast and an unpriceable row cannot open
    assert _targets([5.0, 5.0], conf=[0.4, 0.4]) == [0, 0]
    assert _targets([np.nan, 5.0], thr=[1.0, np.nan]) == [0, 0]


def test_cost_aware_holds_to_the_horizon_through_weak_and_missing_signals():
    #          enter  weak  none  expiry->flat
    er = [2.0, 0.1, np.nan, 0.0, 0.0]
    assert _targets(er, horizon=3) == [1, 1, 1, 0, 0]
    # event time, not rows: the same rows 2 s apart expire one row earlier
    assert _targets(er, horizon=3, ts=[0, 2, 4, 6, 8]) == [1, 1, 0, 0, 0]


def test_cost_aware_flips_only_on_an_opposite_signal_that_clears_the_hurdle():
    assert _targets([2.0, -0.9, -1.1, 0.0], horizon=10) == [1, 1, -1, -1]
    # the flip restarts the clock: entered at t=2, expires at t=5
    assert _targets([2.0, 0.0, -2.0, 0.0, 0.0, 0.0], horizon=3) == [1, 1, -1, -1, -1, 0]


def test_cost_aware_hysteresis_band_renews_an_expired_hold():
    # at expiry (t=3) a same-direction signal above 0.5 * hurdle renews ...
    assert _targets([2.0, 0.0, 0.0, 0.6, 0.0, 0.0, 0.0], horizon=3) == [1, 1, 1, 1, 1, 1, 0]
    # ... one inside the band does not, and it is not enough to re-enter
    assert _targets([2.0, 0.0, 0.0, 0.4, 0.6], horizon=3) == [1, 1, 1, 0, 0]
    # hysteresis 1.0 = no band: staying needs the full entry hurdle
    assert _targets([2.0, 0.0, 0.0, 0.9], horizon=3, hysteresis=1.0) == [1, 1, 1, 0]
    # hysteresis 0.0: any same-direction signal renews
    assert _targets([2.0, 0.0, 0.0, 1e-9], horizon=3, hysteresis=0.0) == [1, 1, 1, 1]


def _policy_backtest(er, config, mids=None, spread_ticks=2.0):
    n = len(er)
    ts = NS_S + np.arange(n, dtype=np.int64) * NS_S
    mids = np.full(n, 25.0) if mids is None else mids
    bt = Backtester(_cost_model(), META, config)
    return bt.run_instrument(1, _frame(ts, mids, spread_ticks), _scores(ts, er))


def test_legacy_sign_policy_is_bit_identical_with_the_cost_aware_fields_present():
    rng = np.random.default_rng(11)
    er = rng.standard_normal(200) * 1e-3
    mids = 25.0 + np.cumsum(rng.standard_normal(200)) * 0.01
    a = _policy_backtest(er, BacktestConfig.legacy(max_pos_qty=100), mids)
    assert a.trade_count > 0
    b = _policy_backtest(
        er,
        BacktestConfig(
            max_pos_qty=100,
            position_policy="sign",
            cap_fills_at_l1=False,
            block_rows_column=None,
            horizon_ns=5 * NS_S,
            hysteresis=0.1,
        ),
        mids,
    )
    assert np.array_equal(a.positions, b.positions)
    assert a.total_pnl == b.total_pnl and a.trade_count == b.trade_count


def test_cost_aware_backtest_trades_less_and_respects_the_hurdle():
    """Forecasts below the round-trip cost never trade under cost_aware (the
    default); the legacy sign rule trades every one of them and pays the
    spread each time."""
    hurdle = (2 * 0.01 + 2 * 0.003) / 25.0  # 1.04e-3
    er = np.tile([0.5 * hurdle, -0.5 * hurdle], 50)
    sign = _policy_backtest(er, BacktestConfig.legacy(max_pos_qty=100, latency_rows=0))
    aware = _policy_backtest(
        er, BacktestConfig(max_pos_qty=100, latency_rows=0, horizon_ns=5 * NS_S)
    )
    assert sign.trade_count == 100 and sign.total_pnl < 0.0
    assert aware.trade_count == 0 and aware.total_pnl == 0.0


def test_cost_aware_backtest_holds_for_the_label_horizon():
    hurdle = (2 * 0.01 + 2 * 0.003) / 25.0
    er = np.zeros(20)
    er[2] = 3 * hurdle  # one forecast over a 5 s horizon
    res = _policy_backtest(er, BacktestConfig(max_pos_qty=100, latency_rows=0).for_horizon("5s"))
    assert res.positions.tolist() == [0, 0] + [100] * 5 + [0] * 13
    assert res.trade_count == 2
    # the legacy sign rule holds it for exactly one row
    one = _policy_backtest(er, BacktestConfig.legacy(max_pos_qty=100, latency_rows=0))
    assert one.positions.tolist() == [0, 0, 100] + [0] * 17


def test_cost_aware_policy_composes_with_latency_and_session_flattening():
    hurdle = (2 * 0.01 + 2 * 0.003) / 25.0
    day = NS_DAY + 14 * 3600 * NS_S + np.arange(10, dtype=np.int64) * NS_S
    ts = np.concatenate([day, day + NS_DAY])
    er = np.zeros(20)
    er[7] = 3 * hurdle  # entered 3 rows before the close
    # every default rule; 15m is the longest pinned label horizon (the
    # scored-rows block needs a pinned one)
    cfg = BacktestConfig(
        max_pos_qty=100,
        latency_rows=1,
        flatten_at_session_end=True,
    ).for_horizon("15m")
    assert cfg.position_policy == "cost_aware" and cfg.horizon_ns == 900 * NS_S
    bt = Backtester(_cost_model(), META, cfg)
    res = bt.run_instrument(1, _frame(ts, np.full(20, 25.0)), _scores(ts, er))
    assert res.positions[:10].tolist() == [0] * 8 + [100, 0]  # latency 1, flat at close
    # the hold (still inside its horizon) resumes after the gap, as the sign
    # rule's standing target would
    assert res.positions[10] == 100


# ---------------------------------------------------------------------------
# 6. meta-label: NaN imputation is explicit, AUC is never fabricated
# ---------------------------------------------------------------------------


def test_build_meta_features_imputation_is_opt_in_legacy():
    """Default: a missing value stays NaN.  ``impute_nan=True`` is the legacy
    rule (non-finite -> 0), selected by name."""
    ctx = np.ones((3, len(META_CONTEXT_COLUMNS)))
    ctx[1, 3] = np.nan
    ctx[2, 0] = np.inf
    pred = np.array([1e-4, -2e-4, 3e-4])
    direction = np.array([1.0, -1.0, 1.0])
    raw = build_meta_features(pred, direction, ctx)
    assert np.isnan(raw[1, 5]) and np.isnan(raw[2, 2])
    assert int(np.isnan(raw).sum()) == 2  # exactly the two missing entries
    assert np.array_equal(
        raw, build_meta_features(pred, direction, ctx, impute_nan=False), equal_nan=True
    )
    pinned = build_meta_features(pred, direction, ctx, impute_nan=True)
    assert np.isfinite(pinned).all() and pinned[1, 5] == 0.0 and pinned[2, 2] == 0.0
    keep = np.isfinite(raw)
    assert np.array_equal(raw[keep], pinned[keep])


def test_single_class_test_segment_reports_no_auc():
    """AUC on a one-class segment is undefined: None, not a fabricated 0.5."""
    from iap.models.metalabel import run_meta_labeling
    from test_metalabel import _meta_dataset

    ds, pred = _meta_dataset()
    n = len(pred)
    tail = np.arange(n) >= int(0.7 * n)  # covers the whole test segment
    y_mid = np.where(tail, -np.sign(pred) * 1e-4, ds.y_mid)  # every tail trade loses
    losing = dataclasses.replace(ds, y_mid=y_mid, y=y_mid - 5e-5)
    res = run_meta_labeling(losing, pred)
    assert res["base_rate_test"] == 0.0
    assert res["auc_test"] is None
    json.dumps(res, allow_nan=False)
    # the two-class case is untouched
    assert run_meta_labeling(ds, pred)["auc_test"] > 0.6

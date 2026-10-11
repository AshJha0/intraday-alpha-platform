"""v1.13 execution realism (quality review X-P1-2, X-P2-8, X-P2-13, X-P2-15).

All synthetic. Every option is default-off; the defaults are pinned here and
by the execution goldens.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pytest
from iap.backtest.costs import CostModel
from iap.backtest.maker import MakerBacktester, MakerConfig
from iap.core.events import EventType, MarketEvent
from iap.execution import ExecutionSimulator, OrderType
from iap.execution.calibration import ExecCalibration, estimate_calibration
from iap.execution.config import load_venues
from iap.execution.optimal import ac_params_from_calibration
from iap.execution.volume_curve import NY_TZ, estimate_volume_curves
from test_execution_rules import LAT, T0, Feeder, child, make_config, seed_book
from test_maker_economics import INS as MINS
from test_maker_economics import TICK, exec_config, make_scores, synth_events

REPO = Path(__file__).resolve().parents[2]
SEC31_2019_BPS = 0.207  # $20.70 per million (SEC Fee Rate Advisory #2, FY2019)
TAF_2019 = (0.000119, 5.95)  # FINRA TAF per share sold, cap per trade


# ---------------------------------------------------------------- X-P1-2 fees


def _sell_then_buy(cfg):
    sim = ExecutionSimulator(cfg)
    f = Feeder()
    seed_book(sim, f)
    sim.submit(child(1, OrderType.MARKET, 0, 100, T0 + 10))  # sell 100 @ 100
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    sim.submit(child(0, OrderType.MARKET, 0, 100, T0 + 1_000_000))  # buy 100 @ 101
    sim.on_event(f.heartbeat(T0 + 1_000_000 + LAT + 1))
    assert len(sim.fills) == 2
    return sim.fills


def _with_fees(cfg, **kw):
    v = next(iter(cfg.venues.values()))
    return replace(cfg, venues={v.venue_id: replace(v, **kw)})


def test_simulator_regulatory_fees_exact():
    base = _sell_then_buy(make_config())
    fee = _sell_then_buy(
        _with_fees(
            make_config(),
            sell_notional_fee_bps=SEC31_2019_BPS,
            per_share_fee_all=TAF_2019[0],
            per_share_fee_cap=TAF_2019[1],
        )
    )
    sell_notional = 100 * 100 * 0.01  # 100 shares at 100 ticks of 0.01
    assert fee[0].fee == pytest.approx(
        base[0].fee + SEC31_2019_BPS * 1e-4 * sell_notional + TAF_2019[0] * 100, abs=1e-15
    )
    assert fee[1].fee == base[1].fee  # buys pay neither Section 31 nor TAF
    # per-trade cap, and the both-sides switch
    capped = _sell_then_buy(_with_fees(make_config(), per_share_fee_all=1.0, per_share_fee_cap=5.0))
    assert capped[0].fee == pytest.approx(base[0].fee + 5.0)
    both = _sell_then_buy(
        _with_fees(make_config(), per_share_fee_all=0.001, per_share_fee_sell_only=False)
    )
    assert both[1].fee == pytest.approx(base[1].fee + 0.1)


def test_simulator_default_fees_byte_identical():
    a = _sell_then_buy(make_config())
    b = _sell_then_buy(_with_fees(make_config(), sell_notional_fee_bps=0.0, per_share_fee_all=0.0))
    assert [x.to_dict() for x in a] == [x.to_dict() for x in b]
    assert a[0].fee == 0.003 * 100.0


def test_venue_loader_reads_optional_fee_keys(tmp_path):
    doc = json.loads((REPO / "configs/venues/venues.json").read_text())
    plain = load_venues(REPO / "configs/venues/venues.json")
    assert all(
        v.sell_notional_fee_bps == 0.0 and v.per_share_fee_all == 0.0 for v in plain.values()
    )
    doc["venues"][0].update(
        sell_notional_fee_bps=SEC31_2019_BPS,
        per_share_fee_all=TAF_2019[0],
        per_share_fee_cap=TAF_2019[1],
    )
    p = tmp_path / "v.json"
    p.write_text(json.dumps(doc))
    v = load_venues(p)[doc["venues"][0]["venue_id"]]
    assert (v.sell_notional_fee_bps, v.per_share_fee_all, v.per_share_fee_cap) == (
        SEC31_2019_BPS,
        TAF_2019[0],
        TAF_2019[1],
    )
    assert v.per_share_fee_sell_only is True


def test_regulatory_fee_template_documents_sources():
    doc = json.loads((REPO / "configs/templates/regulatory_fees.json").read_text())
    by = {p["label"][:4]: p for p in doc["periods"]}
    assert by["2019"]["sell_notional_fee_bps"] == pytest.approx(
        by["2019"]["sec_section31_usd_per_million"] / 100.0
    )
    hosts = {
        urlparse(u).hostname
        for p in doc["periods"]
        for k in ("sec_source", "finra_source")
        for u in re.findall(r"https://[^\s)]+", p[k])
    }
    assert {"www.sec.gov", "www.finra.org"} <= hosts


def test_cost_model_regulatory_fees_exact_and_default_identical():
    cm = CostModel(1.0, 0.003, 0.0)
    q = np.array([100.0, -100.0, -1_000_000.0])
    mid = np.array([50.0, 50.0, 50.0])
    hs = np.full(3, 0.005)
    base = cm.cost_components(q, mid, hs, "EQUITY", 5e6, 1)
    same = replace(cm, sell_notional_fee_bps=0.0).cost_components(q, mid, hs, "EQUITY", 5e6, 1)
    for k in base:
        np.testing.assert_array_equal(base[k], same[k])
    rf = cm.with_regulatory_fees(SEC31_2019_BPS, *TAF_2019)
    got = rf.cost_components(q, mid, hs, "EQUITY", 5e6, 1)
    extra = got["fee"] - base["fee"]
    assert extra[0] == 0.0  # buy
    assert extra[1] == pytest.approx(SEC31_2019_BPS * 1e-4 * 5000.0 + 100 * TAF_2019[0])
    assert extra[2] == pytest.approx(SEC31_2019_BPS * 1e-4 * 5e7 + TAF_2019[1])  # capped TAF
    np.testing.assert_array_equal(got["spread"], base["spread"])
    rt0 = cm.round_trip_cost_return(mid, hs, "EQUITY")
    rt1 = rf.round_trip_cost_return(mid, hs, "EQUITY")
    np.testing.assert_allclose(
        rt1 - rt0, (SEC31_2019_BPS * 1e-4 * 50.0 + TAF_2019[0]) / 50.0, rtol=1e-12
    )


# ------------------------------------------------------- X-P2-8 passive p_exit


@pytest.fixture(scope="module")
def mevents():
    return synth_events(n_steps=1500)


def _gated(events, cal=None, hazard=None, **kw):
    # margin 5 bp puts the gate where the exit probability decides it
    mc = MakerConfig(exit="passive", exit_timeout_ns=3 * 10**9, margin_bps=5.0, **kw)
    bt = MakerBacktester(exec_config(), mc, cal, fill_hazard=hazard)
    return bt.run_instrument(events, make_scores(events), MINS).summary()


def test_calibration_ttl_cell_is_opt_in_and_censored(mevents):
    plain = estimate_calibration(mevents, {MINS: TICK})
    assert "p_fill_within" not in plain["fill_rates"]["all"]
    doc = estimate_calibration(mevents, {MINS: TICK}, fill_within_ns=(10**9, 3 * 10**9))
    cells = doc["fill_rates"]["all"]["p_fill_within"]
    p1, p3 = cells[str(10**9)]["p"], cells[str(3 * 10**9)]["p"]
    assert 0.0 <= p1 <= p3 <= 1.0
    ExecCalibration.from_dict(doc)  # still a valid calibration document


def test_passive_exit_probability_precedence(mevents):
    doc = estimate_calibration(mevents, {MINS: TICK})
    hi, lo = copy.deepcopy(doc), copy.deepcopy(doc)
    hi["fill_rates"]["all"]["p_fill_within"] = {str(3 * 10**9): {"n": 99, "p": 1.0}}
    lo["fill_rates"]["all"]["p_fill_within"] = {str(3 * 10**9): {"n": 99, "p": 0.0}}
    s_hi = _gated(mevents, ExecCalibration.from_dict(hi))
    s_lo = _gated(mevents, ExecCalibration.from_dict(lo))
    assert s_hi["gated_out"] < s_lo["gated_out"]  # the TTL cell is what the gate used
    # the explicit probability still wins over the cell
    s_fix = _gated(mevents, ExecCalibration.from_dict(hi), passive_exit_fill_prob=0.0)
    assert s_fix["gated_out"] == s_lo["gated_out"]

    class Const:
        def __init__(self, p):
            self.p, self.calls = p, []

        def p_fill_within(self, x, t_ns):
            self.calls.append(t_ns)
            return self.p

    hz = Const(1.0)
    s_hz = _gated(mevents, ExecCalibration.from_dict(lo), hazard=hz)
    assert s_hz["gated_out"] == s_hi["gated_out"]  # the hazard beats the cell
    assert set(hz.calls) == {3 * 10**9}


# ------------------------------------------------------- X-P2-13 DST sessions


def _trade(ts_ns: int, qty: int = 100) -> MarketEvent:
    return MarketEvent(
        event_id=1,
        instrument_id=1,
        venue_id=1,
        exchange_ts=ts_ns,
        receive_ts=ts_ns,
        sequence=1,
        event_type=int(EventType.TRADE),
        side=0,
        price_ticks=100,
        qty=qty,
        order_id=0,
        trade_id=1,
    )


def _utc_ns(y, m, d, hh, mm) -> int:
    import datetime as dt

    return int(dt.datetime(y, m, d, hh, mm, tzinfo=dt.UTC).timestamp()) * 10**9


def test_volume_curve_bins_follow_new_york_dst():
    # 09:35 New York: 13:35 UTC in July (EDT), 14:35 UTC in January (EST)
    summer = _utc_ns(2019, 7, 1, 13, 35)
    winter = _utc_ns(2019, 1, 2, 14, 35)
    ny = estimate_volume_curves(
        [_trade(summer), _trade(winter)], n_bins=13, shrinkage=0.0, tz=NY_TZ
    )
    curve = ny["curves"]["1"]["curve"] if "1" in ny["curves"] else ny["curves"][1]["curve"]
    assert curve[0] == pytest.approx(1.0)  # both days' volume in the first bin
    assert ny["tz"] == NY_TZ
    legacy = estimate_volume_curves([_trade(summer), _trade(winter)], n_bins=13, shrinkage=0.0)
    lc = legacy["curves"]["1"]["curve"] if "1" in legacy["curves"] else legacy["curves"][1]["curve"]
    assert lc[0] == pytest.approx(0.5)  # UTC-fixed: January lands an hour late
    assert "tz" not in legacy


# ------------------------------------------------------- X-P2-15 A-C units


def test_ac_params_explicit_units_and_refuses_ambiguity():
    legacy = ac_params_from_calibration(sigma=0.4, adv=2e6, price=50.0, horizon=1 / 13)
    explicit = ac_params_from_calibration(sigma=0.4, price=50.0, adv_per_day=2e6, horizon_s=1800.0)
    assert explicit.eta == legacy.eta
    assert math.isclose(explicit.horizon, 1800.0 / 23_400.0)
    for bad in (
        {"adv": 2e6, "adv_per_day": 2e6, "horizon_s": 60.0},
        {"adv": 2e6, "horizon_s": 60.0},
        {"adv_per_day": 2e6},
        {"adv_per_day": 2e6, "horizon_s": 60.0, "horizon": 1.0},
        {},
    ):
        with pytest.raises(ValueError):
            ac_params_from_calibration(sigma=0.4, price=50.0, **bad)

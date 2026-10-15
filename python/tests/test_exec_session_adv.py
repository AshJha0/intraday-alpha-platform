"""Per-session ADV in the execution simulator (v1.17 A5, review X-9).

Without a table every rule is byte-identical (``test_execution_golden``
and ``test_execution_rules`` run unchanged); with one, the impact adders
(rules 6 / 6b) and the X-3 support check divide by the ADV of the session
the FILL falls in.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest
from conftest import CONFIGS_DIR
from iap.execution import ExecutionSimulator, OrderType
from iap.execution.config import load_exec_config
from iap.reference.session_adv import SessionAdvTable
from test_execution_rules import INS, LAT, T0, Feeder, child, make_config, seed_book

NS = 1_000_000_000
# T0 = 2023-11-14 22:13:20 UTC = 17:13 New York
DAY = "2023-11-14"
NEXT = "2023-11-15"


def _taker_fill(cfg):
    sim = ExecutionSimulator(cfg)
    f = Feeder()
    seed_book(sim, f)
    sim.submit(child(1, OrderType.MARKET, 0, 100, T0 + 10))
    sim.on_event(f.heartbeat(T0 + 10 + LAT + 1))
    assert len(sim.fills) == 1
    return sim, sim.fills[0]


def test_static_default_is_byte_identical_and_session_adv_rescales_impact():
    _, base = _taker_fill(make_config())
    assert math.isclose(base.impact_cost, 2e-4, abs_tol=1e-15)  # the pinned rule-6 number
    table = SessionAdvTable({INS: {DAY: 250_000.0, NEXT: 4_000_000.0}})
    _, scaled = _taker_fill(replace(make_config(), adv_by_session=table))
    # 1e6 / 250e3 = 4x the participation => 4x the impact, same fill otherwise
    assert math.isclose(scaled.impact_cost, 4 * base.impact_cost, rel_tol=1e-12)
    assert scaled.price_ticks == base.price_ticks and scaled.qty == base.qty
    assert scaled.fee == base.fee
    # an instrument the table lacks keeps its static ADV
    other = SessionAdvTable({INS + 1: {DAY: 1.0}})
    _, same = _taker_fill(replace(make_config(), adv_by_session=other))
    assert same.impact_cost == base.impact_cost


def test_session_adv_follows_the_fill_session():
    cfg = make_config()
    assert cfg.session_adv(INS, T0) == 1_000_000.0
    table = SessionAdvTable({INS: {DAY: 250_000.0, NEXT: 4_000_000.0}})
    cfg = replace(cfg, adv_by_session=table)
    assert cfg.session_adv(INS, T0) == 250_000.0
    assert cfg.session_adv(INS, T0 + 86_400 * NS) == 4_000_000.0
    assert cfg.session_adv(INS, T0 + 30 * 86_400 * NS) == 1_000_000.0  # unknown session


def test_parent_mode_and_support_check_use_the_session_adv():
    table = SessionAdvTable({INS: {DAY: 100_000.0}})
    cfg = replace(
        make_config(),
        impact_model="parent",
        impact_fit_support_pct_adv=(0.0, 0.05),  # 100 shares of 100k = 0.1 % > 0.05 %
        adv_by_session=table,
    )
    sim, fill = _taker_fill(cfg)
    # rule 6b, exponent 1: coeff * (q * pct)**1 * q with pct = 1/100k*100 = 1e-3 % per share
    # (bps-shares, times 1e-4 * the unit price of 100 ticks * 0.01)
    expected = 2.0 * (100 * 1e-3) * 100 * 1e-4 * (100 * 0.01)
    assert math.isclose(fill.impact_cost, expected, rel_tol=1e-12)
    assert sim.impact_beyond_support == {(INS, 99)}  # the test child's parent_id
    cfg_static = replace(cfg, adv_by_session=None)  # 100 of 1e6 = 0.01 % <= 0.05 %
    sim2, _ = _taker_fill(cfg_static)
    assert sim2.impact_beyond_support == set()


def test_load_exec_config_adv_table_option(tmp_path):
    cfg = load_exec_config(CONFIGS_DIR)
    assert cfg.adv_by_session is None
    # the repository instruments.json has no adv_by_session: a static table
    cfg_inst = load_exec_config(CONFIGS_DIR, adv_table=True)
    assert cfg_inst.adv_by_session is not None and cfg_inst.adv_by_session.is_static
    iid = sorted(cfg.instruments)[0]
    assert cfg_inst.session_adv(iid, T0) == cfg.instruments[iid].adv
    path = tmp_path / "adv.json"
    path.write_text(
        json.dumps({"adv_by_session": {str(iid): {DAY: 123.0}}, "timezone": "UTC"}),
        encoding="utf-8",
    )
    cfg_path = load_exec_config(CONFIGS_DIR, adv_table=path)
    assert cfg_path.session_adv(iid, T0) == 123.0
    with pytest.raises(ValueError):
        load_exec_config(CONFIGS_DIR, adv_table=tmp_path / "missing.json")

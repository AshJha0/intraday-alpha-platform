"""v1.15 X-P1-1: dated SEC Section 31 + FINRA TAF schedule.

Golden: tests/golden/expected_regulatory_fees.json (also replayed by the
C++ test_regulatory_fees and the Rust venue/tests/regulatory_fees.rs).
Off in code defaults; on only through configs/prereg/v2.json.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from iap.execution import ExecutionSimulator, OrderType
from iap.execution.config import load_exec_config
from iap.execution.regulatory import (
    RegulatoryFeeSchedule,
    load_default_schedule,
    prereg_wants_regulatory_fees,
)
from test_execution_rules import LAT, T0, Feeder, child, make_config, seed_book

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "expected_regulatory_fees.json"
SCHEDULE = REPO / "configs" / "reference" / "regulatory_fee_schedule.json"


def test_golden_cases():
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    sched = RegulatoryFeeSchedule.from_dict(g["schedule"])
    for c in g["cases"]:
        qty = float(c["qty"])
        notional = qty * c["qty_unit"] * float(c["price_ticks"]) * c["tick_size"]
        if c.get("error"):
            with pytest.raises(ValueError):
                sched.fee(c["side"], qty, notional, c["ts"])
        else:
            got = sched.fee(c["side"], qty, notional, c["ts"])
            assert got == pytest.approx(c["fee"], abs=g["tolerance"]), c["label"]


def test_golden_embeds_the_shipped_schedule():
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert g["schedule"] == json.loads(SCHEDULE.read_text(encoding="utf-8"))


def test_schedule_dates_match_ns_and_cite_sources():
    import datetime as dt

    root = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    for table in ("sec_section31", "finra_taf"):
        for row in root[table]:
            for iso, ns in (("from", "from_ns"), ("until", "until_ns")):
                if iso in row:
                    d = dt.datetime.fromisoformat(row[iso]).replace(tzinfo=dt.UTC)
                    assert int(d.timestamp()) * 10**9 == row[ns]
                else:
                    assert ns not in row
            assert "sec.gov" in row["source"] or "finra" in row["source"].lower()


def test_known_rates():
    s = load_default_schedule(REPO / "configs")
    jun26 = 1781222400000000000
    assert s.sec_rate(jun26).usd_per_million == 20.60
    assert s.sec_rate(jun26 - 100 * 86_400 * 10**9).usd_per_million == 0.0  # FY2025 zero rate
    assert (s.taf_rate(jun26).per_share, s.taf_rate(jun26).max_per_trade) == (0.000195, 9.79)
    assert s.fee(0, 100.0, 1e5, jun26) == 0.0  # buys never pay


def test_bad_tables_rejected():
    root = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    overlap = json.loads(json.dumps(root))
    overlap["sec_section31"][1]["from_ns"] = overlap["sec_section31"][0]["from_ns"]
    with pytest.raises(ValueError):
        RegulatoryFeeSchedule.from_dict(overlap)
    neg = json.loads(json.dumps(root))
    neg["finra_taf"][0]["per_share"] = -1.0
    with pytest.raises(ValueError):
        RegulatoryFeeSchedule.from_dict(neg)


def _flat_schedule() -> RegulatoryFeeSchedule:
    return RegulatoryFeeSchedule.from_dict(
        {
            "sec_section31": [{"from_ns": 0, "usd_per_million": 20.60}],
            "finra_taf": [{"from_ns": 0, "per_share": 0.000195, "max_per_trade": 9.79}],
        }
    )


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


def test_simulator_default_off_and_sell_only():
    cfg = make_config()
    assert cfg.regulatory_fees is None
    off = _sell_then_buy(cfg)
    on = _sell_then_buy(replace(cfg, regulatory_fees=_flat_schedule()))
    ins = next(iter(cfg.instruments.values()))
    notional = 100.0 * ins.qty_unit * float(on[0].price_ticks) * ins.tick_size
    want = 20.60 * notional / 1e6 + min(0.000195 * 100.0, 9.79)
    assert on[0].fee - off[0].fee == pytest.approx(want, abs=1e-12)
    assert on[1].fee == off[1].fee  # the buy pays nothing


def test_load_exec_config_default_off_and_prereg_v2_on():
    configs = REPO / "configs"
    assert load_exec_config(configs).regulatory_fees is None
    prereg = json.loads((configs / "prereg" / "v2.json").read_text(encoding="utf-8"))
    assert prereg_wants_regulatory_fees(prereg) is True
    assert prereg_wants_regulatory_fees({}) is False
    cfg = load_exec_config(configs, regulatory_fees=prereg_wants_regulatory_fees(prereg))
    assert cfg.regulatory_fees == load_default_schedule(configs)


def test_research_cost_model_dated_fees():
    from iap.backtest.costs import CostModel

    base = CostModel.load(REPO / "configs" / "execution" / "execution.json")
    s = load_default_schedule(REPO / "configs")
    cm = base.with_dated_regulatory_fees(s, 1781222400000000000)
    assert cm.sell_notional_fee_bps == pytest.approx(0.206)
    assert (cm.per_share_fee_all, cm.per_share_fee_cap) == (0.000195, 9.79)
    assert base.sell_notional_fee_bps == 0.0  # default off

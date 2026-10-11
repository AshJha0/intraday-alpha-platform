"""v1.15 R-P0-2: configs/prereg/v2.json turns the v1.14 execution-realism
opt-ins on; the code defaults (v1) stay off, so v1 numbers and goldens do
not move."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from iap.backtest.maker import MakerConfig
from iap.backtest.quoting import QuotingConfig
from iap.execution import ExecutionSimulator
from iap.execution.calibration import estimate_calibration
from iap.execution.config import load_exec_config
from iap.execution.realism import RealismOptIns, load_prereg

REPO = Path(__file__).resolve().parents[2]
V2 = REPO / "configs" / "prereg" / "v2.json"


def _default(fn, name):
    return inspect.signature(fn).parameters[name].default


def test_v2_turns_each_opt_in_on():
    r = load_prereg(V2)
    assert r.regulatory_fees is True
    assert r.feed_latency_ns > 0
    assert r.exit_route == "sim"
    assert r.flatten_route == "sim"
    assert r.hidden_print_fills is True
    assert r.amend_size_down is True
    assert r.markout_unit == "touch_order"
    assert r.modify_queue_credit is True

    m = r.maker_config(MakerConfig())
    assert (m.feed_latency_ns, m.exit_route, m.hidden_print_fills) == (
        r.feed_latency_ns,
        "sim",
        True,
    )
    q = r.quoting_config(QuotingConfig())
    assert (q.feed_latency_ns, q.flatten_route, q.amend_size_down, q.hidden_print_fills) == (
        r.feed_latency_ns,
        "sim",
        True,
        True,
    )
    assert r.simulator_kwargs() == {"hidden_prints": True}
    assert r.calibration_kwargs() == {"markout_unit": "touch_order"}
    assert r.exec_config_kwargs() == {"regulatory_fees": True}


def test_every_v1_default_is_off_and_matches_the_code():
    v1 = RealismOptIns.from_prereg({})
    assert v1 == RealismOptIns()
    # applying the v1 options changes nothing: the v1 run is the default run
    assert v1.maker_config(MakerConfig()) == MakerConfig()
    assert v1.quoting_config(QuotingConfig()) == QuotingConfig()
    assert v1.simulator_kwargs()["hidden_prints"] == _default(
        ExecutionSimulator.__init__, "hidden_prints"
    )
    assert v1.calibration_kwargs()["markout_unit"] == _default(estimate_calibration, "markout_unit")
    assert v1.exec_config_kwargs()["regulatory_fees"] == _default(
        load_exec_config, "regulatory_fees"
    )
    # and the v2 file differs from v1 in every switch
    v2 = load_prereg(V2)
    for name in (
        "regulatory_fees",
        "feed_latency_ns",
        "exit_route",
        "flatten_route",
        "hidden_print_fills",
        "amend_size_down",
        "markout_unit",
    ):
        assert getattr(v2, name) != getattr(v1, name), name


@pytest.mark.parametrize(
    "block, needle",
    [
        ({"feed_latency_ns": -1}, "feed_latency_ns"),
        ({"feed_latency_ns": True}, "feed_latency_ns"),
        ({"exit_route": "venue"}, "exit_route"),
        ({"markout_unit": "trade"}, "markout_unit"),
        ({"hidden_print_fills": 1}, "hidden_print_fills"),
        ({"modify_queue_credit": False}, "modify_queue_credit"),
        ({"typo_flag": True}, "unknown keys"),
    ],
)
def test_bad_prereg_is_refused(block, needle):
    with pytest.raises(ValueError, match=needle):
        RealismOptIns.from_prereg({"execution_realism": block})


def test_v2_file_is_the_documented_shape():
    doc = json.loads(V2.read_text(encoding="utf-8"))
    assert doc["methods"] == "v2"
    assert set(doc["execution_realism"]) - {"description"} == {
        "modify_queue_credit",
        "feed_latency_ns",
        "exit_route",
        "flatten_route",
        "hidden_print_fills",
        "markout_unit",
        "amend_size_down",
    }

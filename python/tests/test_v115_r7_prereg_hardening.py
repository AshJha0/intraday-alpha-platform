"""v1.15 R7 pre-registration hardening: cost-sensitivity hook (default
byte-identical, regulatory fees unscaled), the rerunnable power analysis,
and the DRAFT's fingerprints / cost-sensitivity / power sections."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from iap.backtest.costs import COST_SENSITIVITY_SCALES, CostModel
from iap.research import r7_power

REPO = Path(__file__).resolve().parents[2]
DRAFT = REPO / "research" / "r7" / "r7_confirm_prereg_DRAFT.json"

QTY = np.array([100.0, -250.0, 1000.0, -3.0])
MID = np.array([150.0, 151.0, 149.5, 150.25])
HS = np.array([0.005, 0.01, 0.005, 0.02])


def _cm(**kw):
    return CostModel(2.0, 0.003, 2.5, **kw).with_regulatory_fees(0.278, 0.000166, 8.3)


def test_default_scale_is_byte_identical():
    a, b = _cm(), _cm().with_cost_sensitivity(1.0)
    for ac in ("EQUITY", "FX"):
        ca = a.cost_components(QTY, MID, HS, ac, 5e6, 1000)
        cb = b.cost_components(QTY, MID, HS, ac, 5e6, 1000)
        for k in ca:
            assert ca[k].tobytes() == cb[k].tobytes()
        ra = a.round_trip_cost_return(MID, HS, ac)
        assert ra.tobytes() == b.round_trip_cost_return(MID, HS, ac).tobytes()
    assert a.breakeven_size(1e-3, 150.0, 0.005, "EQUITY", 5e6, 1) == b.breakeven_size(
        1e-3, 150.0, 0.005, "EQUITY", 5e6, 1
    )


@pytest.mark.parametrize("k", [0.5, 1.5])
def test_scale_moves_explicit_and_implicit_not_regulatory(k):
    base, sc = _cm(), _cm().with_cost_sensitivity(k)
    reg = base._regulatory_fee(QTY, MID)
    assert reg.sum() > 0
    c0 = base.cost_components(QTY, MID, HS, "EQUITY", 5e6, 1)
    c1 = sc.cost_components(QTY, MID, HS, "EQUITY", 5e6, 1)
    np.testing.assert_allclose(c1["spread"], k * c0["spread"])
    np.testing.assert_allclose(c1["impact"], k * c0["impact"])
    np.testing.assert_allclose(c1["fee"], k * (c0["fee"] - reg) + reg)
    fx0 = base.cost_components(QTY, MID, HS, "FX", 5e6, 1000)
    fx1 = sc.cost_components(QTY, MID, HS, "FX", 5e6, 1000)
    np.testing.assert_allclose(fx1["fee"], k * fx0["fee"])
    plain = CostModel(2.0, 0.003, 2.5)
    rt0 = plain.round_trip_cost_return(MID, HS, "EQUITY")
    np.testing.assert_allclose(
        plain.with_cost_sensitivity(k).round_trip_cost_return(MID, HS, "EQUITY"), k * rt0
    )


def test_scale_validation_and_grid():
    assert COST_SENSITIVITY_SCALES == (0.5, 1.0, 1.5)
    for bad in (-0.1, math.nan, math.inf):
        with pytest.raises(ValueError):
            CostModel(2.0, 0.003, 2.5, sensitivity_scale=bad)


def test_power_primitives():
    # more sessions -> more power, smaller MDE
    assert r7_power.hac_power(0.02, 0.02, 0.05, 47, 4.4) > r7_power.hac_power(
        0.02, 0.02, 0.05, 7, 4.4
    )
    assert r7_power.hac_mde(0.02, 0.05, 47, 4.4) < r7_power.hac_mde(0.02, 0.05, 7, 4.4)
    # power at the MDE is the target
    m = r7_power.hac_mde(0.02, 0.05, 7, 4.4)
    assert r7_power.hac_power(m, 0.02, 0.05, 7, 4.4) == pytest.approx(0.8)
    sr = r7_power.dsr_mde_sr(47, 294)
    assert r7_power.dsr_power(sr, 47, 294) == pytest.approx(0.8, abs=1e-6)
    assert r7_power.dsr_power(0.0, 7, 294) < 0.01


def test_committed_report_is_reproducible(tmp_path):
    md, js = tmp_path / "p.md", tmp_path / "p.json"
    subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "r7_power.py"),
            "--out-md",
            str(md),
            "--out-json",
            str(js),
        ],
        check=True,
        capture_output=True,
    )
    committed = (REPO / "docs" / "research" / "R7_POWER.md").read_text(encoding="utf-8")
    assert md.read_text(encoding="utf-8") == committed
    res = json.loads(js.read_text(encoding="utf-8"))
    assert res["hypotheses"]["H1"]["placeholder"] is True
    assert set(res["hypotheses"]["H4"]) == {"EQ01", "EQ02", "EQ04", "EQ05", "EQ07", "EQ10"}


def test_h1_rerun_with_estimate():
    inputs = json.loads((REPO / "research/r7/r7_power_inputs.json").read_text(encoding="utf-8"))
    gate = r7_power.ledger_gate(REPO / "research/experiments.json")
    res = r7_power.analyse(inputs, gate, {"mean_edge_bps": 1.0, "session_sd_bps": 1.0})
    h1 = res["hypotheses"]["H1"]
    assert h1["placeholder"] is False and "cells" in h1


def test_draft_fingerprints_cost_sensitivity_and_power():
    d = json.loads(DRAFT.read_text(encoding="utf-8"))
    assert d["status"].startswith("DRAFT") and d["signed"] is False
    assert d["signature"] is None and d["on_blackboard"] is False
    cand = json.loads((REPO / "research/r7/r7_candidates.json").read_text(encoding="utf-8"))
    holdout = {x["date"]: x for x in cand["days"] if x["role"] == "holdout"}
    files = d["holdout_files"]["files"]
    assert [f["date"] for f in files] == d["holdout_days"] == list(holdout)
    for f in files:
        assert f["file"] == holdout[f["date"]]["file"]
        assert f["bytes"] == holdout[f["date"]]["bytes"]
        assert len(f["sha256"]) == 64 and int(f["sha256"], 16) >= 0
    for path, digest in d["fingerprints"].items():
        if path == "algorithm":
            continue
        assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == digest, path
    cs = d["cost_sensitivity"]
    assert tuple(cs["scales"]) == COST_SENSITIVITY_SCALES and cs["primary_scale"] == 1.0
    assert "never decides" in cs["role"] and "no ledger look" in cs["role"]
    assert (REPO / d["power"]["report"]).is_file()
    # hypotheses unchanged by the hardening
    assert set(d["hypotheses"]) == {"H1", "H4"}
    assert d["hypotheses"]["H4"]["alphas"] == ["EQ10@1s", "EQ05@1s"]
    assert set(d["untested_insufficient_free_data"]["alphas"]) == {"EQ01@1s", "EQ02@5s", "EQ04@5s"}
    assert {"H2", "H3", "EQ07"} <= set(d["dropped"])
    assert not any("deflated-Sharpe gate reads for H3" in x for x in d["open_for_owner"])

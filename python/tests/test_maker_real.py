"""The in-sample real-data maker study driver (iap.research.maker_real): pure helpers."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from iap.research import maker_real as mr

ROOT = Path(__file__).resolve().parents[2]


def test_alpha_side_target_picks_the_side_the_alpha_points_to():
    z = np.array([1.0, -2.0, 0.0, np.nan])
    bid = np.array([1.0, 1.0, 1.0, 1.0])
    ask = np.array([0.0, 0.0, 0.0, 0.0])
    out = mr.alpha_side_target(z, bid, ask)
    assert out[0] == 1.0 and out[1] == 0.0
    assert math.isnan(out[2]) and math.isnan(out[3])


def test_cluster_stats_t_interval_and_bonferroni_is_wider():
    st = mr.cluster_stats([1.0, 2.0, 3.0, None], n_tests=8)
    assert st["n_sessions"] == 3 and st["mean"] == pytest.approx(2.0)
    assert st["se"] == pytest.approx(1.0 / math.sqrt(3))
    lo95, hi95 = st["ci95"]
    lob, hib = st["ci_bonf"]
    assert lob < lo95 < 2.0 < hi95 < hib
    assert mr.cluster_stats([1.0])["ci95"] is None


def test_verdict_needs_positive_bonferroni_ci_and_enough_sessions():
    good = {"n_sessions": 5, "mean": 1.0, "ci_bonf": [0.1, 1.9]}
    assert mr.verdict(good, 4).startswith("EXISTS")
    assert mr.verdict({**good, "n_sessions": 3}, 4).startswith("NO")
    assert mr.verdict({**good, "ci_bonf": [-0.1, 2.0]}, 4).startswith("NO")
    assert mr.verdict({"n_sessions": 6, "mean": None, "ci_bonf": None}, 4).startswith("NO")


def test_summarise_pools_symbols_trip_weighted_within_a_session(tmp_path):
    prereg = {"cells": [{"alpha": "EQ01", "horizon": "1s"}], "exit_modes": ["taker"]}
    units = tmp_path / "units"
    units.mkdir()

    def unit(session, symbol, n, s):
        r = {"n_trips": n, "sum_net_bps": s, "total_net": s, "posted": n}
        doc = {
            "session": session,
            "symbol": symbol,
            "warmup_only": False,
            "results": {"EQ01": {"taker/gated": r}},
        }
        (units / f"{session}_{symbol}.json").write_text(json.dumps(doc), encoding="utf-8")

    unit("d1", "AAPL", 1, 3.0)
    unit("d1", "MSFT", 3, 1.0)
    unit("d2", "AAPL", 2, 2.0)
    out = mr.summarise(units, prereg)
    row = out["cells"]["EQ01@1s taker/gated"]
    assert row["session_mean_net_bps"] == {"d1": pytest.approx(1.0), "d2": pytest.approx(1.0)}
    assert row["n_sessions"] == 2
    assert row["verdict"].startswith("NO")  # too few sessions


def test_committed_prereg_is_the_registered_one():
    path = ROOT / "research" / "maker_real" / "prereg.json"
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    prereg = json.loads(path.read_text(encoding="utf-8"))
    entries = mr.check_prereg(ROOT, prereg)
    assert len(entries) == 4
    assert all(sha in e["body"]["hypothesis"] for e in entries)
    assert prereg["latency_ASSUMED"]["note"].startswith("ASSUMED")

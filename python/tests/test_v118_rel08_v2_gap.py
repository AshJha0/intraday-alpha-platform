"""REL-08 rule v2: the within-session max-gap definition (synthetic arrays only)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "rel08_v2_dryrun", REPO / "research" / "v118" / "rel08" / "dryrun" / "rel08_v2_dryrun.py"
)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)

NS_MIN = dr.NS_MIN
NS_DAY = dr.NS_DAY


def _grid(days=2, step=15, n=26):
    return np.concatenate([d * NS_DAY + (570 + step * np.arange(n)) * NS_MIN for d in range(days)])


def test_leading_gap_and_overnight_excluded():
    g = _grid()
    u = np.ones(len(g), bool)
    u[:20] = False  # first usable rebalance late in day 0: the leading gap is not counted
    u[26 : 26 + 3] = False  # day 1 starts late: not counted either
    assert (
        dr.within_session_max_gap_min(g, u) == 15.0 * 1
    )  # consecutive usable points are 15 min apart


def test_mid_session_hole_counted():
    g = _grid(days=1)
    u = np.ones(len(g), bool)
    u[5:9] = False  # a 75-minute hole between usable rebalances 4 and 9
    assert dr.within_session_max_gap_min(g, u) == 75.0


def test_single_usable_per_day_and_none():
    g = _grid()
    u = np.zeros(len(g), bool)
    u[3] = True
    u[30] = True
    assert dr.within_session_max_gap_min(g, u) == 0.0
    assert dr.within_session_max_gap_min(g, np.zeros(len(g), bool)) == float("inf")

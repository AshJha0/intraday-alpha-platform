"""v1.16 item 15: Holm / Benjamini-Hochberg / Romano-Wolf (report-only)."""

from __future__ import annotations

import math

import numpy as np
import pytest
from iap.validation.multiple_testing import (
    benjamini_hochberg,
    fold_bootstrap_t,
    holm,
    p_two_sided,
    romano_wolf,
)

P = [0.01, 0.04, 0.03, 0.005]


def test_holm_hand_calc():
    # sorted 0.005, 0.01, 0.03, 0.04 -> 4x, 3x, 2x, 1x, running max
    assert holm(P) == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_bh_hand_calc():
    # sorted p * m / rank: 0.02, 0.02, 0.04, 0.04, running min from the top
    assert benjamini_hochberg(P) == pytest.approx([0.02, 0.04, 0.04, 0.02])


def test_none_and_nonfinite_are_excluded():
    assert holm([0.01, None, math.nan]) == [0.01, None, None]
    assert benjamini_hochberg([None, 0.2]) == [None, 0.2]


def test_p_two_sided():
    assert p_two_sided(1.959963984540054) == pytest.approx(0.05)
    assert p_two_sided(None) is None


def test_romano_wolf_properties():
    rng = np.random.default_rng(0)
    boot = rng.standard_normal((4000, 3))
    t = [3.0, 1.0, 2.0]
    rw = romano_wolf(t, boot)
    raw = [float(np.mean(np.abs(boot[:, j]) >= abs(t[j]))) for j in range(3)]
    assert all(r >= p - 1e-12 for r, p in zip(rw, raw, strict=True))  # never below raw
    assert rw[0] <= rw[2] <= rw[1]  # monotone in |t|
    # one hypothesis: RW = its bootstrap p
    assert romano_wolf([2.0], boot[:, :1]) == pytest.approx(
        [float(np.mean(np.abs(boot[:, 0]) >= 2.0))]
    )
    # perfectly dependent hypotheses: no multiplicity penalty
    same = np.repeat(boot[:, :1], 3, axis=1)
    assert romano_wolf([2.0, 2.0, 2.0], same) == pytest.approx(
        [float(np.mean(np.abs(boot[:, 0]) >= 2.0))] * 3
    )
    with pytest.raises(ValueError):
        romano_wolf([1.0, 2.0], boot)


def test_fold_bootstrap_is_deterministic_and_centred():
    x = np.array([[0.02, 0.01], [0.03, -0.01], [0.01, 0.0], [0.02, 0.02], [0.025, -0.005]])
    t1, b1 = fold_bootstrap_t(x, n_boot=500)
    t2, b2 = fold_bootstrap_t(x, n_boot=500)
    assert t1 == t2 and np.array_equal(b1, b2, equal_nan=True)
    assert t1[0] > 3 and abs(t1[1]) < 1
    assert abs(float(np.nanmedian(b1[:, 0]))) < 1.0  # centred at the null
    rw = romano_wolf(t1, b1)
    assert rw[0] < rw[1]


def test_report_family_on_the_committed_alpha_reports():
    import json
    from pathlib import Path

    from iap.validation.multiple_testing import report_family

    d = Path(__file__).resolve().parents[2] / "research" / "alpha_reports"
    reports = {}
    for f in sorted(d.glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        reports[r["alpha_id"]] = r
    rows = {r["alpha_id"]: r for r in report_family(reports)}
    assert len(rows) == len(reports) == 24
    for r in rows.values():
        if r["p"] is not None:
            assert r["p"] <= r["bh"] <= r["holm"] <= r["bonferroni"] + 1e-12
    assert rows == {r["alpha_id"]: r for r in report_family(reports)}  # deterministic

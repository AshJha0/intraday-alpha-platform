"""v2.1 Track 2 showcase (``iap.execution.showcase``): smoke and invariant tests.

Synthetic demonstration only: the tests pin determinism and the accounting
invariants, not the numbers' meaning.
"""

from __future__ import annotations

import pytest
from iap.execution import showcase as sc


@pytest.fixture(scope="module")
def tape():
    return sc.synthetic_tape()


def test_tape_is_deterministic(tape):
    again = sc.synthetic_tape()
    assert again.signed_volume == tape.signed_volume
    assert again.mids == tape.mids
    assert len(again.events) == len(tape.events)


def test_algo_tca_table(tape):
    rows = sc.run_algos(tape)
    assert [r.label.split()[0] for r in rows] == ["TWAP", "VWAP", "IS", "IS"]
    for r in rows:
        assert 0 < r.filled <= r.qty == sc.ALGO_PARENT_QTY
        assert 0.0 < r.fill_rate <= 1.0
        assert 0.0 < r.participation < 1.0
        assert r.impact_bps >= 0.0
    # IS children are MARKET orders: they pay taker fees; TWAP/VWAP join the touch
    assert rows[2].fees_bps > 0.0 > rows[0].fees_bps
    assert rows == sc.run_algos(tape)  # bit-identical rerun


def test_sor_beats_or_matches_naive_on_completion(tape):
    rows = {r.label: r for r in sc.run_sor(tape)}
    naive = rows["naive: all on XV1"]
    sweep = rows["cost-aware sweep (both venues' depth)"]
    pinned = rows["pinned SOR (best price, fee tie-break)"]
    assert naive.share_xv2 == 0.0
    assert 0.0 < pinned.share_xv2 < 1.0
    assert sweep.filled == sweep.target  # the sweep uses both venues' depth
    assert pinned.filled < pinned.target  # one thin venue per child leaves qty unfilled
    for r in rows.values():
        assert 0 < r.filled <= r.target
        assert r.cost_bps >= r.slippage_bps + r.fees_bps - 1e-12  # impact adder >= 0


def test_impact_fit_recovers_the_planted_coefficient(tape):
    fit = sc.fit_impact(tape)
    assert fit.n == sc.SESSION_S - 1
    assert abs(fit.lambda_hat - sc.LAMBDA_TRUE) < 3 * fit.se
    assert fit.se > 0 and 0.0 < fit.r2 < 1.0


def test_cli_prints_all_three_sections(capsys):
    assert sc.main(["all"]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "| TWAP |" in out and "lambda_hat" in out

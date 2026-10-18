"""Claim taxonomy (v1.18 review finding 7): IC, one session or a ranking never classify
above exploratory_signal; promotion needs confirmed_untouched_oos."""

from __future__ import annotations

import itertools

import pytest
from iap.lifecycle.bootstrap import report_claim_status
from iap.research import claims
from iap.research.claims import ClaimFacts, ClaimStatus, classify

WEAK = {ClaimStatus.EXPLORATORY_SIGNAL, ClaimStatus.INCONCLUSIVE}


def test_seven_distinct_statuses():
    assert len({s.value for s in ClaimStatus}) == 7
    assert len(claims.LADDER) == 5
    assert ClaimStatus.INCONCLUSIVE not in claims.LADDER
    assert ClaimStatus.REJECTED not in claims.LADDER


def test_positive_ic_alone_is_exploratory():
    assert classify(ClaimFacts(positive_ic=True)) is ClaimStatus.EXPLORATORY_SIGNAL
    assert classify(ClaimFacts(positive_ic=True, sessions=50)) is ClaimStatus.EXPLORATORY_SIGNAL


def test_ranking_is_ignored():
    assert classify(ClaimFacts(ranked_top=True)) is ClaimStatus.INCONCLUSIVE
    assert classify(ClaimFacts(ranked_top=True, positive_ic=True)) in WEAK


@pytest.mark.parametrize("bits", list(itertools.product([False, True], repeat=7)))
def test_one_session_never_above_exploratory(bits):
    """Exhaustive over every other fact: a one-session result is weak (or rejected)."""
    fv, ic, rank, reg, net, oos, exe = bits
    f = ClaimFacts(
        feature_verified=fv,
        positive_ic=ic,
        sessions=1,
        ranked_top=rank,
        registered=reg,
        net_screen_passed=net,
        untouched_oos_confirmed=oos,
        execution_capacity_validated=exe,
    )
    status = classify(f)
    assert status in WEAK | {ClaimStatus.FEATURE_CORRECT}
    assert not claims.may_promote(status)


def test_unregistered_never_above_exploratory():
    f = ClaimFacts(
        positive_ic=True,
        sessions=20,
        net_screen_passed=True,
        untouched_oos_confirmed=True,
        execution_capacity_validated=True,
    )
    assert classify(f) is ClaimStatus.EXPLORATORY_SIGNAL


def test_ladder_with_registration():
    base = {"registered": True, "sessions": 5}
    assert classify(ClaimFacts(feature_verified=True)) is ClaimStatus.FEATURE_CORRECT
    assert classify(ClaimFacts(**base, net_screen_passed=True)) is (
        ClaimStatus.PASSES_REGISTERED_NET_SCREEN
    )
    oos = ClaimFacts(**base, net_screen_passed=True, untouched_oos_confirmed=True)
    assert classify(oos) is ClaimStatus.CONFIRMED_UNTOUCHED_OOS
    full = ClaimFacts(
        **base,
        net_screen_passed=True,
        untouched_oos_confirmed=True,
        execution_capacity_validated=True,
    )
    assert classify(full) is ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY


def test_rejected_and_inconclusive_win():
    strong = {"registered": True, "sessions": 5, "net_screen_passed": True}
    assert classify(ClaimFacts(**strong, rejected=True)) is ClaimStatus.REJECTED
    assert classify(ClaimFacts(**strong, underpowered=True)) is ClaimStatus.INCONCLUSIVE
    assert classify(ClaimFacts()) is ClaimStatus.INCONCLUSIVE


def test_promotion_requires_confirmed_oos():
    for s in ClaimStatus:
        ok = s in (
            ClaimStatus.CONFIRMED_UNTOUCHED_OOS,
            ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY,
        )
        assert claims.may_promote(s) is ok
        if not ok:
            with pytest.raises(claims.PromotionBlocked):
                claims.require_promotable(s, "PAPER")
    claims.require_promotable(ClaimStatus.CONFIRMED_UNTOUCHED_OOS, "PAPER")
    with pytest.raises(claims.PromotionBlocked):
        claims.require_promotable(ClaimStatus.CONFIRMED_UNTOUCHED_OOS, "ACTIVE")
    claims.require_promotable(ClaimStatus.STRATEGY_VALIDATED_EXECUTION_CAPACITY, "ACTIVE")


@pytest.mark.parametrize("verdict", ["continue", "passed_not_expanded", "iterate"])
def test_screen_verdicts_one_session_are_weak(verdict):
    s = claims.from_screen_verdict(verdict, sessions=1, registered=True, positive_ic=True)
    assert s in WEAK


def test_screen_verdicts():
    f = claims.from_screen_verdict
    assert f("continue", sessions=5, registered=True) is ClaimStatus.PASSES_REGISTERED_NET_SCREEN
    assert f("continue", sessions=5, registered=False) is ClaimStatus.EXPLORATORY_SIGNAL
    assert f("stop", sessions=1, registered=True) is ClaimStatus.REJECTED
    assert f("reject", sessions=9, registered=True) is ClaimStatus.REJECTED
    assert f("underpowered", sessions=9, registered=True) is ClaimStatus.INCONCLUSIVE
    assert f("something-new", sessions=9, registered=True) is ClaimStatus.INCONCLUSIVE
    # a screen never confirms, however many sessions
    assert not claims.may_promote(f("continue", sessions=100, registered=True))


def test_lifecycle_report_mapping():
    assert report_claim_status({"verdict": "REJECT", "gate_ic": 0.3}) is ClaimStatus.REJECTED
    top = {"verdict": "PROMOTE", "gate_ic": 0.3, "gate_n_days": 30, "rank": 1}
    assert report_claim_status(top) is ClaimStatus.EXPLORATORY_SIGNAL
    assert report_claim_status({"verdict": "ITERATE", "gate_ic": -0.1}) is ClaimStatus.INCONCLUSIVE


def test_rel08c_decide_carries_status():
    import pandas as pd
    from iap.auction import rel08c_study as st

    cfg = st.Rel08cConfig(min_sessions=1, min_trades=0, target_bps=0.0)
    one = pd.DataFrame({"incremental_bps": [5.0]})
    out = st.decide(one, 100, cfg, registered=True)
    assert ClaimStatus(out["evidence_status"]) in WEAK | {ClaimStatus.REJECTED}

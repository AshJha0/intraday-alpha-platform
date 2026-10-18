"""End-to-end (synthetic) wiring of REL-16 monitoring, REL-17 limits and the
REL-18 paper order path: stale feed -> UNKNOWN; reconciliation exception ->
REVIEW; loss breach -> HALT -> kill switch latched -> orders refused until an
explicit reset."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from iap.monitoring import Acknowledgement, MonitoringConfig, State
from iap.monitoring.config import DEFAULT_POLICY_PATH, load_monitoring_config
from iap.paper.feed import FeedGuard
from iap.paper.oms import OrderManager
from iap.paper.reconcile import DropCopyFill, reconcile
from iap.paper.supervisor import MonitoringRefused, PaperSupervisor
from iap.paper.venue_sim import PaperVenue
from iap.risk.portfolio_limits import LimitRefused, PortfolioGuard, PortfolioLimits
from monitoring_sim import EXPECTATION, sessions

S = 1_000_000_000
CAPITAL = 10_000_000.0
OBS = sessions(1, 20)
NOW = OBS[-1].as_of_ns + 60 * S


def _rig(tmp_path, cfg=None):
    cfg = cfg or load_monitoring_config()
    guard = PortfolioGuard(limits=PortfolioLimits.load(), capital=CAPITAL)
    venue = PaperVenue()
    oms = OrderManager.open(tmp_path / "j.jsonl", fsync=False)
    oms.sender = lambda o: [oms.handle(e) for e in venue.submit(o)]
    sup = PaperSupervisor(
        cfg=cfg,
        guard=guard,
        oms=oms,
        tick_value={7: 0.01, 8: 0.01},
        check_order_kw={"adv_notional": {7: 1e9, 8: 1e9}},
    )
    feed = FeedGuard(stale_after_ns=2 * S, health=sup)
    return sup, guard, oms, venue, feed


def _order(oms, cl, qty=1_000, side=1, inst=7):
    return oms.submit(cl, instrument_id=inst, side=side, qty=qty, price_ticks=10_000)


def _review_cfg():
    """The default policy with REVIEW persistence 1 and reconciliation HALT
    after 3 evaluations (default: 2/2, which skips REVIEW; see the PR)."""
    doc = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    doc["persistence"]["REVIEW"] = 1
    doc["reconciliation"]["halt_after_evaluations"] = 3
    doc["policy_id"] = "rel18-e2e-test-variant"
    return MonitoringConfig.from_doc(doc)


def test_healthy_rig_is_active_and_trades(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path)
    feed.on_message(1, "m", NOW - S)
    sup.feed_heartbeat(NOW - S)
    assert sup.evaluate(NOW, EXPECTATION, OBS).state is State.ACTIVE
    assert not _order(oms, "A").duplicate and len(venue.orders) == 1


def test_stale_feed_goes_unknown_and_blocks_new_risk(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path)
    feed.on_message(1, "m", NOW - 10 * S)
    sup.feed_heartbeat(NOW - 10 * S)
    assert feed.check_stale(NOW)
    ev = sup.evaluate(NOW, EXPECTATION, OBS)
    assert ev.state is State.UNKNOWN and ev.input_problems["heartbeat"]
    with pytest.raises(MonitoringRefused):
        _order(oms, "A")
    assert "A" not in oms.orders and not venue.orders
    # feed resumes -> OK -> ACTIVE again
    feed.on_message(2, "m", NOW + S)
    sup.feed_heartbeat(NOW + S)
    assert sup.evaluate(NOW + 2 * S, EXPECTATION, OBS).state is State.ACTIVE


def test_reconciliation_exception_goes_review_then_reduce_only(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path, _review_cfg())
    sup.feed_heartbeat(NOW - S)
    _order(oms, "A")
    venue.fill("A", 400)  # on the drop copy only: the order manager missed it
    rep = reconcile(oms, venue.drop_copy, sinks=[sup])
    assert not rep.ok and sup.recon_exceptions == 2  # MISSING_FILL + POSITION_MISMATCH
    assert sup.evaluate(NOW, EXPECTATION, OBS).state is State.REVIEW
    with pytest.raises(MonitoringRefused):
        _order(oms, "B")  # adds risk
    oms.recover(venue)  # pull the missed fill
    assert reconcile(oms, venue.drop_copy, sinks=[sup]).ok and sup.recon_exceptions == 0
    assert not _order(oms, "C", qty=100, side=-1).duplicate  # reduces: allowed in REVIEW


def test_persistent_reconciliation_exception_halts_under_default_policy(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path)
    sup.feed_heartbeat(NOW - S)
    sup.on_recon_exceptions(reconcile(oms, [DropCopyFill("Z", 1, 7, 1, 5, 10_000)]))
    assert sup.evaluate(NOW, EXPECTATION, OBS).state is State.ACTIVE  # 1st: WATCH alert only
    assert sup.evaluate(NOW + S, EXPECTATION, OBS).state is State.HALT
    assert guard.halted and sup.halt_log[-1][2] is True


def test_monitoring_drawdown_halt_latches_kill_switch_until_reset(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path)
    sup.feed_heartbeat(NOW - S)
    losing = [replace(o, net_pnl=-1_500.0) for o in OBS]  # drawdown 30k > limit 12k
    ev = sup.evaluate(NOW, EXPECTATION, losing)
    assert ev.state is State.HALT and guard.halted
    assert "REL-16 monitoring HALT" in guard.halt_reason
    with pytest.raises((LimitRefused, MonitoringRefused)):
        _order(oms, "A")
    assert not venue.orders and not oms.orders
    # explicit operator reset of the kill switch + REL-16 acknowledgement -> REVIEW
    guard.reset(operator="owner", reason="drawdown reviewed")
    ack = Acknowledgement("owner", sup.cfg.sha256, ev.memory.halted_at)
    assert sup.evaluate(NOW + S, EXPECTATION, OBS, ack=ack).state is State.REVIEW
    with pytest.raises(MonitoringRefused):
        _order(oms, "B")  # REVIEW: still no new risk


def test_loss_limit_breach_trips_kill_switch_and_refuses_until_reset(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path)
    sup.feed_heartbeat(NOW - S)
    guard.start_day(CAPITAL)
    assert guard.mark(CAPITAL * 0.97)  # 3 % > daily_loss_limit 2 %
    assert guard.halted and sup.halt_log[-1][2] is True
    with pytest.raises(LimitRefused) as e:
        _order(oms, "A")
    assert e.value.halted and not oms.orders and not venue.orders
    with pytest.raises(LimitRefused):  # loss still breached: reset refused
        guard.reset("owner", "try")
    guard.mark(CAPITAL * 0.99)
    guard.start_day(CAPITAL * 0.99)
    guard.reset("owner", "new day, loss reviewed")
    assert not _order(oms, "A").duplicate and len(venue.orders) == 1


def test_breaching_order_refused_before_journal(tmp_path):
    sup, guard, oms, venue, feed = _rig(tmp_path)
    sup.feed_heartbeat(NOW - S)
    n = oms.journal.n
    with pytest.raises(LimitRefused) as e:
        _order(oms, "BIG", qty=1_000_000)  # 1e6 * 100 = 1e8 notional >> name_max 10 %
    assert not e.value.halted and e.value.breaches
    assert oms.journal.n == n and not venue.orders

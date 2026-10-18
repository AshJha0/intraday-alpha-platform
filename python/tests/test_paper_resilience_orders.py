"""REL-18 order and position integrity: idempotency, journal replay, kill/restart,
reconciliation exceptions and the deterministic failure paths."""

from __future__ import annotations

import random

import pytest
from iap.paper.oms import (
    DuplicateOrderError,
    Journal,
    JournalCorrupt,
    OrderManager,
    OrderStateError,
    OrdState,
)
from iap.paper.reconcile import DropCopyFill, ReconKind, reconcile
from iap.paper.venue_sim import PaperVenue

ORDER = {"instrument_id": 7, "side": 1, "qty": 100, "price_ticks": 1000, "venue_id": 1}


def _oms(tmp_path, venue=None, **kw):
    return OrderManager.open(tmp_path / "journal.jsonl", fsync=False, **kw)


def _route(oms, venue, evs):
    for ev in evs:
        oms.handle(ev)


# ------------------------------------------------------------ idempotency
def test_duplicate_submit_is_deduplicated_not_resent(tmp_path):
    sent = []
    oms = _oms(tmp_path, sender=sent.append)
    r1 = oms.submit("A", **ORDER)
    r2 = oms.submit("A", **ORDER)
    assert not r1.duplicate and r2.duplicate and r2.order is r1.order
    assert len(sent) == 1 and oms.dup_submits == 1


def test_reused_id_with_other_params_is_refused(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    with pytest.raises(DuplicateOrderError):
        oms.submit("A", **{**ORDER, "qty": 101})


def test_duplicate_submit_after_partial_ack_still_matches(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A", 60)
    assert oms.submit("A", **ORDER).duplicate


# ------------------------------------------------------------ journal
def _drive(oms, venue):
    _route(oms, venue, venue.submit(oms.submit("A", **ORDER).order))
    _route(oms, venue, venue.fill("A", 30))
    oms.request_cancel("A")
    _route(oms, venue, venue.cancel(oms.orders["A"]))
    _route(oms, venue, venue.submit(oms.submit("B", **{**ORDER, "side": -1, "qty": 50}).order))
    _route(oms, venue, venue.fill("B"))


def test_replay_reaches_identical_state(tmp_path):
    venue = PaperVenue()
    oms = _oms(tmp_path)
    _drive(oms, venue)
    snap = oms.snapshot()
    oms.close()
    again = _oms(tmp_path)
    assert again.snapshot() == snap and again.replayed > 0
    assert again.orders["A"].state is OrdState.CANCELLED
    assert again.positions == {7: 30 - 50}


def test_torn_tail_is_dropped_and_appends_continue(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.close()
    p = tmp_path / "journal.jsonl"
    p.write_bytes(p.read_bytes() + b'{"n":2,"crc":"dead')  # crash mid-write
    again = _oms(tmp_path)
    assert set(again.orders) == {"A"}
    again.on_ack("A")
    again.close()
    assert _oms(tmp_path).orders["A"].state is OrdState.ACKED


def test_corrupt_middle_line_raises(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A")
    oms.close()
    p = tmp_path / "journal.jsonl"
    lines = p.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b"SUBMIT", b"SUBMIX")
    p.write_bytes(b"\n".join(lines))
    with pytest.raises(JournalCorrupt):
        Journal.read(p)


def test_invalid_event_is_never_journaled(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    n = oms.journal.n
    with pytest.raises(OrderStateError):
        oms.on_expire("A")  # NEW cannot expire
    assert oms.journal.n == n


# ------------------------------------------------------------ kill / restart
@pytest.mark.parametrize(
    "crash_point", ["after_journal_before_send", "after_send_before_ack", "after_fill_lost"]
)
def test_kill_restart_mid_order_no_duplicates_and_reconciled(tmp_path, crash_point):
    venue = PaperVenue(seed=1)
    lost: list = []

    def sender(o):
        if crash_point == "after_journal_before_send":
            raise SystemExit("killed")  # journal has SUBMIT, venue never saw it
        lost.extend(venue.submit(o))
        if crash_point == "after_send_before_ack":
            raise SystemExit("killed")  # venue has it; ack never processed

    oms = _oms(tmp_path, sender=sender)
    if crash_point == "after_fill_lost":
        oms.sender = lambda o: _route(oms, venue, venue.submit(o))
        oms.submit("A", **ORDER)
        venue.fill("A", 40)  # fill sent, process dies before handling it
    else:
        with pytest.raises(SystemExit):
            oms.submit("A", **ORDER)
    oms.close()  # the process dies; in-memory state and `lost` events are gone

    oms2 = _oms(tmp_path)
    oms2.recover(venue)
    # the caller retries its intent with the same id: deduplicated
    assert oms2.submit("A", **ORDER).duplicate
    assert len(venue.orders) == 1
    _route(oms2, venue, venue.fill("A"))
    rep = reconcile(oms2, venue.drop_copy)
    assert rep.ok, rep.to_dict()
    assert oms2.orders["A"].state is OrdState.FILLED and oms2.positions == {7: 100}


def test_random_kill_points_never_duplicate_or_lose(tmp_path):
    rng = random.Random(5)
    venue = PaperVenue(seed=2, reject_p=0.1, partial_ack_p=0.2, fill_p=0.5)
    pending: list = []
    oms = _oms(tmp_path)
    oms.sender = lambda o: pending.extend(venue.submit(o))
    for i in range(300):
        oms.submit(
            f"O{i}",
            instrument_id=i % 3,
            side=rng.choice((1, -1)),
            qty=rng.randint(1, 50),
            price_ticks=100,
        )
        pending.extend(venue.step())
        if rng.random() < 0.3:
            for o in oms.open_orders():
                if rng.random() < 0.2 and oms.request_cancel(o.cl_ord_id):
                    pending.extend(venue.cancel(o))
        if rng.random() < 0.1:  # kill: drop a random tail of unprocessed events
            del pending[rng.randint(0, len(pending)) :]
            for ev in pending:
                oms.handle(ev)
            pending.clear()
            oms.close()
            oms = _oms(tmp_path)
            oms.sender = lambda o: pending.extend(venue.submit(o))
            oms.recover(venue)
        else:
            for ev in pending:
                oms.handle(ev)
            pending.clear()
    _route(oms, venue, venue.end_session())
    oms.recover(venue)
    oms.end_session()
    assert len(venue.orders) == 300
    rep = reconcile(oms, venue.drop_copy)
    assert rep.ok, rep.to_dict()["exceptions"][:5]
    assert not oms.open_orders()


# ------------------------------------------------------------ failure paths
def test_reject_leaves_no_position(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_reject("A", "price band")
    assert oms.orders["A"].state is OrdState.REJECTED and oms.positions == {}
    assert oms.orders["A"].leaves_qty == 0


def test_partial_ack_reduces_qty_and_zero_ack_is_reject(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A", 40)
    o = oms.orders["A"]
    assert (o.qty, o.reduced_by, o.state) == (40, 60, OrdState.ACKED)
    oms.on_fill("A", 1, 40, 1000)
    assert o.state is OrdState.FILLED
    oms.submit("B", **ORDER)
    oms.on_ack("B", 0)
    assert oms.orders["B"].state is OrdState.REJECTED


def test_cancel_after_fill_is_deterministic(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A")
    assert oms.request_cancel("A")
    oms.on_fill("A", 1, 100, 1000)  # fill wins the race
    oms.on_cancel_reject("A", "too late")
    assert oms.orders["A"].state is OrdState.FILLED
    assert not oms.request_cancel("A")  # cancel on a terminal order: no-op
    oms.on_cancel_ack("A")  # a stray ack: no-op
    assert oms.orders["A"].state is OrdState.FILLED and oms.positions == {7: 100}


def test_cancel_reject_returns_to_previous_state(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A")
    oms.on_fill("A", 1, 10, 1000)
    oms.request_cancel("A")
    oms.on_fill("A", 2, 10, 1000)  # partial fill while cancel pending
    assert oms.orders["A"].state is OrdState.CANCEL_PENDING
    oms.on_cancel_reject("A", "venue busy")
    assert oms.orders["A"].state is OrdState.PARTIALLY_FILLED


def test_end_of_session_residuals(tmp_path):
    oms = _oms(tmp_path)
    for cl in ("A", "B", "C"):
        oms.submit(cl, **ORDER)
    oms.on_ack("A")
    oms.on_fill("A", 1, 30, 1000)
    oms.on_ack("B")
    res = oms.end_session()
    assert res == [
        {"cl_ord_id": "A", "qty": 100, "filled": 30, "residual": 70},
        {"cl_ord_id": "B", "qty": 100, "filled": 0, "residual": 100},
        {"cl_ord_id": "C", "qty": 100, "filled": 0, "residual": 100},
    ]
    st = {k: o.state for k, o in oms.orders.items()}
    assert st == {"A": OrdState.EXPIRED, "B": OrdState.EXPIRED, "C": OrdState.REJECTED}
    assert oms.end_session() == []


def test_duplicate_fill_ignored(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A")
    assert oms.on_fill("A", 9, 10, 1000)
    assert not oms.on_fill("A", 9, 10, 1000)
    assert oms.dup_fills == 1 and oms.positions == {7: 10}


# ------------------------------------------------------------ reconciliation exceptions
def _filled(tmp_path):
    oms = _oms(tmp_path)
    oms.submit("A", **ORDER)
    oms.on_ack("A")
    oms.on_fill("A", 1, 60, 1000)
    return oms


def _kinds(rep):
    return sorted(e.kind.value for e in rep.exceptions)


def _want(*kinds):
    return sorted(k.value for k in kinds)


def test_recon_clean(tmp_path):
    oms = _filled(tmp_path)
    rep = reconcile(oms, [DropCopyFill("A", 1, 7, 1, 60, 1000)])
    assert rep.ok and rep.unreconciled_positions == 0


def test_recon_missing_fill(tmp_path):
    oms = _filled(tmp_path)
    rep = reconcile(
        oms, [DropCopyFill("A", 1, 7, 1, 60, 1000), DropCopyFill("A", 2, 7, 1, 5, 1000)]
    )
    assert _kinds(rep) == _want(ReconKind.MISSING_FILL, ReconKind.POSITION_MISMATCH)


def test_recon_duplicate_and_unconfirmed(tmp_path):
    oms = _filled(tmp_path)
    rep = reconcile(oms, [])
    assert _kinds(rep) == _want(ReconKind.UNCONFIRMED_FILL, ReconKind.POSITION_MISMATCH)
    dup = DropCopyFill("A", 1, 7, 1, 60, 1000)
    assert _kinds(reconcile(oms, [dup, dup])) == _want(ReconKind.DUPLICATE_FILL)


def test_recon_qty_and_price_mismatch(tmp_path):
    oms = _filled(tmp_path)
    rep = reconcile(oms, [DropCopyFill("A", 1, 7, 1, 61, 1001)])
    assert _kinds(rep) == _want(
        ReconKind.QTY_MISMATCH, ReconKind.PRICE_MISMATCH, ReconKind.POSITION_MISMATCH
    )


def test_recon_unknown_order_and_overfill_and_late(tmp_path):
    oms = _filled(tmp_path)
    oms.on_fill("Z", 5, 1, 1000)  # never sent
    oms.on_fill("A", 6, 50, 1000)  # 60 + 50 > 100
    oms.on_fill("A", 7, 40, 1000)
    oms.on_fill("A", 8, 1, 1000)  # after FILLED
    dc = [
        DropCopyFill(*a)
        for a in [
            ("A", 1, 7, 1, 60, 1000),
            ("Z", 5, 7, 1, 1, 1000),
            ("A", 6, 7, 1, 50, 1000),
            ("A", 7, 7, 1, 40, 1000),
            ("A", 8, 7, 1, 1, 1000),
        ]
    ]
    rep = reconcile(oms, dc)
    assert _kinds(rep) == _want(
        ReconKind.QTY_MISMATCH,
        ReconKind.UNKNOWN_ORDER,
        ReconKind.LATE_FILL,
        ReconKind.POSITION_MISMATCH,
    )


def test_recon_sink_receives_report(tmp_path):
    got = []

    class Sink:
        def on_recon_exceptions(self, report):
            got.append(report)

    reconcile(_filled(tmp_path), [], sinks=[Sink()])
    assert got and not got[0].ok and got[0].to_dict()["unreconciled_positions"] == 1


def test_pre_trade_gate_refuses_before_journal(tmp_path):
    oms = _oms(tmp_path)

    def gate(m, cl, inst, side, qty, px):
        if qty * px > 50_000:
            raise ValueError("limit")

    oms.pre_trade = gate
    n = oms.journal.n
    with pytest.raises(ValueError):
        oms.submit("A", **ORDER)
    assert oms.journal.n == n and "A" not in oms.orders
    assert not oms.submit("B", **{**ORDER, "qty": 10}).duplicate

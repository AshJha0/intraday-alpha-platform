"""Golden replay of ``tests/golden/expected_risk_decisions.json`` (exact
decisions), the byte-exact audit golden (``expected_risk_audit.jsonl``)
and the snapshot golden (``expected_risk_snapshot.json``) — the Python
port of ``rust/risk/tests/golden_risk.rs`` / Java ``RiskGoldenTest``.

The engine is built from ``configs/risk/risk.json`` and the golden's
instrument table, then driven through the pinned step script exactly as
the Rust harness does: every order step's decision, deciding rule and
severity must match, the pinned notification events must appear in
order, the audit log must be byte-identical to the golden file, the
snapshot after ``snapshot_after_step`` must be byte-identical to the
golden (canonical serde_json pretty layout) and restoring it must
reproduce the remaining audit tail bit for bit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from iap.risk import (
    Decision,
    Fill,
    OrderRequest,
    RiskEngine,
    RiskEvent,
    RiskLimits,
    Scope,
    fmt_fixed,
    instrument_refs_from_golden,
)

_NOTIFICATION_RULES = frozenset({
    "VENUE_DISCONNECT", "VENUE_RECONNECT", "KILL_SWITCH_ENGAGED",
    "KILL_SWITCH_CLEARED", "LOSS_LIMIT_OVERRIDE", "SESSION_ROLLED",
    "BOOTSTRAP_COMPLETE", "STATE_RESTORED", "MALFORMED_FILL",
})


def is_notification(rule_id: str, decision: int) -> bool:
    """Rule ids that are notification (non-decision) audit records."""
    if rule_id in _NOTIFICATION_RULES:
        return True
    return rule_id in ("STRATEGY_LOSS", "DAILY_LOSS") and decision == int(Decision.KILL)


@pytest.fixture(scope="module")
def golden(golden_dir: Path) -> Dict[str, Any]:
    with open(golden_dir / "expected_risk_decisions.json") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def repo_root(golden_dir: Path) -> Path:
    return golden_dir.parents[1]


@pytest.fixture(scope="module")
def config(golden: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    with open(repo_root / golden["config"]) as f:
        return json.load(f)


def parse_order(v: Dict[str, Any]) -> OrderRequest:
    return OrderRequest(
        order_id=v["order_id"],
        instrument_id=v["instrument_id"],
        side=v["side"],
        qty=v["qty"],
        price_ticks=v["price_ticks"],
        order_type=v["order_type"],
        venue_id=v["venue_id"],
        strategy_id=v["strategy_id"],
        urgency=v["urgency"],
        timestamp=v["timestamp"],
    )


def build(golden: Dict[str, Any], config: Dict[str, Any]) -> RiskEngine:
    """Build the engine exactly as the reference harness does."""
    return RiskEngine.from_config(config, instrument_refs_from_golden(golden["instruments"]))


def apply(eng: RiskEngine, i: int, step: Dict[str, Any], check: bool) -> None:
    """Apply one step (order steps are checked when ``check``)."""
    kind = step["type"]
    ts = step.get("ts")
    if kind == "market":
        eng.on_market(step["instrument_id"], step["bid_ticks"], step["ask_ticks"], ts)
    elif kind == "fill":
        eng.on_fill(Fill(
            ts=ts,
            strategy_id=step["strategy_id"],
            instrument_id=step["instrument_id"],
            order_id=step["order_id"],
            side=step["side"],
            qty=step["qty"],
            price_ticks=step["price_ticks"],
        ))
    elif kind == "cancel":
        eng.on_order_done(step["order_id"])
    elif kind == "gap":
        eng.on_sequence_gap(step["instrument_id"], ts)
    elif kind == "recover":
        eng.on_feed_recovered(step["instrument_id"], ts)
    elif kind == "venue_down":
        eng.on_venue_disconnect(step["venue_id"], ts)
    elif kind == "venue_up":
        eng.on_venue_reconnect(step["venue_id"], ts)
    elif kind == "kill":
        eng.engage_kill(Scope.parse(step["scope"]), step["scope_id"], ts,
                        step.get("reason", ""))
    elif kind == "unkill":
        eng.clear_kill(Scope.parse(step["scope"]), step["scope_id"], ts,
                       step.get("reason", ""))
    elif kind == "override_loss":
        eng.override_loss_limit(Scope.parse(step["scope"]), step["scope_id"],
                                step["new_limit"], ts, step.get("approver", ""))
    elif kind == "roll_session":
        eng.roll_session(ts, step.get("reason", ""))
    elif kind == "order":
        order = parse_order(step["order"])
        d = eng.check_order(order)
        if check:
            exp = step["expect"]
            assert int(d.decision) == exp["decision"], \
                f"step {i} order {order.order_id}: decision ({d.rule_id} / {d.reason})"
            assert d.rule_id == exp["rule_id"], \
                f"step {i} order {order.order_id}: rule ({d.reason})"
            assert int(d.severity) == exp["severity"], \
                f"step {i} order {order.order_id}: severity"
    else:
        raise AssertionError(f"unknown step type {kind}")


def replay(golden: Dict[str, Any], config: Dict[str, Any], check: bool) -> str:
    """Run the golden script once; returns the audit JSONL."""
    eng = build(golden, config)
    for i, step in enumerate(golden["steps"]):
        apply(eng, i, step, check)
    return eng.audit_jsonl()


def _n_orders(golden: Dict[str, Any]) -> int:
    return sum(1 for s in golden["steps"] if s["type"] == "order")


def test_golden_decisions_match(golden, config):
    assert golden["x-version"] == 3
    assert _n_orders(golden) >= 55, "golden vector must pin at least 55 orders"
    replay(golden, config, check=True)


def test_golden_notification_events_in_order(golden, config):
    audit = replay(golden, config, check=False)
    events = [RiskEvent.from_json_line(line) for line in audit.splitlines()]
    notif = [e for e in events if is_notification(e.rule_id, e.decision)]
    expected = golden["expected_notification_events"]
    assert len(notif) == len(expected), "notification event count"
    for got, want in zip(notif, expected):
        assert got.rule_id == want["rule_id"]
        assert got.scope == Scope.parse(want["scope"])
        assert got.scope_id == want["scope_id"]
        assert got.decision == want["decision"]
    # the script exercises: a mark-driven latch, a re-latch after a clear
    # without override, an override, a session roll and a malformed fill
    ids = {e.rule_id for e in notif}
    for must in ("LOSS_LIMIT_OVERRIDE", "SESSION_ROLLED", "MALFORMED_FILL",
                 "DAILY_LOSS", "STRATEGY_LOSS"):
        assert must in ids, f"golden must pin {must}"


def test_golden_audit_log_matches_byte_for_byte(golden, config, golden_dir):
    a = replay(golden, config, check=False)
    b = replay(golden, config, check=False)
    assert a == b, "audit must be deterministic"
    want = (golden_dir / "expected_risk_audit.jsonl").read_bytes()
    assert a.encode("utf-8") == want, "audit JSONL must be byte-identical to the golden"
    # one audit line per decision + one per pinned notification event
    n_notif = len(golden["expected_notification_events"])
    assert len(a.splitlines()) == _n_orders(golden) + n_notif


def test_golden_snapshot_matches_byte_for_byte(golden, config, golden_dir):
    k = golden["snapshot_after_step"]
    eng = build(golden, config)
    for i, step in enumerate(golden["steps"][: k + 1]):
        apply(eng, i, step, check=True)
    want_bytes = (golden_dir / "expected_risk_snapshot.json").read_bytes()
    # canonical serde_json::to_string_pretty layout + the writer's newline
    assert (eng.snapshot_json(pretty=True) + "\n").encode("utf-8") == want_bytes
    assert eng.snapshot() == json.loads(want_bytes), \
        f"snapshot after step {k} must equal the golden"
    # the compact form is the same document
    assert json.loads(eng.snapshot_json(pretty=False)) == eng.snapshot()


def test_golden_snapshot_restore_reproduces_the_audit_tail(golden, config, golden_dir):
    steps: List[Dict[str, Any]] = golden["steps"]
    k = golden["snapshot_after_step"]
    # 1. the engine's own snapshot after step k equals the golden snapshot
    eng = build(golden, config)
    for i, step in enumerate(steps[: k + 1]):
        apply(eng, i, step, check=True)
    with open(golden_dir / "expected_risk_snapshot.json") as f:
        want = json.load(f)
    assert eng.snapshot() == want
    # 2. restoring the golden snapshot and replaying the rest reproduces
    #    the unbroken run's audit tail exactly (after the STATE_RESTORED line)
    limits = RiskLimits.from_json(config)
    restored = RiskEngine.restore(
        limits, instrument_refs_from_golden(golden["instruments"]), want, 0
    )
    assert restored.audit_len() == 1
    assert restored.audit()[0].rule_id == "STATE_RESTORED"
    for i in range(k + 1, len(steps)):
        apply(restored, i, steps[i], check=True)
        apply(eng, i, steps[i], check=True)
    full_lines = eng.audit_jsonl().splitlines()
    tail = restored.audit_jsonl().splitlines()[1:]
    assert tail == full_lines[len(full_lines) - len(tail):]
    assert restored.snapshot() == eng.snapshot(), "state converges"
    assert restored.snapshot_json() == eng.snapshot_json()


def test_golden_restore_from_every_step_is_bit_identical(golden, config):
    """Restoring the snapshot taken after ANY step and replaying the rest
    reproduces the unbroken run's decisions, audit tail and final state."""
    steps: List[Dict[str, Any]] = golden["steps"]
    refs = instrument_refs_from_golden(golden["instruments"])
    limits = RiskLimits.from_json(config)
    unbroken = build(golden, config)
    snapshots = []
    for i, step in enumerate(steps):
        apply(unbroken, i, step, check=True)
        snapshots.append((i, unbroken.snapshot_json()))
    full_audit = unbroken.audit_jsonl().splitlines()
    for cut, snap_json in snapshots[::7]:
        restored = RiskEngine.restore(limits, refs, json.loads(snap_json), 0)
        for i in range(cut + 1, len(steps)):
            apply(restored, i, steps[i], check=True)
        tail = restored.audit_jsonl().splitlines()[1:]
        assert tail == full_audit[len(full_audit) - len(tail):], f"cut at step {cut}"
        assert restored.snapshot_json() == unbroken.snapshot_json(), f"cut at step {cut}"


def test_golden_fixed_format_cases(golden):
    cases = golden["fixed_format_cases"]
    assert len(cases) >= 10
    for v, d, want in cases:
        assert fmt_fixed(v, d) == want, f"fmt_fixed({v}, {d})"

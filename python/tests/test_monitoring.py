"""REL-16 monitoring policy: the declared external-style evidence.

* stale, missing or invalid inputs give UNKNOWN, never ACTIVE;
* pure noise reaches REVIEW-or-worse within the declared horizon at no more
  than the declared false-alarm rate (Wilson 95 % upper bound over seeds);
* a planted decay of the declared size is caught at REVIEW within the
  declared delay at the declared quantile.

The measured numbers are printed (``pytest -s``) for the PR / release notes.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest
from iap.monitoring import (
    Acknowledgement,
    OrdersSnapshot,
    PositionsSnapshot,
    State,
    build_report,
    evaluate,
    load_monitoring_config,
    permits_new_risk,
    render_markdown,
    run_stream,
)
from iap.monitoring.config import MonitoringConfig, canonical_sha256
from iap.monitoring.explain import check_explanation, explanation_facts
from iap.research.power_stats import wilson_interval
from monitoring_sim import EXPECTATION, first_at_least, sessions, snapshot

CFG = load_monitoring_config()
S = 1_000_000_000


def test_config_hash_is_canonical_and_versioned():
    assert CFG.sha256 == canonical_sha256(json.loads(json.dumps(dict(CFG.doc))))
    doc = dict(CFG.doc)
    assert MonitoringConfig.from_doc({**doc, "clear_after": 4}).sha256 != CFG.sha256
    with pytest.raises(ValueError, match="x-version"):
        MonitoringConfig.from_doc({**doc, "x-version": 2})
    with pytest.raises(ValueError, match="unknown keys"):
        MonitoringConfig.from_doc({**doc, "extra": 1})


def test_clean_stream_is_assessed_and_deterministic():
    snap = snapshot(sessions(1, 30))
    a, b = evaluate(CFG, snap), evaluate(CFG, snap)
    assert a == b
    assert a.state in (State.ACTIVE, State.WATCH)
    assert a.config_sha256 == CFG.sha256


@pytest.mark.parametrize(
    "over, channel",
    [
        ({"expectation": None}, "expectation"),
        ({"positions": None}, "positions"),
        ({"orders": None}, "orders"),
        ({"heartbeat_ns": None}, "heartbeat"),
        ({"sessions": ()}, "sessions"),
    ],
)
def test_missing_input_is_unknown(over, channel):
    ev = evaluate(CFG, snapshot(sessions(2, 30), **over))
    assert ev.state is State.UNKNOWN and not permits_new_risk(ev.state)
    assert ev.input_problems[channel]


@pytest.mark.parametrize("channel", ["sessions", "positions", "orders", "heartbeat"])
def test_stale_input_is_unknown(channel):
    base = snapshot(sessions(3, 30))
    old = base.evaluated_at_ns - int((CFG.freshness_s[channel] + 5) * S)
    if channel == "sessions":
        snap = dataclasses.replace(
            base, evaluated_at_ns=base.evaluated_at_ns + int((CFG.freshness_s[channel] + 5) * S)
        )
        snap = dataclasses.replace(
            snap,
            positions=dataclasses.replace(base.positions, as_of_ns=snap.evaluated_at_ns),
            orders=dataclasses.replace(base.orders, as_of_ns=snap.evaluated_at_ns),
            heartbeat_ns=snap.evaluated_at_ns,
        )
    elif channel == "heartbeat":
        snap = dataclasses.replace(base, heartbeat_ns=old)
    else:
        snap = dataclasses.replace(
            base, **{channel: dataclasses.replace(getattr(base, channel), as_of_ns=old)}
        )
    ev = evaluate(CFG, snap)
    assert ev.state is State.UNKNOWN
    assert [c for c, r in ev.input_problems.items() if r] == [channel]
    assert "stale" in " ".join(ev.input_problems[channel])


def test_future_timestamp_is_unknown():
    base = snapshot(sessions(3, 30))
    ev = evaluate(CFG, dataclasses.replace(base, heartbeat_ns=base.evaluated_at_ns + S))
    assert ev.state is State.UNKNOWN and "clock" in ev.input_problems["heartbeat"][0]


def test_invalid_inputs_are_unknown():
    obs = sessions(4, 30)
    bad = [
        dataclasses.replace(obs[-1], net_pnl=float("nan")),
        dataclasses.replace(obs[-1], completeness=0.2),
        dataclasses.replace(obs[-1], net_pnl="12.0"),  # free text is never a number
        dataclasses.replace(obs[-1], as_of_ns=obs[-2].as_of_ns),
    ]
    for b in bad:
        ev = evaluate(CFG, snapshot([*obs[:-1], b]))
        assert ev.state is State.UNKNOWN, b
    now = snapshot(obs).evaluated_at_ns
    ev = evaluate(CFG, snapshot(obs, orders=OrdersSnapshot(now, -1, 0)))
    assert ev.state is State.UNKNOWN
    ev = evaluate(CFG, snapshot(obs[: CFG.min_sessions - 1]))
    assert ev.state is State.UNKNOWN and "insufficient sample" in " ".join(
        ev.input_problems["sessions"]
    )


def test_unknown_persists_escalates_and_clears_only_on_restored_inputs():
    obs = sessions(5, 30)
    snaps = [snapshot(obs, heartbeat_ns=None)] * CFG.unknown_escalate_after + [snapshot(obs)]
    evs = run_stream(CFG, snaps)
    assert all(e.state is State.UNKNOWN for e in evs[:-1])
    assert any(a.evidence.get("escalated") for a in evs[-2].alerts)
    assert evs[-1].state is not State.UNKNOWN


def test_hard_triggers_halt_and_latch():
    snap = snapshot(sessions(6, 30))
    over = PositionsSnapshot(snap.evaluated_at_ns, 2.0e6, 0.0, 1.0e6, 5.0e5)
    ev = evaluate(CFG, dataclasses.replace(snap, positions=over))
    assert ev.state is State.HALT
    ev2 = evaluate(CFG, snap, ev.memory)  # clean evidence, no acknowledgement
    assert ev2.state is State.HALT
    wrong = Acknowledgement("owner", "0" * 64, ev.memory.halted_at or 0)
    assert evaluate(CFG, snap, ev2.memory, wrong).state is State.HALT
    ok = Acknowledgement("owner", CFG.sha256, ev.memory.halted_at or 0)
    assert evaluate(CFG, snap, ev2.memory, ok).state is State.REVIEW
    # an acknowledgement does not release while the HALT trigger persists
    assert (
        evaluate(CFG, dataclasses.replace(snap, positions=over), ev2.memory, ok).state is State.HALT
    )
    # HALT wins even while another channel is missing
    ev3 = evaluate(CFG, dataclasses.replace(snap, positions=over, heartbeat_ns=None))
    assert ev3.state is State.HALT


def test_reconciliation_exceptions_halt_when_persisting():
    base = snapshot(sessions(7, 30))
    snap = dataclasses.replace(base, orders=OrdersSnapshot(base.evaluated_at_ns, 2, 1))
    evs = run_stream(CFG, [snap] * CFG.recon_halt_after)
    assert evs[0].state is not State.HALT
    assert evs[-1].state is State.HALT


def test_drawdown_limit_halts():
    obs = sessions(8, 30)
    obs[-1] = dataclasses.replace(obs[-1], net_pnl=-2 * EXPECTATION.drawdown_limit)
    assert evaluate(CFG, snapshot(obs)).state is State.HALT


def test_cost_overrun_reaches_review_after_persistence():
    obs = [
        dataclasses.replace(o, realised_cost_bps=o.realised_cost_bps + 1.5) for o in sessions(9, 30)
    ]
    evs = run_stream(CFG, [snapshot(obs)] * CFG.persistence["REVIEW"])
    assert evs[-1].state is State.REVIEW
    assert any(a.metric == "costs" for a in evs[-1].alerts)


def test_report_and_explanation_contract():
    obs = sessions(10, 30)
    snap = snapshot(obs)
    ev = evaluate(CFG, snap)
    rep = build_report(CFG, snap, ev)
    assert rep["config_sha256"] == CFG.sha256 and rep["state"] == ev.state.value
    assert "sharpe" not in json.dumps(rep).lower()
    for key in ("data", "period", "pnl", "costs", "exposure", "drawdown", "orders", "alerts"):
        assert key in rep
    md = render_markdown(rep)
    assert f"state {ev.state.value}" in md and "MDE" in md
    usnap = snapshot(obs, heartbeat_ns=None)
    unk = evaluate(CFG, usnap)
    assert "state UNKNOWN" in render_markdown(build_report(CFG, usnap, unk))
    assert explanation_facts(unk)["state"] == "UNKNOWN"
    assert check_explanation("State is UNKNOWN: the heartbeat is missing.", unk) == []
    assert check_explanation("Looks fine, ACTIVE.", unk)


def test_noise_false_alarm_rate_within_declared():
    horizon = int(CFG.declared["horizon_sessions"])
    n_seeds = 1000
    alarms = sum(
        first_at_least(CFG, sessions(s, horizon), State.REVIEW) is not None for s in range(n_seeds)
    )
    lo, hi = wilson_interval(alarms, n_seeds)
    print(
        f"\nREL-16 false alarm (REVIEW+ within {horizon} sessions, pure noise): "
        f"{alarms}/{n_seeds} = {alarms / n_seeds:.4f}, Wilson 95% [{lo:.4f}, {hi:.4f}]; "
        f"declared <= {CFG.declared['false_alarm_rate_review']}"
    )
    assert hi <= CFG.declared["false_alarm_rate_review"]


def test_planted_decay_detected_within_declared_delay():
    horizon = int(CFG.declared["horizon_sessions"])
    decay_at = 20
    max_delay = int(CFG.declared["max_detection_delay_sessions"])
    q = CFG.declared["detection_quantile"]
    n_seeds = 400
    delays, early = [], 0
    for s in range(n_seeds):
        obs = sessions(
            100_000 + s, horizon, decay_at=decay_at, decay_sd=CFG.declared["planted_decay_sd"]
        )
        if first_at_least(CFG, obs[:decay_at], State.REVIEW) is not None:
            early += 1  # a false alarm before the decay: not counted as a detection
        t = first_at_least(CFG, obs, State.REVIEW, start=decay_at + 1)
        delays.append(np.inf if t is None else t - decay_at)
    d = np.array(delays)
    within = int((d <= max_delay).sum())
    lo, hi = wilson_interval(within, n_seeds)
    finite = d[np.isfinite(d)]
    print(
        f"\nREL-16 detection delay ({CFG.declared['planted_decay_sd']} sd decay at session {decay_at}, "
        f"{n_seeds} seeds): median {np.median(finite):.0f}, p90 {np.percentile(d, 90):.1f}, "
        f"p95 {np.percentile(d, 95):.1f}, max {np.max(d)}, undetected {int(np.isinf(d).sum())}, "
        f"pre-decay alarms {early}; within {max_delay}: {within}/{n_seeds} = {within / n_seeds:.3f} "
        f"Wilson 95% [{lo:.3f}, {hi:.3f}]; declared >= {q}"
    )
    assert within / n_seeds >= q

"""Golden group: tests/golden/expected_lifecycle.json and the committed
bootstrap artefacts.

Pins: the golden document is reproduced byte-for-byte by
``iap.lifecycle.golden`` under the pinned config; every scenario step is
reproduced by an independent replay of the golden's own inputs through a
fresh machine built from the golden's embedded config (states, outcomes,
gate results, counters and transitions exact); every logged transition
validates against its schema; the transition table in the golden equals the
code's; and ``python -m iap.lifecycle bootstrap`` reproduces the committed
``research/alpha_registry.json`` / ``research/lifecycle_transitions.jsonl``
byte-for-byte.
"""

from __future__ import annotations

import json

import pytest

from iap.contracts.types import Actor, LifecycleState, LifecycleTransition
from iap.contracts.validate import validate_typed
from iap.lifecycle import (
    AlphaLifecycle,
    AlphaRegistry,
    Evidence,
    Outcome,
    PolicyConfig,
    load_policy_config,
    transition_table,
)
from iap.lifecycle.bootstrap import REGISTRY_RELPATH, TRANSITIONS_RELPATH, run_bootstrap
from iap.lifecycle.config import repo_root
from iap.lifecycle.golden import (
    EXPECTED_FINAL_STATES,
    GOLDEN_X_VERSION,
    STEP_NS,
    T0,
    golden_document,
    render_golden,
)

GOLDEN_NAME = "expected_lifecycle.json"
ROOT = repo_root()


@pytest.fixture(scope="module")
def golden_text(golden_dir) -> str:
    return (golden_dir / GOLDEN_NAME).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def golden(golden_text):
    return json.loads(golden_text)


def test_golden_header_and_config(golden):
    assert golden["x-version"] == GOLDEN_X_VERSION
    assert golden["t0"] == T0 and golden["step_ns"] == STEP_NS
    assert golden["states"] == {s.name: int(s) for s in LifecycleState}
    assert golden["transition_table"] == transition_table()
    assert PolicyConfig.from_dict(golden["config"]) == load_policy_config()
    assert sorted(golden["scenarios"]) == ["LC01", "LC02", "LC03"]


def test_golden_reproduced_byte_for_byte(golden_text):
    assert render_golden(golden_document(load_policy_config())) == golden_text


@pytest.mark.parametrize("alpha_id", ["LC01", "LC02", "LC03"])
def test_golden_scenario_replays_exactly(golden, alpha_id):
    """Independent replay: the golden's inputs through a machine built only
    from the golden's embedded config."""
    config = PolicyConfig.from_dict(golden["config"])
    registry = AlphaRegistry(config.policy)
    machine = AlphaLifecycle(config, registry)
    machine.register(alpha_id, golden["t0"])
    steps = golden["scenarios"][alpha_id]
    assert [s["step"] for s in steps] == list(range(len(steps)))
    for step in steps:
        ts = step["event_ts"]
        assert ts == golden["t0"] + (step["step"] + 1) * golden["step_ns"]
        expected = step["expected"]
        if step["action"] == "advance":
            transition = machine.advance(alpha_id, ts, Evidence.from_dict(step["evidence"]))
            evaluation = machine.evaluations[-1]
            assert evaluation.outcome == expected["outcome"]
            assert {n: g.to_dict() for n, g in evaluation.gates.items()} == expected["gates"]
            assert list(evaluation.gates) == list(expected["gates"]), "gate order is pinned"
        elif step["action"] == "retire":
            transition = machine.retire(alpha_id, ts, step["reason"], actor=Actor.HUMAN)
            assert expected["outcome"] == Outcome.TRANSITION
        else:
            transition = machine.reset_to_research(alpha_id, ts, step["reason"],
                                                   actor=Actor.HUMAN)
            assert expected["outcome"] == Outcome.TRANSITION
        rec = registry.get(alpha_id)
        assert rec.state.name == expected["state"]
        assert int(rec.state) == expected["state_index"]
        assert rec.consecutive_failures == expected["consecutive_failures"]
        assert rec.breach_count == expected["breach_count"]
        assert rec.recovery_count == expected["recovery_count"]
        if transition is None:
            assert expected["transition"] is None
        else:
            assert transition.to_dict() == expected["transition"]
            validate_typed(LifecycleTransition.from_dict(expected["transition"]))
    assert registry.get(alpha_id).state is EXPECTED_FINAL_STATES[alpha_id]


def test_golden_scenarios_cover_every_system_edge(golden):
    seen = set()
    for steps in golden["scenarios"].values():
        for step in steps:
            tr = step["expected"]["transition"]
            if tr is not None:
                seen.add((tr["from_state"], tr["to_state"], tr["actor"]))
    expected = {
        ("RESEARCH", "CANDIDATE", "SYSTEM"), ("CANDIDATE", "VALIDATING", "SYSTEM"),
        ("VALIDATING", "PAPER", "SYSTEM"), ("PAPER", "ACTIVE", "SYSTEM"),
        ("ACTIVE", "WATCH", "SYSTEM"), ("WATCH", "ACTIVE", "SYSTEM"),
        ("WATCH", "RETIRED", "SYSTEM"), ("CANDIDATE", "RESEARCH", "SYSTEM"),
        ("PAPER", "CANDIDATE", "SYSTEM"), ("CANDIDATE", "RETIRED", "HUMAN"),
        ("RETIRED", "RESEARCH", "HUMAN"),
    }
    assert seen == expected


def test_bootstrap_matches_committed_artefacts(tmp_path):
    committed_registry = (ROOT / REGISTRY_RELPATH).read_text(encoding="utf-8")
    committed_log = (ROOT / TRANSITIONS_RELPATH).read_text(encoding="utf-8")
    result = run_bootstrap(ROOT, write=False)
    assert result.registry.render() == committed_registry
    lines = [json.dumps(validate_typed(t), sort_keys=True, separators=(",", ":"))
             for t in result.machine.transitions]
    assert "\n".join(lines) + "\n" == committed_log
    loaded = AlphaRegistry.load(ROOT / REGISTRY_RELPATH)
    assert loaded.render() == committed_registry
    assert all(loaded.get(a).state is LifecycleState.CANDIDATE for a in loaded.alpha_ids())
    assert len(loaded) == 24

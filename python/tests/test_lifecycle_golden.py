"""Golden group: tests/golden/expected_lifecycle.json and the committed
bootstrap artefacts.

Pins: the golden document (``x-version`` 2) is reproduced byte-for-byte by
``iap.lifecycle.golden`` under the pinned config; every scenario step of
BOTH sections — ``scenarios`` (LC01..LC04, the default policy: ledger
significance threshold, CUSUM retirement) and ``legacy.scenarios`` (LG01,
the rules up to v1.4.0: fixed threshold, consecutive breaches) — is
reproduced by an independent replay of the golden's own inputs through a
fresh machine built from that section's embedded config (states, outcomes,
gate results, counters, the CUSUM statistic and transitions exact); every
logged transition validates against its schema; the transition table in the
golden equals the code's; and ``python -m iap.lifecycle bootstrap`` reproduces the committed
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
    legacy_config,
    legacy_scripts,
    render_golden,
    scripts,
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
    assert GOLDEN_X_VERSION == 2
    assert PolicyConfig.from_dict(golden["config"]) == load_policy_config()
    assert sorted(golden["scenarios"]) == ["LC01", "LC02", "LC03", "LC04"] == sorted(scripts())
    # the default policy, by name
    assert golden["config"]["tstat_threshold"] == "ledger"
    assert golden["config"]["live"]["breach_rule"] == "cusum"
    assert (golden["config"]["live"]["cusum_k"], golden["config"]["live"]["cusum_h"]) == (
        0.0025,
        0.01,
    )


def test_golden_legacy_section_header(golden):
    """``legacy`` holds the same machine under the rules up to v1.4.0, with
    its own embedded config: the default gates, the fixed threshold and the
    consecutive-breach rule."""
    legacy = golden["legacy"]
    assert sorted(legacy) == ["config", "scenarios"]
    assert PolicyConfig.from_dict(legacy["config"]) == legacy_config(load_policy_config())
    assert sorted(legacy["scenarios"]) == ["LG01"] == sorted(legacy_scripts())
    assert legacy["config"]["tstat_threshold"] == "fixed"
    assert legacy["config"]["live"]["breach_rule"] == "consecutive"
    for key in ("policy", "gates", "demotion"):
        assert legacy["config"][key] == golden["config"][key], key
    for key in ("watch_ic_gate", "reactivate_ic_gate", "retire_breach_evals", "reactivate_evals"):
        assert legacy["config"]["live"][key] == golden["config"]["live"][key], key


def _section(golden, name: str) -> dict:
    return golden if name == "default" else golden["legacy"]


def _transition_count(scenarios) -> int:
    return sum(
        1 for steps in scenarios.values() for s in steps if s["expected"]["transition"] is not None
    )


def test_golden_transition_counts(golden):
    assert _transition_count(golden["scenarios"]) == 36
    assert _transition_count(golden["legacy"]["scenarios"]) == 9


def test_golden_reproduced_byte_for_byte(golden_text):
    assert render_golden(golden_document(load_policy_config())) == golden_text


@pytest.mark.parametrize(
    "section, alpha_id",
    [
        ("default", "LC01"),
        ("default", "LC02"),
        ("default", "LC03"),
        ("default", "LC04"),
        ("legacy", "LG01"),
    ],
)
def test_golden_scenario_replays_exactly(golden, section, alpha_id):
    """Independent replay: the golden's inputs through a machine built only
    from the embedded config of the scenario's own section (the default
    policy, or the legacy one), step by step."""
    doc = _section(golden, section)
    config = PolicyConfig.from_dict(doc["config"])
    registry = AlphaRegistry(config.policy)
    machine = AlphaLifecycle(config, registry)
    machine.register(alpha_id, golden["t0"])
    steps = doc["scenarios"][alpha_id]
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
            transition = machine.reset_to_research(alpha_id, ts, step["reason"], actor=Actor.HUMAN)
            assert expected["outcome"] == Outcome.TRANSITION
        rec = registry.get(alpha_id)
        assert rec.state.name == expected["state"]
        assert int(rec.state) == expected["state_index"]
        assert rec.consecutive_failures == expected["consecutive_failures"]
        assert rec.breach_count == expected["breach_count"]
        assert rec.recovery_count == expected["recovery_count"]
        assert rec.cusum == expected["cusum"]  # exact: no tolerance
        if section == "legacy":
            assert rec.cusum == 0.0, "the consecutive rule never moves the statistic"
        else:
            assert rec.breach_count == 0, "the CUSUM rule does not count breaches"
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
        ("RESEARCH", "CANDIDATE", "SYSTEM"),
        ("CANDIDATE", "VALIDATING", "SYSTEM"),
        ("VALIDATING", "PAPER", "SYSTEM"),
        ("PAPER", "ACTIVE", "SYSTEM"),
        ("ACTIVE", "WATCH", "SYSTEM"),
        ("WATCH", "ACTIVE", "SYSTEM"),
        ("WATCH", "RETIRED", "SYSTEM"),
        ("CANDIDATE", "RESEARCH", "SYSTEM"),
        ("PAPER", "CANDIDATE", "SYSTEM"),
        ("CANDIDATE", "RETIRED", "HUMAN"),
        ("RETIRED", "RESEARCH", "HUMAN"),
    }
    assert seen == expected
    # LG01 is the happy path plus every live edge under the consecutive rule
    legacy_seen = set()
    for steps in golden["legacy"]["scenarios"].values():
        for step in steps:
            tr = step["expected"]["transition"]
            if tr is not None:
                legacy_seen.add((tr["from_state"], tr["to_state"], tr["actor"]))
    assert legacy_seen == expected - {
        ("CANDIDATE", "RESEARCH", "SYSTEM"),
        ("PAPER", "CANDIDATE", "SYSTEM"),
        ("CANDIDATE", "RETIRED", "HUMAN"),
    }


def test_golden_pins_what_the_default_policy_changes(golden):
    """The scenario steps that exist because of v1.5.0, read off the golden
    and checked against a hand calculation."""
    lc03 = golden["scenarios"]["LC03"]
    # step 1: t 5.0 against the evidence's ledger threshold 5.5 -> fails
    sig = lc03[1]["expected"]["gates"]["statistical_significance"]
    assert lc03[1]["evidence"]["significance_threshold"] == 5.5
    assert sig == {"passed": False, "value": 5.0, "threshold": 5.5}
    # step 2: no threshold in the evidence -> fails, threshold null, value kept
    sig = lc03[2]["expected"]["gates"]["statistical_significance"]
    assert lc03[2]["evidence"]["significance_threshold"] is None
    assert sig == {"passed": False, "value": 5.0, "threshold": None}
    assert lc03[2]["expected"]["state"] == "CANDIDATE"
    # step 3: a threshold below the floor: max(3.0, 2.0) = 3.0 -> VALIDATING
    sig = lc03[3]["expected"]["gates"]["statistical_significance"]
    assert lc03[3]["evidence"]["significance_threshold"] == 2.0
    assert sig == {"passed": True, "value": 5.0, "threshold": 3.0}
    assert lc03[3]["expected"]["state"] == "VALIDATING"
    # LG01, legacy: no threshold in the evidence passes the fixed 3.0 gate
    lg01 = golden["legacy"]["scenarios"]["LG01"]
    assert lg01[1]["evidence"]["significance_threshold"] is None
    assert lg01[1]["expected"]["gates"]["statistical_significance"] == {
        "passed": True,
        "value": 4.0,
        "threshold": 3.0,
    }
    # ... and the sixth consecutive breach retires (breach_count 1..5 before)
    live = [s for s in lg01 if s["evidence"] and s["evidence"]["live"] is not None]
    assert [s["expected"]["breach_count"] for s in live[-7:-1]] == [1, 2, 3, 4, 5, 0]
    assert live[-2]["expected"]["state"] == "RETIRED"
    # LC04: eight breaches of -0.001 are inside the slack k = 0.0025
    # (0.125 * (0.0 - (-0.001) - 0.0025) < 0): S stays 0 and nothing retires
    lc04 = golden["scenarios"]["LC04"]
    slack = [s for s in lc04 if s["evidence"] and (s["evidence"]["live"] or {}).get("eval_index")]
    slack = [s for s in slack if s["evidence"]["live"]["rolling_ic"] == -0.001]
    assert len(slack) == 8
    assert [s["expected"]["state"] for s in slack] == ["WATCH"] * 8
    assert [s["expected"]["cusum"] for s in slack] == [0.0] * 8
    # LC01: the first breach of -0.01 adds 0.125 * (0.0 + 0.01 - 0.0025) = 0.0009375
    lc01 = golden["scenarios"]["LC01"]
    assert lc01[7]["evidence"]["live"] == {
        "rolling_ic": -0.01,
        "n_buckets": 8,
        "eval_index": 4,
        "informative": True,
        "new_fraction": 0.125,
    }
    assert lc01[7]["expected"]["state"] == "WATCH"
    assert lc01[7]["expected"]["cusum"] == 0.125 * (0.0 - (-0.01) - 0.0025) == 0.0009375


def test_bootstrap_matches_committed_artefacts(tmp_path):
    committed_registry = (ROOT / REGISTRY_RELPATH).read_text(encoding="utf-8")
    committed_log = (ROOT / TRANSITIONS_RELPATH).read_text(encoding="utf-8")
    result = run_bootstrap(ROOT, write=False)
    assert result.registry.render() == committed_registry
    lines = [
        json.dumps(validate_typed(t), sort_keys=True, separators=(",", ":"))
        for t in result.machine.transitions
    ]
    assert "\n".join(lines) + "\n" == committed_log
    loaded = AlphaRegistry.load(ROOT / REGISTRY_RELPATH)
    assert loaded.render() == committed_registry
    assert all(loaded.get(a).state is LifecycleState.CANDIDATE for a in loaded.alpha_ids())
    assert len(loaded) == 24

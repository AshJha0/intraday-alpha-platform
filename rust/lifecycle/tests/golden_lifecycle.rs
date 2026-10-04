//! Golden replay of `tests/golden/expected_lifecycle.json`: every step of
//! LC01 .. LC06 is driven through a fresh machine built from the golden's
//! embedded default config (ledger significance threshold, CUSUM retirement)
//! and every step of LG01 through one built from the embedded `legacy`
//! config (fixed threshold, consecutive breaches — the rules up to v1.4.0),
//! asserting after each step the state, the outcome, every gate result (in
//! evaluation order), the counters, the CUSUM statistic and the
//! transition's canonical JSON — exactly, no tolerance. Also: the embedded
//! transition table equals [`lifecycle::ALLOWED_TRANSITIONS`], the state ids,
//! the policy loaded from `configs/` equals the embedded one, and
//! `research/alpha_registry.json` reloads and re-renders byte-identically.

use contracts::canonical_json;
use lifecycle::{
    promotion_edge, transition_table, Actor, AlphaLifecycle, AlphaRegistry, BreachRule, Evidence,
    GateResult, LifecycleState, NetPnlCiGate, Outcome, PolicyConfig, TstatThreshold,
};
use serde_json::Value;

fn repo_path(rel: &str) -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join(rel)
}

fn golden() -> Value {
    let text = std::fs::read_to_string(repo_path("tests/golden/expected_lifecycle.json"))
        .expect("golden readable");
    serde_json::from_str(&text).expect("golden parses")
}

fn gate_result(v: &Value) -> GateResult {
    GateResult::from_value(v).expect("gate result parses")
}

/// Run one scenario of the default section, asserting every step.
fn replay(golden: &Value, alpha_id: &str) -> AlphaLifecycle {
    replay_section(golden, golden, alpha_id)
}

/// Run one scenario of `section` (the golden itself, or its `legacy` block:
/// an object with `config` and `scenarios`), asserting every step.
fn replay_section(golden: &Value, section: &Value, alpha_id: &str) -> AlphaLifecycle {
    let config = PolicyConfig::from_value(&section["config"]).expect("embedded config parses");
    let t0 = golden["t0"].as_i64().expect("t0");
    let step_ns = golden["step_ns"].as_i64().expect("step_ns");
    let registry = AlphaRegistry::new(&config.policy).expect("policy");
    let mut machine = AlphaLifecycle::new(config, registry).expect("machine");
    machine.register(alpha_id, t0).expect("register");
    assert_eq!(
        machine.state(alpha_id).expect("known"),
        LifecycleState::Research
    );

    let steps = section["scenarios"][alpha_id]
        .as_array()
        .expect("scenario steps");
    for (k, step) in steps.iter().enumerate() {
        let where_ = format!(
            "{alpha_id} step {k} ({})",
            step["note"].as_str().unwrap_or("")
        );
        assert_eq!(step["step"].as_u64(), Some(k as u64), "{where_}");
        let event_ts = step["event_ts"].as_i64().expect("event_ts");
        assert_eq!(
            event_ts,
            t0 + (k as i64 + 1) * step_ns,
            "{where_}: event time"
        );
        let expected = &step["expected"];

        let (transition, outcome, gates): (Option<_>, Outcome, Vec<(String, GateResult)>) =
            match step["action"].as_str().expect("action") {
                "advance" => {
                    let evidence =
                        Evidence::from_value(&step["evidence"]).expect("evidence parses");
                    // Evidence documents round-trip exactly.
                    assert_eq!(
                        evidence.to_value().expect("finite"),
                        step["evidence"],
                        "{where_}: evidence round trip"
                    );
                    let t = machine
                        .advance(alpha_id, event_ts, &evidence)
                        .unwrap_or_else(|e| panic!("{where_}: {e}"));
                    let ev = machine
                        .evaluations()
                        .last()
                        .expect("evaluation recorded")
                        .clone();
                    assert_eq!(ev.alpha_id, alpha_id);
                    assert_eq!(ev.event_ts, event_ts);
                    (t, ev.outcome, ev.gates)
                }
                "retire" => {
                    let reason = step["reason"].as_str().expect("reason");
                    let t = machine
                        .retire(alpha_id, event_ts, reason, Actor::Human)
                        .unwrap_or_else(|e| panic!("{where_}: {e}"));
                    (Some(t), Outcome::Transition, Vec::new())
                }
                "reset" => {
                    let reason = step["reason"].as_str().expect("reason");
                    let t = machine
                        .reset_to_research(alpha_id, event_ts, reason, Actor::Human)
                        .unwrap_or_else(|e| panic!("{where_}: {e}"));
                    (Some(t), Outcome::Transition, Vec::new())
                }
                other => panic!("{where_}: unknown action {other}"),
            };

        let rec = machine.record(alpha_id).expect("record");
        assert_eq!(
            rec.state.name(),
            expected["state"].as_str().expect("state"),
            "{where_}: state"
        );
        assert_eq!(
            u64::from(rec.state.index()),
            expected["state_index"].as_u64().expect("index"),
            "{where_}: state_index"
        );
        assert_eq!(
            outcome.name(),
            expected["outcome"].as_str().expect("outcome"),
            "{where_}: outcome"
        );
        assert_eq!(
            rec.consecutive_failures,
            expected["consecutive_failures"].as_u64().expect("cf"),
            "{where_}: consecutive_failures"
        );
        assert_eq!(
            rec.breach_count,
            expected["breach_count"].as_u64().expect("bc"),
            "{where_}: breach_count"
        );
        assert_eq!(
            rec.recovery_count,
            expected["recovery_count"].as_u64().expect("rc"),
            "{where_}: recovery_count"
        );
        assert_eq!(
            Some(rec.cusum),
            expected["cusum"].as_f64(),
            "{where_}: cusum"
        );

        // Gate results: same set, same values (exact), evaluation order = edge order.
        let expected_gates = expected["gates"].as_object().expect("gates object");
        assert_eq!(gates.len(), expected_gates.len(), "{where_}: gate count");
        for (name, got) in &gates {
            let want = expected_gates
                .get(name)
                .unwrap_or_else(|| panic!("{where_}: unexpected gate {name}"));
            assert_eq!(*got, gate_result(want), "{where_}: gate {name}");
        }
        if !gates.is_empty() {
            let before =
                LifecycleState::from_name(step_state_before(section, alpha_id, k)).expect("state");
            let expected_order: Vec<&str> = if before.is_live() {
                vec!["rolling_ic"]
            } else {
                // the edge's gates the section's policy evaluates
                machine
                    .config()
                    .evaluated_gates(promotion_edge(before).expect("promotion edge").gates)
            };
            let got_order: Vec<&str> = gates.iter().map(|(n, _)| n.as_str()).collect();
            assert_eq!(got_order, expected_order, "{where_}: gate evaluation order");
        }

        // Transition: canonical JSON byte-identical to the golden's document.
        match (&transition, &expected["transition"]) {
            (None, Value::Null) => {}
            (Some(t), want) if !want.is_null() => {
                assert_eq!(
                    t.to_canonical_json().expect("valid"),
                    canonical_json(want).expect("finite"),
                    "{where_}: transition canonical JSON"
                );
                assert_eq!(
                    rec.last_transition.as_ref(),
                    Some(t),
                    "{where_}: last_transition"
                );
                assert_eq!(rec.since_ts, event_ts, "{where_}: since_ts");
            }
            (got, want) => panic!("{where_}: transition mismatch: got {got:?}, want {want}"),
        }
    }
    machine
}

/// The state the alpha was in before step `k` (from the previous step's
/// expectation, RESEARCH for the first).
fn step_state_before<'a>(section: &'a Value, alpha_id: &str, k: usize) -> &'a str {
    if k == 0 {
        "RESEARCH"
    } else {
        section["scenarios"][alpha_id][k - 1]["expected"]["state"]
            .as_str()
            .expect("state")
    }
}

#[test]
fn golden_header_and_tables() {
    let g = golden();
    assert_eq!(g["x-version"], 3);
    assert_eq!(
        transition_table(),
        g["transition_table"],
        "transition table"
    );
    for s in LifecycleState::ALL {
        assert_eq!(
            g["states"][s.name()].as_u64(),
            Some(u64::from(s.index())),
            "{}",
            s.name()
        );
    }
    let embedded = PolicyConfig::from_value(&g["config"]).expect("embedded config");
    assert_eq!(embedded.to_value(), g["config"], "config round trip");
    let loaded = PolicyConfig::load(
        &repo_path("configs/strategies/lifecycle.json"),
        &repo_path("configs/strategies/strategies.json"),
    )
    .expect("configs load");
    assert_eq!(
        loaded, embedded,
        "configs/ equals the embedded golden config"
    );
    // The committed policy is the v1.5.0 default; the legacy section names
    // the rules up to v1.4.0, round-trips too and shares every gate.
    assert_eq!(embedded.tstat_threshold, TstatThreshold::Ledger);
    assert_eq!(embedded.live.breach_rule, BreachRule::Cusum);
    let legacy = PolicyConfig::from_value(&g["legacy"]["config"]).expect("legacy config");
    assert_eq!(
        legacy.to_value(),
        g["legacy"]["config"],
        "legacy round trip"
    );
    assert_eq!(legacy.tstat_threshold, TstatThreshold::Fixed);
    assert_eq!(legacy.live.breach_rule, BreachRule::Consecutive);
    assert_eq!(legacy.gates, embedded.gates);
    // The bootstrap gate is evaluated by the default policy and left out by
    // the legacy one; the edge lists it under either.
    assert_eq!(embedded.net_pnl_ci_gate, NetPnlCiGate::Required);
    assert_eq!(legacy.net_pnl_ci_gate, NetPnlCiGate::Absent);
    assert_eq!(embedded.gates.min_net_pnl_ci_low, 0.0);
    assert_eq!(embedded.gates.net_pnl_ci_level, 0.95);
    assert_eq!(
        promotion_edge(LifecycleState::Candidate)
            .expect("promotion edge")
            .gates
            .len(),
        11
    );
    // The correlation gate is configured the same under either policy.
    assert_eq!(embedded.cross_alpha_min_state, LifecycleState::Validating);
    assert_eq!(legacy.cross_alpha_min_state, LifecycleState::Validating);
    assert_eq!(embedded.gates.max_cross_alpha_correlation, 0.7);
    assert_eq!(
        promotion_edge(LifecycleState::Candidate)
            .expect("promotion edge")
            .gates
            .last()
            .copied(),
        Some("cross_alpha_correlation")
    );
    let names = |v: &Value| -> Vec<String> {
        v.as_object()
            .expect("scenarios object")
            .keys()
            .cloned()
            .collect()
    };
    let mut default_names = names(&g["scenarios"]);
    default_names.sort_unstable();
    assert_eq!(default_names, ["LC01", "LC02", "LC03", "LC04", "LC05", "LC06"]);
    assert_eq!(names(&g["legacy"]["scenarios"]), ["LG01"]);
}

#[test]
fn scenario_lc01_full_life() {
    let g = golden();
    let m = replay(&g, "LC01");
    assert_eq!(m.state("LC01").expect("known"), LifecycleState::Research);
    assert_eq!(m.transitions().len(), 9);
}

#[test]
fn scenario_lc02_leakage_demotion() {
    let g = golden();
    let m = replay(&g, "LC02");
    assert_eq!(m.state("LC02").expect("known"), LifecycleState::Research);
}

#[test]
fn scenario_lc03_demotion_counter_and_manual_retire() {
    let g = golden();
    let m = replay(&g, "LC03");
    assert_eq!(m.state("LC03").expect("known"), LifecycleState::Retired);
    // Every SYSTEM edge of the table except the VALIDATING -> CANDIDATE
    // demotion (exercised by the unit tests) is covered by the scenarios —
    // the same pinned set as the Python golden test.
    let mut covered: Vec<(String, String)> = Vec::new();
    for alpha in ["LC01", "LC02", "LC03", "LC04", "LC05", "LC06"] {
        for step in g["scenarios"][alpha].as_array().expect("steps") {
            let t = &step["expected"]["transition"];
            if t["actor"] == "SYSTEM" {
                covered.push((
                    t["from_state"].as_str().expect("from").to_string(),
                    t["to_state"].as_str().expect("to").to_string(),
                ));
            }
        }
    }
    for e in lifecycle::ALLOWED_TRANSITIONS.iter().filter(|e| {
        e.actor == Actor::System
            && !(e.from_state == LifecycleState::Validating
                && e.to_state == LifecycleState::Candidate)
    }) {
        assert!(
            covered.contains(&(
                e.from_state.name().to_string(),
                e.to_state.name().to_string()
            )),
            "edge {} -> {} not covered",
            e.from_state.name(),
            e.to_state.name()
        );
    }
}

#[test]
fn scenario_lc04_cusum_retirement() {
    let g = golden();
    let m = replay(&g, "LC04");
    assert_eq!(m.state("LC04").expect("known"), LifecycleState::Retired);
    let retirements = m
        .transitions()
        .iter()
        .filter(|t| t.to_state == LifecycleState::Retired)
        .count();
    assert_eq!(retirements, 3, "three CUSUM retirements");
    // The six default scripts carry 52 transitions between them.
    let total: usize = ["LC01", "LC02", "LC03", "LC04", "LC05", "LC06"]
        .iter()
        .map(|a| replay(&g, a).transitions().len())
        .sum();
    assert_eq!(total, 52);
}

#[test]
fn scenario_lc05_cross_alpha_correlation_gate() {
    let g = golden();
    let m = replay(&g, "LC05");
    assert_eq!(m.state("LC05").expect("known"), LifecycleState::Validating);
    assert_eq!(m.transitions().len(), 14);
    // The alpha passes the gate four times; each time it is the last of the
    // eleven results the transition carries.
    let promotions: Vec<_> = m
        .transitions()
        .iter()
        .filter(|t| t.to_state == LifecycleState::Validating)
        .collect();
    assert_eq!(promotions.len(), 4);
    for t in &promotions {
        assert_eq!(t.gates.len(), 11);
        assert!(t.gates["cross_alpha_correlation"].passed);
    }
    // The golden pins the fail-closed case and the tie.
    let steps = g["scenarios"]["LC05"].as_array().expect("steps");
    let gate = |k: usize| &steps[k]["expected"]["gates"]["cross_alpha_correlation"];
    assert!(steps[1]["evidence"]["cross_alpha"].is_null());
    assert_eq!(steps[1]["expected"]["outcome"], "HOLD");
    assert!(gate(1)["value"].is_null(), "no block: value null");
    assert_eq!(gate(1)["passed"].as_bool(), Some(false));
    assert_eq!(gate(1)["threshold"], 0.7);
    assert_eq!(steps[8]["expected"]["state"], "VALIDATING");
    assert_eq!(gate(8)["value"], 0.7, "the tie");
    assert_eq!(gate(8)["passed"].as_bool(), Some(true));
    assert_eq!(gate(8)["threshold"], 0.7);
}

#[test]
fn scenario_lc06_net_pnl_bootstrap_gate() {
    let g = golden();
    let m = replay(&g, "LC06");
    assert_eq!(m.state("LC06").expect("known"), LifecycleState::Validating);
    assert_eq!(m.transitions().len(), 2);
    let promoted = m.transitions().last().expect("promotion");
    assert_eq!(promoted.reason, "all 11 gates passed: CANDIDATE -> VALIDATING");
    assert_eq!(promoted.gates["net_pnl_bootstrap_ci"].value, Some(12.5));
    // The golden pins every way the gate decides.
    let steps = g["scenarios"]["LC06"].as_array().expect("steps");
    assert_eq!(steps.len(), 8);
    let gate = |k: usize| &steps[k]["expected"]["gates"]["net_pnl_bootstrap_ci"];
    assert!(steps[1]["evidence"]["pnl_bootstrap"].is_null());
    let expected = |k: usize| &steps[k]["expected"];
    for k in 1..=6 {
        assert_eq!(expected(k)["outcome"], "HOLD", "step {k}");
        assert_eq!(gate(k)["passed"].as_bool(), Some(false), "step {k}");
        assert_eq!(gate(k)["threshold"], 0.0, "step {k}");
    }
    // no block, no trade, no bounds, another level: value null
    for k in [1, 3, 4, 5] {
        assert!(gate(k)["value"].is_null(), "step {k}");
    }
    assert_eq!(steps[3]["evidence"]["pnl_bootstrap"]["n_trades"], 0);
    assert!(steps[4]["evidence"]["pnl_bootstrap"]["ci_low"].is_null());
    assert_eq!(steps[5]["evidence"]["pnl_bootstrap"]["level"], 0.9);
    // an interval spanning zero fails at its lower bound; exactly 0.0 fails (strict)
    assert_eq!(gate(2)["value"], -35.5);
    assert_eq!(gate(6)["value"], 0.0);
    assert_eq!(gate(7)["value"], 12.5);
    assert_eq!(gate(7)["passed"].as_bool(), Some(true));
    assert_eq!(steps[7]["expected"]["state"], "VALIDATING");
}

#[test]
fn scenario_lg01_legacy_rules() {
    let g = golden();
    let m = replay_section(&g, &g["legacy"], "LG01");
    assert_eq!(m.state("LG01").expect("known"), LifecycleState::Research);
    assert_eq!(m.transitions().len(), 9);
    // The legacy policy leaves the bootstrap gate out: ten gates, by name.
    let promoted = m
        .transitions()
        .iter()
        .find(|t| t.to_state == LifecycleState::Validating)
        .expect("promotion");
    assert_eq!(promoted.reason, "all 10 gates passed: CANDIDATE -> VALIDATING");
    assert!(!promoted.gates.contains_key("net_pnl_bootstrap_ci"));
    assert!(m
        .transitions()
        .iter()
        .any(|t| t.reason == "persistent breach: 6 consecutive evals below watch gate 0.0"));
}

#[test]
fn registry_file_round_trips_byte_identically() {
    let path = repo_path("research/alpha_registry.json");
    let text = std::fs::read_to_string(&path).expect("registry readable");
    let registry = AlphaRegistry::load(&path).expect("registry loads strictly");
    assert_eq!(registry.len(), 24);
    assert_eq!(registry.policy(), "lifecycle_v1");
    assert_eq!(
        registry.render().expect("finite"),
        text,
        "byte-identical re-render"
    );
    // A machine resumes from it and the file remains stable.
    let config = PolicyConfig::load(
        &repo_path("configs/strategies/lifecycle.json"),
        &repo_path("configs/strategies/strategies.json"),
    )
    .expect("configs");
    let machine = AlphaLifecycle::new(config, registry).expect("machine");
    for id in machine.registry().alpha_ids() {
        assert_eq!(
            machine.state(id).expect("known"),
            LifecycleState::Candidate,
            "{id}"
        );
    }
    assert_eq!(machine.into_registry().render().expect("finite"), text);
}

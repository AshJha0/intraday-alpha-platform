//! Rule-level tests of the lifecycle machine (edges, gates, actors) and a
//! SplitMix64-driven property test: the system never moves a RETIRED alpha,
//! an evaluation never advances more than one state, only a HUMAN retires
//! or resets, and the same seed replays to the same transitions.
//!
//! The rule tests written for v1.4.0 run under [`legacy_config`], which names
//! the rules they were written for (`tstat_threshold = "fixed"`,
//! `breach_rule = "consecutive"`); the v1.5.0 defaults (ledger significance
//! threshold, CUSUM retirement) have their own tests under
//! [`default_config`], and the property test runs under both. The
//! `cross_alpha_correlation` gate is a row of the table under either policy:
//! the research helpers carry an empty peer list (the vacuous pass), and the
//! gate's own rule tests set the block explicitly. So is
//! `net_pnl_bootstrap_ci`, which the legacy policy leaves out by name
//! (`net_pnl_ci_gate = "absent"`): the research helpers carry a passing
//! bootstrap interval, a constant.

use std::collections::BTreeMap;

use lifecycle::{
    Actor, AlphaLifecycle, AlphaRegistry, CrossAlphaEvidence, EdgeKind, Evidence, ExperimentResult,
    GateResult, LifecycleState, LiveEvidence, NetPnlCiGate, Outcome, PaperEvidence,
    PeerCorrelation, PnlBootstrapEvidence, PolicyConfig, TransitionLog, ValidationEvidence,
    Verdict, ALLOWED_TRANSITIONS,
};
use marketdata::SplitMix64;
use serde_json::json;

fn policy(tstat_threshold: &str, net_pnl_ci_gate: &str, live: serde_json::Value) -> PolicyConfig {
    PolicyConfig::from_value(&json!({
        "policy": "lifecycle_v1",
        "tstat_threshold": tstat_threshold,
        "cross_alpha_min_state": "VALIDATING",
        "net_pnl_ci_gate": net_pnl_ci_gate,
        "gates": {
            "min_experiments_in_ledger": 1, "min_oos_ic": 0.01, "min_nw_tstat": 3.0,
            "min_fold_sign_consistency": 0.7, "min_folds": 3, "min_net_return_bps": 0.0,
            "min_capacity_usd": 1000000.0, "max_ic_rank_gap": 1.0, "ic_rank_gap_eps": 1e-12,
            "max_holdout_ic_gap": 0.01, "min_paper_sessions": 5, "max_paper_ic_gap": 0.01,
            "min_paper_net_pnl": 0.0, "max_kill_events": 0,
            "max_cross_alpha_correlation": 0.7, "min_net_pnl_ci_low": 0.0,
            "net_pnl_ci_level": 0.95
        },
        "demotion": {"max_consecutive_failures": 3},
        "live": live
    }))
    .expect("valid config")
}

/// The LEGACY policy, by name: the fixed 3.0 significance threshold and the
/// consecutive-breach retirement rule, without the net P&L bootstrap gate
/// (the rules up to v1.4.0).
fn legacy_config() -> PolicyConfig {
    policy(
        "fixed",
        "absent",
        json!({"watch_ic_gate": 0.0, "reactivate_ic_gate": 0.005, "retire_breach_evals": 6,
               "reactivate_evals": 3, "breach_rule": "consecutive"}),
    )
}

/// The default policy since v1.5.0: the ledger significance threshold, the
/// net P&L bootstrap gate and the CUSUM retirement rule with the pinned
/// parameters.
fn default_config() -> PolicyConfig {
    policy(
        "ledger",
        "required",
        json!({"watch_ic_gate": 0.0, "reactivate_ic_gate": 0.005, "retire_breach_evals": 6,
               "reactivate_evals": 3, "breach_rule": "cusum", "cusum_k": 0.0025, "cusum_h": 0.01}),
    )
}

fn machine_with(cfg: PolicyConfig, alpha: &str) -> AlphaLifecycle {
    let reg = AlphaRegistry::new(&cfg.policy).expect("policy");
    let mut m = AlphaLifecycle::new(cfg, reg).expect("machine");
    m.register(alpha, 0).expect("register");
    m
}

/// A machine under the legacy policy.
fn machine(alpha: &str) -> AlphaLifecycle {
    machine_with(legacy_config(), alpha)
}

fn research(
    alpha: &str,
    ic: f64,
    rank_ic: f64,
    t_stat: f64,
    leakage: bool,
    hypothesis: Option<bool>,
    net: f64,
) -> ExperimentResult {
    ExperimentResult {
        experiment_id: format!("{}-research", alpha.to_lowercase()),
        alpha_id: alpha.to_string(),
        dataset_version: "0123456789abcdef".repeat(4),
        feature_version: "fedcba9876543210".repeat(4),
        model_version: None,
        ic,
        rank_ic,
        t_stat,
        nw_lags: 2,
        hit_rate: 0.55,
        turnover: 40.0,
        gross_return_bps: net + 7.0,
        transaction_cost_bps: 7.0,
        net_return_bps: net,
        max_drawdown_bps: 20.0,
        sharpe: 1.5,
        fold_consistency: 1.0,
        n_folds: 4,
        leakage_passed: leakage,
        leakage_detail: json!({"shift_ok": leakage}),
        hypothesis_sign_confirmed: hypothesis,
        verdict: if leakage {
            Verdict::Promote
        } else {
            Verdict::Reject
        },
        n_experiments_in_ledger: 10,
        git_commit: "golden".to_string(),
        created_ts: 0,
    }
}

fn good(alpha: &str) -> ExperimentResult {
    research(alpha, 0.02, 0.03, 4.0, true, Some(true), 5.0)
}

/// "There is no other alpha": the vacuous pass of the correlation gate.
fn no_peers() -> CrossAlphaEvidence {
    CrossAlphaEvidence { peers: vec![] }
}

/// A bootstrap interval of 626 bars at level 0.95 with the given lower bound
/// (`None`: no bounds) and trade count.
fn bootstrap(ci_low: Option<f64>, ci_high: Option<f64>, n_trades: u64) -> PnlBootstrapEvidence {
    PnlBootstrapEvidence {
        ci_low,
        ci_high,
        level: 0.95,
        n_resamples: 1000,
        seed: 20260829,
        mean_block: 9.0,
        n_bars: 626,
        n_trades,
    }
}

/// An interval that passes the bootstrap gate under the default policy.
fn passing_bootstrap() -> PnlBootstrapEvidence {
    bootstrap(Some(12.5), Some(90.0), 40)
}

fn ev_research_at(r: ExperimentResult, capacity: Option<f64>, threshold: Option<f64>) -> Evidence {
    Evidence {
        research: Some(r),
        capacity_usd: capacity,
        significance_threshold: threshold,
        pnl_bootstrap: Some(passing_bootstrap()),
        cross_alpha: Some(no_peers()),
        validation: None,
        paper: None,
        live: None,
    }
}

/// Research evidence that carries no significance threshold.
fn ev_research(r: ExperimentResult, capacity: Option<f64>) -> Evidence {
    ev_research_at(r, capacity, None)
}

fn ev_validation(parity: bool) -> Evidence {
    Evidence {
        research: None,
        capacity_usd: None,
        significance_threshold: None,
        pnl_bootstrap: None,
        cross_alpha: None,
        validation: Some(ValidationEvidence {
            holdout_ic: 0.02,
            research_ic: 0.02,
            replay_hash_match: true,
            parity,
        }),
        paper: None,
        live: None,
    }
}

fn ev_paper(ok: bool) -> Evidence {
    Evidence {
        research: None,
        capacity_usd: None,
        significance_threshold: None,
        pnl_bootstrap: None,
        cross_alpha: None,
        validation: None,
        paper: Some(PaperEvidence {
            n_sessions: if ok { 5 } else { 2 },
            realized_ic: 0.02,
            research_ic: 0.02,
            net_pnl: 100.0,
            n_kill_events: 0,
            tracking_error: 0.0,
        }),
        live: None,
    }
}

fn ev_live_at(ic: Option<f64>, informative: bool, new_fraction: f64) -> Evidence {
    Evidence {
        research: None,
        capacity_usd: None,
        significance_threshold: None,
        pnl_bootstrap: None,
        cross_alpha: None,
        validation: None,
        paper: None,
        live: Some(LiveEvidence {
            rolling_ic: ic,
            n_buckets: 8,
            eval_index: 1,
            informative,
            new_fraction,
        }),
    }
}

/// A live reading one 15-minute block after the last (1/8 of the window new).
fn ev_live(ic: Option<f64>, informative: bool) -> Evidence {
    ev_live_at(ic, informative, 0.125)
}

/// Walk an alpha to ACTIVE (the research evidence carries the threshold 3.5,
/// which the fixed policy does not read and the ledger policy passes at t 4.0).
fn to_active(m: &mut AlphaLifecycle, alpha: &str) {
    m.advance(alpha, 1, &ev_research(good(alpha), None))
        .expect("ok");
    m.advance(
        alpha,
        2,
        &ev_research_at(good(alpha), Some(5e6), Some(3.5)),
    )
    .expect("ok");
    m.advance(alpha, 3, &ev_validation(true)).expect("ok");
    m.advance(alpha, 4, &ev_paper(true)).expect("ok");
    assert_eq!(m.state(alpha).expect("known"), LifecycleState::Active);
}

#[test]
fn research_with_empty_evidence_holds_with_null_values() {
    let mut m = machine("A");
    let t = m.advance("A", 1, &Evidence::empty()).expect("ok");
    assert!(t.is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.outcome, Outcome::Hold);
    assert_eq!(
        ev.failed_gates(),
        vec!["ledger_entry_exists", "leakage_clean"]
    );
    assert!(ev.gates.iter().all(|(_, g)| g.value.is_none()));
    assert_eq!(ev.gates[0].1.threshold, Some(1.0));
}

#[test]
fn candidate_non_leakage_failure_holds_without_a_counter() {
    let mut m = machine("A");
    m.advance("A", 1, &ev_research(good("A"), None))
        .expect("ok");
    assert_eq!(m.state("A").expect("known"), LifecycleState::Candidate);
    // Capacity too small: hold, counter stays 0, gate order pinned.
    let t = m
        .advance("A", 2, &ev_research(good("A"), Some(1.0)))
        .expect("ok");
    assert!(t.is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.outcome, Outcome::Hold);
    assert_eq!(ev.failed_gates(), vec!["capacity"]);
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 0);
    // Absent research block at CANDIDATE: silence.
    let t = m.advance("A", 3, &Evidence::empty()).expect("ok");
    assert!(t.is_none());
    assert_eq!(
        m.evaluations().last().expect("recorded").outcome,
        Outcome::NoEvidence
    );
    // Leakage demotes at once and carries all ten gate results.
    let t = m
        .advance(
            "A",
            4,
            &ev_research(
                research("A", 0.02, 0.03, 4.0, false, Some(true), 5.0),
                Some(5e6),
            ),
        )
        .expect("ok")
        .expect("demoted");
    assert_eq!(t.to_state, LifecycleState::Research);
    assert_eq!(t.gates.len(), 10);
    assert_eq!(
        t.reason,
        "leakage_clean failed: a leaking alpha is not a candidate"
    );
}

#[test]
fn validating_demotes_on_third_consecutive_failure_and_silence_does_not_count() {
    let mut m = machine("A");
    m.advance("A", 1, &ev_research(good("A"), None))
        .expect("ok");
    m.advance("A", 2, &ev_research(good("A"), Some(5e6)))
        .expect("ok");
    assert_eq!(m.state("A").expect("known"), LifecycleState::Validating);
    assert!(m
        .advance("A", 3, &ev_validation(false))
        .expect("ok")
        .is_none());
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 1);
    assert!(m.advance("A", 4, &Evidence::empty()).expect("ok").is_none());
    assert_eq!(
        m.record("A").expect("rec").consecutive_failures,
        1,
        "silence is not evidence"
    );
    assert!(m
        .advance("A", 5, &ev_validation(false))
        .expect("ok")
        .is_none());
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 2);
    let t = m
        .advance("A", 6, &ev_validation(false))
        .expect("ok")
        .expect("demoted");
    assert_eq!(
        (t.from_state, t.to_state),
        (LifecycleState::Validating, LifecycleState::Candidate)
    );
    assert_eq!(
        t.reason,
        "3 consecutive failed evaluations (max 3); failed gates: cross_language_parity"
    );
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 0);
    // A passing evaluation resets the counter.
    m.advance("A", 7, &ev_research(good("A"), Some(5e6)))
        .expect("ok");
    assert!(m
        .advance("A", 8, &ev_validation(false))
        .expect("ok")
        .is_none());
    let t = m
        .advance("A", 9, &ev_validation(true))
        .expect("ok")
        .expect("promoted");
    assert_eq!(t.to_state, LifecycleState::Paper);
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 0);
}

#[test]
fn default_policy_reads_the_significance_threshold_from_the_evidence() {
    let mut m = machine_with(default_config(), "A");
    m.advance("A", 1, &ev_research(good("A"), None))
        .expect("ok");
    assert_eq!(m.state("A").expect("known"), LifecycleState::Candidate);
    // No threshold in the evidence: the gate fails with a null threshold.
    assert!(m
        .advance("A", 2, &ev_research(good("A"), Some(5e6)))
        .expect("ok")
        .is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.failed_gates(), vec!["statistical_significance"]);
    let gate = &ev.gates[2];
    assert_eq!(gate.0, "statistical_significance");
    assert_eq!((gate.1.value, gate.1.threshold), (Some(4.0), None));
    // t 4.0 under a ledger threshold of 4.5: fails at that threshold.
    assert!(m
        .advance("A", 3, &ev_research_at(good("A"), Some(5e6), Some(4.5)))
        .expect("ok")
        .is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.failed_gates(), vec!["statistical_significance"]);
    assert_eq!(ev.gates[2].1.threshold, Some(4.5));
    // A threshold under the floor is replaced by the floor; t 4.0 passes.
    let t = m
        .advance("A", 4, &ev_research_at(good("A"), Some(5e6), Some(2.0)))
        .expect("ok")
        .expect("promoted");
    assert_eq!(t.to_state, LifecycleState::Validating);
    assert_eq!(t.gates["statistical_significance"].threshold, Some(3.0));
    // The same thresholdless evidence passes the LEGACY fixed gate.
    let mut legacy = machine("A");
    legacy
        .advance("A", 1, &ev_research(good("A"), None))
        .expect("ok");
    let t = legacy
        .advance("A", 2, &ev_research(good("A"), Some(5e6)))
        .expect("ok")
        .expect("promoted");
    assert_eq!(t.gates["statistical_significance"].threshold, Some(3.0));
}

const CORRELATION_GATE: &str = "cross_alpha_correlation";

/// A cross-alpha block from `(alpha_id, state, correlation)` rows.
fn peers(rows: &[(&str, LifecycleState, f64)]) -> CrossAlphaEvidence {
    CrossAlphaEvidence {
        peers: rows
            .iter()
            .map(|(alpha_id, state, correlation)| PeerCorrelation {
                alpha_id: (*alpha_id).to_string(),
                state: *state,
                correlation: *correlation,
            })
            .collect(),
    }
}

/// CANDIDATE evidence that passes every other gate under either policy.
fn ev_candidate(alpha: &str, cross_alpha: Option<CrossAlphaEvidence>) -> Evidence {
    let mut ev = ev_research_at(good(alpha), Some(5e6), Some(3.5));
    ev.cross_alpha = cross_alpha;
    ev
}

/// A machine whose alpha "A" is at CANDIDATE.
fn candidate_machine(cfg: PolicyConfig) -> AlphaLifecycle {
    let mut m = machine_with(cfg, "A");
    m.advance("A", 1, &ev_research(good("A"), None))
        .expect("ok");
    assert_eq!(m.state("A").expect("known"), LifecycleState::Candidate);
    m
}

/// Gates a CANDIDATE evaluation carries under the machine's policy: the
/// edge's eleven, ten when the policy leaves the bootstrap gate out.
fn candidate_gate_count(m: &AlphaLifecycle) -> usize {
    match m.config().net_pnl_ci_gate {
        NetPnlCiGate::Required => 11,
        NetPnlCiGate::Absent => 10,
    }
}

/// The correlation gate's result in the last recorded evaluation.
fn correlation_gate(m: &AlphaLifecycle) -> GateResult {
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.gates.len(), candidate_gate_count(m));
    let (name, result) = ev.gates.last().expect("gates");
    assert_eq!(name.as_str(), CORRELATION_GATE, "evaluated last");
    result.clone()
}

/// Evaluate `cross_alpha` at CANDIDATE and expect a HOLD on the correlation
/// gate alone; returns the gate's result.
fn held_by_correlation(cfg: PolicyConfig, cross_alpha: Option<CrossAlphaEvidence>) -> GateResult {
    let mut m = candidate_machine(cfg);
    let t = m
        .advance("A", 2, &ev_candidate("A", cross_alpha))
        .expect("ok");
    assert!(t.is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.outcome, Outcome::Hold);
    assert_eq!(ev.failed_gates(), vec![CORRELATION_GATE]);
    assert_eq!(m.state("A").expect("known"), LifecycleState::Candidate);
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 0);
    correlation_gate(&m)
}

/// Evaluate `cross_alpha` at CANDIDATE and expect the promotion to
/// VALIDATING; returns the gate's result as the transition carries it.
fn promoted_past_correlation(cfg: PolicyConfig, cross_alpha: CrossAlphaEvidence) -> GateResult {
    let mut m = candidate_machine(cfg);
    let t = m
        .advance("A", 2, &ev_candidate("A", Some(cross_alpha)))
        .expect("ok")
        .expect("promoted");
    assert_eq!(
        (t.from_state, t.to_state),
        (LifecycleState::Candidate, LifecycleState::Validating)
    );
    assert_eq!(t.gates.len(), candidate_gate_count(&m));
    let result = t.gates[CORRELATION_GATE].clone();
    assert_eq!(result, correlation_gate(&m));
    result
}

#[test]
fn cross_alpha_missing_block_fails_closed() {
    for cfg in [default_config(), legacy_config()] {
        let r = held_by_correlation(cfg, None);
        assert_eq!((r.passed, r.value, r.threshold), (false, None, Some(0.7)));
    }
    // Silence at CANDIDATE still keys on the research block alone: a
    // cross-alpha block without research evaluates nothing.
    let mut m = candidate_machine(default_config());
    let mut alone = Evidence::empty();
    alone.cross_alpha = Some(no_peers());
    assert!(m.advance("A", 2, &alone).expect("ok").is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.outcome, Outcome::NoEvidence);
    assert!(ev.gates.is_empty());
}

#[test]
fn cross_alpha_eligible_peer_above_threshold_holds() {
    let r = held_by_correlation(
        default_config(),
        Some(peers(&[("P", LifecycleState::Active, 0.82)])),
    );
    assert_eq!((r.passed, r.value, r.threshold), (false, Some(0.82), Some(0.7)));
    // Every state from VALIDATING to WATCH counts under the default policy.
    for state in [
        LifecycleState::Validating,
        LifecycleState::Paper,
        LifecycleState::Active,
        LifecycleState::Watch,
    ] {
        let r = held_by_correlation(default_config(), Some(peers(&[("P", state, 0.75)])));
        assert_eq!(r.value, Some(0.75), "{}", state.name());
    }
}

#[test]
fn cross_alpha_absolute_value_is_gated() {
    let r = held_by_correlation(
        default_config(),
        Some(peers(&[
            ("P", LifecycleState::Paper, -0.9),
            ("Q", LifecycleState::Active, 0.1),
        ])),
    );
    assert_eq!((r.passed, r.value), (false, Some(0.9)));
}

#[test]
fn cross_alpha_ineligible_peers_are_ignored() {
    let r = promoted_past_correlation(
        default_config(),
        peers(&[
            ("P", LifecycleState::Research, 0.99),
            ("Q", LifecycleState::Candidate, 0.95),
            ("R", LifecycleState::Retired, -0.99),
        ]),
    );
    assert_eq!((r.passed, r.value, r.threshold), (true, Some(0.0), Some(0.7)));
    // An eligible peer beside them is the one that counts.
    let r = promoted_past_correlation(
        default_config(),
        peers(&[
            ("P", LifecycleState::Candidate, 0.95),
            ("Q", LifecycleState::Active, 0.25),
            ("R", LifecycleState::Validating, -0.4),
        ]),
    );
    assert_eq!((r.passed, r.value), (true, Some(0.4)));
}

#[test]
fn cross_alpha_tie_at_the_threshold_passes() {
    let r = promoted_past_correlation(
        default_config(),
        peers(&[
            ("P", LifecycleState::Paper, 0.7),
            ("Q", LifecycleState::Watch, -0.7),
        ]),
    );
    assert_eq!((r.passed, r.value, r.threshold), (true, Some(0.7), Some(0.7)));
}

#[test]
fn cross_alpha_empty_peer_list_passes_vacuously() {
    for cfg in [default_config(), legacy_config()] {
        let r = promoted_past_correlation(cfg, no_peers());
        assert_eq!((r.passed, r.value, r.threshold), (true, Some(0.0), Some(0.7)));
    }
}

#[test]
fn cross_alpha_min_state_active_ignores_earlier_states() {
    let rows = [
        ("P", LifecycleState::Validating, 0.9),
        ("Q", LifecycleState::Paper, -0.95),
    ];
    // Counted under the default VALIDATING ...
    let r = held_by_correlation(default_config(), Some(peers(&rows)));
    assert_eq!(r.value, Some(0.95));
    // ... and not under ACTIVE.
    let mut doc = default_config().to_value();
    doc["cross_alpha_min_state"] = json!("ACTIVE");
    let active = PolicyConfig::from_value(&doc).expect("valid config");
    assert_eq!(active.cross_alpha_min_state, LifecycleState::Active);
    let r = promoted_past_correlation(active.clone(), peers(&rows));
    assert_eq!((r.passed, r.value), (true, Some(0.0)));
    // ACTIVE and WATCH peers still count; RETIRED never does.
    let r = held_by_correlation(
        active.clone(),
        Some(peers(&[
            ("P", LifecycleState::Retired, 0.99),
            ("Q", LifecycleState::Watch, 0.8),
        ])),
    );
    assert_eq!(r.value, Some(0.8));
    // PAPER as the lowest state: VALIDATING is ignored, PAPER counts.
    doc["cross_alpha_min_state"] = json!("PAPER");
    let paper = PolicyConfig::from_value(&doc).expect("valid config");
    let r = held_by_correlation(paper, Some(peers(&rows)));
    assert_eq!(r.value, Some(0.95));
    let r = promoted_past_correlation(active, peers(&rows[..1]));
    assert_eq!(r.value, Some(0.0));
}

#[test]
fn cross_alpha_config_and_evidence_strictness() {
    let rejects = |doc: &serde_json::Value, key: &str| {
        let err = PolicyConfig::from_value(doc).expect_err(key);
        assert!(err.to_string().contains(key), "{err}");
    };
    let mut doc = default_config().to_value();
    doc["cross_alpha_min_state"] = json!("CANDIDATE");
    rejects(&doc, "cross_alpha_min_state");
    doc["cross_alpha_min_state"] = json!("RETIRED");
    rejects(&doc, "cross_alpha_min_state");
    doc.as_object_mut()
        .expect("object")
        .remove("cross_alpha_min_state");
    rejects(&doc, "cross_alpha_min_state");
    let mut doc = default_config().to_value();
    doc["gates"]["max_cross_alpha_correlation"] = json!(1.5);
    rejects(&doc, "max_cross_alpha_correlation");
    doc["gates"]
        .as_object_mut()
        .expect("object")
        .remove("max_cross_alpha_correlation");
    rejects(&doc, "max_cross_alpha_correlation");
    // The merged view carries both keys and round-trips.
    let doc = default_config().to_value();
    assert_eq!(doc["cross_alpha_min_state"], "VALIDATING");
    assert_eq!(doc["gates"]["max_cross_alpha_correlation"], 0.7);
    assert_eq!(
        PolicyConfig::from_value(&doc).expect("round trip"),
        default_config()
    );
    // Invalid evidence is rejected before it reaches the machine.
    let mut m = candidate_machine(default_config());
    for bad in [
        peers(&[
            ("Q", LifecycleState::Active, 0.1),
            ("P", LifecycleState::Active, 0.1),
        ]),
        peers(&[
            ("P", LifecycleState::Active, 0.1),
            ("P", LifecycleState::Paper, 0.2),
        ]),
        peers(&[("P", LifecycleState::Active, 1.5)]),
        peers(&[("P", LifecycleState::Active, f64::NAN)]),
        peers(&[("", LifecycleState::Active, 0.1)]),
    ] {
        assert!(m.advance("A", 2, &ev_candidate("A", Some(bad))).is_err());
    }
    assert_eq!(m.evaluations().len(), 1, "nothing was recorded");
    // The document form requires the key.
    let mut doc = Evidence::empty().to_value().expect("finite");
    assert_eq!(doc["cross_alpha"], serde_json::Value::Null);
    assert!(Evidence::from_value(&doc).is_ok());
    doc.as_object_mut().expect("object").remove("cross_alpha");
    assert!(Evidence::from_value(&doc).is_err());
}

const BOOTSTRAP_GATE: &str = "net_pnl_bootstrap_ci";

/// CANDIDATE evidence that passes every gate but, possibly, the bootstrap one.
fn ev_bootstrap(alpha: &str, pnl_bootstrap: Option<PnlBootstrapEvidence>) -> Evidence {
    let mut ev = ev_research_at(good(alpha), Some(5e6), Some(3.5));
    ev.pnl_bootstrap = pnl_bootstrap;
    ev
}

/// Evaluate `pnl_bootstrap` at CANDIDATE under the default policy and expect
/// a HOLD on the bootstrap gate alone; returns the gate's result.
fn held_by_bootstrap(pnl_bootstrap: Option<PnlBootstrapEvidence>) -> GateResult {
    let mut m = candidate_machine(default_config());
    let t = m
        .advance("A", 2, &ev_bootstrap("A", pnl_bootstrap))
        .expect("ok");
    assert!(t.is_none());
    let ev = m.evaluations().last().expect("recorded");
    assert_eq!(ev.outcome, Outcome::Hold);
    assert_eq!(ev.failed_gates(), vec![BOOTSTRAP_GATE]);
    assert_eq!(ev.gates.len(), 11);
    assert_eq!(ev.gates[6].0, "net_pnl_after_costs");
    assert_eq!(ev.gates[7].0, BOOTSTRAP_GATE);
    assert_eq!(ev.gates[8].0, "capacity");
    assert_eq!(m.state("A").expect("known"), LifecycleState::Candidate);
    assert_eq!(m.record("A").expect("rec").consecutive_failures, 0);
    ev.gates[7].1.clone()
}

#[test]
fn bootstrap_missing_block_fails_closed() {
    let r = held_by_bootstrap(None);
    assert_eq!((r.passed, r.value, r.threshold), (false, None, Some(0.0)));
}

#[test]
fn bootstrap_interval_spanning_zero_fails_at_its_value() {
    let r = held_by_bootstrap(Some(bootstrap(Some(-35.5), Some(60.25), 40)));
    assert_eq!((r.passed, r.value, r.threshold), (false, Some(-35.5), Some(0.0)));
}

#[test]
fn bootstrap_unusable_interval_is_no_evidence_for_the_gate() {
    // No trade in any fold: the degenerate [0, 0] interval is not evidence.
    let r = held_by_bootstrap(Some(bootstrap(Some(0.0), Some(0.0), 0)));
    assert_eq!((r.passed, r.value, r.threshold), (false, None, Some(0.0)));
    // A positive interval of an untraded alpha is not evidence either.
    let r = held_by_bootstrap(Some(bootstrap(Some(12.5), Some(90.0), 0)));
    assert_eq!((r.passed, r.value), (false, None));
    // Too few bars: no bounds.
    let mut short = bootstrap(None, None, 40);
    short.n_bars = 5;
    let r = held_by_bootstrap(Some(short));
    assert_eq!((r.passed, r.value), (false, None));
    // Taken at another level than the policy's 0.95.
    let mut other_level = passing_bootstrap();
    other_level.level = 0.9;
    let r = held_by_bootstrap(Some(other_level));
    assert_eq!((r.passed, r.value, r.threshold), (false, None, Some(0.0)));
}

#[test]
fn bootstrap_lower_bound_is_compared_strictly() {
    let r = held_by_bootstrap(Some(bootstrap(Some(0.0), Some(45.0), 40)));
    assert_eq!((r.passed, r.value, r.threshold), (false, Some(0.0), Some(0.0)));
    let mut m = candidate_machine(default_config());
    let t = m
        .advance("A", 2, &ev_bootstrap("A", Some(passing_bootstrap())))
        .expect("ok")
        .expect("promoted");
    assert_eq!(t.to_state, LifecycleState::Validating);
    assert_eq!(t.reason, "all 11 gates passed: CANDIDATE -> VALIDATING");
    assert_eq!(t.gates.len(), 11);
    let r = &t.gates[BOOTSTRAP_GATE];
    assert_eq!((r.passed, r.value, r.threshold), (true, Some(12.5), Some(0.0)));
}

#[test]
fn bootstrap_gate_is_not_evaluated_under_the_absent_policy() {
    assert_eq!(legacy_config().net_pnl_ci_gate, NetPnlCiGate::Absent);
    assert_eq!(default_config().net_pnl_ci_gate, NetPnlCiGate::Required);
    // A null block, and a block that would fail, are both promoted on ten gates.
    for block in [None, Some(bootstrap(Some(-35.5), Some(60.25), 40))] {
        let mut m = candidate_machine(legacy_config());
        let t = m
            .advance("A", 2, &ev_bootstrap("A", block))
            .expect("ok")
            .expect("promoted");
        assert_eq!(t.to_state, LifecycleState::Validating);
        assert_eq!(t.reason, "all 10 gates passed: CANDIDATE -> VALIDATING");
        assert_eq!(t.gates.len(), 10);
        assert!(!t.gates.contains_key(BOOTSTRAP_GATE));
        let ev = m.evaluations().last().expect("recorded");
        assert_eq!(ev.gates.len(), 10);
        assert!(ev.gates.iter().all(|(name, _)| name.as_str() != BOOTSTRAP_GATE));
        assert_eq!(ev.gates[6].0, "net_pnl_after_costs");
        assert_eq!(ev.gates[7].0, "capacity");
    }
    // A failed evaluation under the absent policy does not list the gate.
    let mut m = candidate_machine(legacy_config());
    let mut ev = ev_bootstrap("A", None);
    ev.capacity_usd = Some(1.0);
    assert!(m.advance("A", 2, &ev).expect("ok").is_none());
    let eval = m.evaluations().last().expect("recorded");
    assert_eq!(eval.failed_gates(), vec!["capacity"]);
    assert_eq!(eval.gates.len(), 10);
    // The transition table is the same under either policy: eleven gates.
    let edge = lifecycle::promotion_edge(LifecycleState::Candidate).expect("promotion edge");
    assert_eq!(edge.gates.len(), 11);
    assert_eq!(edge.gates[7], BOOTSTRAP_GATE);
    assert_eq!(default_config().evaluated_gates(edge.gates), edge.gates);
    let evaluated = legacy_config().evaluated_gates(edge.gates);
    assert_eq!(evaluated.len(), 10);
    assert!(!evaluated.contains(&BOOTSTRAP_GATE));
    // The same policy with the gate required holds the null block.
    let mut doc = legacy_config().to_value();
    doc["net_pnl_ci_gate"] = json!("required");
    let required = PolicyConfig::from_value(&doc).expect("valid config");
    let mut m = candidate_machine(required);
    assert!(m
        .advance("A", 2, &ev_bootstrap("A", None))
        .expect("ok")
        .is_none());
    assert_eq!(
        m.evaluations().last().expect("recorded").failed_gates(),
        vec![BOOTSTRAP_GATE]
    );
}

#[test]
fn bootstrap_config_and_evidence_strictness() {
    let rejects = |doc: &serde_json::Value, key: &str| {
        let err = PolicyConfig::from_value(doc).expect_err(key);
        assert!(err.to_string().contains(key), "{err}");
    };
    let mut doc = default_config().to_value();
    doc["net_pnl_ci_gate"] = json!("optional");
    rejects(&doc, "net_pnl_ci_gate");
    doc.as_object_mut()
        .expect("object")
        .remove("net_pnl_ci_gate");
    rejects(&doc, "net_pnl_ci_gate");
    let mut doc = default_config().to_value();
    doc["gates"]["net_pnl_ci_level"] = json!(1.0);
    rejects(&doc, "net_pnl_ci_level");
    doc["gates"]
        .as_object_mut()
        .expect("object")
        .remove("net_pnl_ci_level");
    rejects(&doc, "net_pnl_ci_level");
    let mut doc = default_config().to_value();
    doc["gates"]
        .as_object_mut()
        .expect("object")
        .remove("min_net_pnl_ci_low");
    rejects(&doc, "min_net_pnl_ci_low");
    // The merged view carries the three keys and round-trips.
    let doc = default_config().to_value();
    assert_eq!(doc["net_pnl_ci_gate"], "required");
    assert_eq!(doc["gates"]["min_net_pnl_ci_low"], 0.0);
    assert_eq!(doc["gates"]["net_pnl_ci_level"], 0.95);
    assert_eq!(legacy_config().to_value()["net_pnl_ci_gate"], "absent");
    assert_eq!(
        PolicyConfig::from_value(&doc).expect("round trip"),
        default_config()
    );
    // Invalid evidence is rejected before it reaches the machine.
    let mut level_one = passing_bootstrap();
    level_one.level = 1.0;
    let mut no_resample = passing_bootstrap();
    no_resample.n_resamples = 0;
    let mut short_block = passing_bootstrap();
    short_block.mean_block = 0.5;
    let mut m = candidate_machine(default_config());
    for bad in [
        bootstrap(Some(12.5), None, 40),
        bootstrap(None, Some(90.0), 40),
        bootstrap(Some(12.5), Some(12.0), 40),
        bootstrap(Some(f64::NAN), Some(90.0), 40),
        level_one,
        no_resample,
        short_block,
    ] {
        assert!(m.advance("A", 2, &ev_bootstrap("A", Some(bad))).is_err());
    }
    assert_eq!(m.evaluations().len(), 1, "nothing was recorded");
    // The document form requires the key, and every key of the block.
    let mut doc = Evidence::empty().to_value().expect("finite");
    assert_eq!(doc["pnl_bootstrap"], serde_json::Value::Null);
    doc.as_object_mut().expect("object").remove("pnl_bootstrap");
    assert!(Evidence::from_value(&doc).is_err());
    let full = ev_bootstrap("A", Some(passing_bootstrap()))
        .to_value()
        .expect("finite");
    assert_eq!(
        Evidence::from_value(&full)
            .expect("round trip")
            .pnl_bootstrap,
        Some(passing_bootstrap())
    );
    let mut missing = full;
    missing["pnl_bootstrap"]
        .as_object_mut()
        .expect("object")
        .remove("n_trades");
    assert!(Evidence::from_value(&missing).is_err());
}

#[test]
fn default_policy_retires_by_cusum_and_resumes_from_the_registry() {
    let mut m = machine_with(default_config(), "A");
    to_active(&mut m, "A");
    // Eight breaches inside the slack: WATCH on the first, never RETIRED
    // (the legacy rule retires at the sixth), CUSUM stays 0.
    let t = m
        .advance("A", 5, &ev_live(Some(-0.001), true))
        .expect("ok")
        .expect("watch");
    assert_eq!(t.to_state, LifecycleState::Watch);
    for k in 0..7 {
        assert_eq!(
            m.advance("A", 6 + k, &ev_live(Some(-0.001), true))
                .expect("ok"),
            None
        );
    }
    let rec = m.record("A").expect("rec");
    assert_eq!((rec.breach_count, rec.cusum), (0, 0.0));
    // One deep breach in a disjoint window retires: S = 0.0135 - 0.0025.
    let t = m
        .advance("A", 13, &ev_live_at(Some(-0.0135), true, 1.0))
        .expect("ok")
        .expect("retired");
    assert_eq!(t.to_state, LifecycleState::Retired);
    assert_eq!(
        t.reason,
        "persistent breach: CUSUM 0.011000 >= 0.01 (slack 0.0025) below watch gate 0.0"
    );
    assert!(!t.gates["rolling_ic"].passed);
    assert_eq!(m.record("A").expect("rec").cusum, 0.0);

    // The statistic is persisted: a machine rebuilt from the rendered
    // registry retires on the same reading as the one that kept running.
    let mut a = machine_with(default_config(), "B");
    to_active(&mut a, "B");
    a.advance("B", 5, &ev_live(Some(-0.06), true))
        .expect("ok")
        .expect("watch");
    let stat = a.record("B").expect("rec").cusum;
    assert!((stat - 0.0071875).abs() < 1e-15, "S = {stat}");
    let text = a.registry().render().expect("finite");
    let doc: serde_json::Value = serde_json::from_str(&text).expect("json");
    let reloaded = AlphaRegistry::from_value(&doc).expect("strict");
    assert_eq!(reloaded.get("B").expect("rec").cusum, stat);
    let mut b = AlphaLifecycle::new(default_config(), reloaded).expect("machine");
    let ta = a
        .advance("B", 6, &ev_live(Some(-0.06), true))
        .expect("ok")
        .expect("retired");
    let tb = b
        .advance("B", 6, &ev_live(Some(-0.06), true))
        .expect("ok")
        .expect("retired");
    assert_eq!(ta, tb);
    assert_eq!(ta.to_state, LifecycleState::Retired);
    // A reading outside (0, 1] is rejected before it reaches the machine.
    let mut c = machine_with(default_config(), "C");
    to_active(&mut c, "C");
    assert!(c
        .advance("C", 5, &ev_live_at(Some(-0.01), true, 0.0))
        .is_err());
}

#[test]
fn legacy_live_edges_and_terminal_retirement() {
    let mut m = machine("A");
    to_active(&mut m, "A");
    assert_eq!(m.advance("A", 5, &ev_live(None, true)).expect("ok"), None);
    assert_eq!(
        m.evaluations().last().expect("recorded").outcome,
        Outcome::NoEvidence
    );
    assert_eq!(
        m.advance("A", 6, &ev_live(Some(-0.5), false)).expect("ok"),
        None
    );
    assert_eq!(
        m.evaluations().last().expect("recorded").outcome,
        Outcome::NoEvidence
    );
    let t = m
        .advance("A", 7, &ev_live(Some(-0.01), true))
        .expect("ok")
        .expect("watch");
    assert_eq!(t.to_state, LifecycleState::Watch);
    assert_eq!(t.gates["rolling_ic"].threshold, Some(0.0));
    assert!(!t.gates["rolling_ic"].passed);
    assert_eq!(m.record("A").expect("rec").breach_count, 1);
    for k in 0..5 {
        let r = m
            .advance("A", 8 + k, &ev_live(Some(-0.01), true))
            .expect("ok");
        if k < 4 {
            assert!(r.is_none());
        } else {
            assert_eq!(r.expect("retired").to_state, LifecycleState::Retired);
        }
    }
    assert_eq!(m.state("A").expect("known"), LifecycleState::Retired);
    // Terminal for SYSTEM: nothing moves, not even on a perfect reading.
    for k in 0..10 {
        assert_eq!(
            m.advance("A", 20 + k, &ev_live(Some(0.5), true))
                .expect("ok"),
            None
        );
        assert_eq!(
            m.evaluations().last().expect("recorded").outcome,
            Outcome::Terminal
        );
        assert_eq!(m.state("A").expect("known"), LifecycleState::Retired);
    }
    assert!(
        m.retire("A", 31, "again", Actor::Human).is_err(),
        "already retired"
    );
    let t = m
        .reset_to_research("A", 32, "re-research", Actor::Human)
        .expect("reset");
    assert_eq!(t.to_state, LifecycleState::Research);
    assert!(t.gates.is_empty());
    assert_eq!(t.actor, Actor::Human);
}

#[test]
fn manual_edges_require_a_human_and_a_reason() {
    let mut m = machine("A");
    assert!(m.retire("A", 1, "x", Actor::System).is_err());
    assert!(m.retire("A", 1, "   ", Actor::Human).is_err());
    assert!(
        m.reset_to_research("A", 1, "x", Actor::Human).is_err(),
        "not RETIRED"
    );
    assert!(
        m.retire("B", 1, "x", Actor::Human).is_err(),
        "unknown alpha"
    );
    for from in LifecycleState::ALL
        .iter()
        .filter(|s| **s != LifecycleState::Retired)
    {
        let e = lifecycle::edge_for(*from, LifecycleState::Retired, EdgeKind::Manual)
            .expect("manual edge");
        assert_eq!(e.actor, Actor::Human);
        assert!(e.gates.is_empty());
    }
    let t = m
        .retire("A", 2, "desk decision", Actor::Human)
        .expect("retire");
    assert_eq!(
        (t.from_state, t.to_state),
        (LifecycleState::Research, LifecycleState::Retired)
    );
    assert_eq!(t.policy, "lifecycle_v1");
    // The transition log holds canonical lines that parse back.
    let mut log = TransitionLog::new(Vec::new());
    log.append(&t).expect("append");
    let text = String::from_utf8(log.into_inner()).expect("ascii");
    assert!(text.starts_with("{\"actor\":\"HUMAN\",\"alpha_id\":\"A\","));
    assert_eq!(
        TransitionLog::<Vec<u8>>::read_all(&text).expect("parses"),
        vec![t]
    );
}

/// A random evidence document from the pinned RNG. The bootstrap and the
/// cross-alpha blocks are constants (a passing interval, no peers), so the
/// walk draws exactly the numbers it always did.
fn random_evidence(rng: &mut SplitMix64, alpha: &str) -> Evidence {
    let mut ev = Evidence::empty();
    ev.pnl_bootstrap = Some(passing_bootstrap());
    ev.cross_alpha = Some(no_peers());
    if rng.uniform() < 0.6 {
        let leakage = rng.uniform() < 0.9;
        let hypothesis = if rng.uniform() < 0.8 {
            Some(true)
        } else {
            None
        };
        let ic = rng.uniform() * 0.04 - 0.005;
        let rank_ic = ic * (0.5 + rng.uniform());
        let t_stat = rng.uniform() * 6.0;
        let net = rng.uniform() * 10.0 - 2.0;
        ev.research = Some(research(
            alpha, ic, rank_ic, t_stat, leakage, hypothesis, net,
        ));
    }
    if rng.uniform() < 0.7 {
        ev.capacity_usd = Some(rng.uniform() * 3e6);
    }
    if rng.uniform() < 0.7 {
        ev.significance_threshold = Some(2.5 + rng.uniform() * 2.0);
    }
    if rng.uniform() < 0.5 {
        ev.validation = Some(ValidationEvidence {
            holdout_ic: 0.02 + rng.uniform() * 0.02 - 0.01,
            research_ic: 0.02,
            replay_hash_match: rng.uniform() < 0.8,
            parity: rng.uniform() < 0.8,
        });
    }
    if rng.uniform() < 0.5 {
        ev.paper = Some(PaperEvidence {
            n_sessions: rng.below(8).expect("n > 0") as u64,
            realized_ic: 0.02 + rng.uniform() * 0.02 - 0.01,
            research_ic: 0.02,
            net_pnl: rng.uniform() * 200.0 - 50.0,
            n_kill_events: if rng.uniform() < 0.8 { 0 } else { 1 },
            tracking_error: rng.uniform(),
        });
    }
    if rng.uniform() < 0.8 {
        let ic = if rng.uniform() < 0.15 {
            None
        } else {
            Some(rng.uniform() * 0.04 - 0.02)
        };
        ev.live = Some(LiveEvidence {
            rolling_ic: ic,
            n_buckets: 8,
            eval_index: 1,
            informative: rng.uniform() < 0.9,
            new_fraction: if rng.uniform() < 0.8 { 0.125 } else { 1.0 },
        });
    }
    ev
}

fn run_property(seed: u64, cfg: PolicyConfig) -> Vec<lifecycle::LifecycleTransition> {
    let mut rng = SplitMix64::new(seed);
    let alphas = ["P1", "P2", "P3"];
    let reg = AlphaRegistry::new(&cfg.policy).expect("policy");
    let mut m = AlphaLifecycle::new(cfg, reg).expect("machine");
    for a in alphas {
        m.register(a, 0).expect("register");
    }
    let mut retired_by_system = 0u32;
    for step in 1..=3000i64 {
        let alpha = alphas[rng.below(3).expect("n > 0") as usize];
        let before = m.state(alpha).expect("known");
        let roll = rng.uniform();
        if roll < 0.9 {
            let ev = random_evidence(&mut rng, alpha);
            let t = m
                .advance(alpha, step, &ev)
                .expect("advance never fails on valid evidence");
            let after = m.state(alpha).expect("known");
            if before == LifecycleState::Retired {
                assert!(t.is_none(), "SYSTEM moved a RETIRED alpha");
                assert_eq!(after, LifecycleState::Retired);
                assert_eq!(
                    m.evaluations().last().expect("recorded").outcome,
                    Outcome::Terminal
                );
            }
            assert!(
                after.index() <= before.index() + 1,
                "advanced more than one step: {} -> {}",
                before.name(),
                after.name()
            );
            if let Some(t) = &t {
                assert_eq!(t.actor, Actor::System);
                assert!(ALLOWED_TRANSITIONS
                    .iter()
                    .any(|e| e.from_state == t.from_state
                        && e.to_state == t.to_state
                        && e.actor == Actor::System));
                if t.to_state == LifecycleState::Retired {
                    retired_by_system += 1;
                    assert_eq!(t.from_state, LifecycleState::Watch);
                }
            } else {
                assert!(after == before, "no transition but the state changed");
            }
        } else if roll < 0.95 {
            match m.retire(alpha, step, "property", Actor::Human) {
                Ok(t) => {
                    assert_ne!(before, LifecycleState::Retired);
                    assert_eq!(t.to_state, LifecycleState::Retired);
                }
                Err(_) => assert_eq!(before, LifecycleState::Retired),
            }
            assert_eq!(m.state(alpha).expect("known"), LifecycleState::Retired);
        } else {
            match m.reset_to_research(alpha, step, "property", Actor::Human) {
                Ok(_) => {
                    assert_eq!(before, LifecycleState::Retired);
                    assert_eq!(m.state(alpha).expect("known"), LifecycleState::Research);
                }
                Err(_) => {
                    assert_ne!(before, LifecycleState::Retired);
                    assert_eq!(m.state(alpha).expect("known"), before);
                }
            }
        }
        // Counters are always consistent with the state.
        let rec = m.record(alpha).expect("rec");
        if !rec.state.is_live() {
            assert_eq!((rec.breach_count, rec.recovery_count), (0, 0));
            assert_eq!(rec.cusum, 0.0);
        }
        assert!(rec.cusum.is_finite() && rec.cusum >= 0.0);
        if !matches!(
            rec.state,
            LifecycleState::Validating | LifecycleState::Paper
        ) {
            assert_eq!(rec.consecutive_failures, 0);
        }
    }
    // Replaying the transition log from RESEARCH reproduces every state.
    let mut replay: BTreeMap<&str, LifecycleState> = alphas
        .iter()
        .map(|a| (*a, LifecycleState::Research))
        .collect();
    for t in m.transitions() {
        let cur = replay.get_mut(t.alpha_id.as_str()).expect("known alpha");
        assert_eq!(*cur, t.from_state);
        *cur = t.to_state;
    }
    for a in alphas {
        assert_eq!(replay[a], m.state(a).expect("known"));
    }
    assert!(
        m.transitions().len() > 20,
        "the walk exercised the machine ({} transitions)",
        m.transitions().len()
    );
    assert!(
        retired_by_system > 0
            || m.transitions()
                .iter()
                .any(|t| t.to_state == LifecycleState::Watch)
    );
    m.transitions().to_vec()
}

#[test]
fn property_system_never_moves_retired_and_never_skips_states() {
    for cfg in [default_config(), legacy_config()] {
        let a = run_property(0x5EED_2026_0919, cfg.clone());
        let b = run_property(0x5EED_2026_0919, cfg.clone());
        assert_eq!(a, b, "same seed, same transitions");
        let c = run_property(7, cfg);
        assert_ne!(a, c, "a different seed walks differently");
    }
}

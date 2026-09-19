//! Rule-level tests of the lifecycle machine (edges, gates, actors) and a
//! SplitMix64-driven property test: the system never moves a RETIRED alpha,
//! an evaluation never advances more than one state, only a HUMAN retires
//! or resets, and the same seed replays to the same transitions.

use std::collections::BTreeMap;

use lifecycle::{
    Actor, AlphaLifecycle, AlphaRegistry, EdgeKind, Evidence, ExperimentResult, LifecycleState,
    LiveEvidence, Outcome, PaperEvidence, PolicyConfig, TransitionLog, ValidationEvidence, Verdict,
    ALLOWED_TRANSITIONS,
};
use marketdata::SplitMix64;
use serde_json::json;

fn config() -> PolicyConfig {
    PolicyConfig::from_value(&json!({
        "policy": "lifecycle_v1",
        "gates": {
            "min_experiments_in_ledger": 1, "min_oos_ic": 0.01, "min_nw_tstat": 3.0,
            "min_fold_sign_consistency": 0.7, "min_folds": 3, "min_net_return_bps": 0.0,
            "min_capacity_usd": 1000000.0, "max_ic_rank_gap": 1.0, "ic_rank_gap_eps": 1e-12,
            "max_holdout_ic_gap": 0.01, "min_paper_sessions": 5, "max_paper_ic_gap": 0.01,
            "min_paper_net_pnl": 0.0, "max_kill_events": 0
        },
        "demotion": {"max_consecutive_failures": 3},
        "live": {"watch_ic_gate": 0.0, "reactivate_ic_gate": 0.005, "retire_breach_evals": 6, "reactivate_evals": 3}
    }))
    .expect("valid config")
}

fn machine(alpha: &str) -> AlphaLifecycle {
    let cfg = config();
    let reg = AlphaRegistry::new(&cfg.policy).expect("policy");
    let mut m = AlphaLifecycle::new(cfg, reg).expect("machine");
    m.register(alpha, 0).expect("register");
    m
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

fn ev_research(r: ExperimentResult, capacity: Option<f64>) -> Evidence {
    Evidence {
        research: Some(r),
        capacity_usd: capacity,
        validation: None,
        paper: None,
        live: None,
    }
}

fn ev_validation(parity: bool) -> Evidence {
    Evidence {
        research: None,
        capacity_usd: None,
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

fn ev_live(ic: Option<f64>, informative: bool) -> Evidence {
    Evidence {
        research: None,
        capacity_usd: None,
        validation: None,
        paper: None,
        live: Some(LiveEvidence {
            rolling_ic: ic,
            n_buckets: 8,
            eval_index: 1,
            informative,
        }),
    }
}

/// Walk an alpha to ACTIVE.
fn to_active(m: &mut AlphaLifecycle, alpha: &str) {
    m.advance(alpha, 1, &ev_research(good(alpha), None))
        .expect("ok");
    m.advance(alpha, 2, &ev_research(good(alpha), Some(5e6)))
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
    // Leakage demotes at once and carries all nine gate results.
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
    assert_eq!(t.gates.len(), 9);
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
fn live_edges_and_terminal_retirement() {
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

/// A random evidence document from the pinned RNG.
fn random_evidence(rng: &mut SplitMix64, alpha: &str) -> Evidence {
    let mut ev = Evidence::empty();
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
        });
    }
    ev
}

fn run_property(seed: u64) -> Vec<lifecycle::LifecycleTransition> {
    let mut rng = SplitMix64::new(seed);
    let alphas = ["P1", "P2", "P3"];
    let cfg = config();
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
        }
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
    let a = run_property(0x5EED_2026_0919);
    let b = run_property(0x5EED_2026_0919);
    assert_eq!(a, b, "same seed, same transitions");
    let c = run_property(7);
    assert_ne!(a, c, "a different seed walks differently");
}

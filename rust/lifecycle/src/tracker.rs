//! The live ACTIVE / WATCH / RETIRED sub-machine — an exact port of
//! `iap.adaptive.lifecycle.LifecycleTracker` (Java `LifecycleGauge`), rules
//! pinned in API_ADAPTIVE.md §6:
//!
//! - **ACTIVE**: `rolling_ic < watch_ic_gate` → WATCH (the entering breach
//!   counts as breach #1).
//! - **WATCH**: `retire_breach_evals` consecutive breaches → RETIRED;
//!   `reactivate_evals` consecutive `rolling_ic >= reactivate_ic_gate` → ACTIVE;
//!   the neutral zone `[watch, reactivate)` resets BOTH counters (hysteresis).
//! - **RETIRED**: `reactivate_evals` consecutive recoveries → WATCH (kept for
//!   parity with the adaptive study; the platform machine never calls the
//!   tracker while RETIRED — retirement is terminal for the system).
//! - `rolling_ic == None` or `informative == false`: no evidence, no
//!   movement, counters unchanged (only `eval_index` advances).
//!
//! Comparisons are strict `<` for breach and inclusive `>=` for recovery.
//! Reason texts are byte-identical to Python's (`{ic:.6f}` for the IC, the
//! gate printed like `repr(float)`).

use contracts::format_float;
use marketdata::IapError;

use crate::gates::LiveConfig;
use crate::state::LifecycleState;

/// One tracker transition (`research/lifecycle_log.jsonl` record shape).
#[derive(Debug, Clone, PartialEq)]
pub struct TrackerTransition {
    /// Alpha.
    pub alpha_id: String,
    /// Policy name.
    pub policy: String,
    /// Event time, ns.
    pub event_ts: i64,
    /// State before.
    pub from_state: LifecycleState,
    /// State after.
    pub to_state: LifecycleState,
    /// Reason text (pinned wording).
    pub reason: String,
    /// The rolling IC that decided it.
    pub rolling_ic: Option<f64>,
    /// Evaluation index at the transition.
    pub eval_index: u64,
}

/// Per-alpha live tracker.
#[derive(Debug, Clone, PartialEq)]
pub struct LiveTracker {
    alpha_id: String,
    config: LiveConfig,
    policy: String,
    state: LifecycleState,
    breach_count: u64,
    recovery_count: u64,
    eval_index: u64,
    transitions: Vec<TrackerTransition>,
}

impl LiveTracker {
    /// A tracker resuming at `state` (ACTIVE / WATCH / RETIRED) with the
    /// given counters.
    pub fn new(
        alpha_id: &str,
        config: LiveConfig,
        policy: &str,
        state: LifecycleState,
        breach_count: u64,
        recovery_count: u64,
    ) -> Result<LiveTracker, IapError> {
        if !matches!(
            state,
            LifecycleState::Active | LifecycleState::Watch | LifecycleState::Retired
        ) {
            return Err(IapError::InvalidArgument(format!(
                "LiveTracker: {} is not a live state",
                state.name()
            )));
        }
        Ok(LiveTracker {
            alpha_id: alpha_id.to_string(),
            config,
            policy: policy.to_string(),
            state,
            breach_count,
            recovery_count,
            eval_index: 0,
            transitions: Vec::new(),
        })
    }

    /// Current state.
    pub fn state(&self) -> LifecycleState {
        self.state
    }

    /// Consecutive breaches counted in WATCH.
    pub fn breach_count(&self) -> u64 {
        self.breach_count
    }

    /// Consecutive recoveries counted in WATCH / RETIRED.
    pub fn recovery_count(&self) -> u64 {
        self.recovery_count
    }

    /// Evaluations seen (including uninformative ones).
    pub fn eval_index(&self) -> u64 {
        self.eval_index
    }

    /// Policy name.
    pub fn policy(&self) -> &str {
        &self.policy
    }

    /// Transitions made by this tracker, in order.
    pub fn transitions(&self) -> &[TrackerTransition] {
        &self.transitions
    }

    /// Retirement halts allocation; ACTIVE and WATCH trade.
    pub fn allocatable(&self) -> bool {
        self.state != LifecycleState::Retired
    }

    fn transition(
        &mut self,
        to_state: LifecycleState,
        ts: i64,
        reason: String,
        rolling_ic: Option<f64>,
    ) {
        self.transitions.push(TrackerTransition {
            alpha_id: self.alpha_id.clone(),
            policy: self.policy.clone(),
            event_ts: ts,
            from_state: self.state,
            to_state,
            reason,
            rolling_ic,
            eval_index: self.eval_index,
        });
        self.state = to_state;
        self.breach_count = 0;
        self.recovery_count = 0;
    }

    fn gate_text(gate: f64) -> String {
        // The gates are finite by construction (config validation).
        format_float(gate).unwrap_or_else(|_| gate.to_string())
    }

    /// One evaluation at event time `ts`; returns the (possibly new) state.
    pub fn update(
        &mut self,
        ts: i64,
        rolling_ic: Option<f64>,
        informative: bool,
    ) -> LifecycleState {
        self.eval_index += 1;
        let ic = match rolling_ic {
            Some(ic) if informative => ic,
            _ => return self.state, // no evidence, no movement (pinned)
        };
        let cfg = self.config.clone();
        let breach = ic < cfg.watch_ic_gate;
        let recover = ic >= cfg.reactivate_ic_gate;

        match self.state {
            LifecycleState::Active => {
                if breach {
                    self.transition(
                        LifecycleState::Watch,
                        ts,
                        format!(
                            "rolling_ic {ic:.6} < watch gate {}",
                            Self::gate_text(cfg.watch_ic_gate)
                        ),
                        Some(ic),
                    );
                    self.breach_count = 1; // the entering breach counts (pinned)
                }
            }
            LifecycleState::Watch => {
                if breach {
                    self.breach_count += 1;
                    self.recovery_count = 0;
                    if self.breach_count >= cfg.retire_breach_evals {
                        self.transition(
                            LifecycleState::Retired,
                            ts,
                            format!(
                                "persistent breach: {} consecutive evals below watch gate {}",
                                cfg.retire_breach_evals,
                                Self::gate_text(cfg.watch_ic_gate)
                            ),
                            Some(ic),
                        );
                    }
                } else if recover {
                    self.recovery_count += 1;
                    self.breach_count = 0;
                    if self.recovery_count >= cfg.reactivate_evals {
                        self.transition(
                            LifecycleState::Active,
                            ts,
                            format!(
                                "re-activation: {} consecutive evals >= reactivate gate {}",
                                cfg.reactivate_evals,
                                Self::gate_text(cfg.reactivate_ic_gate)
                            ),
                            Some(ic),
                        );
                    }
                } else {
                    self.breach_count = 0;
                    self.recovery_count = 0;
                }
            }
            LifecycleState::Retired => {
                if recover {
                    self.recovery_count += 1;
                    if self.recovery_count >= cfg.reactivate_evals {
                        self.transition(
                            LifecycleState::Watch,
                            ts,
                            format!(
                                "recovery from retirement: {} consecutive evals >= reactivate gate {}; probation before ACTIVE",
                                cfg.reactivate_evals,
                                Self::gate_text(cfg.reactivate_ic_gate)
                            ),
                            Some(ic),
                        );
                    }
                } else {
                    self.recovery_count = 0;
                }
            }
            LifecycleState::Research
            | LifecycleState::Candidate
            | LifecycleState::Validating
            | LifecycleState::Paper => {
                // Unreachable by construction (`new` rejects them); a
                // non-live state moves nothing.
            }
        }
        self.state
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> LiveConfig {
        LiveConfig {
            watch_ic_gate: 0.0,
            reactivate_ic_gate: 0.005,
            retire_breach_evals: 6,
            reactivate_evals: 3,
        }
    }

    fn tracker() -> LiveTracker {
        LiveTracker::new("A", cfg(), "lifecycle_v1", LifecycleState::Active, 0, 0)
            .expect("live state")
    }

    #[test]
    fn breach_watch_retire_with_hysteresis() {
        let mut t = tracker();
        assert_eq!(t.update(1, Some(0.02), true), LifecycleState::Active);
        assert_eq!(t.update(2, None, true), LifecycleState::Active);
        assert_eq!(t.update(3, Some(-0.5), false), LifecycleState::Active);
        assert_eq!(t.update(4, Some(-0.01), true), LifecycleState::Watch);
        assert_eq!(t.breach_count(), 1);
        assert_eq!(
            t.transitions()[0].reason,
            "rolling_ic -0.010000 < watch gate 0.0"
        );
        assert_eq!(t.update(5, Some(-0.01), true), LifecycleState::Watch);
        assert_eq!(t.breach_count(), 2);
        assert_eq!(t.update(6, Some(0.001), true), LifecycleState::Watch);
        assert_eq!((t.breach_count(), t.recovery_count()), (0, 0));
        for k in 0..2 {
            assert_eq!(t.update(7 + k, Some(0.005), true), LifecycleState::Watch);
        }
        assert_eq!(t.recovery_count(), 2);
        assert_eq!(t.update(9, Some(0.01), true), LifecycleState::Active);
        assert_eq!(
            t.transitions()[1].reason,
            "re-activation: 3 consecutive evals >= reactivate gate 0.005"
        );
        assert_eq!(t.update(10, Some(-0.02), true), LifecycleState::Watch);
        for k in 0..4 {
            assert_eq!(t.update(11 + k, Some(-0.02), true), LifecycleState::Watch);
        }
        assert_eq!(t.breach_count(), 5);
        assert_eq!(t.update(15, Some(-0.02), true), LifecycleState::Retired);
        assert_eq!(
            t.transitions()[3].reason,
            "persistent breach: 6 consecutive evals below watch gate 0.0"
        );
        assert!(!t.allocatable());
        assert_eq!(t.eval_index(), 15);
    }

    #[test]
    fn retired_recovers_to_watch_only() {
        let mut t = LiveTracker::new("A", cfg(), "p", LifecycleState::Retired, 0, 0).expect("live");
        for k in 0..2 {
            assert_eq!(t.update(k, Some(0.01), true), LifecycleState::Retired);
        }
        assert_eq!(t.update(2, Some(0.001), true), LifecycleState::Retired);
        assert_eq!(t.recovery_count(), 0);
        for k in 3..5 {
            assert_eq!(t.update(k, Some(0.01), true), LifecycleState::Retired);
        }
        assert_eq!(t.update(5, Some(0.01), true), LifecycleState::Watch);
        assert!(LiveTracker::new("A", cfg(), "p", LifecycleState::Paper, 0, 0).is_err());
    }
}

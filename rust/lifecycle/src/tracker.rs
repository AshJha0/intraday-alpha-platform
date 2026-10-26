//! The live ACTIVE / WATCH / RETIRED sub-machine — an exact port of
//! `iap.adaptive.lifecycle.LifecycleTracker` (Java `LifecycleGauge`), rules
//! pinned in API_ADAPTIVE.md §6:
//!
//! - **ACTIVE**: `rolling_ic < watch_ic_gate` → WATCH.
//! - **WATCH**: a persistent breach → RETIRED, by the configured retirement
//!   rule (below); `reactivate_evals` consecutive
//!   `rolling_ic >= reactivate_ic_gate` → ACTIVE.
//! - **RETIRED**: `reactivate_evals` consecutive recoveries → WATCH (kept for
//!   parity with the adaptive study; the platform machine never calls the
//!   tracker while RETIRED — retirement is terminal for the system).
//! - `rolling_ic == None` or `informative == false`: no evidence, no
//!   movement, counters and statistic unchanged (only `eval_index` advances).
//!
//! **Retirement rule** ([`BreachRule`], named by the config).
//!
//! - [`BreachRule::Cusum`] — the default since v1.5.0. Successive readings
//!   share most of their window, so each one is weighted by its share of new
//!   information:
//!
//!   ```text
//!   s = S + new_fraction * (watch_ic_gate - rolling_ic - cusum_k)
//!   S = if s > 0 { s } else { 0 }
//!   ```
//!
//!   `S` accumulates in ACTIVE and WATCH alike. In WATCH a reading that is
//!   itself a breach and leaves `S >= cusum_h` retires the alpha — never the
//!   reading that entered WATCH. `S` is kept across ACTIVE → WATCH and reset
//!   by every other transition. A reading that is not a recovery resets the
//!   recovery count; `breach_count` is unused and stays 0. The statistic is
//!   computed with exactly the two expressions above (one multiplication,
//!   left-to-right subtraction, an explicit comparison), as in Python and
//!   Java, so it is bit-identical across the three.
//! - [`BreachRule::Consecutive`] — the legacy rule, the default up to
//!   v1.4.0: `retire_breach_evals` consecutive breaches retire (the breach
//!   entering WATCH counts as #1) and the neutral zone `[watch, reactivate)`
//!   resets BOTH counters (hysteresis). `new_fraction` is not read.
//!
//! Comparisons are strict `<` for breach and inclusive `>=` for recovery.
//! Reason texts are byte-identical to Python's (`{ic:.6f}` for the IC, the
//! gate printed like `repr(float)`).

use contracts::format_float;
use marketdata::IapError;

use crate::gates::{BreachRule, LiveConfig};
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
    cusum: f64,
    eval_index: u64,
    transitions: Vec<TrackerTransition>,
}

impl LiveTracker {
    /// A tracker resuming at `state` (ACTIVE / WATCH / RETIRED) with the
    /// given counters and CUSUM statistic (0.0 under the consecutive rule).
    pub fn new(
        alpha_id: &str,
        config: LiveConfig,
        policy: &str,
        state: LifecycleState,
        breach_count: u64,
        recovery_count: u64,
        cusum: f64,
    ) -> Result<LiveTracker, IapError> {
        if !(cusum.is_finite() && cusum >= 0.0) {
            return Err(IapError::InvalidArgument(
                "LiveTracker: cusum must be a finite number >= 0".to_string(),
            ));
        }
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
            cusum,
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

    /// The CUSUM statistic `S` (0.0 under the consecutive rule).
    pub fn cusum(&self) -> f64 {
        self.cusum
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
        let from_state = self.state;
        self.state = to_state;
        self.breach_count = 0;
        self.recovery_count = 0;
        if to_state != LifecycleState::Watch || from_state == LifecycleState::Retired {
            self.cusum = 0.0; // a verdict was reached; evidence starts over
        }
    }

    fn gate_text(gate: f64) -> String {
        // The gates are finite by construction (config validation).
        format_float(gate).unwrap_or_else(|_| gate.to_string())
    }

    /// One evaluation at event time `ts`; returns the (possibly new) state.
    ///
    /// `new_fraction` is the share of this reading's window that is new
    /// since the last counted one; the CUSUM rule weights the reading by it
    /// (and rejects a value outside `(0, 1]`), the consecutive rule does not
    /// read it.
    pub fn update(
        &mut self,
        ts: i64,
        rolling_ic: Option<f64>,
        informative: bool,
        new_fraction: f64,
    ) -> Result<LifecycleState, IapError> {
        self.eval_index += 1;
        let ic = match rolling_ic {
            Some(ic) if informative => ic,
            _ => return Ok(self.state), // no evidence, no movement (pinned)
        };
        let cfg = self.config.clone();
        let breach = ic < cfg.watch_ic_gate;
        let recover = ic >= cfg.reactivate_ic_gate;
        if cfg.breach_rule == BreachRule::Cusum {
            return self.update_cusum(ts, ic, breach, recover, new_fraction);
        }

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
        Ok(self.state)
    }

    /// The CUSUM retirement rule (module docs).
    fn update_cusum(
        &mut self,
        ts: i64,
        ic: f64,
        breach: bool,
        recover: bool,
        new_fraction: f64,
    ) -> Result<LifecycleState, IapError> {
        if !(new_fraction > 0.0 && new_fraction <= 1.0) {
            return Err(IapError::InvalidArgument(
                "new_fraction must be in (0, 1]".to_string(),
            ));
        }
        let cfg = self.config.clone();
        if self.state != LifecycleState::Retired {
            // Exactly these two expressions in every port (module docs).
            let s = self.cusum + new_fraction * (cfg.watch_ic_gate - ic - cfg.cusum_k);
            self.cusum = if s > 0.0 { s } else { 0.0 };
        }

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
                }
            }
            LifecycleState::Watch => {
                if breach && self.cusum >= cfg.cusum_h {
                    let stat = self.cusum;
                    self.transition(
                        LifecycleState::Retired,
                        ts,
                        format!(
                            "persistent breach: CUSUM {stat:.6} >= {} (slack {}) below watch gate {}",
                            Self::gate_text(cfg.cusum_h),
                            Self::gate_text(cfg.cusum_k),
                            Self::gate_text(cfg.watch_ic_gate)
                        ),
                        Some(ic),
                    );
                } else if recover {
                    self.recovery_count += 1;
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
        Ok(self.state)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The pinned gates under the LEGACY consecutive rule (up to v1.4.0).
    fn cfg() -> LiveConfig {
        LiveConfig::legacy_consecutive(0.0, 0.005, 6, 3)
    }

    /// The pinned gates under the default CUSUM rule (strategies.json).
    fn cusum_cfg() -> LiveConfig {
        LiveConfig {
            breach_rule: BreachRule::Cusum,
            cusum_k: 0.0025,
            cusum_h: 0.01,
            ..cfg()
        }
    }

    fn tracker() -> LiveTracker {
        LiveTracker::new(
            "A",
            cfg(),
            "lifecycle_v1",
            LifecycleState::Active,
            0,
            0,
            0.0,
        )
        .expect("live state")
    }

    fn cusum_tracker() -> LiveTracker {
        LiveTracker::new(
            "A",
            cusum_cfg(),
            "lifecycle_v1",
            LifecycleState::Active,
            0,
            0,
            0.0,
        )
        .expect("live state")
    }

    /// One reading; the fraction is valid in every call of these tests.
    fn step(t: &mut LiveTracker, ts: i64, ic: Option<f64>, informative: bool) -> LifecycleState {
        t.update(ts, ic, informative, 0.125).expect("valid reading")
    }

    #[test]
    fn legacy_consecutive_breach_watch_retire_with_hysteresis() {
        let mut t = tracker();
        assert_eq!(step(&mut t, 1, Some(0.02), true), LifecycleState::Active);
        assert_eq!(step(&mut t, 2, None, true), LifecycleState::Active);
        assert_eq!(step(&mut t, 3, Some(-0.5), false), LifecycleState::Active);
        assert_eq!(step(&mut t, 4, Some(-0.01), true), LifecycleState::Watch);
        assert_eq!(t.breach_count(), 1);
        assert_eq!(
            t.transitions()[0].reason,
            "rolling_ic -0.010000 < watch gate 0.0"
        );
        assert_eq!(step(&mut t, 5, Some(-0.01), true), LifecycleState::Watch);
        assert_eq!(t.breach_count(), 2);
        assert_eq!(step(&mut t, 6, Some(0.001), true), LifecycleState::Watch);
        assert_eq!((t.breach_count(), t.recovery_count()), (0, 0));
        for k in 0..2 {
            assert_eq!(step(&mut t, 7 + k, Some(0.005), true), LifecycleState::Watch);
        }
        assert_eq!(t.recovery_count(), 2);
        assert_eq!(step(&mut t, 9, Some(0.01), true), LifecycleState::Active);
        assert_eq!(
            t.transitions()[1].reason,
            "re-activation: 3 consecutive evals >= reactivate gate 0.005"
        );
        assert_eq!(step(&mut t, 10, Some(-0.02), true), LifecycleState::Watch);
        for k in 0..4 {
            assert_eq!(
                step(&mut t, 11 + k, Some(-0.02), true),
                LifecycleState::Watch
            );
        }
        assert_eq!(t.breach_count(), 5);
        assert_eq!(step(&mut t, 15, Some(-0.02), true), LifecycleState::Retired);
        assert_eq!(
            t.transitions()[3].reason,
            "persistent breach: 6 consecutive evals below watch gate 0.0"
        );
        assert!(!t.allocatable());
        assert_eq!(t.eval_index(), 15);
        // the legacy rule keeps no statistic and does not read the fraction
        assert_eq!(t.cusum(), 0.0);
        let mut u = tracker();
        assert_eq!(
            u.update(1, Some(-0.01), true, 7.0).expect("fraction unread"),
            LifecycleState::Watch
        );
    }

    #[test]
    fn retired_recovers_to_watch_only() {
        for config in [cfg(), cusum_cfg()] {
            let mut t = LiveTracker::new("A", config.clone(), "p", LifecycleState::Retired, 0, 0, 0.0)
                .expect("live");
            for k in 0..2 {
                assert_eq!(step(&mut t, k, Some(0.01), true), LifecycleState::Retired);
            }
            assert_eq!(step(&mut t, 2, Some(0.001), true), LifecycleState::Retired);
            assert_eq!(t.recovery_count(), 0);
            for k in 3..5 {
                assert_eq!(step(&mut t, k, Some(0.01), true), LifecycleState::Retired);
            }
            assert_eq!(step(&mut t, 5, Some(0.01), true), LifecycleState::Watch);
            assert_eq!(t.cusum(), 0.0, "nothing accumulates while RETIRED");
            assert!(
                LiveTracker::new("A", config.clone(), "p", LifecycleState::Paper, 0, 0, 0.0)
                    .is_err()
            );
            assert!(
                LiveTracker::new("A", config, "p", LifecycleState::Watch, 0, 0, f64::NAN).is_err()
            );
        }
    }

    #[test]
    fn cusum_weights_a_reading_by_its_new_information() {
        // the same four breaches: overlapping windows (1/8 new) do not
        // retire, disjoint windows (all new) do on the second
        let mut overlapping = cusum_tracker();
        let mut disjoint = cusum_tracker();
        for k in 0..4 {
            overlapping
                .update(k, Some(-0.01), true, 0.125)
                .expect("valid");
        }
        assert_eq!(overlapping.state(), LifecycleState::Watch);
        assert!((overlapping.cusum() - 4.0 * 0.125 * 0.0075).abs() < 1e-15);
        assert_eq!(overlapping.breach_count(), 0, "unused under the CUSUM rule");
        assert_eq!(
            disjoint.update(1, Some(-0.01), true, 1.0).expect("valid"),
            LifecycleState::Watch
        );
        assert_eq!(
            disjoint.update(2, Some(-0.01), true, 1.0).expect("valid"),
            LifecycleState::Retired
        );
        assert!(!disjoint.allocatable());
        assert_eq!(
            disjoint.transitions()[1].reason,
            "persistent breach: CUSUM 0.015000 >= 0.01 (slack 0.0025) below watch gate 0.0"
        );
        for bad in [0.0, 1.5, -0.25, f64::NAN] {
            assert!(
                cusum_tracker().update(1, Some(-0.01), true, bad).is_err(),
                "new_fraction {bad}"
            );
        }
        // a reading that carries no evidence is not validated either
        assert!(cusum_tracker().update(1, None, true, 0.0).is_ok());
    }

    #[test]
    fn cusum_retires_only_on_a_breach_and_never_on_the_entering_reading() {
        let mut t = cusum_tracker();
        // one reading takes S past the threshold: WATCH, not RETIRED
        assert_eq!(step(&mut t, 1, Some(-0.09), true), LifecycleState::Watch);
        assert_eq!(t.cusum(), 0.0109375);
        // above the gate with S still over the threshold: no retirement
        assert_eq!(step(&mut t, 2, Some(0.004), true), LifecycleState::Watch);
        assert!(t.cusum() >= 0.01);
        // the next breach retires and resets S
        assert_eq!(step(&mut t, 3, Some(-0.003), true), LifecycleState::Retired);
        assert_eq!((t.cusum(), t.breach_count()), (0.0, 0));

        // None and uninformative readings move nothing, S included
        let mut h = cusum_tracker();
        step(&mut h, 1, Some(-0.02), true);
        let before = h.cusum();
        assert_eq!(step(&mut h, 2, None, true), LifecycleState::Watch);
        assert_eq!(step(&mut h, 3, Some(-0.5), false), LifecycleState::Watch);
        assert_eq!(h.cusum(), before);
        // recovery drains S and three in a row re-activate, resetting it
        step(&mut h, 4, Some(0.01), true);
        step(&mut h, 5, Some(0.01), true);
        assert_eq!(step(&mut h, 6, Some(0.01), true), LifecycleState::Active);
        assert_eq!(h.cusum(), 0.0);

        // a restored tracker continues from the persisted statistic
        let mut r = LiveTracker::new("A", cusum_cfg(), "p", LifecycleState::Watch, 0, 0, 0.009)
            .expect("live");
        assert_eq!(step(&mut r, 1, Some(-0.02), true), LifecycleState::Retired);
        assert_eq!(
            r.transitions()[0].reason,
            "persistent breach: CUSUM 0.011187 >= 0.01 (slack 0.0025) below watch gate 0.0"
        );
    }
}

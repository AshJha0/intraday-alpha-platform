//! The seven lifecycle states (`iap.contracts.types.LifecycleState`,
//! `schemas/alpha/lifecycle_transition.schema.json`): ordered integers 0..6,
//! serialised by name.

use marketdata::IapError;
use serde::{Deserialize, Serialize};

/// Alpha lifecycle state (integer id = promotion order).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum LifecycleState {
    /// 0 — hypothesis under research.
    #[serde(rename = "RESEARCH")]
    Research,
    /// 1 — a research result exists and is leakage-clean.
    #[serde(rename = "CANDIDATE")]
    Candidate,
    /// 2 — passed the research gates; held-out replay in progress.
    #[serde(rename = "VALIDATING")]
    Validating,
    /// 3 — trading the paper environment.
    #[serde(rename = "PAPER")]
    Paper,
    /// 4 — allocated.
    #[serde(rename = "ACTIVE")]
    Active,
    /// 5 — allocated, on probation.
    #[serde(rename = "WATCH")]
    Watch,
    /// 6 — allocation halted (terminal for the system).
    #[serde(rename = "RETIRED")]
    Retired,
}

impl LifecycleState {
    /// Every state in id order.
    pub const ALL: [LifecycleState; 7] = [
        LifecycleState::Research,
        LifecycleState::Candidate,
        LifecycleState::Validating,
        LifecycleState::Paper,
        LifecycleState::Active,
        LifecycleState::Watch,
        LifecycleState::Retired,
    ];

    /// Number of states.
    pub const COUNT: usize = 7;

    /// Wire name.
    pub fn name(self) -> &'static str {
        match self {
            LifecycleState::Research => "RESEARCH",
            LifecycleState::Candidate => "CANDIDATE",
            LifecycleState::Validating => "VALIDATING",
            LifecycleState::Paper => "PAPER",
            LifecycleState::Active => "ACTIVE",
            LifecycleState::Watch => "WATCH",
            LifecycleState::Retired => "RETIRED",
        }
    }

    /// Integer id (0..6).
    pub fn index(self) -> u8 {
        match self {
            LifecycleState::Research => 0,
            LifecycleState::Candidate => 1,
            LifecycleState::Validating => 2,
            LifecycleState::Paper => 3,
            LifecycleState::Active => 4,
            LifecycleState::Watch => 5,
            LifecycleState::Retired => 6,
        }
    }

    /// Parse a wire name.
    pub fn from_name(name: &str) -> Result<LifecycleState, IapError> {
        LifecycleState::ALL
            .into_iter()
            .find(|s| s.name() == name)
            .ok_or_else(|| IapError::InvalidArgument(format!("unknown lifecycle state {name:?}")))
    }

    /// Parse an integer id.
    pub fn from_index(index: u8) -> Result<LifecycleState, IapError> {
        LifecycleState::ALL
            .get(index as usize)
            .copied()
            .ok_or_else(|| {
                IapError::InvalidArgument(format!("lifecycle state index {index} out of range"))
            })
    }

    /// True for the live sub-machine states ACTIVE / WATCH (RETIRED is the
    /// tracker's third state but terminal on the platform).
    pub fn is_live(self) -> bool {
        matches!(self, LifecycleState::Active | LifecycleState::Watch)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_indices_round_trip() {
        for (i, s) in LifecycleState::ALL.into_iter().enumerate() {
            assert_eq!(s.index() as usize, i);
            assert_eq!(LifecycleState::from_index(i as u8).expect("in range"), s);
            assert_eq!(LifecycleState::from_name(s.name()).expect("known"), s);
            let json = serde_json::to_string(&s).expect("serialises");
            assert_eq!(json, format!("\"{}\"", s.name()));
        }
        assert!(LifecycleState::from_index(7).is_err());
        assert!(LifecycleState::from_name("LIVE").is_err());
        assert!(LifecycleState::Research < LifecycleState::Retired);
    }
}

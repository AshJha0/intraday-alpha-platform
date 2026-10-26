//! The stateless dark-first pre-trade check of the B10 dark-venue model
//! (v1.17 F8; `docs/RESEARCH_NOTES.md`): a pure function of the
//! inputs the execution replay supplies per dark post, pinned by
//! `tests/golden/expected_dark_controls.json` (the rows of the Python
//! mirror's `iap.execution.dark.DARK_CONTROL_GOLDENS`).
//!
//! This is the caller-scoped companion of the engine-state checks 31-33
//! (`limits_eval.rs`): the replay knows the parent's cumulative dark
//! notional and the trailing displayed lit volume, which the risk engine
//! does not track, so it evaluates these three controls itself before a dark
//! post and sends a rejected step to the lit book. Evaluation order is
//! `venue_type`, `participation`, `notional`; the first failure wins.

use std::collections::BTreeSet;

use marketdata::IapError;

use crate::limits::VenueType;

/// The three result codes, in evaluation order (`""` = accepted).
pub const DARK_CONTROL_CODES: [&str; 3] = ["venue_type", "participation", "notional"];

/// Controls of the dark-first rule (`DarkControls` in the Python mirror;
/// the same keys in its `to_dict()`).
#[derive(Debug, Clone, PartialEq)]
pub struct DarkControls {
    /// `qty <= max_participation * trailing displayed lit volume`; 0 = off.
    pub max_participation: f64,
    /// The trailing window of the participation cap (ns, > 0).
    pub participation_window_ns: i64,
    /// Venue types a dark post may go to.
    pub allowed_venue_types: BTreeSet<VenueType>,
    /// Cap on a parent's cumulative dark-filled notional incl. this child; 0 = off.
    pub max_dark_notional: f64,
}

impl Default for DarkControls {
    fn default() -> DarkControls {
        DarkControls {
            max_participation: 0.0,
            participation_window_ns: 30_000_000_000,
            allowed_venue_types: [VenueType::Dark, VenueType::Midpoint].into_iter().collect(),
            max_dark_notional: 0.0,
        }
    }
}

impl DarkControls {
    /// `Some(reason)` when the controls are not a valid configuration (the
    /// Python constructor's `ValueError`s).
    pub fn validation_error(&self) -> Option<&'static str> {
        if self.max_participation.is_nan()
            || self.max_participation < 0.0
            || self.max_dark_notional.is_nan()
            || self.max_dark_notional < 0.0
        {
            return Some("DarkControls: max_participation / max_dark_notional must be >= 0");
        }
        if self.participation_window_ns <= 0 {
            return Some("DarkControls.participation_window_ns must be > 0");
        }
        None
    }
}

/// `Ok("")` when the post may go, else the first failing
/// [`DARK_CONTROL_CODES`] entry; `Err` for `qty <= 0` (a caller error: the
/// Python mirror raises, this port rejects the request as malformed).
///
/// - `venue_type`: `venue_type` not in `allowed_venue_types`;
/// - `participation`: with `max_participation > 0`,
///   `qty > max_participation * trailing_lit_volume` (f64 compare, so a zero
///   trailing volume rejects every positive qty);
/// - `notional`: with `max_dark_notional > 0`,
///   `dark_notional_so_far + child_notional > max_dark_notional`.
pub fn dark_pretrade_check(
    controls: &DarkControls,
    venue_type: VenueType,
    qty: i64,
    trailing_lit_volume: i64,
    dark_notional_so_far: f64,
    child_notional: f64,
) -> Result<&'static str, IapError> {
    if qty <= 0 {
        return Err(IapError::InvalidArgument(
            "dark_pretrade_check: qty must be > 0".to_string(),
        ));
    }
    if !controls.allowed_venue_types.contains(&venue_type) {
        return Ok(DARK_CONTROL_CODES[0]);
    }
    if controls.max_participation > 0.0
        && qty as f64 > controls.max_participation * trailing_lit_volume as f64
    {
        return Ok(DARK_CONTROL_CODES[1]);
    }
    if controls.max_dark_notional > 0.0
        && dark_notional_so_far + child_notional > controls.max_dark_notional
    {
        return Ok(DARK_CONTROL_CODES[2]);
    }
    Ok("")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_caller_errors() {
        let c = DarkControls::default();
        assert!(c.validation_error().is_none());
        assert!(c.allowed_venue_types.contains(&VenueType::Dark));
        assert!(!c.allowed_venue_types.contains(&VenueType::Lit));
        assert!(dark_pretrade_check(&c, VenueType::Dark, 0, 0, 0.0, 0.0).is_err());
        let bad = DarkControls {
            max_participation: -0.1,
            ..DarkControls::default()
        };
        assert!(bad.validation_error().is_some());
    }
}

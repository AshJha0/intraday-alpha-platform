//! Kill-switch engage/clear, loss-limit override, session roll and venue
//! connectivity, split out of `engine.rs` verbatim. This is an
//! `impl RiskEngine` continued from `engine.rs`.

use marketdata::IapError;

use crate::engine::RiskEngine;
use crate::event::{fmt_fixed, rules, Decision, RiskEvent, Scope, Severity};

impl RiskEngine {
    /// Venue disconnect: orders to the venue reject until reconnect.
    pub fn on_venue_disconnect(&mut self, venue_id: u16, ts: i64) {
        self.venues_down.insert(venue_id, true);
        self.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope: Scope::Venue,
            scope_id: venue_id.to_string(),
            rule_id: rules::VENUE_DISCONNECT.to_string(),
            severity: Severity::Warn as u8,
            decision: Decision::Kill as u8,
            reason: format!("venue {venue_id} disconnected"),
        });
    }

    /// Venue reconnect.
    pub fn on_venue_reconnect(&mut self, venue_id: u16, ts: i64) {
        self.venues_down.insert(venue_id, false);
        self.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope: Scope::Venue,
            scope_id: venue_id.to_string(),
            rule_id: rules::VENUE_RECONNECT.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: format!("venue {venue_id} reconnected"),
        });
    }

    /// Manually engage a kill switch. Errors (changing nothing in the
    /// requested scope, emitting `MALFORMED_KILL` instead of
    /// `KILL_SWITCH_ENGAGED`) when `scope_id` does not parse.
    ///
    /// SILENT NO-OP defect: an unparseable scope id used to leave the
    /// engine untouched while the audit log recorded a convincing
    /// KILL_SWITCH_ENGAGED, so an operator halting an instrument by ticker
    /// ("AAPL") believed the halt was in force and the next order was
    /// ALLOWed. Fail-closed: the operator's intent is to STOP trading and
    /// the narrow scope is undeterminable, so the engine takes the wider
    /// safe interpretation and latches the GLOBAL kill, then reports the
    /// failure loudly. Over-halting is recoverable; a phantom halt is not.
    pub fn engage_kill(
        &mut self,
        scope: Scope,
        scope_id: &str,
        ts: i64,
        reason: &str,
    ) -> Result<(), IapError> {
        if !self.set_kill(scope, scope_id, true) {
            self.metrics.counter("risk_malformed_kills_total").inc();
            let _ = self.set_kill(Scope::Global, "", true);
            let why = format!(
                "kill scope id \"{}\" is not a valid {} id: escalated to GLOBAL (fail-closed)",
                scope_id,
                Self::scope_name(scope)
            );
            self.emit(RiskEvent {
                seq: 0,
                prev_sha256: String::new(),
                timestamp: ts,
                scope,
                scope_id: scope_id.to_string(),
                rule_id: rules::MALFORMED_KILL.to_string(),
                severity: Severity::Breach as u8,
                decision: Decision::Kill as u8,
                reason: format!("{why}: {reason}"),
            });
            return Err(IapError::InvalidArgument(why));
        }
        self.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope,
            scope_id: scope_id.to_string(),
            rule_id: rules::KILL_SWITCH_ENGAGED.to_string(),
            severity: Severity::Breach as u8,
            decision: Decision::Kill as u8,
            reason: reason.to_string(),
        });
        Ok(())
    }

    /// Clear a kill switch (the switch only — see the re-arm precedence).
    /// Errors (clearing NOTHING and emitting `MALFORMED_KILL` instead of
    /// `KILL_SWITCH_CLEARED`) when `scope_id` does not parse: clearing is
    /// the permissive direction, so an unresolvable scope leaves every
    /// switch exactly as it was.
    pub fn clear_kill(
        &mut self,
        scope: Scope,
        scope_id: &str,
        ts: i64,
        reason: &str,
    ) -> Result<(), IapError> {
        if !self.set_kill(scope, scope_id, false) {
            self.metrics.counter("risk_malformed_kills_total").inc();
            let why = format!(
                "kill scope id \"{}\" is not a valid {} id: nothing cleared (fail-closed)",
                scope_id,
                Self::scope_name(scope)
            );
            self.emit(RiskEvent {
                seq: 0,
                prev_sha256: String::new(),
                timestamp: ts,
                scope,
                scope_id: scope_id.to_string(),
                rule_id: rules::MALFORMED_KILL.to_string(),
                severity: Severity::Breach as u8,
                decision: Decision::Reject as u8,
                reason: format!("{why}: {reason}"),
            });
            return Err(IapError::InvalidArgument(why));
        }
        self.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope,
            scope_id: scope_id.to_string(),
            rule_id: rules::KILL_SWITCH_CLEARED.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: reason.to_string(),
        });
        Ok(())
    }

    /// Raise (or lower) the effective daily loss limit of the GLOBAL or a
    /// STRATEGY scope with written approval. Audited; never clears a
    /// latched kill switch. Errors on a non-positive/non-finite limit or an
    /// unsupported scope (nothing changes). Bounded and multi-party
    /// (G-P2-3): with `overrides.min_approvers` the comma-separated
    /// `approver` list must name that many distinct (space-trimmed,
    /// non-empty) approvers, and with `overrides.max_loss_override_multiple`
    /// the new limit may not exceed that multiple of the configured limit;
    /// a refused override changes nothing, is audited
    /// `LOSS_LIMIT_OVERRIDE_REJECTED` and returns an error.
    pub fn override_loss_limit(
        &mut self,
        scope: Scope,
        scope_id: &str,
        new_limit: f64,
        ts: i64,
        approver: &str,
    ) -> Result<(), IapError> {
        if !(new_limit.is_finite() && new_limit > 0.0) {
            return Err(IapError::InvalidArgument(format!(
                "loss limit override must be finite and > 0, got {new_limit}"
            )));
        }
        let Some(limits) = self.limits.clone() else {
            return Err(IapError::InvalidArgument(
                "engine is fail-closed (no limits)".to_string(),
            ));
        };
        let base = match scope {
            Scope::Global => limits.max_daily_loss,
            Scope::Strategy => limits.strategy_max_daily_loss,
            _ => {
                return Err(IapError::InvalidArgument(
                    "loss limits exist at GLOBAL and STRATEGY scope only".to_string(),
                ))
            }
        };
        let mut why: Option<String> = None;
        if let Some(min) = limits.min_approvers {
            let names: std::collections::BTreeSet<&str> = approver
                .split(',')
                .map(|p| p.trim_matches(' '))
                .filter(|p| !p.is_empty())
                .collect();
            if (names.len() as i64) < min {
                why = Some(format!(
                    "loss limit override needs {min} distinct approvers, got {}: {approver}",
                    names.len()
                ));
            }
        }
        if why.is_none() {
            if let Some(mult) = limits.max_loss_override_multiple {
                let cap = base * mult;
                if new_limit > cap {
                    why = Some(format!(
                        "loss limit override {} exceeds cap {} ({}x the configured {})",
                        fmt_fixed(new_limit, 2),
                        fmt_fixed(cap, 2),
                        fmt_fixed(mult, 2),
                        fmt_fixed(base, 2)
                    ));
                }
            }
        }
        if let Some(why) = why {
            self.emit(RiskEvent {
                seq: 0,
                prev_sha256: String::new(),
                timestamp: ts,
                scope,
                scope_id: scope_id.to_string(),
                rule_id: rules::LOSS_LIMIT_OVERRIDE_REJECTED.to_string(),
                severity: Severity::Breach as u8,
                decision: Decision::Reject as u8,
                reason: why.clone(),
            });
            return Err(IapError::InvalidArgument(why));
        }
        let old = match scope {
            Scope::Global => {
                let old = self.loss_override_global.unwrap_or(limits.max_daily_loss);
                self.loss_override_global = Some(new_limit);
                old
            }
            Scope::Strategy => {
                let old = self
                    .loss_override_strategy
                    .get(scope_id)
                    .copied()
                    .unwrap_or(limits.strategy_max_daily_loss);
                self.loss_override_strategy
                    .insert(scope_id.to_string(), new_limit);
                old
            }
            _ => {
                return Err(IapError::InvalidArgument(
                    "loss limits exist at GLOBAL and STRATEGY scope only".to_string(),
                ))
            }
        };
        self.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope,
            scope_id: scope_id.to_string(),
            rule_id: rules::LOSS_LIMIT_OVERRIDE.to_string(),
            severity: Severity::Warn as u8,
            decision: Decision::Allow as u8,
            reason: format!(
                "daily loss limit {} -> {} approved by {approver}",
                fmt_fixed(old, 2),
                fmt_fixed(new_limit, 2)
            ),
        });
        Ok(())
    }

    /// Session roll: realized P&L zeroed, every marked lot re-based to its
    /// mark (unrealized restarts at 0; unmarked lots keep their cost),
    /// loss-limit overrides cleared. Kill switches, positions, open orders
    /// and seen order ids are untouched. Audited.
    pub fn roll_session(&mut self, ts: i64, reason: &str) {
        self.realized.clear();
        let marks: Vec<(u32, f64)> = self
            .lots
            .keys()
            .map(|(_, iid)| *iid)
            .filter_map(|iid| self.mark_price(iid).map(|m| (iid, m)))
            .collect();
        for ((_, iid), lot) in self.lots.iter_mut() {
            if let Some((_, m)) = marks.iter().find(|(i, _)| i == iid) {
                lot.avg_price = *m;
            }
        }
        self.loss_override_global = None;
        self.loss_override_strategy.clear();
        self.market_volume.clear();
        self.executed.clear();
        self.refresh_pnl_gauges();
        self.emit(RiskEvent {
            seq: 0,
            prev_sha256: String::new(),
            timestamp: ts,
            scope: Scope::Global,
            scope_id: String::new(),
            rule_id: rules::SESSION_ROLLED.to_string(),
            severity: Severity::Info as u8,
            decision: Decision::Allow as u8,
            reason: reason.to_string(),
        });
    }

    /// Apply a kill-switch change. Returns `false` (changing NOTHING) when
    /// `scope_id` does not name a scope this engine can address — an
    /// INSTRUMENT id that is not a u32 or a VENUE id that is not a u16.
    /// Callers MUST act on `false`: a silently dropped kill is the defect
    /// this return value exists to prevent.
    #[must_use]
    pub(crate) fn set_kill(&mut self, scope: Scope, scope_id: &str, engaged: bool) -> bool {
        match scope {
            Scope::Global => {
                self.kill_global = engaged;
                self.metrics
                    .gauge("risk_kill_switch_engaged")
                    .set(if engaged { 1.0 } else { 0.0 });
                true
            }
            Scope::Strategy => {
                self.kill_strategies.insert(scope_id.to_string(), engaged);
                true
            }
            Scope::Instrument => match scope_id.parse::<u32>() {
                Ok(iid) => {
                    self.kill_instruments.insert(iid, engaged);
                    true
                }
                Err(_) => false,
            },
            Scope::Venue => match scope_id.parse::<u16>() {
                Ok(vid) => {
                    self.kill_venues.insert(vid, engaged);
                    true
                }
                Err(_) => false,
            },
        }
    }

    /// `"INSTRUMENT"` / `"VENUE"` / ... for the malformed-kill reason.
    fn scope_name(scope: Scope) -> &'static str {
        match scope {
            Scope::Global => "GLOBAL",
            Scope::Strategy => "STRATEGY",
            Scope::Instrument => "INSTRUMENT",
            Scope::Venue => "VENUE",
        }
    }
}

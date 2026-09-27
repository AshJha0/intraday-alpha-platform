package com.iap.risk;

import java.util.Map;
import java.util.TreeMap;

/**
 * Kill-switch engage/clear, loss-limit override, session roll and venue
 * connectivity, split out of {@code RiskEngine} verbatim. Every method
 * takes the {@link RiskEngine} instance it operates on — the Java analogue
 * of the Rust port's {@code impl RiskEngine} continued in a separate file
 * ({@code rust/risk/src/killswitch.rs}). {@code RiskEngine}'s public
 * methods of the same name delegate here.
 */
final class KillSwitch {
    private KillSwitch() {
    }

    /** Venue disconnect: orders to the venue reject until reconnect. */
    static void onVenueDisconnect(RiskEngine e, int venueId, long ts) {
        e.venuesDown.put(venueId, true);
        e.emit(new RiskEvent(ts, Scope.VENUE, Integer.toString(venueId),
                Rules.VENUE_DISCONNECT, Severity.WARN.code(),
                Decision.KILL.code(), "venue " + venueId + " disconnected"));
    }

    /** Venue reconnect. */
    static void onVenueReconnect(RiskEngine e, int venueId, long ts) {
        e.venuesDown.put(venueId, false);
        e.emit(new RiskEvent(ts, Scope.VENUE, Integer.toString(venueId),
                Rules.VENUE_RECONNECT, Severity.INFO.code(),
                Decision.ALLOW.code(), "venue " + venueId + " reconnected"));
    }

    /**
     * Manually engage a kill switch. Throws {@link IllegalArgumentException}
     * (changing nothing in the requested scope, emitting
     * {@code MALFORMED_KILL} instead of {@code KILL_SWITCH_ENGAGED}) when
     * {@code scopeId} does not parse.
     *
     * <p>SILENT NO-OP defect: an unparseable scope id used to leave the
     * engine untouched while the audit log recorded a convincing
     * KILL_SWITCH_ENGAGED, so an operator halting an instrument by ticker
     * ("AAPL") believed the halt was in force and the next order was
     * ALLOWed. Fail-closed: the operator's intent is to STOP trading and the
     * narrow scope is undeterminable, so the engine takes the wider safe
     * interpretation and latches the GLOBAL kill, then reports the failure
     * loudly. Over-halting is recoverable; a phantom halt is not.
     */
    static void engageKill(RiskEngine e, Scope scope, String scopeId, long ts,
            String reason) {
        if (!setKill(e, scope, scopeId, true)) {
            e.metrics.counter("risk_malformed_kills_total").inc();
            setKill(e, Scope.GLOBAL, "", true);
            String why = "kill scope id \"" + scopeId + "\" is not a valid "
                    + scopeName(scope) + " id: escalated to GLOBAL (fail-closed)";
            e.emit(new RiskEvent(ts, scope, scopeId, Rules.MALFORMED_KILL,
                    Severity.BREACH.code(), Decision.KILL.code(),
                    why + ": " + reason));
            throw new IllegalArgumentException(why);
        }
        e.emit(new RiskEvent(ts, scope, scopeId, Rules.KILL_SWITCH_ENGAGED,
                Severity.BREACH.code(), Decision.KILL.code(), reason));
    }

    /**
     * Clear a kill switch (the switch only — see the re-arm precedence).
     * Throws {@link IllegalArgumentException} (clearing NOTHING and emitting
     * {@code MALFORMED_KILL} instead of {@code KILL_SWITCH_CLEARED}) when
     * {@code scopeId} does not parse: clearing is the permissive direction,
     * so an unresolvable scope leaves every switch exactly as it was.
     */
    static void clearKill(RiskEngine e, Scope scope, String scopeId, long ts,
            String reason) {
        if (!setKill(e, scope, scopeId, false)) {
            e.metrics.counter("risk_malformed_kills_total").inc();
            String why = "kill scope id \"" + scopeId + "\" is not a valid "
                    + scopeName(scope) + " id: nothing cleared (fail-closed)";
            e.emit(new RiskEvent(ts, scope, scopeId, Rules.MALFORMED_KILL,
                    Severity.BREACH.code(), Decision.REJECT.code(),
                    why + ": " + reason));
            throw new IllegalArgumentException(why);
        }
        e.emit(new RiskEvent(ts, scope, scopeId, Rules.KILL_SWITCH_CLEARED,
                Severity.INFO.code(), Decision.ALLOW.code(), reason));
    }

    /**
     * Raise (or lower) the effective daily loss limit of the GLOBAL or a
     * STRATEGY scope with written approval. Audited; never clears a latched
     * kill switch. Throws on a non-positive/non-finite limit or an
     * unsupported scope (nothing changes).
     */
    static void overrideLossLimit(RiskEngine e, Scope scope, String scopeId,
            double newLimit, long ts, String approver) {
        if (!(Double.isFinite(newLimit) && newLimit > 0.0)) {
            throw new IllegalArgumentException(
                    "loss limit override must be finite and > 0, got " + newLimit);
        }
        if (e.limits == null) {
            throw new IllegalStateException("engine is fail-closed (no limits)");
        }
        double old;
        switch (scope) {
            case GLOBAL -> {
                old = e.lossOverrideGlobal == null
                        ? e.limits.maxDailyLoss() : e.lossOverrideGlobal;
                e.lossOverrideGlobal = newLimit;
            }
            case STRATEGY -> {
                Double prev = e.lossOverrideStrategy.get(scopeId);
                old = prev == null ? e.limits.strategyMaxDailyLoss() : prev;
                e.lossOverrideStrategy.put(scopeId, newLimit);
            }
            default -> throw new IllegalArgumentException(
                    "loss limits exist at GLOBAL and STRATEGY scope only");
        }
        e.emit(new RiskEvent(ts, scope, scopeId, Rules.LOSS_LIMIT_OVERRIDE,
                Severity.WARN.code(), Decision.ALLOW.code(),
                "daily loss limit " + RiskEngine.fmtFixed(old, 2) + " -> "
                        + RiskEngine.fmtFixed(newLimit, 2) + " approved by " + approver));
    }

    /**
     * Session roll: realized P&amp;L zeroed, every marked lot re-based to
     * its mark, loss-limit overrides cleared. Kill switches, positions,
     * open orders and seen order ids are untouched. Audited.
     */
    static void rollSession(RiskEngine e, long ts, String reason) {
        e.realized.clear();
        for (TreeMap<Long, RiskEngine.Lot> byIns : e.lots.values()) {
            for (Map.Entry<Long, RiskEngine.Lot> en : byIns.entrySet()) {
                Double mark = e.markPrice(en.getKey());
                if (mark != null) {
                    en.getValue().avgPrice = mark;
                }
            }
        }
        e.lossOverrideGlobal = null;
        e.lossOverrideStrategy.clear();
        e.refreshPnlGauges();
        e.emit(new RiskEvent(ts, Scope.GLOBAL, "", Rules.SESSION_ROLLED,
                Severity.INFO.code(), Decision.ALLOW.code(), reason));
    }

    /**
     * Apply a kill-switch change. Returns {@code false} (changing NOTHING)
     * when {@code scopeId} does not name a scope this engine can address —
     * an INSTRUMENT id that is not a u32 or a VENUE id that is not a u16.
     * Callers MUST act on {@code false}: a silently dropped kill is the
     * defect this return value exists to prevent.
     */
    static boolean setKill(RiskEngine e, Scope scope, String scopeId, boolean engaged) {
        switch (scope) {
            case GLOBAL -> e.setKillGlobal(engaged);
            case STRATEGY -> e.killStrategies.put(scopeId, engaged);
            case INSTRUMENT -> {
                try {
                    // Instrument ids are u32 (the Rust reference parses the
                    // scope id with parse::<u32>()).
                    e.killInstruments.put(
                            Integer.toUnsignedLong(
                                    Integer.parseUnsignedInt(scopeId)),
                            engaged);
                } catch (NumberFormatException ex) {
                    return false;
                }
            }
            case VENUE -> {
                int vid;
                try {
                    vid = Integer.parseInt(scopeId);
                } catch (NumberFormatException ex) {
                    return false;
                }
                if (vid < 0 || vid > 0xFFFF) {
                    return false;
                }
                e.killVenues.put(vid, engaged);
            }
        }
        return true;
    }

    /** {@code "INSTRUMENT"} / {@code "VENUE"} / ... for malformed-kill reasons. */
    private static String scopeName(Scope scope) {
        return switch (scope) {
            case GLOBAL -> "GLOBAL";
            case STRATEGY -> "STRATEGY";
            case INSTRUMENT -> "INSTRUMENT";
            case VENUE -> "VENUE";
        };
    }
}

package com.iap.risk;

/**
 * Pinned rule identifiers, in pre-trade evaluation order (the Rust
 * reference {@code rust/risk/src/event.rs::rules} is normative). The FIRST
 * failing rule decides; kill switches always take precedence, global before
 * strategy before instrument before venue.
 */
public final class Rules {
    /** Global kill switch engaged. */
    public static final String KILL_GLOBAL = "KILL_GLOBAL";
    /** Strategy kill switch engaged. */
    public static final String KILL_STRATEGY = "KILL_STRATEGY";
    /** Instrument kill switch engaged. */
    public static final String KILL_INSTRUMENT = "KILL_INSTRUMENT";
    /** Venue kill switch engaged. */
    public static final String KILL_VENUE = "KILL_VENUE";
    /** Schema-level order validation failed. */
    public static final String MALFORMED_ORDER = "MALFORMED_ORDER";
    /** No tick size / reference data for the instrument. */
    public static final String UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT";
    /** order_id already used inside the duplicate window. */
    public static final String DUPLICATE_ORDER_ID = "DUPLICATE_ORDER_ID";
    /** Target venue is disconnected. */
    public static final String VENUE_DISCONNECTED = "VENUE_DISCONNECTED";
    /** Market-data feed has an unrecovered sequence gap. */
    public static final String SEQUENCE_GAP = "SEQUENCE_GAP";
    /** Reference price missing or older than the stale timeout. */
    public static final String STALE_PRICE = "STALE_PRICE";
    /** Order quantity above the fat-finger cap. */
    public static final String FAT_FINGER_QTY = "FAT_FINGER_QTY";
    /** Order notional above the fat-finger cap. */
    public static final String FAT_FINGER_NOTIONAL = "FAT_FINGER_NOTIONAL";
    /** Limit price outside the band around the last mid. */
    public static final String PRICE_BAND = "PRICE_BAND";
    /** Per-strategy event-time token bucket exhausted. */
    public static final String RATE_THROTTLE = "RATE_THROTTLE";
    /** Order would cross an own resting order. */
    public static final String SELF_MATCH = "SELF_MATCH";
    /** Projected position beyond the per-instrument cap. */
    public static final String POSITION_LIMIT = "POSITION_LIMIT";
    /** Projected per-instrument notional beyond the cap. */
    public static final String INSTRUMENT_NOTIONAL = "INSTRUMENT_NOTIONAL";
    /** Projected gross notional beyond the cap (fail-closed on missing marks). */
    public static final String GROSS_NOTIONAL = "GROSS_NOTIONAL";
    /** Projected net notional beyond the cap. */
    public static final String NET_NOTIONAL = "NET_NOTIONAL";
    /** Firm-wide daily loss limit (realized + unrealized) breached. */
    public static final String DAILY_LOSS = "DAILY_LOSS";
    /** Per-strategy daily loss limit (realized + unrealized) breached. */
    public static final String STRATEGY_LOSS = "STRATEGY_LOSS";
    /** Quote-to-reporting currency conversion rate missing or stale. */
    public static final String FX_RATE_MISSING = "FX_RATE_MISSING";
    /** Engine awaits a position bootstrap (drop-copy) or state restore. */
    public static final String NOT_BOOTSTRAPPED = "NOT_BOOTSTRAPPED";
    /** Order passed every check. */
    public static final String ALLOW = "ALLOW";
    /** Engine is fail-closed (missing/invalid configuration). */
    public static final String CONFIG_MISSING = "CONFIG_MISSING";
    /** Manual kill switch engaged (audit record). */
    public static final String KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED";
    /** Manual kill switch cleared (audit record). */
    public static final String KILL_SWITCH_CLEARED = "KILL_SWITCH_CLEARED";
    /** Venue disconnect notification (audit record). */
    public static final String VENUE_DISCONNECT = "VENUE_DISCONNECT";
    /** Venue reconnect notification (audit record). */
    public static final String VENUE_RECONNECT = "VENUE_RECONNECT";
    /** A fill was rejected as malformed / unpriceable (audit record). */
    public static final String MALFORMED_FILL = "MALFORMED_FILL";
    /** A loss limit was overridden with approval (audit record). */
    public static final String LOSS_LIMIT_OVERRIDE = "LOSS_LIMIT_OVERRIDE";
    /** The trading session rolled: daily P&amp;L re-based (audit record). */
    public static final String SESSION_ROLLED = "SESSION_ROLLED";
    /** Position bootstrap completed (audit record). */
    public static final String BOOTSTRAP_COMPLETE = "BOOTSTRAP_COMPLETE";
    /** Engine state restored from a snapshot (audit record). */
    public static final String STATE_RESTORED = "STATE_RESTORED";

    private Rules() {
    }
}

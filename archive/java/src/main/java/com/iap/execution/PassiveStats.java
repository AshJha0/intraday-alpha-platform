package com.iap.execution;

/** Per-parent transition counters of the PASSIVE state machine. */
public final class PassiveStats {
    /** LIMIT children posted (first posts and reprices). */
    public long posts;
    /** Cancel + re-post at a new price. */
    public long reprices;
    /** Deadline renewed at an unchanged price (queue position kept). */
    public long restExtensions;
    /** MARKET for a remainder after the last reprice. */
    public long crossesTimeout;
    /** MARKET for a remainder because the schedule was behind. */
    public long crossesBehind;
    /** Schedule step sent as MARKET without posting. */
    public long crossesImmediate;
}

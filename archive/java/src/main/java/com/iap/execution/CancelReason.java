package com.iap.execution;

/** Why a child order reached CANCELLED (mirrors the C++ CancelReason). */
public enum CancelReason {
    /** Not cancelled. */
    NONE,
    /** MARKET/IOC remainder, FOK miss (rule 3). */
    UNFILLED_REMAINDER,
    /** Arrived while the venue was halted / in auction / stale (rule 8). */
    VENUE_NOT_TRADING,
    /** A {@link ExecutionSimulator#cancel(long, long)} arrived (rule 7). */
    USER,
    /** Time-in-force: expire_ts reached (rule 7). */
    EXPIRED,
    /** {@link ExecutionSimulator#cancelAll()} end-of-stream sweep. */
    END_OF_STREAM,
}

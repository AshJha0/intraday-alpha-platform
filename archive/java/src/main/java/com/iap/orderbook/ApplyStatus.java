package com.iap.orderbook;

/**
 * Per-event verdict returned by {@link OrderBook#apply} (pinned).
 *
 * <p>{@code applied + dropped + held == events fed} for every book;
 * downstream consumers (the feature engine, API_FEATURES.md section 2) MUST
 * ignore every event that is not {@link #APPLIED}.
 */
public enum ApplyStatus {
    /** The event changed book state (or was a valid no-op: HEARTBEAT/STATUS). */
    APPLIED,
    /** The event was rejected and counted in exactly one drop counter. */
    DROPPED,
    /**
     * The event is buffered behind a sequence hole (reorderWindow &gt; 0); it
     * is reported APPLIED/DROPPED when the buffer drains.
     */
    HELD
}

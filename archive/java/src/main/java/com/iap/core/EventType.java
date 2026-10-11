package com.iap.core;

/** Canonical event types (u8), pinned by PLATFORM_CONVENTIONS.md section 1. */
public final class EventType {
    public static final int ADD = 1;
    public static final int MODIFY = 2;
    public static final int CANCEL = 3;
    public static final int EXECUTE = 4;
    public static final int TRADE = 5;
    public static final int QUOTE = 6;
    public static final int SNAPSHOT = 7;
    public static final int STATUS = 8;
    public static final int HEARTBEAT = 9;

    private EventType() {
    }

    /** True when {@code eventType} is a known event type code. */
    public static boolean isValid(int eventType) {
        return eventType >= ADD && eventType <= HEARTBEAT;
    }
}

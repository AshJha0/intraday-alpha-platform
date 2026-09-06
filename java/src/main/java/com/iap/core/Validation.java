package com.iap.core;

/**
 * Contract validation for {@link MarketEvent}, mirroring
 * {@code iap/core/events.py::validation_error}. Integer domains, the
 * receive_ts &gt;= exchange_ts invariant, and the per-event-type payload rules
 * of schemas/FORMAT.md section 4.
 *
 * <p>u64 fields are stored as long bit patterns, so every value is in-domain
 * by construction; u32/u16/u8 fields are range-checked explicitly.
 */
public final class Validation {
    public static final long U32_MAX = 0xFFFFFFFFL;
    public static final int U16_MAX = 0xFFFF;
    /** Reserved synthetic order-id range (top 16 bits set; conventions section 1). */
    public static final long SYNTHETIC_ID_BASE = 0xFFFF000000000000L;

    private Validation() {
    }

    /** Return a reason string if {@code ev} violates the contract, else null. */
    public static String validationError(MarketEvent ev) {
        if (ev.instrumentId < 0 || ev.instrumentId > U32_MAX) {
            return "instrument_id out of u32 range: " + ev.instrumentId;
        }
        if (ev.venueId < 0 || ev.venueId > U16_MAX) {
            return "venue_id out of u16 range: " + ev.venueId;
        }
        if (ev.receiveTs < ev.exchangeTs) {
            return "receive_ts " + ev.receiveTs + " < exchange_ts " + ev.exchangeTs;
        }
        if (!EventType.isValid(ev.eventType)) {
            return "unknown event_type: " + ev.eventType;
        }
        if (!Side.isValid(ev.side)) {
            return "side must be 0 (BID) or 1 (ASK): " + ev.side;
        }

        int et = ev.eventType;
        if (Long.compareUnsigned(ev.orderId, SYNTHETIC_ID_BASE) >= 0
                && (et == EventType.ADD || et == EventType.QUOTE || et == EventType.SNAPSHOT)) {
            return "order_id in reserved synthetic range: " + Long.toUnsignedString(ev.orderId);
        }
        boolean bookType = et == EventType.ADD || et == EventType.MODIFY
                || et == EventType.CANCEL || et == EventType.EXECUTE;
        if (bookType) {
            if (ev.orderId == 0) {
                return "order_id required for event_type " + et;
            }
            if (ev.qty <= 0 && et != EventType.CANCEL) {
                return "qty must be > 0 for event_type " + et + ": " + ev.qty;
            }
            if (ev.priceTicks <= 0 && et != EventType.CANCEL) {
                return "price_ticks must be > 0 for event_type " + et + ": " + ev.priceTicks;
            }
        } else if (et == EventType.TRADE || et == EventType.QUOTE || et == EventType.SNAPSHOT) {
            if (ev.qty <= 0) {
                return "qty must be > 0 for event_type " + et + ": " + ev.qty;
            }
            if (ev.priceTicks <= 0) {
                return "price_ticks must be > 0 for event_type " + et + ": " + ev.priceTicks;
            }
            if (et == EventType.TRADE && ev.tradeId == 0) {
                return "trade_id required for TRADE";
            }
        } else if (et == EventType.STATUS) {
            if (!SessionStatus.isValid(ev.qty)) {
                return "STATUS qty must be a SessionStatus code: " + ev.qty;
            }
        }
        // HEARTBEAT: no payload constraints.
        return null;
    }

    /** Throw IllegalArgumentException if {@code ev} violates the contract. */
    public static void validate(MarketEvent ev) {
        String reason = validationError(ev);
        if (reason != null) {
            throw new IllegalArgumentException(
                    "invalid MarketEvent (event_id=" + Long.toUnsignedString(ev.eventId)
                            + "): " + reason);
        }
    }
}

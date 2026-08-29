package com.iap.core;

/**
 * One canonical market event (API_CORE.md section 1). Field order is normative:
 * it is the JSONL key order and the IAP1 record field order — do not reorder.
 *
 * <p>Storage conventions (Java has no unsigned integers):
 * <ul>
 *   <li>u64 fields ({@code eventId}, {@code sequence}, {@code orderId},
 *       {@code tradeId}) hold the unsigned bit pattern in a {@code long};
 *       compare with {@link Long#compareUnsigned} where ordering matters.</li>
 *   <li>u32 {@code instrumentId} is stored in a {@code long}
 *       (range 0..4294967295, checked by {@link Validation}).</li>
 *   <li>u16 {@code venueId} and u8 {@code eventType}/{@code side} are stored
 *       in {@code int}s.</li>
 *   <li>i64 fields ({@code exchangeTs}, {@code receiveTs}, {@code priceTicks},
 *       {@code qty}) are plain signed longs.</li>
 * </ul>
 */
public final class MarketEvent {
    public final long eventId;      // u64
    public final long instrumentId; // u32
    public final int venueId;       // u16
    public final long exchangeTs;   // i64 ns since epoch
    public final long receiveTs;    // i64 ns since epoch
    public final long sequence;     // u64, per (venue, instrument) stream
    public final int eventType;     // u8, see EventType
    public final int side;          // u8, see Side
    public final long priceTicks;   // i64
    public final long qty;          // i64
    public final long orderId;      // u64
    public final long tradeId;      // u64

    /** Construct an event; parameters in canonical field order. */
    public MarketEvent(
            long eventId,
            long instrumentId,
            int venueId,
            long exchangeTs,
            long receiveTs,
            long sequence,
            int eventType,
            int side,
            long priceTicks,
            long qty,
            long orderId,
            long tradeId) {
        this.eventId = eventId;
        this.instrumentId = instrumentId;
        this.venueId = venueId;
        this.exchangeTs = exchangeTs;
        this.receiveTs = receiveTs;
        this.sequence = sequence;
        this.eventType = eventType;
        this.side = side;
        this.priceTicks = priceTicks;
        this.qty = qty;
        this.orderId = orderId;
        this.tradeId = tradeId;
    }

    @Override
    public boolean equals(Object obj) {
        if (this == obj) {
            return true;
        }
        if (!(obj instanceof MarketEvent o)) {
            return false;
        }
        return eventId == o.eventId
                && instrumentId == o.instrumentId
                && venueId == o.venueId
                && exchangeTs == o.exchangeTs
                && receiveTs == o.receiveTs
                && sequence == o.sequence
                && eventType == o.eventType
                && side == o.side
                && priceTicks == o.priceTicks
                && qty == o.qty
                && orderId == o.orderId
                && tradeId == o.tradeId;
    }

    @Override
    public int hashCode() {
        int h = Long.hashCode(eventId);
        h = 31 * h + Long.hashCode(instrumentId);
        h = 31 * h + venueId;
        h = 31 * h + Long.hashCode(exchangeTs);
        h = 31 * h + Long.hashCode(receiveTs);
        h = 31 * h + Long.hashCode(sequence);
        h = 31 * h + eventType;
        h = 31 * h + side;
        h = 31 * h + Long.hashCode(priceTicks);
        h = 31 * h + Long.hashCode(qty);
        h = 31 * h + Long.hashCode(orderId);
        h = 31 * h + Long.hashCode(tradeId);
        return h;
    }

    @Override
    public String toString() {
        return "MarketEvent(event_id=" + Long.toUnsignedString(eventId)
                + ", instrument_id=" + instrumentId
                + ", venue_id=" + venueId
                + ", exchange_ts=" + exchangeTs
                + ", receive_ts=" + receiveTs
                + ", sequence=" + Long.toUnsignedString(sequence)
                + ", event_type=" + eventType
                + ", side=" + side
                + ", price_ticks=" + priceTicks
                + ", qty=" + qty
                + ", order_id=" + Long.toUnsignedString(orderId)
                + ", trade_id=" + Long.toUnsignedString(tradeId) + ")";
    }
}

package com.iap.orderbook;

import java.util.Arrays;
import java.util.List;
import java.util.Objects;

/**
 * Full deterministic serialization of one venue book, mirroring the Python
 * reference {@code OrderBook.checkpoint()}: levels in sorted (side, price)
 * order, each with its FIFO order list, plus sequence/timestamps/flow/status/
 * stale/counters. {@link OrderBook#restore} rebuilds a bit-identical book.
 */
public final class BookCheckpoint {
    /** One price level: side, price, and the FIFO (order_id, qty) queue. */
    public static final class LevelCheckpoint {
        public final int side;
        public final long priceTicks;
        public final long[] orderIds; // FIFO head..tail
        public final long[] qtys;     // parallel to orderIds

        public LevelCheckpoint(int side, long priceTicks, long[] orderIds, long[] qtys) {
            if (orderIds.length != qtys.length) {
                throw new IllegalArgumentException("orderIds/qtys length mismatch");
            }
            this.side = side;
            this.priceTicks = priceTicks;
            this.orderIds = orderIds;
            this.qtys = qtys;
        }

        @Override
        public boolean equals(Object obj) {
            if (this == obj) {
                return true;
            }
            if (!(obj instanceof LevelCheckpoint o)) {
                return false;
            }
            return side == o.side && priceTicks == o.priceTicks
                    && Arrays.equals(orderIds, o.orderIds) && Arrays.equals(qtys, o.qtys);
        }

        @Override
        public int hashCode() {
            int h = side;
            h = 31 * h + Long.hashCode(priceTicks);
            h = 31 * h + Arrays.hashCode(orderIds);
            h = 31 * h + Arrays.hashCode(qtys);
            return h;
        }
    }

    public final long instrumentId;
    public final int venueId;
    public final List<LevelCheckpoint> levels;
    /**
     * Order ids of every resting order in global arrival (insertion) order —
     * restore() rebuilds it so the global queue-arrival order round-trips
     * checkpoints exactly (levels alone only pin per-level FIFO).
     */
    public final long[] arrivalOrder;
    public final long lastSequence;
    public final long exchangeTs;
    public final long receiveTs;
    public final long tradeFlow;
    public final long status;
    public final boolean stale;
    public final boolean snapshotActive;
    public final boolean snapshotBroken;
    public final long duplicatesDropped;
    public final long gapsDetected;
    public final long droppedWhileStale;
    public final long unknownOrderEvents;
    public final long invalidSideDropped;
    public final long eventsApplied;

    public BookCheckpoint(
            long instrumentId, int venueId, List<LevelCheckpoint> levels,
            long[] arrivalOrder, long lastSequence, long exchangeTs,
            long receiveTs, long tradeFlow, long status, boolean stale,
            boolean snapshotActive, boolean snapshotBroken,
            long duplicatesDropped, long gapsDetected, long droppedWhileStale,
            long unknownOrderEvents, long invalidSideDropped,
            long eventsApplied) {
        this.instrumentId = instrumentId;
        this.venueId = venueId;
        this.levels = List.copyOf(levels);
        this.arrivalOrder = arrivalOrder.clone();
        this.lastSequence = lastSequence;
        this.exchangeTs = exchangeTs;
        this.receiveTs = receiveTs;
        this.tradeFlow = tradeFlow;
        this.status = status;
        this.stale = stale;
        this.snapshotActive = snapshotActive;
        this.snapshotBroken = snapshotBroken;
        this.duplicatesDropped = duplicatesDropped;
        this.gapsDetected = gapsDetected;
        this.droppedWhileStale = droppedWhileStale;
        this.unknownOrderEvents = unknownOrderEvents;
        this.invalidSideDropped = invalidSideDropped;
        this.eventsApplied = eventsApplied;
    }

    @Override
    public boolean equals(Object obj) {
        if (this == obj) {
            return true;
        }
        if (!(obj instanceof BookCheckpoint o)) {
            return false;
        }
        return instrumentId == o.instrumentId
                && venueId == o.venueId
                && levels.equals(o.levels)
                && Arrays.equals(arrivalOrder, o.arrivalOrder)
                && lastSequence == o.lastSequence
                && exchangeTs == o.exchangeTs
                && receiveTs == o.receiveTs
                && tradeFlow == o.tradeFlow
                && status == o.status
                && stale == o.stale
                && snapshotActive == o.snapshotActive
                && snapshotBroken == o.snapshotBroken
                && duplicatesDropped == o.duplicatesDropped
                && gapsDetected == o.gapsDetected
                && droppedWhileStale == o.droppedWhileStale
                && unknownOrderEvents == o.unknownOrderEvents
                && invalidSideDropped == o.invalidSideDropped
                && eventsApplied == o.eventsApplied;
    }

    @Override
    public int hashCode() {
        return Objects.hash(instrumentId, venueId, levels,
                Arrays.hashCode(arrivalOrder), lastSequence, exchangeTs,
                receiveTs, tradeFlow, status, stale, snapshotActive,
                snapshotBroken, duplicatesDropped, gapsDetected,
                droppedWhileStale, unknownOrderEvents, invalidSideDropped,
                eventsApplied);
    }
}

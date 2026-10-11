package com.iap.orderbook;

import java.util.Arrays;
import java.util.List;
import java.util.Objects;

import com.iap.core.MarketEvent;

/**
 * Full deterministic serialization of one venue book, mirroring the Python
 * reference {@code OrderBook.checkpoint()} (API_CORE.md section 4, x-version
 * 2): levels in sorted (side, price) order, each with its FIFO order list,
 * the global arrival order, sequence/epoch/timestamps/flow/status/stale,
 * SNAPSHOT burst state, the reorder buffer and the 12 QC counters.
 * {@link OrderBook#restore} rebuilds a bit-identical book.
 */
public final class BookCheckpoint {
    /** Checkpoint schema version (API_CORE section 4/5). */
    public static final long VERSION = 2;

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

    /** The 12 QC counters in pinned order. */
    public static final class Counters {
        public final long duplicatesDropped;
        public final long gapsDetected;
        public final long droppedWhileStale;
        public final long unknownOrderEvents;
        public final long invalidSideDropped;
        public final long invalidPayloadDropped;
        public final long unknownTypeDropped;
        public final long modifyPriceMismatch;
        public final long snapshotRestarts;
        public final long sequenceResets;
        public final long lateRecovered;
        public final long eventsApplied;

        public Counters(long duplicatesDropped, long gapsDetected, long droppedWhileStale,
                long unknownOrderEvents, long invalidSideDropped, long invalidPayloadDropped,
                long unknownTypeDropped, long modifyPriceMismatch, long snapshotRestarts,
                long sequenceResets, long lateRecovered, long eventsApplied) {
            this.duplicatesDropped = duplicatesDropped;
            this.gapsDetected = gapsDetected;
            this.droppedWhileStale = droppedWhileStale;
            this.unknownOrderEvents = unknownOrderEvents;
            this.invalidSideDropped = invalidSideDropped;
            this.invalidPayloadDropped = invalidPayloadDropped;
            this.unknownTypeDropped = unknownTypeDropped;
            this.modifyPriceMismatch = modifyPriceMismatch;
            this.snapshotRestarts = snapshotRestarts;
            this.sequenceResets = sequenceResets;
            this.lateRecovered = lateRecovered;
            this.eventsApplied = eventsApplied;
        }

        /** Sum of the drop counters (applied + drops + pending == events fed). */
        public long drops() {
            return duplicatesDropped + droppedWhileStale + unknownOrderEvents
                    + invalidSideDropped + invalidPayloadDropped + unknownTypeDropped
                    + modifyPriceMismatch;
        }

        @Override
        public boolean equals(Object obj) {
            if (this == obj) {
                return true;
            }
            if (!(obj instanceof Counters o)) {
                return false;
            }
            return duplicatesDropped == o.duplicatesDropped
                    && gapsDetected == o.gapsDetected
                    && droppedWhileStale == o.droppedWhileStale
                    && unknownOrderEvents == o.unknownOrderEvents
                    && invalidSideDropped == o.invalidSideDropped
                    && invalidPayloadDropped == o.invalidPayloadDropped
                    && unknownTypeDropped == o.unknownTypeDropped
                    && modifyPriceMismatch == o.modifyPriceMismatch
                    && snapshotRestarts == o.snapshotRestarts
                    && sequenceResets == o.sequenceResets
                    && lateRecovered == o.lateRecovered
                    && eventsApplied == o.eventsApplied;
        }

        @Override
        public int hashCode() {
            return Objects.hash(duplicatesDropped, gapsDetected, droppedWhileStale,
                    unknownOrderEvents, invalidSideDropped, invalidPayloadDropped,
                    unknownTypeDropped, modifyPriceMismatch, snapshotRestarts,
                    sequenceResets, lateRecovered, eventsApplied);
        }

        @Override
        public String toString() {
            return "Counters(dup=" + duplicatesDropped + ", gaps=" + gapsDetected
                    + ", stale=" + droppedWhileStale + ", unknown=" + unknownOrderEvents
                    + ", side=" + invalidSideDropped + ", payload=" + invalidPayloadDropped
                    + ", type=" + unknownTypeDropped + ", modify=" + modifyPriceMismatch
                    + ", restarts=" + snapshotRestarts + ", resets=" + sequenceResets
                    + ", late=" + lateRecovered + ", applied=" + eventsApplied + ")";
        }
    }

    public final long instrumentId;
    public final int venueId;
    public final List<LevelCheckpoint> levels;
    /** Order ids of every resting order in global arrival (insertion) order. */
    public final long[] arrivalOrder;
    public final long lastSequence;   // u64 bit pattern
    public final boolean hasSequence;
    public final long sequenceEpoch;
    public final long exchangeTs;
    public final long receiveTs;
    public final long tradeFlow;
    public final long status;
    public final boolean stale;
    public final boolean snapshotActive;
    public final boolean snapshotBroken;
    public final long snapshotCountdown; // u64 bit pattern
    public final long[] snapshotSyntheticNext; // [bid ordinal, ask ordinal]
    public final int reorderWindow;
    public final MarketEvent[] reorderPending; // sequence order
    public final Counters counters;

    public BookCheckpoint(
            long instrumentId, int venueId, List<LevelCheckpoint> levels,
            long[] arrivalOrder, long lastSequence, boolean hasSequence,
            long sequenceEpoch, long exchangeTs, long receiveTs, long tradeFlow,
            long status, boolean stale, boolean snapshotActive, boolean snapshotBroken,
            long snapshotCountdown, long[] snapshotSyntheticNext, int reorderWindow,
            MarketEvent[] reorderPending, Counters counters) {
        if (snapshotSyntheticNext.length != 2) {
            throw new IllegalArgumentException("snapshotSyntheticNext must have 2 entries");
        }
        this.instrumentId = instrumentId;
        this.venueId = venueId;
        this.levels = List.copyOf(levels);
        this.arrivalOrder = arrivalOrder.clone();
        this.lastSequence = lastSequence;
        this.hasSequence = hasSequence;
        this.sequenceEpoch = sequenceEpoch;
        this.exchangeTs = exchangeTs;
        this.receiveTs = receiveTs;
        this.tradeFlow = tradeFlow;
        this.status = status;
        this.stale = stale;
        this.snapshotActive = snapshotActive;
        this.snapshotBroken = snapshotBroken;
        this.snapshotCountdown = snapshotCountdown;
        this.snapshotSyntheticNext = snapshotSyntheticNext.clone();
        this.reorderWindow = reorderWindow;
        this.reorderPending = reorderPending.clone();
        this.counters = counters;
    }

    // Convenience accessors kept for callers that read individual counters.

    public long duplicatesDropped() {
        return counters.duplicatesDropped;
    }

    public long gapsDetected() {
        return counters.gapsDetected;
    }

    public long droppedWhileStale() {
        return counters.droppedWhileStale;
    }

    public long unknownOrderEvents() {
        return counters.unknownOrderEvents;
    }

    public long invalidSideDropped() {
        return counters.invalidSideDropped;
    }

    public long eventsApplied() {
        return counters.eventsApplied;
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
                && hasSequence == o.hasSequence
                && sequenceEpoch == o.sequenceEpoch
                && exchangeTs == o.exchangeTs
                && receiveTs == o.receiveTs
                && tradeFlow == o.tradeFlow
                && status == o.status
                && stale == o.stale
                && snapshotActive == o.snapshotActive
                && snapshotBroken == o.snapshotBroken
                && snapshotCountdown == o.snapshotCountdown
                && Arrays.equals(snapshotSyntheticNext, o.snapshotSyntheticNext)
                && reorderWindow == o.reorderWindow
                && Arrays.equals(reorderPending, o.reorderPending)
                && counters.equals(o.counters);
    }

    @Override
    public int hashCode() {
        return Objects.hash(instrumentId, venueId, levels,
                Arrays.hashCode(arrivalOrder), lastSequence, hasSequence, sequenceEpoch,
                exchangeTs, receiveTs, tradeFlow, status, stale, snapshotActive,
                snapshotBroken, snapshotCountdown, Arrays.hashCode(snapshotSyntheticNext),
                reorderWindow, Arrays.hashCode(reorderPending), counters);
    }
}

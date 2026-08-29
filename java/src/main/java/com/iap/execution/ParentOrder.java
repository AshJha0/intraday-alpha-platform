package com.iap.execution;

/**
 * A parent order worked over an event-time window {@code [start_ts, end_ts)}
 * by scheduling child orders (see {@link Algos} for the pinned schedules).
 */
public final class ParentOrder {
    public long parentId;
    public long instrumentId;   // u32
    public int venueId;         // u16; 0 = SOR-routed
    public int side;            // 0 = buy, 1 = sell
    public long qty;
    public AlgoType algo = AlgoType.TWAP;
    public long startTs;
    public long endTs;
    public int slices = 8;               // TWAP / VWAP / IS
    public double participation = 0.05;  // POV
    public double riskAversion = 1.0;    // IS
    public long maxChildQty = 1000;
}

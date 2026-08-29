package com.iap.execution;

/**
 * A child order worked by the {@link ExecutionSimulator}. The caller fills
 * the request fields; the simulator owns the runtime state
 * (arrival/state/remaining/queue position).
 */
public final class ChildOrder {
    // ---- request fields (caller) ----
    public long orderId;      // assigned by submit()
    public long parentId;
    public long instrumentId; // u32
    public int venueId;       // u16
    public int side;          // 0 = buy, 1 = sell
    public OrderType type = OrderType.LIMIT;
    public long limitTicks;   // ignored for MARKET
    public long qty;
    public long decisionTs;
    // ---- simulator-owned runtime state ----
    public long arrivalTs;
    public OrderState state = OrderState.PENDING;
    public long remaining;
    public long aheadQty;     // displayed qty ahead of us at our level
    public boolean resting;
    public boolean crossExempt; // see the crossing-rule exemption (rule 4)

    /** Field-by-field copy (checkpoint support). */
    public ChildOrder copy() {
        ChildOrder o = new ChildOrder();
        o.orderId = orderId;
        o.parentId = parentId;
        o.instrumentId = instrumentId;
        o.venueId = venueId;
        o.side = side;
        o.type = type;
        o.limitTicks = limitTicks;
        o.qty = qty;
        o.decisionTs = decisionTs;
        o.arrivalTs = arrivalTs;
        o.state = state;
        o.remaining = remaining;
        o.aheadQty = aheadQty;
        o.resting = resting;
        o.crossExempt = crossExempt;
        return o;
    }
}

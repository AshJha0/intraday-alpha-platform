package com.iap.execution;

/** Named simulator counters (conventions §8: every drop is counted). */
public final class ExecCounters {
    /** Rule 8: aggressive arrivals cancelled on a gated venue. */
    public long venueNotTradingCancels;
    /** Rule 7: orders expired by time-in-force. */
    public long expiredOrders;
    /** Rule 7: cancel arrivals applied. */
    public long userCancels;
    /** Rule 8: resting orders filled at the touch on a reopen. */
    public long reopenTouchFills;
    /** Rule 3b: aggressive walks that saw already-consumed liquidity. */
    public long overlayThinnedFills;

    ExecCounters copy() {
        ExecCounters c = new ExecCounters();
        c.venueNotTradingCancels = venueNotTradingCancels;
        c.expiredOrders = expiredOrders;
        c.userCancels = userCancels;
        c.reopenTouchFills = reopenTouchFills;
        c.overlayThinnedFills = overlayThinnedFills;
        return c;
    }
}

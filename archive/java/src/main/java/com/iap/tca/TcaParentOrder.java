package com.iap.tca;

import java.util.ArrayList;
import java.util.List;

/** A parent order with its child fills (TCA data model, spec §19). */
public final class TcaParentOrder {
    public final long orderId;
    public final long instrumentId;
    /** 0 = BID (buy), 1 = ASK (sell). */
    public final int side;
    public final long qtyTarget;
    /** When the signal fired. */
    public final long decisionTs;
    /** When the first child could act (post decision→arrival delay). */
    public final long arrivalTs;
    /** End of the execution horizon. */
    public final long endTs;
    public final List<TcaFill> fills = new ArrayList<>();

    public TcaParentOrder(long orderId, long instrumentId, int side,
            long qtyTarget, long decisionTs, long arrivalTs, long endTs) {
        if (side < 0 || side > 1) {
            throw new IllegalArgumentException("side must be 0 or 1: " + side);
        }
        if (qtyTarget <= 0) {
            throw new IllegalArgumentException("qty_target must be > 0");
        }
        this.orderId = orderId;
        this.instrumentId = instrumentId;
        this.side = side;
        this.qtyTarget = qtyTarget;
        this.decisionTs = decisionTs;
        this.arrivalTs = arrivalTs;
        this.endTs = endTs;
    }

    /** +1 for buys, -1 for sells. */
    public int sign() {
        return side == 0 ? 1 : -1;
    }

    /** Total filled quantity. */
    public long qtyFilled() {
        long q = 0;
        for (TcaFill f : fills) {
            q += f.qty();
        }
        return q;
    }

    /** Quantity-weighted average fill price (NaN when unfilled). */
    public double fillVwap() {
        long q = qtyFilled();
        if (q == 0) {
            return Double.NaN;
        }
        double num = 0.0;
        for (TcaFill f : fills) {
            num += f.price() * (double) f.qty();
        }
        return num / (double) q;
    }
}

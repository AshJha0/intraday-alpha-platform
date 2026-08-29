package com.iap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;

/** Package-private event builders for book/replay unit tests. */
final class Ev {
    static final long INST = 7;
    static final int VENUE = 3;

    private long seq;
    private long eventId;
    private long ts = 1_000_000_000L;

    /** Build one raw event on the shared test stream (7@3), auto seq/ids/ts. */
    MarketEvent raw(int type, int side, long price, long qty, long orderId, long tradeId) {
        seq++;
        eventId++;
        ts += 1000;
        return new MarketEvent(eventId, INST, VENUE, ts, ts + 50, seq, type, side,
                price, qty, orderId, tradeId);
    }

    /** Same as {@link #raw} but with an explicit sequence number. */
    MarketEvent atSeq(long sequence, int type, int side, long price, long qty,
            long orderId, long tradeId) {
        eventId++;
        ts += 1000;
        if (sequence > seq) {
            seq = sequence;
        }
        return new MarketEvent(eventId, INST, VENUE, ts, ts + 50, sequence, type, side,
                price, qty, orderId, tradeId);
    }

    MarketEvent add(int side, long price, long qty, long orderId) {
        return raw(EventType.ADD, side, price, qty, orderId, 0);
    }

    MarketEvent modify(long orderId, long newQty) {
        return raw(EventType.MODIFY, 0, 0, newQty, orderId, 0);
    }

    MarketEvent cancel(long orderId) {
        return raw(EventType.CANCEL, 0, 0, 0, orderId, 0);
    }

    MarketEvent execute(long orderId, long qty) {
        return raw(EventType.EXECUTE, 0, 0, qty, orderId, 0);
    }

    MarketEvent trade(int side, long price, long qty, long tradeId) {
        return raw(EventType.TRADE, side, price, qty, 0, tradeId);
    }

    MarketEvent quote(int side, long price, long qty, long orderId) {
        return raw(EventType.QUOTE, side, price, qty, orderId, 0);
    }

    MarketEvent snapshot(int side, long price, long qty, long orderId, long remaining) {
        return raw(EventType.SNAPSHOT, side, price, qty, orderId, remaining);
    }

    MarketEvent status(long code) {
        return raw(EventType.STATUS, 0, 0, code, 0, 0);
    }

    MarketEvent heartbeat() {
        return raw(EventType.HEARTBEAT, 0, 0, 0, 0, 0);
    }
}

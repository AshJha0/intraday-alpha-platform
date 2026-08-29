package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.SessionStatus;
import com.iap.core.Side;
import com.iap.core.Validation;

/** Contract validation error paths (mirrors iap/core/events.py). */
public class ValidationTest {

    private static MarketEvent ev(int type, int side, long price, long qty,
            long orderId, long tradeId) {
        return new MarketEvent(1, 1, 1, 1000, 1001, 1, type, side, price, qty,
                orderId, tradeId);
    }

    @Test
    public void validAddPasses() {
        assertNull(Validation.validationError(ev(EventType.ADD, Side.BID, 100, 10, 5, 0)));
    }

    @Test
    public void receiveBeforeExchangeRejected() {
        MarketEvent bad = new MarketEvent(1, 1, 1, 1000, 999, 1, EventType.ADD,
                Side.BID, 100, 10, 5, 0);
        String reason = Validation.validationError(bad);
        assertNotNull(reason);
        assertTrue(reason, reason.contains("receive_ts"));
    }

    @Test
    public void unknownEventTypeRejected() {
        String reason = Validation.validationError(ev(0, Side.BID, 100, 10, 5, 0));
        assertNotNull(reason);
        assertTrue(reason, reason.contains("event_type"));
        assertNotNull(Validation.validationError(ev(10, Side.BID, 100, 10, 5, 0)));
    }

    @Test
    public void badSideRejected() {
        String reason = Validation.validationError(ev(EventType.ADD, 2, 100, 10, 5, 0));
        assertNotNull(reason);
        assertTrue(reason, reason.contains("side"));
    }

    @Test
    public void instrumentOutOfU32Rejected() {
        MarketEvent bad = new MarketEvent(1, 0x1_0000_0000L, 1, 1000, 1001, 1,
                EventType.HEARTBEAT, Side.BID, 0, 0, 0, 0);
        String reason = Validation.validationError(bad);
        assertNotNull(reason);
        assertTrue(reason, reason.contains("instrument_id"));
    }

    @Test
    public void venueOutOfU16Rejected() {
        MarketEvent bad = new MarketEvent(1, 1, 0x10000, 1000, 1001, 1,
                EventType.HEARTBEAT, Side.BID, 0, 0, 0, 0);
        String reason = Validation.validationError(bad);
        assertNotNull(reason);
        assertTrue(reason, reason.contains("venue_id"));
    }

    @Test
    public void bookTypesRequireOrderId() {
        for (int t : new int[] {EventType.ADD, EventType.MODIFY, EventType.CANCEL,
                EventType.EXECUTE}) {
            String reason = Validation.validationError(ev(t, Side.BID, 100, 10, 0, 0));
            assertNotNull("type " + t, reason);
            assertTrue(reason, reason.contains("order_id"));
        }
    }

    @Test
    public void addRequiresPositiveQtyAndPrice() {
        assertNotNull(Validation.validationError(ev(EventType.ADD, Side.BID, 100, 0, 5, 0)));
        assertNotNull(Validation.validationError(ev(EventType.ADD, Side.BID, 0, 10, 5, 0)));
        assertNotNull(Validation.validationError(ev(EventType.ADD, Side.BID, -1, 10, 5, 0)));
    }

    @Test
    public void cancelAllowsZeroQtyAndPrice() {
        assertNull(Validation.validationError(ev(EventType.CANCEL, Side.BID, 0, 0, 5, 0)));
    }

    @Test
    public void tradeRequiresTradeId() {
        String reason = Validation.validationError(ev(EventType.TRADE, Side.BID, 100, 10, 0, 0));
        assertNotNull(reason);
        assertTrue(reason, reason.contains("trade_id"));
        assertNull(Validation.validationError(ev(EventType.TRADE, Side.BID, 100, 10, 0, 9)));
    }

    @Test
    public void quoteAndSnapshotRequirePositivePayload() {
        assertNotNull(Validation.validationError(ev(EventType.QUOTE, Side.ASK, 100, 0, 5, 0)));
        assertNotNull(Validation.validationError(ev(EventType.SNAPSHOT, Side.ASK, 0, 5, 5, 0)));
        assertNull(Validation.validationError(ev(EventType.QUOTE, Side.ASK, 100, 5, 5, 0)));
        assertNull(Validation.validationError(ev(EventType.SNAPSHOT, Side.ASK, 100, 5, 5, 2)));
    }

    @Test
    public void statusRequiresKnownCode() {
        assertNull(Validation.validationError(ev(EventType.STATUS, Side.BID, 0,
                SessionStatus.HALT, 0, 0)));
        String reason = Validation.validationError(ev(EventType.STATUS, Side.BID, 0, 9, 0, 0));
        assertNotNull(reason);
        assertTrue(reason, reason.contains("SessionStatus"));
    }

    @Test
    public void heartbeatHasNoPayloadConstraints() {
        assertNull(Validation.validationError(ev(EventType.HEARTBEAT, Side.BID, 0, 0, 0, 0)));
    }

    @Test
    public void validateThrowsWithReason() {
        try {
            Validation.validate(ev(EventType.ADD, Side.BID, 100, 10, 0, 0));
            fail("expected IllegalArgumentException");
        } catch (IllegalArgumentException e) {
            assertTrue(e.getMessage(), e.getMessage().contains("invalid MarketEvent"));
            assertTrue(e.getMessage(), e.getMessage().contains("order_id"));
        }
    }

    @Test
    public void allGoldenEventsValidate() {
        for (MarketEvent ev : Golden.eq()) {
            assertNull(Validation.validationError(ev));
        }
        for (MarketEvent ev : Golden.fx()) {
            assertNull(Validation.validationError(ev));
        }
        assertEquals(2000, Golden.eq().size());
        assertEquals(800, Golden.fx().size());
    }
}

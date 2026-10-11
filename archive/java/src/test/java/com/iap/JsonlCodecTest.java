package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.fail;

import org.junit.Test;

import com.iap.codec.JsonlCodec;
import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;

/** Strict JSONL codec behavior (schemas/FORMAT.md section 1). */
public class JsonlCodecTest {

    private static final String VALID =
            "{\"event_id\":1,\"instrument_id\":2,\"venue_id\":3,\"exchange_ts\":100,"
            + "\"receive_ts\":101,\"sequence\":4,\"event_type\":1,\"side\":0,"
            + "\"price_ticks\":2450,\"qty\":300,\"order_id\":9,\"trade_id\":0}";

    private static void assertRejected(String line, String needle) {
        try {
            JsonlCodec.decodeLine(line);
            fail("expected rejection of: " + line);
        } catch (IllegalArgumentException e) {
            if (!e.getMessage().contains(needle)) {
                fail("wrong message for " + line + ": " + e.getMessage());
            }
        }
    }

    @Test
    public void decodeValidLine() {
        MarketEvent ev = JsonlCodec.decodeLine(VALID);
        assertEquals(1, ev.eventId);
        assertEquals(2, ev.instrumentId);
        assertEquals(3, ev.venueId);
        assertEquals(100, ev.exchangeTs);
        assertEquals(101, ev.receiveTs);
        assertEquals(4, ev.sequence);
        assertEquals(EventType.ADD, ev.eventType);
        assertEquals(Side.BID, ev.side);
        assertEquals(2450, ev.priceTicks);
        assertEquals(300, ev.qty);
        assertEquals(9, ev.orderId);
        assertEquals(0, ev.tradeId);
    }

    @Test
    public void encodeIsCanonical() {
        assertEquals(VALID, JsonlCodec.encodeLine(JsonlCodec.decodeLine(VALID)));
    }

    @Test
    public void whitespaceBetweenTokensTolerated() {
        String spaced = VALID.replace(",\"qty\":300", " , \"qty\" : 300");
        assertEquals(JsonlCodec.decodeLine(VALID), JsonlCodec.decodeLine(spaced));
    }

    @Test
    public void unsignedU64RoundTrip() {
        // order_id = 2^64 - 1 must survive encode/decode via unsigned printing.
        MarketEvent ev = new MarketEvent(1, 2, 3, 100, 101, 4, EventType.ADD,
                Side.BID, 2450, 300, -1L, 0);
        String line = JsonlCodec.encodeLine(ev);
        if (!line.contains("\"order_id\":18446744073709551615")) {
            fail("unsigned u64 not printed unsigned: " + line);
        }
        assertEquals(ev, JsonlCodec.decodeLine(line));
    }

    @Test
    public void negativeSignedFieldsRoundTrip() {
        MarketEvent ev = new MarketEvent(1, 2, 3, -100, -50, 4, EventType.HEARTBEAT,
                Side.BID, -7, -8, 0, 0);
        assertEquals(ev, JsonlCodec.decodeLine(JsonlCodec.encodeLine(ev)));
    }

    @Test
    public void missingKeyRejected() {
        assertRejected(VALID.replace("\"qty\":300,", ""), "keys mismatch");
    }

    @Test
    public void misorderedKeysRejected() {
        String swapped = VALID
                .replace("\"price_ticks\":2450,\"qty\":300",
                        "\"qty\":300,\"price_ticks\":2450");
        assertRejected(swapped, "keys mismatch");
    }

    @Test
    public void extraKeyRejected() {
        assertRejected(VALID.replace(",\"trade_id\":0}", ",\"trade_id\":0,\"x\":1}"),
                "expected '}'");
    }

    @Test
    public void floatValueRejected() {
        assertRejected(VALID.replace("\"qty\":300", "\"qty\":300.5"), "integer");
    }

    @Test
    public void exponentValueRejected() {
        assertRejected(VALID.replace("\"qty\":300", "\"qty\":3e2"), "integer");
    }

    @Test
    public void stringValueRejected() {
        assertRejected(VALID.replace("\"qty\":300", "\"qty\":\"300\""), "integer");
    }

    @Test
    public void booleanValueRejected() {
        assertRejected(VALID.replace("\"qty\":300", "\"qty\":true"), "integer");
    }

    @Test
    public void negativeUnsignedFieldRejected() {
        assertRejected(VALID.replace("\"order_id\":9", "\"order_id\":-9"),
                "non-negative");
    }

    @Test
    public void leadingZeroRejected() {
        assertRejected(VALID.replace("\"qty\":300", "\"qty\":0300"), "leading zero");
    }

    @Test
    public void u64OverflowRejected() {
        assertRejected(VALID.replace("\"order_id\":9",
                "\"order_id\":18446744073709551616"), "out of range");
    }

    @Test
    public void venueOutOfU16Rejected() {
        assertRejected(VALID.replace("\"venue_id\":3", "\"venue_id\":65536"),
                "venue_id out of u16");
    }

    @Test
    public void trailingGarbageRejected() {
        assertRejected(VALID + "x", "trailing");
    }

    @Test
    public void truncatedLineRejected() {
        assertRejected(VALID.substring(0, VALID.length() - 1), "expected '}'");
    }
}

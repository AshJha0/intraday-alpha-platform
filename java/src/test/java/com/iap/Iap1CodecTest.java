package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.util.Arrays;
import java.util.List;

import org.junit.Test;

import com.iap.codec.Iap1Codec;
import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;

/** IAP1 binary layout and error paths (schemas/FORMAT.md section 2). */
public class Iap1CodecTest {

    private static List<MarketEvent> sample() {
        return List.of(
                new MarketEvent(1, 2, 3, 100, 101, 1, EventType.ADD, Side.BID,
                        2450, 300, 11, 0),
                new MarketEvent(2, 2, 3, 102, 103, 2, EventType.TRADE, Side.ASK,
                        2451, 50, 0, 77));
    }

    @Test
    public void headerBytesAreLittleEndianMagicVersionCount() {
        byte[] data = Iap1Codec.encode(sample());
        // magic 0x49415031 LE => "1PAI" on disk.
        assertEquals('1', data[0]);
        assertEquals('P', data[1]);
        assertEquals('A', data[2]);
        assertEquals('I', data[3]);
        // version 2 LE
        assertEquals(2, data[4]);
        assertEquals(0, data[5]);
        // count = 2 as u64 LE
        assertEquals(2, data[8]);
        assertEquals(0, data[15]);
        // trailer: crc32(header + records) | reserved 0 | count echo
        int body = 16 + 72 * 2;
        int crc = Iap1Codec.crc32(data, body);
        assertEquals((byte) crc, data[body]);
        assertEquals((byte) (crc >>> 24), data[body + 3]);
        assertEquals(0, data[body + 4]);
        assertEquals(2, data[body + 8]);
    }

    @Test
    public void crc32KnownAnswerAndCorruptionDetected() {
        byte[] s = "123456789".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
        assertEquals(0xCBF43926, Iap1Codec.crc32(s, s.length));
        assertEquals(0, Iap1Codec.crc32(new byte[0], 0));
        byte[] data = Iap1Codec.encode(sample());
        data[16 + 50] ^= 0x01; // a qty byte of record 0
        assertRejected(data, "CRC-32");
        byte[] echo = Iap1Codec.encode(sample());
        echo[echo.length - 8] ^= 0x01;
        assertRejected(echo, "count echo");
        byte[] reserved = Iap1Codec.encode(sample());
        reserved[reserved.length - 12] = 1;
        assertRejected(reserved, "reserved");
    }

    @Test
    public void legacyV1AcceptedWithoutIntegrity() {
        byte[] data = Iap1Codec.encode(sample());
        byte[] legacy = Arrays.copyOf(data, data.length - Iap1Codec.TRAILER_SIZE);
        legacy[4] = 1;
        Iap1Codec.Decoded d = Iap1Codec.decodeEx(legacy);
        assertEquals(1, d.version());
        assertEquals(false, d.integrityChecked());
        assertEquals(sample(), d.events());
        Iap1Codec.Decoded v2 = Iap1Codec.decodeEx(data);
        assertEquals(2, v2.version());
        assertEquals(true, v2.integrityChecked());
    }

    @Test
    public void fileSizeIsHeaderPlus72PerRecordPlusTrailer() {
        assertEquals(32, Iap1Codec.encode(List.of()).length);
        assertEquals(16 + 72 * 2 + 16, Iap1Codec.encode(sample()).length);
        assertEquals(List.of(), Iap1Codec.decode(Iap1Codec.encode(List.of())));
    }

    @Test
    public void recordFieldOffsetsMatchFormatMd() {
        MarketEvent ev = new MarketEvent(0x0102030405060708L, 0xA1B2C3D4L, 0xBEEF,
                -2, -1, 5, EventType.EXECUTE, Side.ASK, -9, -10, 42, 43);
        byte[] data = Iap1Codec.encode(List.of(ev));
        java.nio.ByteBuffer buf = java.nio.ByteBuffer.wrap(data)
                .order(java.nio.ByteOrder.LITTLE_ENDIAN);
        assertEquals(0x0102030405060708L, buf.getLong(16));
        assertEquals(0xA1B2C3D4L, Integer.toUnsignedLong(buf.getInt(16 + 8)));
        assertEquals(0xBEEF, Short.toUnsignedInt(buf.getShort(16 + 12)));
        assertEquals(EventType.EXECUTE, buf.get(16 + 14));
        assertEquals(Side.ASK, buf.get(16 + 15));
        assertEquals(-2L, buf.getLong(16 + 16));
        assertEquals(-1L, buf.getLong(16 + 24));
        assertEquals(5L, buf.getLong(16 + 32));
        assertEquals(-9L, buf.getLong(16 + 40));
        assertEquals(-10L, buf.getLong(16 + 48));
        assertEquals(42L, buf.getLong(16 + 56));
        assertEquals(43L, buf.getLong(16 + 64));
    }

    @Test
    public void roundTripPreservesAllFields() {
        List<MarketEvent> events = sample();
        assertEquals(events, Iap1Codec.decode(Iap1Codec.encode(events)));
    }

    @Test
    public void emptyVectorRoundTrips() {
        assertEquals(List.of(), Iap1Codec.decode(Iap1Codec.encode(List.of())));
    }

    private static void assertRejected(byte[] data, String needle) {
        try {
            Iap1Codec.decode(data);
            fail("expected rejection");
        } catch (IllegalArgumentException e) {
            assertTrue(e.getMessage(), e.getMessage().contains(needle));
        }
    }

    @Test
    public void truncatedHeaderRejected() {
        assertRejected(new byte[10], "truncated");
    }

    @Test
    public void badMagicRejected() {
        byte[] data = Iap1Codec.encode(sample());
        data[0] = 'X';
        assertRejected(data, "bad IAP1 magic");
    }

    @Test
    public void badVersionRejected() {
        byte[] data = Iap1Codec.encode(sample());
        data[4] = 3;
        assertRejected(data, "unsupported IAP1 version");
    }

    @Test
    public void truncatedRecordsRejected() {
        byte[] data = Iap1Codec.encode(sample());
        assertRejected(Arrays.copyOf(data, data.length - 1), "size mismatch");
    }

    @Test
    public void countMismatchRejected() {
        byte[] data = Iap1Codec.encode(sample());
        data[8] = 3; // header claims 3 records, body has 2
        assertRejected(data, "size mismatch");
    }

    @Test
    public void hugeCountRejectedWithoutOverflow() {
        byte[] data = Iap1Codec.encode(sample());
        for (int i = 8; i < 16; i++) {
            data[i] = (byte) 0xFF; // count = 2^64 - 1
        }
        assertRejected(data, "size mismatch");
    }
}

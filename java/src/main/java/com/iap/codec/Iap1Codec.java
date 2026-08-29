package com.iap.codec;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import com.iap.core.MarketEvent;

/**
 * IAP1 binary codec (schemas/FORMAT.md section 2). Little-endian, 16-byte
 * header (magic u32 | version u32 | count u64) followed by fixed 72-byte
 * records with no padding. Byte-identical across languages (SHA-256 golden).
 */
public final class Iap1Codec {
    public static final int MAGIC = 0x49415031;
    public static final int VERSION = 1;
    public static final int HEADER_SIZE = 16;
    public static final int RECORD_SIZE = 72;

    private Iap1Codec() {
    }

    /** Encode events to IAP1 bytes (header + fixed 72-byte LE records). */
    public static byte[] encode(List<MarketEvent> events) {
        ByteBuffer buf = ByteBuffer.allocate(HEADER_SIZE + RECORD_SIZE * events.size());
        buf.order(ByteOrder.LITTLE_ENDIAN);
        buf.putInt(MAGIC);
        buf.putInt(VERSION);
        buf.putLong(events.size());
        for (MarketEvent ev : events) {
            buf.putLong(ev.eventId);
            buf.putInt((int) ev.instrumentId);
            buf.putShort((short) ev.venueId);
            buf.put((byte) ev.eventType);
            buf.put((byte) ev.side);
            buf.putLong(ev.exchangeTs);
            buf.putLong(ev.receiveTs);
            buf.putLong(ev.sequence);
            buf.putLong(ev.priceTicks);
            buf.putLong(ev.qty);
            buf.putLong(ev.orderId);
            buf.putLong(ev.tradeId);
        }
        return buf.array();
    }

    /** Decode IAP1 bytes. Rejects bad magic/version, truncation, count mismatch. */
    public static List<MarketEvent> decode(byte[] data) {
        if (data.length < HEADER_SIZE) {
            throw new IllegalArgumentException(
                    "IAP1 file truncated: " + data.length + " bytes < 16-byte header");
        }
        ByteBuffer buf = ByteBuffer.wrap(data);
        buf.order(ByteOrder.LITTLE_ENDIAN);
        int magic = buf.getInt();
        if (magic != MAGIC) {
            throw new IllegalArgumentException(String.format(
                    "bad IAP1 magic: 0x%08X (expected 0x%08X)", magic, MAGIC));
        }
        int version = buf.getInt();
        if (version != VERSION) {
            throw new IllegalArgumentException("unsupported IAP1 version: " + version);
        }
        long count = buf.getLong();
        long body = (long) data.length - HEADER_SIZE;
        if (count < 0 || count > body / RECORD_SIZE
                || body != count * RECORD_SIZE) {
            throw new IllegalArgumentException("IAP1 size mismatch: " + data.length
                    + " bytes, header count=" + Long.toUnsignedString(count)
                    + " implies 16 + 72*count");
        }
        List<MarketEvent> events = new ArrayList<>((int) count);
        for (long i = 0; i < count; i++) {
            long eventId = buf.getLong();
            long instrumentId = Integer.toUnsignedLong(buf.getInt());
            int venueId = Short.toUnsignedInt(buf.getShort());
            int eventType = Byte.toUnsignedInt(buf.get());
            int side = Byte.toUnsignedInt(buf.get());
            long exchangeTs = buf.getLong();
            long receiveTs = buf.getLong();
            long sequence = buf.getLong();
            long priceTicks = buf.getLong();
            long qty = buf.getLong();
            long orderId = buf.getLong();
            long tradeId = buf.getLong();
            events.add(new MarketEvent(eventId, instrumentId, venueId, exchangeTs,
                    receiveTs, sequence, eventType, side, priceTicks, qty, orderId, tradeId));
        }
        return events;
    }

    /** Write an IAP1 file; return the number of events written. */
    public static int write(Path path, List<MarketEvent> events) throws IOException {
        Files.write(path, encode(events));
        return events.size();
    }

    /** Read an IAP1 file. */
    public static List<MarketEvent> read(Path path) throws IOException {
        return decode(Files.readAllBytes(path));
    }
}

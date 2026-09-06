package com.iap.codec;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.CRC32;

import com.iap.core.MarketEvent;

/**
 * IAP1 binary codec (schemas/FORMAT.md section 2). Little-endian, 16-byte
 * header (magic u32 | version u32 | count u64) followed by fixed 72-byte
 * records with no padding, then (version 2) a 16-byte integrity trailer
 * {@code crc32 | reserved 0 | count echo} where crc32 is CRC-32 (IEEE
 * 802.3 / zlib, {@link java.util.zip.CRC32}) of header + records. Decoders
 * verify the trailer and accept version-1 files (no trailer) as unverified
 * legacy input. Byte-identical across languages (SHA-256 golden).
 */
public final class Iap1Codec {
    public static final int MAGIC = 0x49415031;
    /** Version written by {@link #encode} (header + records + trailer). */
    public static final int VERSION = 2;
    /** Legacy version (no trailer) still accepted by the decoder. */
    public static final int VERSION_LEGACY = 1;
    public static final int HEADER_SIZE = 16;
    public static final int RECORD_SIZE = 72;
    public static final int TRAILER_SIZE = 16;

    /** Result of {@link #decodeEx}: events, format version, integrity flag. */
    public record Decoded(List<MarketEvent> events, int version, boolean integrityChecked) {
    }

    private Iap1Codec() {
    }

    /** CRC-32 (IEEE 802.3 / zlib) of {@code data[0, len)}; crc32("123456789") = 0xCBF43926. */
    public static int crc32(byte[] data, int len) {
        CRC32 crc = new CRC32();
        crc.update(data, 0, len);
        return (int) crc.getValue();
    }

    /** Encode events to IAP1 v2 bytes (header + fixed 72-byte LE records + trailer). */
    public static byte[] encode(List<MarketEvent> events) {
        int body = HEADER_SIZE + RECORD_SIZE * events.size();
        ByteBuffer buf = ByteBuffer.allocate(body + TRAILER_SIZE);
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
        buf.putInt(crc32(buf.array(), body));
        buf.putInt(0);
        buf.putLong(events.size());
        return buf.array();
    }

    /** Decode IAP1 bytes. Rejects bad magic/version, truncation, count/CRC mismatch. */
    public static List<MarketEvent> decode(byte[] data) {
        return decodeEx(data).events();
    }

    /**
     * Decode IAP1 bytes (version 2 with trailer, or legacy version 1). Rejects
     * bad magic / unknown version / truncation / count mismatch / bad trailer
     * (reserved != 0, count echo, CRC mismatch).
     */
    public static Decoded decodeEx(byte[] data) {
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
        if (version != VERSION && version != VERSION_LEGACY) {
            throw new IllegalArgumentException("unsupported IAP1 version: " + version);
        }
        boolean withTrailer = version == VERSION;
        long count = buf.getLong();
        long payload = (long) data.length - HEADER_SIZE - (withTrailer ? TRAILER_SIZE : 0);
        if (count < 0 || payload < 0 || count > payload / RECORD_SIZE
                || payload != count * RECORD_SIZE) {
            throw new IllegalArgumentException("IAP1 size mismatch: " + data.length
                    + " bytes, header count=" + Long.toUnsignedString(count)
                    + " (version " + version + ") implies 16 + 72*count"
                    + (withTrailer ? " + 16" : ""));
        }
        if (withTrailer) {
            int body = (int) (HEADER_SIZE + count * RECORD_SIZE);
            int crc = buf.getInt(body);
            int reserved = buf.getInt(body + 4);
            long echo = buf.getLong(body + 8);
            if (reserved != 0) {
                throw new IllegalArgumentException(
                        "IAP1 trailer reserved field must be 0: " + reserved);
            }
            if (echo != count) {
                throw new IllegalArgumentException("IAP1 trailer count echo "
                        + Long.toUnsignedString(echo) + " != header count "
                        + Long.toUnsignedString(count));
            }
            int actual = crc32(data, body);
            if (actual != crc) {
                throw new IllegalArgumentException(String.format(
                        "IAP1 CRC-32 mismatch: trailer 0x%08X, computed 0x%08X", crc, actual));
            }
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
        return new Decoded(events, version, withTrailer);
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

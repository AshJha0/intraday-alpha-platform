package com.iap.codec;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

import com.iap.core.MarketEvent;

/**
 * Canonical JSONL codec for the pinned flat MarketEvent schema
 * (schemas/FORMAT.md section 1). The encoder is byte-exact: keys in pinned
 * order, compact separators, ASCII integers only, one LF per line.
 *
 * <p>The decoder is a hand-rolled strict scanner: it rejects missing, extra,
 * or misordered keys, non-integer values (floats, exponents, strings,
 * booleans, leading zeros, a leading '+'), negative values for unsigned
 * fields, and any trailing garbage.
 */
public final class JsonlCodec {
    /** Canonical key order (normative). */
    static final String[] KEYS = {
        "event_id", "instrument_id", "venue_id", "exchange_ts", "receive_ts",
        "sequence", "event_type", "side", "price_ticks", "qty", "order_id", "trade_id",
    };

    /** True where the field is signed i64 (exchange_ts/receive_ts/price_ticks/qty). */
    private static final boolean[] SIGNED = {
        false, false, false, true, true, false, false, false, true, true, false, false,
    };

    private JsonlCodec() {
    }

    // ------------------------------------------------------------- encoding

    /** Encode one event as a canonical JSONL line (no trailing newline). */
    public static String encodeLine(MarketEvent ev) {
        StringBuilder sb = new StringBuilder(220);
        sb.append("{\"event_id\":").append(Long.toUnsignedString(ev.eventId));
        sb.append(",\"instrument_id\":").append(ev.instrumentId);
        sb.append(",\"venue_id\":").append(ev.venueId);
        sb.append(",\"exchange_ts\":").append(ev.exchangeTs);
        sb.append(",\"receive_ts\":").append(ev.receiveTs);
        sb.append(",\"sequence\":").append(Long.toUnsignedString(ev.sequence));
        sb.append(",\"event_type\":").append(ev.eventType);
        sb.append(",\"side\":").append(ev.side);
        sb.append(",\"price_ticks\":").append(ev.priceTicks);
        sb.append(",\"qty\":").append(ev.qty);
        sb.append(",\"order_id\":").append(Long.toUnsignedString(ev.orderId));
        sb.append(",\"trade_id\":").append(Long.toUnsignedString(ev.tradeId));
        sb.append('}');
        return sb.toString();
    }

    /** Encode events to canonical JSONL bytes (LF after every line). */
    public static byte[] encode(List<MarketEvent> events) {
        StringBuilder sb = new StringBuilder(events.size() * 220 + 16);
        for (MarketEvent ev : events) {
            sb.append(encodeLine(ev)).append('\n');
        }
        return sb.toString().getBytes(StandardCharsets.UTF_8);
    }

    /** Write a canonical JSONL file; return the number of events written. */
    public static int write(Path path, List<MarketEvent> events) throws IOException {
        Files.write(path, encode(events));
        return events.size();
    }

    // ------------------------------------------------------------- decoding

    /** Decode one canonical JSONL line. Strict: exact keys, integer values. */
    public static MarketEvent decodeLine(String line) {
        Parser p = new Parser(line);
        p.skipWs();
        p.expect('{');
        long[] vals = new long[KEYS.length];
        for (int i = 0; i < KEYS.length; i++) {
            if (i > 0) {
                p.skipWs();
                p.expect(',');
            }
            p.skipWs();
            p.expectKey(KEYS[i]);
            p.skipWs();
            p.expect(':');
            p.skipWs();
            vals[i] = p.parseInteger(KEYS[i], SIGNED[i]);
        }
        p.skipWs();
        p.expect('}');
        p.skipWs();
        if (!p.atEnd()) {
            throw new IllegalArgumentException(
                    "malformed JSONL line: trailing content at offset " + p.pos);
        }
        long instrumentId = vals[1];
        if (instrumentId < 0 || instrumentId > 0xFFFFFFFFL) {
            throw new IllegalArgumentException("instrument_id out of u32 range: "
                    + Long.toUnsignedString(instrumentId));
        }
        long venueId = vals[2];
        if (venueId < 0 || venueId > 0xFFFF) {
            throw new IllegalArgumentException("venue_id out of u16 range: "
                    + Long.toUnsignedString(venueId));
        }
        long eventType = vals[6];
        long side = vals[7];
        if (eventType < 0 || eventType > 0xFF) {
            throw new IllegalArgumentException("event_type out of u8 range: "
                    + Long.toUnsignedString(eventType));
        }
        if (side < 0 || side > 0xFF) {
            throw new IllegalArgumentException("side out of u8 range: "
                    + Long.toUnsignedString(side));
        }
        return new MarketEvent(
                vals[0], instrumentId, (int) venueId, vals[3], vals[4], vals[5],
                (int) eventType, (int) side, vals[8], vals[9], vals[10], vals[11]);
    }

    /** Read all events from a canonical JSONL file (blank lines skipped). */
    public static List<MarketEvent> read(Path path) throws IOException {
        List<MarketEvent> out = new ArrayList<>();
        for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
            String s = line.strip();
            if (!s.isEmpty()) {
                out.add(decodeLine(s));
            }
        }
        return out;
    }

    // ------------------------------------------------------------ the scanner

    private static final class Parser {
        private final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c != ' ' && c != '\t' && c != '\n' && c != '\r') {
                    break;
                }
                pos++;
            }
        }

        boolean atEnd() {
            return pos >= s.length();
        }

        void expect(char c) {
            if (pos >= s.length() || s.charAt(pos) != c) {
                throw new IllegalArgumentException("malformed JSONL line: expected '" + c
                        + "' at offset " + pos + " in " + preview());
            }
            pos++;
        }

        void expectKey(String key) {
            int need = key.length() + 2;
            if (pos + need > s.length()
                    || s.charAt(pos) != '"'
                    || !s.regionMatches(pos + 1, key, 0, key.length())
                    || s.charAt(pos + 1 + key.length()) != '"') {
                throw new IllegalArgumentException(
                        "JSONL keys mismatch: expected key \"" + key + "\" at offset " + pos
                                + " in " + preview());
            }
            pos += need;
        }

        long parseInteger(String key, boolean signed) {
            int start = pos;
            boolean negative = false;
            if (pos < s.length() && s.charAt(pos) == '-') {
                negative = true;
                pos++;
            }
            int digitsStart = pos;
            while (pos < s.length() && s.charAt(pos) >= '0' && s.charAt(pos) <= '9') {
                pos++;
            }
            int nDigits = pos - digitsStart;
            if (nDigits == 0) {
                throw new IllegalArgumentException("JSONL field \"" + key
                        + "\" must be an integer, got " + tokenPreview(start));
            }
            if (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == '.' || c == 'e' || c == 'E') {
                    throw new IllegalArgumentException("JSONL field \"" + key
                            + "\" must be an integer (no floats), got " + tokenPreview(start));
                }
            }
            if (nDigits > 1 && s.charAt(digitsStart) == '0') {
                throw new IllegalArgumentException("JSONL field \"" + key
                        + "\" has a leading zero: " + tokenPreview(start));
            }
            String token = s.substring(digitsStart, pos);
            try {
                if (signed) {
                    return negative ? Long.parseLong("-" + token) : Long.parseLong(token);
                }
                if (negative) {
                    throw new IllegalArgumentException("JSONL field \"" + key
                            + "\" must be a non-negative integer: -" + token);
                }
                return Long.parseUnsignedLong(token);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("JSONL field \"" + key
                        + "\" out of range: " + tokenPreview(start));
            }
        }

        private String tokenPreview(int start) {
            int end = Math.min(s.length(), Math.max(pos, start + 24));
            return "'" + s.substring(start, end) + "'";
        }

        private String preview() {
            return s.length() <= 80 ? "'" + s + "'" : "'" + s.substring(0, 80) + "...'";
        }
    }
}

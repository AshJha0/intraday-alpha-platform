package com.iap.replay;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

import com.iap.config.Json;
import com.iap.core.MarketEvent;
import com.iap.orderbook.BookCheckpoint;
import com.iap.orderbook.ConsolidatedBook;

/**
 * Cross-language JSON codec for engine and book checkpoints (API_CORE.md
 * section 5 shape, x-version 2). {@link #write} produces a document
 * structurally equal to the Python reference's {@code ReplayEngine.checkpoint()}
 * (compact, keys in the pinned order; u64 values as unsigned decimal
 * integers); {@link #read} rejects missing keys, wrong versions and
 * out-of-domain values with {@link IllegalArgumentException}.
 */
public final class CheckpointJson {
    private CheckpointJson() {
    }

    // -------------------------------------------------------------- writing

    /** Serialize an engine checkpoint to compact JSON. */
    public static String write(ReplayEngine.Checkpoint cp) {
        StringBuilder sb = new StringBuilder(4096);
        sb.append("{\"x-version\":").append(ReplayEngine.VERSION);
        sb.append(",\"events_processed\":").append(Long.toUnsignedString(cp.eventsProcessed));
        sb.append(",\"last_exchange_ts\":").append(cp.lastExchangeTs);
        sb.append(",\"time_regressions\":").append(Long.toUnsignedString(cp.timeRegressions));
        sb.append(",\"unknown_instrument_dropped\":")
                .append(Long.toUnsignedString(cp.unknownInstrumentDropped));
        sb.append(",\"unknown_venue_dropped\":")
                .append(Long.toUnsignedString(cp.unknownVenueDropped));
        sb.append(",\"checkpoint_every\":").append(cp.checkpointEvery);
        sb.append(",\"snapshot_every\":").append(cp.snapshotEvery);
        sb.append(",\"keep_checkpoints\":").append(cp.keepCheckpoints);
        sb.append(",\"keep_snapshots\":").append(cp.keepSnapshots);
        sb.append(",\"snapshots_emitted\":").append(Long.toUnsignedString(cp.snapshotsEmitted));
        sb.append(",\"reorder_window\":").append(cp.reorderWindow);
        sb.append(",\"universe\":");
        if (cp.universe == null) {
            sb.append("null");
        } else {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<Long, TreeSet<Integer>> e : cp.universe.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append('"').append(e.getKey()).append("\":[");
                boolean fv = true;
                for (int vid : e.getValue()) {
                    if (!fv) {
                        sb.append(',');
                    }
                    fv = false;
                    sb.append(vid);
                }
                sb.append(']');
            }
            sb.append('}');
        }
        sb.append(",\"books\":{");
        boolean first = true;
        for (Map.Entry<Long, ConsolidatedBook.Checkpoint> e : cp.books.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            ConsolidatedBook.Checkpoint ccp = e.getValue();
            sb.append('"').append(e.getKey()).append("\":{\"instrument_id\":")
                    .append(ccp.instrumentId).append(",\"reorder_window\":")
                    .append(ccp.reorderWindow).append(",\"venues\":{");
            boolean fv = true;
            for (Map.Entry<Integer, BookCheckpoint> v : ccp.venues.entrySet()) {
                if (!fv) {
                    sb.append(',');
                }
                fv = false;
                sb.append('"').append(v.getKey()).append("\":");
                writeBook(sb, v.getValue());
            }
            sb.append("}}");
        }
        sb.append("}}");
        return sb.toString();
    }

    /** Serialize one book checkpoint to compact JSON. */
    public static String writeBook(BookCheckpoint cp) {
        StringBuilder sb = new StringBuilder(1024);
        writeBook(sb, cp);
        return sb.toString();
    }

    private static void writeEvent(StringBuilder sb, MarketEvent ev) {
        sb.append('[').append(Long.toUnsignedString(ev.eventId))
                .append(',').append(ev.instrumentId)
                .append(',').append(ev.venueId)
                .append(',').append(ev.exchangeTs)
                .append(',').append(ev.receiveTs)
                .append(',').append(Long.toUnsignedString(ev.sequence))
                .append(',').append(ev.eventType)
                .append(',').append(ev.side)
                .append(',').append(ev.priceTicks)
                .append(',').append(ev.qty)
                .append(',').append(Long.toUnsignedString(ev.orderId))
                .append(',').append(Long.toUnsignedString(ev.tradeId)).append(']');
    }

    private static void writeBook(StringBuilder sb, BookCheckpoint cp) {
        sb.append("{\"x-version\":").append(BookCheckpoint.VERSION);
        sb.append(",\"instrument_id\":").append(cp.instrumentId);
        sb.append(",\"venue_id\":").append(cp.venueId);
        sb.append(",\"levels\":[");
        for (int i = 0; i < cp.levels.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            BookCheckpoint.LevelCheckpoint lvl = cp.levels.get(i);
            sb.append("{\"side\":").append(lvl.side).append(",\"price_ticks\":")
                    .append(lvl.priceTicks).append(",\"orders\":[");
            for (int k = 0; k < lvl.orderIds.length; k++) {
                if (k > 0) {
                    sb.append(',');
                }
                sb.append('[').append(Long.toUnsignedString(lvl.orderIds[k])).append(',')
                        .append(lvl.qtys[k]).append(']');
            }
            sb.append("]}");
        }
        sb.append("],\"arrival_order\":[");
        for (int i = 0; i < cp.arrivalOrder.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(Long.toUnsignedString(cp.arrivalOrder[i]));
        }
        sb.append("],\"last_sequence\":").append(Long.toUnsignedString(cp.lastSequence));
        sb.append(",\"has_sequence\":").append(cp.hasSequence);
        sb.append(",\"sequence_epoch\":").append(Long.toUnsignedString(cp.sequenceEpoch));
        sb.append(",\"exchange_ts\":").append(cp.exchangeTs);
        sb.append(",\"receive_ts\":").append(cp.receiveTs);
        sb.append(",\"trade_flow\":").append(cp.tradeFlow);
        sb.append(",\"status\":").append(cp.status);
        sb.append(",\"stale\":").append(cp.stale);
        sb.append(",\"snapshot_active\":").append(cp.snapshotActive);
        sb.append(",\"snapshot_broken\":").append(cp.snapshotBroken);
        sb.append(",\"snapshot_countdown\":").append(Long.toUnsignedString(cp.snapshotCountdown));
        sb.append(",\"snapshot_synthetic_next\":[")
                .append(Long.toUnsignedString(cp.snapshotSyntheticNext[0])).append(',')
                .append(Long.toUnsignedString(cp.snapshotSyntheticNext[1])).append(']');
        sb.append(",\"reorder_window\":").append(cp.reorderWindow);
        sb.append(",\"reorder_pending\":[");
        for (int i = 0; i < cp.reorderPending.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            writeEvent(sb, cp.reorderPending[i]);
        }
        BookCheckpoint.Counters c = cp.counters;
        sb.append("],\"counters\":{\"duplicates_dropped\":").append(c.duplicatesDropped)
                .append(",\"gaps_detected\":").append(c.gapsDetected)
                .append(",\"dropped_while_stale\":").append(c.droppedWhileStale)
                .append(",\"unknown_order_events\":").append(c.unknownOrderEvents)
                .append(",\"invalid_side_dropped\":").append(c.invalidSideDropped)
                .append(",\"invalid_payload_dropped\":").append(c.invalidPayloadDropped)
                .append(",\"unknown_type_dropped\":").append(c.unknownTypeDropped)
                .append(",\"modify_price_mismatch\":").append(c.modifyPriceMismatch)
                .append(",\"snapshot_restarts\":").append(c.snapshotRestarts)
                .append(",\"sequence_resets\":").append(c.sequenceResets)
                .append(",\"late_recovered\":").append(c.lateRecovered)
                .append(",\"events_applied\":").append(c.eventsApplied).append("}}");
    }

    // -------------------------------------------------------------- reading

    private static Object field(Map<String, Object> obj, String key) {
        if (!obj.containsKey(key)) {
            throw new IllegalArgumentException("checkpoint JSON: missing key " + key);
        }
        return obj.get(key);
    }

    private static long u64(Object v, String what) {
        if (!(v instanceof Long)) {
            throw new IllegalArgumentException("checkpoint JSON: " + what + " must be an integer");
        }
        return (Long) v;
    }

    /** Unsigned integer that must fit in {@code bits}. */
    private static long unsigned(Object v, String what, int bits) {
        long x = u64(v, what);
        if (bits < 64 && (x < 0 || x > ((1L << bits) - 1))) {
            throw new IllegalArgumentException("checkpoint JSON: " + what + " out of range");
        }
        return x;
    }

    private static long i64(Object v, String what) {
        return u64(v, what);
    }

    private static boolean bool(Object v, String what) {
        if (!(v instanceof Boolean)) {
            throw new IllegalArgumentException("checkpoint JSON: " + what + " must be a bool");
        }
        return (Boolean) v;
    }

    private static long keyU64(String key, String what) {
        try {
            return Long.parseUnsignedLong(key);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("checkpoint JSON: bad numeric key " + what);
        }
    }

    /** Parse an engine checkpoint document. */
    public static ReplayEngine.Checkpoint read(String text) {
        Map<String, Object> j = Json.object(Json.parse(text));
        if (i64(field(j, "x-version"), "x-version") != ReplayEngine.VERSION) {
            throw new IllegalArgumentException("checkpoint JSON: unsupported engine x-version");
        }
        TreeMap<Long, TreeSet<Integer>> universe = null;
        Object uni = field(j, "universe");
        if (uni != null) {
            universe = new TreeMap<>();
            for (Map.Entry<String, Object> e : Json.object(uni).entrySet()) {
                long iid = keyU64(e.getKey(), "universe key");
                if (iid < 0 || iid > 0xFFFFFFFFL) {
                    throw new IllegalArgumentException("checkpoint JSON: universe key out of range");
                }
                TreeSet<Integer> venues = new TreeSet<>();
                for (Object v : Json.array(e.getValue())) {
                    venues.add((int) unsigned(v, "universe venue", 16));
                }
                universe.put(iid, venues);
            }
        }
        TreeMap<Long, ConsolidatedBook.Checkpoint> books = new TreeMap<>();
        for (Map.Entry<String, Object> e : Json.object(field(j, "books")).entrySet()) {
            Map<String, Object> cj = Json.object(e.getValue());
            long iid = unsigned(field(cj, "instrument_id"), "instrument_id", 32);
            if (keyU64(e.getKey(), "books key") != iid) {
                throw new IllegalArgumentException("checkpoint JSON: books key != instrument_id");
            }
            int window = (int) unsigned(field(cj, "reorder_window"), "reorder_window", 31);
            TreeMap<Integer, BookCheckpoint> venues = new TreeMap<>();
            for (Map.Entry<String, Object> v : Json.object(field(cj, "venues")).entrySet()) {
                BookCheckpoint bcp = readBook(Json.object(v.getValue()));
                if (keyU64(v.getKey(), "venues key") != bcp.venueId) {
                    throw new IllegalArgumentException("checkpoint JSON: venues key != venue_id");
                }
                venues.put(bcp.venueId, bcp);
            }
            books.put(iid, new ConsolidatedBook.Checkpoint(iid, window, venues));
        }
        return new ReplayEngine.Checkpoint(
                u64(field(j, "events_processed"), "events_processed"),
                i64(field(j, "last_exchange_ts"), "last_exchange_ts"),
                u64(field(j, "time_regressions"), "time_regressions"),
                u64(field(j, "unknown_instrument_dropped"), "unknown_instrument_dropped"),
                u64(field(j, "unknown_venue_dropped"), "unknown_venue_dropped"),
                (int) unsigned(field(j, "checkpoint_every"), "checkpoint_every", 31),
                (int) unsigned(field(j, "snapshot_every"), "snapshot_every", 31),
                (int) unsigned(field(j, "keep_checkpoints"), "keep_checkpoints", 31),
                (int) unsigned(field(j, "keep_snapshots"), "keep_snapshots", 31),
                u64(field(j, "snapshots_emitted"), "snapshots_emitted"),
                (int) unsigned(field(j, "reorder_window"), "reorder_window", 31),
                universe, books);
    }

    /** Parse one book checkpoint document. */
    public static BookCheckpoint readBook(String text) {
        return readBook(Json.object(Json.parse(text)));
    }

    private static MarketEvent readEvent(Object row) {
        List<Object> f = Json.array(row);
        if (f.size() != 12) {
            throw new IllegalArgumentException("checkpoint JSON: reorder_pending row must have 12 fields");
        }
        return new MarketEvent(
                u64(f.get(0), "event_id"),
                unsigned(f.get(1), "instrument_id", 32),
                (int) unsigned(f.get(2), "venue_id", 16),
                i64(f.get(3), "exchange_ts"),
                i64(f.get(4), "receive_ts"),
                u64(f.get(5), "sequence"),
                (int) unsigned(f.get(6), "event_type", 8),
                (int) unsigned(f.get(7), "side", 8),
                i64(f.get(8), "price_ticks"),
                i64(f.get(9), "qty"),
                u64(f.get(10), "order_id"),
                u64(f.get(11), "trade_id"));
    }

    private static BookCheckpoint readBook(Map<String, Object> j) {
        if (i64(field(j, "x-version"), "x-version") != BookCheckpoint.VERSION) {
            throw new IllegalArgumentException("checkpoint JSON: unsupported book x-version");
        }
        List<BookCheckpoint.LevelCheckpoint> levels = new ArrayList<>();
        for (Object lj : Json.array(field(j, "levels"))) {
            Map<String, Object> lm = Json.object(lj);
            List<Object> rows = Json.array(field(lm, "orders"));
            long[] ids = new long[rows.size()];
            long[] qtys = new long[rows.size()];
            for (int i = 0; i < rows.size(); i++) {
                List<Object> pair = Json.array(rows.get(i));
                if (pair.size() != 2) {
                    throw new IllegalArgumentException("checkpoint JSON: order entry must be [id, qty]");
                }
                ids[i] = u64(pair.get(0), "order_id");
                qtys[i] = i64(pair.get(1), "qty");
            }
            levels.add(new BookCheckpoint.LevelCheckpoint(
                    (int) unsigned(field(lm, "side"), "side", 8),
                    i64(field(lm, "price_ticks"), "price_ticks"), ids, qtys));
        }
        List<Object> arr = Json.array(field(j, "arrival_order"));
        long[] arrival = new long[arr.size()];
        for (int i = 0; i < arr.size(); i++) {
            arrival[i] = u64(arr.get(i), "arrival_order");
        }
        List<Object> nxt = Json.array(field(j, "snapshot_synthetic_next"));
        if (nxt.size() != 2) {
            throw new IllegalArgumentException("checkpoint JSON: snapshot_synthetic_next must have 2 entries");
        }
        List<Object> pend = Json.array(field(j, "reorder_pending"));
        MarketEvent[] pending = new MarketEvent[pend.size()];
        for (int i = 0; i < pend.size(); i++) {
            pending[i] = readEvent(pend.get(i));
        }
        Map<String, Object> c = Json.object(field(j, "counters"));
        BookCheckpoint.Counters counters = new BookCheckpoint.Counters(
                u64(field(c, "duplicates_dropped"), "duplicates_dropped"),
                u64(field(c, "gaps_detected"), "gaps_detected"),
                u64(field(c, "dropped_while_stale"), "dropped_while_stale"),
                u64(field(c, "unknown_order_events"), "unknown_order_events"),
                u64(field(c, "invalid_side_dropped"), "invalid_side_dropped"),
                u64(field(c, "invalid_payload_dropped"), "invalid_payload_dropped"),
                u64(field(c, "unknown_type_dropped"), "unknown_type_dropped"),
                u64(field(c, "modify_price_mismatch"), "modify_price_mismatch"),
                u64(field(c, "snapshot_restarts"), "snapshot_restarts"),
                u64(field(c, "sequence_resets"), "sequence_resets"),
                u64(field(c, "late_recovered"), "late_recovered"),
                u64(field(c, "events_applied"), "events_applied"));
        return new BookCheckpoint(
                unsigned(field(j, "instrument_id"), "instrument_id", 32),
                (int) unsigned(field(j, "venue_id"), "venue_id", 16),
                levels, arrival,
                u64(field(j, "last_sequence"), "last_sequence"),
                bool(field(j, "has_sequence"), "has_sequence"),
                u64(field(j, "sequence_epoch"), "sequence_epoch"),
                i64(field(j, "exchange_ts"), "exchange_ts"),
                i64(field(j, "receive_ts"), "receive_ts"),
                i64(field(j, "trade_flow"), "trade_flow"),
                i64(field(j, "status"), "status"),
                bool(field(j, "stale"), "stale"),
                bool(field(j, "snapshot_active"), "snapshot_active"),
                bool(field(j, "snapshot_broken"), "snapshot_broken"),
                u64(field(j, "snapshot_countdown"), "snapshot_countdown"),
                new long[] {u64(nxt.get(0), "snapshot_synthetic_next"),
                            u64(nxt.get(1), "snapshot_synthetic_next")},
                (int) unsigned(field(j, "reorder_window"), "reorder_window", 31),
                pending, counters);
    }
}

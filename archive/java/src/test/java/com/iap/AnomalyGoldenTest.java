package com.iap;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.codec.JsonlCodec;
import com.iap.core.MarketEvent;
import com.iap.orderbook.BookCheckpoint;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;
import com.iap.replay.CheckpointJson;
import com.iap.replay.ReplayEngine;

/**
 * Golden group (round 3): anomaly vectors (states + all 12 counters +
 * consolidated view at pinned indices, reorder_window 0 and 4), the
 * cross-language checkpoint interchange golden, and the shared JSONL
 * strictness fixture (API_CORE.md sections 3-6).
 */
public class AnomalyGoldenTest {
    private static final String[] VECTORS = {"events_eq_anomalies.jsonl", "events_fx_anomalies.jsonl"};

    private static List<MarketEvent> load(String name) {
        try {
            return JsonlCodec.read(Golden.DIR.resolve(name));
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private static void assertCounters(Map<String, Object> exp, BookCheckpoint.Counters c, String what) {
        assertEquals(what + " duplicates_dropped", Json.asLong(exp.get("duplicates_dropped")), c.duplicatesDropped);
        assertEquals(what + " gaps_detected", Json.asLong(exp.get("gaps_detected")), c.gapsDetected);
        assertEquals(what + " dropped_while_stale", Json.asLong(exp.get("dropped_while_stale")), c.droppedWhileStale);
        assertEquals(what + " unknown_order_events", Json.asLong(exp.get("unknown_order_events")), c.unknownOrderEvents);
        assertEquals(what + " invalid_side_dropped", Json.asLong(exp.get("invalid_side_dropped")), c.invalidSideDropped);
        assertEquals(what + " invalid_payload_dropped", Json.asLong(exp.get("invalid_payload_dropped")), c.invalidPayloadDropped);
        assertEquals(what + " unknown_type_dropped", Json.asLong(exp.get("unknown_type_dropped")), c.unknownTypeDropped);
        assertEquals(what + " modify_price_mismatch", Json.asLong(exp.get("modify_price_mismatch")), c.modifyPriceMismatch);
        assertEquals(what + " snapshot_restarts", Json.asLong(exp.get("snapshot_restarts")), c.snapshotRestarts);
        assertEquals(what + " sequence_resets", Json.asLong(exp.get("sequence_resets")), c.sequenceResets);
        assertEquals(what + " late_recovered", Json.asLong(exp.get("late_recovered")), c.lateRecovered);
        assertEquals(what + " events_applied", Json.asLong(exp.get("events_applied")), c.eventsApplied);
    }

    private static void assertLevel(Object exp, long[] got, String what) {
        if (exp == null) {
            assertNull(what, got);
            return;
        }
        List<Object> row = Json.array(exp);
        assertArrayEquals(what, new long[] {Json.asLong(row.get(0)), Json.asLong(row.get(1))}, got);
    }

    private static void checkVector(String name) {
        Map<String, Object> expected = Golden.json("expected_anomaly_states.json");
        Map<String, Object> spec = Json.object(Json.object(expected.get("vectors")).get(name));
        List<MarketEvent> events = load(name);
        assertEquals(Json.asLong(spec.get("events")), events.size());
        long instrumentId = Json.asLong(spec.get("instrument_id"));
        for (Object runObj : Json.array(spec.get("runs"))) {
            Map<String, Object> run = Json.object(runObj);
            int window = (int) Json.asLong(run.get("reorder_window"));
            ConsolidatedBook cons = new ConsolidatedBook(instrumentId, window);
            Map<String, Object> states = Json.object(run.get("states"));
            for (int i = 1; i <= events.size(); i++) {
                cons.apply(events.get(i - 1));
                Object expObj = states.get(Integer.toString(i));
                if (expObj == null) {
                    continue;
                }
                String where = name + " window=" + window + " index " + i;
                Map<String, Object> exp = Json.object(expObj);
                Map<String, Object> venues = Json.object(exp.get("venues"));
                assertEquals(where, venues.size(), cons.venues().size());
                for (Map.Entry<Integer, OrderBook> e : cons.venues().entrySet()) {
                    OrderBook book = e.getValue();
                    Map<String, Object> vexp = Json.object(venues.get(Integer.toString(e.getKey())));
                    String what = where + " venue " + e.getKey();
                    assertEquals(what, Golden.bookState(Json.object(vexp.get("summary"))), book.stateSummary());
                    assertCounters(Json.object(vexp.get("counters")), book.counters(), what);
                    assertEquals(what + " stale", vexp.get("stale"), book.isStale());
                    assertEquals(what + " status", Json.asLong(vexp.get("status")), book.status());
                    assertEquals(what + " has_sequence", vexp.get("has_sequence"), book.hasSequence());
                    assertEquals(what + " sequence_epoch", Json.asLong(vexp.get("sequence_epoch")), book.sequenceEpoch());
                    assertEquals(what + " pending_count", Json.asLong(vexp.get("pending_count")), book.pendingCount());
                }
                Map<String, Object> cexp = Json.object(exp.get("consolidated"));
                assertLevel(cexp.get("best_bid"), cons.bestBid(), where + " best_bid");
                assertLevel(cexp.get("best_ask"), cons.bestAsk(), where + " best_ask");
                assertArrayEquals(where + " depth_bid_top5", Golden.pairs(cexp.get("depth_bid_top5")), cons.depth(0, 5));
                assertArrayEquals(where + " depth_ask_top5", Golden.pairs(cexp.get("depth_ask_top5")), cons.depth(1, 5));
                assertEquals(where + " is_crossed", cexp.get("is_crossed"), cons.isCrossed());
                assertEquals(where + " is_locked", cexp.get("is_locked"), cons.isLocked());
                List<Object> av = Json.array(cexp.get("active_venues"));
                int[] active = new int[av.size()];
                for (int k = 0; k < av.size(); k++) {
                    active[k] = (int) Json.asLong(av.get(k));
                }
                assertArrayEquals(where + " active_venues", active, cons.activeVenues());
                assertEquals(where + " trade_flow", Json.asLong(cexp.get("trade_flow")), cons.tradeFlow());
            }
            // Accounting invariant: applied + drops + pending == events fed.
            TreeMap<Integer, Long> fed = new TreeMap<>();
            for (MarketEvent ev : events) {
                fed.merge(ev.venueId, 1L, Long::sum);
            }
            for (Map.Entry<Integer, OrderBook> e : cons.venues().entrySet()) {
                OrderBook b = e.getValue();
                assertEquals(name + " venue " + e.getKey(), (long) fed.get(e.getKey()),
                        b.eventsApplied() + b.counters().drops() + b.pendingCount());
            }
        }
    }

    @Test
    public void anomalyEqVectorStatesAndCountersMatchGolden() {
        checkVector("events_eq_anomalies.jsonl");
    }

    @Test
    public void anomalyFxVectorStatesAndCountersMatchGolden() {
        checkVector("events_fx_anomalies.jsonl");
    }

    @Test
    public void anomalyVectorsByteStableAndCheckpointRoundTrip() {
        for (String name : VECTORS) {
            List<MarketEvent> events = load(name);
            assertArrayEquals(Golden.bytes(name), JsonlCodec.encode(events));
            for (int window : new int[] {0, 4}) {
                ConsolidatedBook full = new ConsolidatedBook(events.get(0).instrumentId, window);
                for (MarketEvent ev : events) {
                    full.apply(ev);
                }
                for (int split : new int[] {137, 500, events.size() - 20}) {
                    ConsolidatedBook part = new ConsolidatedBook(events.get(0).instrumentId, window);
                    for (int i = 0; i < split; i++) {
                        part.apply(events.get(i));
                    }
                    TreeMap<Long, ConsolidatedBook.Checkpoint> books = new TreeMap<>();
                    books.put(events.get(0).instrumentId, part.checkpoint());
                    ReplayEngine.Checkpoint ecp = new ReplayEngine.Checkpoint(split, 0, 0, 0, 0, 0, 0, 4, 4,
                            0, window, null, books);
                    ReplayEngine.Checkpoint back = CheckpointJson.read(CheckpointJson.write(ecp));
                    ConsolidatedBook resumed = ConsolidatedBook.restore(
                            back.books.get(events.get(0).instrumentId));
                    for (int i = split; i < events.size(); i++) {
                        resumed.apply(events.get(i));
                    }
                    assertEquals(name + " window " + window + " split " + split,
                            full.checkpoint(), resumed.checkpoint());
                }
            }
        }
    }

    @Test
    public void checkpointEq1000InterchangeWithPython() {
        List<MarketEvent> events = Golden.eq();
        String text = new String(Golden.bytes("expected_checkpoint_eq_1000.json"), StandardCharsets.UTF_8);
        ReplayEngine.Checkpoint cp = CheckpointJson.read(text);
        ReplayEngine resumed = ReplayEngine.restore(cp);
        assertEquals(4, resumed.keepCheckpoints);
        resumed.run(events.subList(1000, events.size()));
        Map<String, Object> expected = Golden.json("expected_book_states.json");
        Map<String, Object> states = Json.object(expected.get("states"));
        assertEquals(Golden.bookState(Json.object(states.get("2000"))),
                resumed.bookStates().get(1L).get(1));
        ReplayEngine full = new ReplayEngine();
        full.run(events);
        assertEquals(full.checkpoint(), resumed.checkpoint());
        // Our own checkpoint at 1000 is structurally identical to Python's.
        ReplayEngine own = new ReplayEngine();
        own.run(events.subList(0, 1000));
        assertEquals(cp, own.checkpoint());
        assertEquals(Json.parse(text), Json.parse(CheckpointJson.write(own.checkpoint())));
        // Wrong versions are rejected (fail closed).
        try {
            CheckpointJson.read(text.replace("\"x-version\": 2,\n  \"events_processed\"",
                    "\"x-version\": 1,\n  \"events_processed\""));
            fail("x-version 1 must be rejected");
        } catch (IllegalArgumentException expected2) {
            assertTrue(expected2.getMessage().contains("x-version"));
        }
    }

    @Test
    public void jsonlRejectCasesFixtureParity() throws IOException {
        List<String> lines = Files.readAllLines(Golden.DIR.resolve("jsonl_reject_cases.txt"),
                StandardCharsets.UTF_8);
        int rejected = 0;
        int accepted = 0;
        boolean acceptBlock = false;
        List<String> wronglyAccepted = new ArrayList<>();
        for (String line : lines) {
            if (line.equals("# ACCEPT")) {
                acceptBlock = true;
                continue;
            }
            if (line.isEmpty() || line.startsWith("#")) {
                continue;
            }
            if (acceptBlock) {
                MarketEvent ev = JsonlCodec.decodeLine(line);
                assertEquals(ev, JsonlCodec.decodeLine(JsonlCodec.encodeLine(ev)));
                accepted++;
            } else {
                try {
                    JsonlCodec.decodeLine(line);
                    wronglyAccepted.add(line);
                } catch (IllegalArgumentException expected) {
                    rejected++;
                }
            }
        }
        assertEquals("accepted lines that must be rejected: " + wronglyAccepted, 0, wronglyAccepted.size());
        assertTrue(rejected >= 25);
        assertTrue(accepted >= 5);
    }
}

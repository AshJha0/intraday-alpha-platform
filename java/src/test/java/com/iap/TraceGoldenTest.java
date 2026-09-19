package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.TreeMap;

import org.junit.Test;

import com.iap.contracts.CanonicalJson;
import com.iap.trace.Attribution;
import com.iap.trace.DecisionTrace;
import com.iap.trace.Explain;
import com.iap.trace.JsonlTraceSink;
import com.iap.trace.TraceDigest;

/**
 * Decision-trace parity with the Python reference: the example
 * {@code DecisionTrace} of tests/golden/expected_contracts_examples.json
 * re-serialises to the pinned canonical line (sha256 + length in
 * expected_canonical_json.json), the pinned stream digests, and the
 * pinned {@code explain()} text.
 */
public class TraceGoldenTest {
    private static Map<String, Object> example() {
        Map<String, Object> ex = Json.object(Json.object(
                Golden.json("expected_contracts_examples.json").get("examples"))
                .get("DecisionTrace"));
        assertEquals("trace/decision_trace.schema.json", ex.get("schema"));
        assertEquals(1L, Json.asLong(ex.get("x_version")));
        return Json.object(ex.get("value"));
    }

    private static Map<String, Object> digestPins() {
        return Json.object(Golden.json("expected_canonical_json.json")
                .get("trace_digest"));
    }

    @Test
    public void exampleTraceReserialisesToThePinnedLine() {
        Map<String, Object> value = example();
        DecisionTrace trace = DecisionTrace.fromTree(value);
        String line = trace.toLine();
        Map<String, Object> pins = digestPins();
        assertEquals(Json.asLong(pins.get("line_length")), line.length());
        assertEquals(pins.get("line_sha256"), CanonicalJson.sha256Hex(line));
        // the typed round trip is lossless against the raw tree
        assertEquals(CanonicalJson.serialize(value), line);
        assertEquals(trace, DecisionTrace.fromLine(line));
        assertEquals(CanonicalJson.makeTraceId(trace.sessionId(),
                trace.instrumentId(), trace.eventTs(), trace.sequence()),
                trace.traceId());
    }

    @Test
    public void digestsMatchThePinnedStreams() throws IOException {
        DecisionTrace trace = DecisionTrace.fromTree(example());
        Map<String, Object> pins = digestPins();
        assertEquals(pins.get("digest_empty_stream"), new TraceDigest().hexDigest());
        TraceDigest one = new TraceDigest().update(trace);
        assertEquals(pins.get("digest_one_trace"), one.hexDigest());
        assertEquals(1, one.count());
        TraceDigest twice = new TraceDigest().update(trace).update(trace);
        assertEquals(pins.get("digest_same_trace_twice"), twice.hexDigest());
        // hexDigest() is non-destructive
        assertEquals(pins.get("digest_same_trace_twice"), twice.hexDigest());

        // the JSONL sink writes exactly line + "\n" and its digest equals
        // the re-canonicalised file digest
        Path dir = Files.createTempDirectory("iap-trace");
        Path file = dir.resolve("decision_traces.jsonl");
        try (JsonlTraceSink sink = new JsonlTraceSink(file)) {
            sink.emit(trace);
            sink.emit(trace);
            assertEquals(2, sink.pendingLines());
            sink.flush();
            assertEquals(0, sink.pendingLines());
            assertEquals(pins.get("digest_same_trace_twice"), sink.digest().hexDigest());
        }
        String text = Files.readString(file, StandardCharsets.US_ASCII);
        assertEquals(trace.toLine() + "\n" + trace.toLine() + "\n", text);
        assertEquals(pins.get("digest_same_trace_twice"),
                TraceDigest.ofJsonl(file).hexDigest());
        // a resumed sink continues the digest instead of restarting it
        try (JsonlTraceSink resumed = new JsonlTraceSink(file, true,
                TraceDigest.ofJsonl(file))) {
            resumed.emit(trace);
            resumed.flush();
            assertEquals(3, resumed.count());
            assertEquals(new TraceDigest().update(trace).update(trace).update(trace)
                    .hexDigest(), resumed.digest().hexDigest());
        }
        assertEquals(3, TraceDigest.ofJsonl(file).count());
    }

    @Test
    public void explainRendersThePinnedBlock() {
        Map<String, Object> pin = Json.object(
                Golden.json("expected_contracts_examples.json").get("explain"));
        Map<Integer, String> names = new TreeMap<>();
        for (Map.Entry<String, Object> e : Json.object(pin.get("venue_names")).entrySet()) {
            names.put(Integer.parseInt(e.getKey()), (String) e.getValue());
        }
        DecisionTrace trace = DecisionTrace.fromTree(example());
        assertEquals(pin.get("text"), Explain.render(trace, names));
        // unnamed venues render as decimal ids; nothing else changes
        String unnamed = Explain.render(trace, Map.of());
        assertTrue(unnamed, unnamed.contains("SOR:        1 = 45%  2 = 35%  3 = 20%"));
    }

    @Test
    public void attributionIdentityIsPinned() {
        DecisionTrace trace = DecisionTrace.fromTree(example());
        Attribution a = Attribution.of(0, trace.stages().signal().get(0).expectedReturn(),
                trace.stages().tca().get(0));
        // the golden's Attribution is a fixture (6.2/-0.8/-2.1/-0.4/-0.2);
        // attribute() of the example inputs gives 4.2/-0.8/-0.5/-0.4/-0.2 = 2.3
        assertEquals(4.2, a.alphaBps(), 1e-9);
        assertEquals(-0.8, a.spreadBps(), 1e-9);
        assertEquals(-0.5, a.impactBps(), 1e-9);
        assertEquals(-0.4, a.feesBps(), 1e-9);
        assertEquals(-0.2, a.timingBps(), 1e-9);
        assertEquals(2.3, a.totalBps(), 1e-9);
    }
}

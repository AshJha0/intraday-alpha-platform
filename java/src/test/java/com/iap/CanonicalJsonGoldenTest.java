package com.iap;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.math.BigInteger;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.junit.Test;

import com.iap.contracts.CanonicalJson;

/**
 * Byte-level parity of {@link CanonicalJson} with the Python reference
 * ({@code iap.contracts.versions.canonical_json}) against every case in
 * tests/golden/expected_canonical_json.json: float layout from raw bits,
 * string escaping from code points, whole documents + sha256, the trace id
 * and a parse-then-reserialise of every pinned document.
 */
public class CanonicalJsonGoldenTest {
    private static Map<String, Object> golden() {
        return Golden.json("expected_canonical_json.json");
    }

    @Test
    public void everyFloatReprCaseMatchesPythonRepr() {
        List<Object> cases = Json.array(golden().get("float_repr"));
        assertTrue("golden carries the float cases", cases.size() > 2000);
        for (Object o : cases) {
            Map<String, Object> c = Json.object(o);
            String bits = (String) c.get("bits_hex");
            double v = Double.longBitsToDouble(Long.parseUnsignedLong(bits, 16));
            assertEquals("bits " + bits, c.get("repr"), CanonicalJson.floatRepr(v));
            // and the repr round-trips to the same double
            assertEquals("round trip " + bits, Double.doubleToRawLongBits(v),
                    Double.doubleToRawLongBits(Double.parseDouble(
                            CanonicalJson.floatRepr(v))));
        }
    }

    @Test
    public void everyStringEscapeCaseMatches() {
        for (Object o : Json.array(golden().get("string_escape"))) {
            Map<String, Object> c = Json.object(o);
            StringBuilder sb = new StringBuilder();
            for (Object cp : Json.array(c.get("input_codepoints"))) {
                sb.appendCodePoint((int) Json.asLong(cp));
            }
            assertEquals(c.get("json"), CanonicalJson.quote(sb.toString()));
            assertEquals(c.get("json"), CanonicalJson.serialize(sb.toString()));
        }
    }

    @Test
    public void everyDocumentReserialisesByteIdenticallyWithItsSha256() {
        List<Object> docs = Json.array(golden().get("documents"));
        assertEquals(9, docs.size());
        for (Object o : docs) {
            Map<String, Object> c = Json.object(o);
            String canonical = (String) c.get("canonical");
            // parse in wide-integer mode: u64 max keeps its decimal value
            Object tree = com.iap.config.Json.parse(canonical, true);
            assertEquals(canonical, CanonicalJson.serialize(tree));
            assertEquals(c.get("sha256"), CanonicalJson.contentHash(tree));
            assertEquals(c.get("sha256"), CanonicalJson.sha256Hex(canonical));
        }
    }

    @Test
    public void u64AndI64ExtremesAreExact() {
        Map<String, Object> ids = new LinkedHashMap<>();
        ids.put("u64_max", new BigInteger("18446744073709551615"));
        ids.put("i64_min", Long.MIN_VALUE);
        ids.put("i64_max", Long.MAX_VALUE);
        ids.put("two53_plus_one", 9007199254740993L);
        Map<String, Object> doc = Map.of("ids", ids);
        String expected = "{\"ids\":{\"i64_max\":9223372036854775807,"
                + "\"i64_min\":-9223372036854775808,"
                + "\"two53_plus_one\":9007199254740993,"
                + "\"u64_max\":18446744073709551615}}";
        assertEquals(expected, CanonicalJson.serialize(doc));
    }

    @Test
    public void keysSortByCodePointNotUtf16Units() {
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("\uFFFF", 1L);          // U+FFFF
        doc.put("\uD835\uDD18", 2L);   // U+1D518 (surrogate pair)
        doc.put("A", 3L);
        assertEquals("{\"A\":3,\"\\uffff\":1,\"\\ud835\\udd18\":2}",
                CanonicalJson.serialize(doc));
    }

    @Test
    public void rejectsNonFiniteAndNonStringKeys() {
        for (double bad : new double[] {Double.NaN, Double.POSITIVE_INFINITY,
                Double.NEGATIVE_INFINITY}) {
            try {
                CanonicalJson.serialize(Map.of("x", List.of(1L, bad)));
                fail("accepted " + bad);
            } catch (IllegalArgumentException expected) {
                assertTrue(expected.getMessage().contains("non-finite"));
            }
        }
        Map<Object, Object> intKey = new LinkedHashMap<>();
        intKey.put(1L, "x");
        try {
            CanonicalJson.serialize(intKey);
            fail("accepted an integer key");
        } catch (IllegalArgumentException expected) {
            assertTrue(expected.getMessage().contains("keys must be strings"));
        }
    }

    @Test
    public void traceIdMatchesThePinnedPreimageAndHash() {
        Map<String, Object> t = Json.object(golden().get("trace_id"));
        Map<String, Object> in = Json.object(t.get("inputs"));
        String id = CanonicalJson.makeTraceId((String) in.get("session_id"),
                Json.asLong(in.get("instrument_id")), Json.asLong(in.get("event_ts")),
                Json.asLong(in.get("sequence")));
        assertEquals(t.get("expected"), id);
        assertEquals(t.get("expected"),
                CanonicalJson.sha256Hex((String) t.get("preimage")).substring(0, 32));
        // the contracts golden pins the same id
        Map<String, Object> c = Json.object(
                Golden.json("expected_contracts_examples.json").get("trace_id"));
        assertEquals(c.get("expected"), id);
    }

    @Test
    public void contractsGoldenCanonicalKnownAnswer() {
        Map<String, Object> c = Json.object(
                Golden.json("expected_contracts_examples.json").get("canonical_json"));
        String text = CanonicalJson.serialize(c.get("input"));
        assertEquals(c.get("text"), text);
        assertEquals(c.get("sha256"), CanonicalJson.sha256Hex(text));
    }

    @Test
    public void indentedLayoutMatchesPythonIndentTwo() {
        Map<String, Object> doc = new LinkedHashMap<>();
        doc.put("b", List.of(1L, Map.of(), List.of()));
        doc.put("a", Map.of("y", -0.0, "z", "\u00e9"));
        String expected = "{\n  \"a\": {\n    \"y\": -0.0,\n    \"z\": \"\\u00e9\"\n  },\n"
                + "  \"b\": [\n    1,\n    {},\n    []\n  ]\n}";
        assertEquals(expected, CanonicalJson.indented(doc));
    }
}

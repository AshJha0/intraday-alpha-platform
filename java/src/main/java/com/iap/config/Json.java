package com.iap.config;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON parser for the platform config files (configs/*.json) and
 * golden fixtures. Produces {@code Map<String,Object>} (insertion-ordered),
 * {@code List<Object>}, {@code String}, {@code Long} (u64 bit pattern for
 * huge integers), {@code Double}, {@code Boolean}, or {@code null}. Not a
 * general-purpose parser: just enough for the pinned platform files, strict
 * about trailing garbage.
 */
public final class Json {
    private final String s;
    private int pos;

    private Json(String s) {
        this.s = s;
    }

    /** Parse a complete JSON document; rejects trailing content. */
    public static Object parse(String text) {
        Json p = new Json(text);
        p.ws();
        Object v = p.value();
        p.ws();
        if (p.pos != text.length()) {
            throw new IllegalArgumentException("trailing JSON content at " + p.pos);
        }
        return v;
    }

    /** Parse a JSON file (UTF-8). */
    public static Object parseFile(Path path) {
        try {
            return parse(new String(Files.readAllBytes(path), StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new IllegalStateException("cannot read JSON file " + path, e);
        }
    }

    /** Cast helper: the value as an object (map). */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> object(Object v) {
        return (Map<String, Object>) v;
    }

    /** Cast helper: the value as an array (list). */
    @SuppressWarnings("unchecked")
    public static List<Object> array(Object v) {
        return (List<Object>) v;
    }

    /** Numeric value as a long (integer JSON numbers only). */
    public static long asLong(Object v) {
        return ((Long) v).longValue();
    }

    /** Numeric value as a double (integer or floating JSON numbers). */
    public static double asDouble(Object v) {
        if (v instanceof Long l) {
            return l.doubleValue();
        }
        return ((Double) v).doubleValue();
    }

    private void ws() {
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if (c != ' ' && c != '\t' && c != '\n' && c != '\r') {
                break;
            }
            pos++;
        }
    }

    private char peek() {
        if (pos >= s.length()) {
            throw new IllegalArgumentException("unexpected end of JSON at " + pos);
        }
        return s.charAt(pos);
    }

    private void expect(char c) {
        if (peek() != c) {
            throw new IllegalArgumentException(
                    "expected '" + c + "' at " + pos + ", got '" + peek() + "'");
        }
        pos++;
    }

    private Object value() {
        char c = peek();
        return switch (c) {
            case '{' -> obj();
            case '[' -> arr();
            case '"' -> str();
            case 't' -> lit("true", Boolean.TRUE);
            case 'f' -> lit("false", Boolean.FALSE);
            case 'n' -> lit("null", null);
            default -> num();
        };
    }

    private Object lit(String word, Object v) {
        if (!s.startsWith(word, pos)) {
            throw new IllegalArgumentException("bad literal at " + pos);
        }
        pos += word.length();
        return v;
    }

    private Map<String, Object> obj() {
        expect('{');
        Map<String, Object> out = new LinkedHashMap<>();
        ws();
        if (peek() == '}') {
            pos++;
            return out;
        }
        while (true) {
            ws();
            String key = str();
            ws();
            expect(':');
            ws();
            out.put(key, value());
            ws();
            if (peek() == ',') {
                pos++;
            } else {
                expect('}');
                return out;
            }
        }
    }

    private List<Object> arr() {
        expect('[');
        List<Object> out = new ArrayList<>();
        ws();
        if (peek() == ']') {
            pos++;
            return out;
        }
        while (true) {
            ws();
            out.add(value());
            ws();
            if (peek() == ',') {
                pos++;
            } else {
                expect(']');
                return out;
            }
        }
    }

    private String str() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            char c = peek();
            pos++;
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                char e = peek();
                pos++;
                switch (e) {
                    case '"' -> sb.append('"');
                    case '\\' -> sb.append('\\');
                    case '/' -> sb.append('/');
                    case 'b' -> sb.append('\b');
                    case 'f' -> sb.append('\f');
                    case 'n' -> sb.append('\n');
                    case 'r' -> sb.append('\r');
                    case 't' -> sb.append('\t');
                    case 'u' -> {
                        sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                        pos += 4;
                    }
                    default -> throw new IllegalArgumentException("bad escape \\" + e);
                }
            } else {
                sb.append(c);
            }
        }
    }

    private Object num() {
        int start = pos;
        boolean floating = false;
        if (pos < s.length() && s.charAt(pos) == '-') {
            pos++;
        }
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if (c >= '0' && c <= '9') {
                pos++;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                floating = true;
                pos++;
            } else {
                break;
            }
        }
        String token = s.substring(start, pos);
        if (token.isEmpty() || token.equals("-")) {
            throw new IllegalArgumentException("bad number at " + start);
        }
        if (floating) {
            return Double.parseDouble(token);
        }
        try {
            return Long.parseLong(token);
        } catch (NumberFormatException e) {
            return Long.parseUnsignedLong(token);
        }
    }
}

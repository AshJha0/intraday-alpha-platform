package com.iap.contracts;

import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

import com.iap.codec.Sha256;

/**
 * Canonical JSON — the byte-level contract shared by every language
 * ({@code iap.contracts.versions.canonical_json}, golden
 * {@code tests/golden/expected_canonical_json.json}): exactly Python's
 * {@code json.dumps(obj, sort_keys=True, separators=(",", ":"),
 * ensure_ascii=True, allow_nan=False)}.
 *
 * <p>Tree model: {@code Map<String,Object>} (any map; keys are sorted here
 * by Unicode code point of the raw key, recursively), {@code List<Object>},
 * {@code String}, {@code Long}/{@code Integer} (i64), {@code BigInteger}
 * (integers outside i64 — the u64 upper half, as
 * {@code Json.parse(text, true)} produces them), {@code Double}/{@code Float}
 * (finite only), {@code Boolean} and {@code null}.
 *
 * <p>Float layout is Python's {@code float.__repr__}: the shortest digit
 * string that round-trips (Java 21's {@code Double.toString} supplies the
 * digits, JDK-4511638), laid out in exponent form iff the decimal exponent
 * is {@code < -4} or {@code >= 16} with the exponent written {@code e-05} /
 * {@code e+16}, integral values keeping {@code .0}, and {@code -0.0}
 * preserved. NaN / infinities are rejected with
 * {@link IllegalArgumentException}.
 */
public final class CanonicalJson {
    private CanonicalJson() {
    }

    /** Raw-key code-point order (Python {@code sort_keys=True}). */
    public static final Comparator<String> KEY_ORDER = CanonicalJson::compareCodePoints;

    /** Serialise a tree to canonical JSON text. */
    public static String serialize(Object tree) {
        StringBuilder sb = new StringBuilder(256);
        write(tree, sb);
        return sb.toString();
    }

    /**
     * Serialise a tree exactly like {@code json.dumps(obj, sort_keys=True,
     * indent=2, ensure_ascii=True, allow_nan=False)} (two-space indent,
     * {@code ": "} and {@code ","} separators, empty containers as
     * {@code {}} / {@code []}); no trailing newline.
     */
    public static String indented(Object tree) {
        StringBuilder sb = new StringBuilder(1024);
        writeIndented(tree, sb, 0);
        return sb.toString();
    }

    /** Lower-case SHA-256 hex of the UTF-8 bytes of {@code text}. */
    public static String sha256Hex(String text) {
        return Sha256.hex(text.getBytes(StandardCharsets.UTF_8));
    }

    /** {@code sha256Hex(serialize(tree))} — the content hash of a document. */
    public static String contentHash(Object tree) {
        return sha256Hex(serialize(tree));
    }

    /**
     * Trace id: the first 32 hex characters of
     * {@code sha256("<session_id>|<instrument_id>|<event_ts>|<sequence>")}
     * with the integers in decimal ({@code sequence} unsigned).
     */
    public static String makeTraceId(String sessionId, long instrumentId,
            long eventTs, long sequence) {
        String preimage = sessionId + "|" + instrumentId + "|" + eventTs + "|"
                + Long.toUnsignedString(sequence);
        return sha256Hex(preimage).substring(0, 32);
    }

    /** Python {@code float.__repr__} of a finite double. */
    public static String floatRepr(double v) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            throw new IllegalArgumentException(
                    "canonical JSON: non-finite float " + v);
        }
        if (v == 0.0) {
            return (Double.doubleToRawLongBits(v) < 0) ? "-0.0" : "0.0";
        }
        boolean negative = v < 0.0;
        String s = Double.toString(Math.abs(v));
        int ePos = s.indexOf('E');
        String mantissa = ePos < 0 ? s : s.substring(0, ePos);
        int exp10 = ePos < 0 ? 0 : Integer.parseInt(s.substring(ePos + 1));
        int dot = mantissa.indexOf('.');
        String intPart = dot < 0 ? mantissa : mantissa.substring(0, dot);
        String fracPart = dot < 0 ? "" : mantissa.substring(dot + 1);
        // digits = intPart + fracPart, value = 0.digits * 10^(len(intPart)+exp10)
        String digits = intPart + fracPart;
        int pointPos = intPart.length() + exp10; // decimal point after this many digits
        int lead = 0;
        while (lead < digits.length() - 1 && digits.charAt(lead) == '0') {
            lead++;
        }
        digits = digits.substring(lead);
        pointPos -= lead;
        int end = digits.length();
        while (end > 1 && digits.charAt(end - 1) == '0') {
            end--;
        }
        digits = digits.substring(0, end);
        // scientific exponent: value = d.ddd * 10^sciExp
        int sciExp = pointPos - 1;
        // Double.toString prints at least two significant digits (d.d), so
        // a value that round-trips from ONE digit (Python: 5e-324) comes
        // back as two (4.9E-324): try the one-digit roundings and keep the
        // one that parses back to the same double.
        if (digits.length() == 2) {
            double abs = Math.abs(v);
            int d0 = digits.charAt(0) - '0';
            int up = d0 + 1;
            String nearest = digits.charAt(1) >= '5' ? Integer.toString(up)
                    : Integer.toString(d0);
            String other = digits.charAt(1) >= '5' ? Integer.toString(d0)
                    : Integer.toString(up);
            for (String cand : new String[] {nearest, other}) {
                int e = sciExp;
                String c = cand;
                if (c.equals("10")) {
                    c = "1";
                    e++;
                }
                if (Double.parseDouble(c + "e" + e) == abs) {
                    digits = c;
                    sciExp = e;
                    pointPos = e + 1;
                    break;
                }
            }
        }
        StringBuilder out = new StringBuilder(24);
        if (negative) {
            out.append('-');
        }
        if (sciExp < -4 || sciExp >= 16) {
            out.append(digits.charAt(0));
            if (digits.length() > 1) {
                out.append('.').append(digits, 1, digits.length());
            }
            out.append('e').append(sciExp < 0 ? '-' : '+');
            int ae = Math.abs(sciExp);
            if (ae < 10) {
                out.append('0');
            }
            out.append(ae);
        } else if (pointPos <= 0) {
            out.append("0.");
            for (int i = pointPos; i < 0; i++) {
                out.append('0');
            }
            out.append(digits);
        } else if (pointPos >= digits.length()) {
            out.append(digits);
            for (int i = digits.length(); i < pointPos; i++) {
                out.append('0');
            }
            out.append(".0");
        } else {
            out.append(digits, 0, pointPos).append('.')
                    .append(digits, pointPos, digits.length());
        }
        return out.toString();
    }

    /** Python {@code json.dumps(ensure_ascii=True)} string literal, with quotes. */
    public static String quote(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 8);
        writeString(s, sb);
        return sb.toString();
    }

    private static void writeString(String s, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                default -> {
                    if (c < 0x20 || c > 0x7e) {
                        sb.append("\\u");
                        String hex = Integer.toHexString(c);
                        for (int k = hex.length(); k < 4; k++) {
                            sb.append('0');
                        }
                        sb.append(hex);
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }

    private static void writeScalar(Object v, StringBuilder sb) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(s, sb);
        } else if (v instanceof Boolean b) {
            sb.append(b ? "true" : "false");
        } else if (v instanceof Long || v instanceof Integer
                || v instanceof BigInteger || v instanceof Short
                || v instanceof Byte) {
            sb.append(v.toString());
        } else if (v instanceof Double d) {
            sb.append(floatRepr(d));
        } else if (v instanceof Float f) {
            sb.append(floatRepr(f.doubleValue()));
        } else {
            throw new IllegalArgumentException(
                    "canonical JSON: unsupported value type "
                            + v.getClass().getName());
        }
    }

    private static List<String> sortedKeys(Map<?, ?> map) {
        List<String> keys = new ArrayList<>(map.size());
        for (Object k : map.keySet()) {
            if (!(k instanceof String s)) {
                throw new IllegalArgumentException(
                        "canonical JSON: object keys must be strings, got "
                                + (k == null ? "null" : k.getClass().getName()));
            }
            keys.add(s);
        }
        keys.sort(KEY_ORDER);
        return keys;
    }

    private static void write(Object v, StringBuilder sb) {
        if (v instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (String k : sortedKeys(map)) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(k, sb);
                sb.append(':');
                write(map.get(k), sb);
            }
            sb.append('}');
        } else if (v instanceof List<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                write(item, sb);
            }
            sb.append(']');
        } else {
            writeScalar(v, sb);
        }
    }

    private static void newline(StringBuilder sb, int level) {
        sb.append('\n');
        for (int i = 0; i < level; i++) {
            sb.append("  ");
        }
    }

    private static void writeIndented(Object v, StringBuilder sb, int level) {
        if (v instanceof Map<?, ?> map) {
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append('{');
            boolean first = true;
            for (String k : sortedKeys(map)) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                newline(sb, level + 1);
                writeString(k, sb);
                sb.append(": ");
                writeIndented(map.get(k), sb, level + 1);
            }
            newline(sb, level);
            sb.append('}');
        } else if (v instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                newline(sb, level + 1);
                writeIndented(item, sb, level + 1);
            }
            newline(sb, level);
            sb.append(']');
        } else {
            writeScalar(v, sb);
        }
    }

    private static int compareCodePoints(String a, String b) {
        int i = 0;
        int j = 0;
        while (i < a.length() && j < b.length()) {
            int ca = a.codePointAt(i);
            int cb = b.codePointAt(j);
            if (ca != cb) {
                return Integer.compare(ca, cb);
            }
            i += Character.charCount(ca);
            j += Character.charCount(cb);
        }
        return Integer.compare(a.length() - i, b.length() - j);
    }
}

package com.iap.contracts;

import java.math.BigInteger;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Strict typed access to a JSON tree ({@code Map<String,Object>} /
 * {@code List<Object>} / scalars as {@code com.iap.config.Json} produces
 * them) for the contract records: every accessor names the offending path
 * in its {@link IllegalArgumentException}, integers are range-checked
 * ({@code u16} / {@code u32} / {@code i64} / {@code u64}), a boolean is
 * never accepted as a number, and floats must be finite.
 *
 * <p>u64 convention (the platform's codec convention): a u64 is carried in
 * a {@code long} as its bit pattern; on the tree it is a {@code Long} when
 * it fits i64 and a {@link BigInteger} above ({@link #u64Tree}).
 */
public final class Trees {
    private static final BigInteger U64_MAX = BigInteger.ONE.shiftLeft(64)
            .subtract(BigInteger.ONE);

    private Trees() {
    }

    /** The value at {@code path} as an object. */
    public static Map<String, Object> obj(Object v, String path) {
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException(path + ": expected object, got "
                    + typeName(v));
        }
        return com.iap.config.Json.object(v);
    }

    /** The value at {@code path} as an array. */
    public static List<Object> arr(Object v, String path) {
        if (!(v instanceof List)) {
            throw new IllegalArgumentException(path + ": expected array, got "
                    + typeName(v));
        }
        return com.iap.config.Json.array(v);
    }

    /** Reject unknown and missing keys ({@code required} in schema order). */
    public static void checkKeys(Map<String, Object> map, String[] required,
            String path) {
        Set<String> allowed = new TreeSet<>(List.of(required));
        for (String k : map.keySet()) {
            if (!allowed.contains(k)) {
                throw new IllegalArgumentException(path + ": unknown key '" + k + "'");
            }
        }
        for (String k : required) {
            if (!map.containsKey(k)) {
                throw new IllegalArgumentException(path + ": missing key '" + k + "'");
            }
        }
    }

    private static Object need(Map<String, Object> map, String key, String path) {
        if (!map.containsKey(key)) {
            throw new IllegalArgumentException(path + "." + key + ": missing");
        }
        return map.get(key);
    }

    /** Signed 64-bit integer (a {@code Long} on the tree). */
    public static long i64(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        if (!(v instanceof Long l)) {
            throw new IllegalArgumentException(path + "." + key
                    + ": expected integer, got " + typeName(v));
        }
        return l;
    }

    /** Integer in {@code [min, max]}. */
    public static long ranged(Map<String, Object> map, String key, String path,
            long min, long max) {
        long v = i64(map, key, path);
        if (v < min || v > max) {
            throw new IllegalArgumentException(path + "." + key + ": " + v
                    + " outside [" + min + ", " + max + "]");
        }
        return v;
    }

    /** u32 (0..4294967295) as a long. */
    public static long u32(Map<String, Object> map, String key, String path) {
        return ranged(map, key, path, 0, 0xFFFFFFFFL);
    }

    /** u16 (0..65535) as an int. */
    public static int u16(Map<String, Object> map, String key, String path) {
        return (int) ranged(map, key, path, 0, 0xFFFF);
    }

    /** u64 as its bit pattern (a {@code Long} or a {@code BigInteger} on the tree). */
    public static long u64(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        if (v instanceof Long l) {
            if (l < 0) {
                throw new IllegalArgumentException(path + "." + key + ": " + l
                        + " is negative (u64)");
            }
            return l;
        }
        if (v instanceof BigInteger b) {
            if (b.signum() < 0 || b.compareTo(U64_MAX) > 0) {
                throw new IllegalArgumentException(path + "." + key + ": " + b
                        + " outside the u64 domain");
            }
            return b.longValue();
        }
        throw new IllegalArgumentException(path + "." + key
                + ": expected integer, got " + typeName(v));
    }

    /** Tree value of a u64 bit pattern (Long when it fits i64, else BigInteger). */
    public static Object u64Tree(long v) {
        return v >= 0 ? (Object) Long.valueOf(v)
                : new BigInteger(Long.toUnsignedString(v));
    }

    /** Finite number (integer or float on the tree). */
    public static double num(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        return finite(v, path + "." + key);
    }

    /** Finite number or {@code null}. */
    public static Double optNum(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        return v == null ? null : finite(v, path + "." + key);
    }

    private static double finite(Object v, String where) {
        double d;
        if (v instanceof Long l) {
            d = l.doubleValue();
        } else if (v instanceof Double x) {
            d = x;
        } else if (v instanceof BigInteger b) {
            d = b.doubleValue();
        } else {
            throw new IllegalArgumentException(where + ": expected number, got "
                    + typeName(v));
        }
        if (!Double.isFinite(d)) {
            throw new IllegalArgumentException(where + ": non-finite number");
        }
        return d;
    }

    /** Non-null string. */
    public static String str(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        if (!(v instanceof String s)) {
            throw new IllegalArgumentException(path + "." + key
                    + ": expected string, got " + typeName(v));
        }
        return s;
    }

    /** String or {@code null}. */
    public static String optStr(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s)) {
            throw new IllegalArgumentException(path + "." + key
                    + ": expected string or null, got " + typeName(v));
        }
        return s;
    }

    /** Boolean. */
    public static boolean bool(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        if (!(v instanceof Boolean b)) {
            throw new IllegalArgumentException(path + "." + key
                    + ": expected boolean, got " + typeName(v));
        }
        return b;
    }

    /** Boolean or {@code null}. */
    public static Boolean optBool(Map<String, Object> map, String key, String path) {
        Object v = need(map, key, path);
        if (v == null) {
            return null;
        }
        if (!(v instanceof Boolean b)) {
            throw new IllegalArgumentException(path + "." + key
                    + ": expected boolean or null, got " + typeName(v));
        }
        return b;
    }

    /** Object-valued member. */
    public static Map<String, Object> obj(Map<String, Object> map, String key,
            String path) {
        return obj(need(map, key, path), path + "." + key);
    }

    /** Array-valued member. */
    public static List<Object> arr(Map<String, Object> map, String key, String path) {
        return arr(need(map, key, path), path + "." + key);
    }

    /** A finite double that a record accepted (constructor guard). */
    public static double finite(double v, String where) {
        if (!Double.isFinite(v)) {
            throw new IllegalArgumentException(where + ": non-finite number " + v);
        }
        return v;
    }

    /** Lowercase 64-hex sha256 check. */
    public static boolean isSha256Hex(String s) {
        if (s == null || s.length() != 64) {
            return false;
        }
        for (int i = 0; i < 64; i++) {
            char c = s.charAt(i);
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
                return false;
            }
        }
        return true;
    }

    /** Generic identifier: {@code ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$}. */
    public static boolean isGenericId(String s) {
        if (s == null || s.isEmpty() || s.length() > 128) {
            return false;
        }
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            boolean alnum = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z')
                    || (c >= 'a' && c <= 'z');
            if (i == 0 ? !alnum
                    : !(alnum || c == '.' || c == '_' || c == ':' || c == '-')) {
                return false;
            }
        }
        return true;
    }

    /** Require a sha256 hex string at {@code where}. */
    public static String sha256(String s, String where) {
        if (!isSha256Hex(s)) {
            throw new IllegalArgumentException(where + ": expected a lowercase sha256 hex");
        }
        return s;
    }

    /** Require a generic identifier at {@code where}. */
    public static String ident(String s, String where) {
        if (!isGenericId(s)) {
            throw new IllegalArgumentException(where + ": '" + s
                    + "' is not a valid identifier");
        }
        return s;
    }

    /** Ordered tree builder (schema key order). */
    public static Map<String, Object> ordered() {
        return new LinkedHashMap<>();
    }

    private static String typeName(Object v) {
        return v == null ? "null" : v.getClass().getSimpleName();
    }
}

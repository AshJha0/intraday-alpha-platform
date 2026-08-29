package com.iap.codec;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/** SHA-256 helper for codec golden parity (hex digests). */
public final class Sha256 {
    private static final char[] HEX = "0123456789abcdef".toCharArray();

    private Sha256() {
    }

    /** Hex SHA-256 of a byte array. */
    public static String hex(byte[] data) {
        MessageDigest md;
        try {
            md = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
        byte[] digest = md.digest(data);
        StringBuilder sb = new StringBuilder(digest.length * 2);
        for (byte b : digest) {
            sb.append(HEX[(b >> 4) & 0xF]).append(HEX[b & 0xF]);
        }
        return sb.toString();
    }
}

package com.iap.trace;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * Running SHA-256 over a trace stream (the pinned digest algorithm,
 * {@code iap.trace.digest.TraceDigest}): for every emitted trace, in
 * emission order, hash the ASCII bytes of its canonical line and then one
 * byte {@code 0x0A}. {@link #hexDigest()} is the lowercase hex over
 * everything hashed so far; the empty stream digests to
 * {@code e3b0c442...b855}. The digest of a live run equals
 * {@link #ofJsonl} of the JSONL it wrote (lines re-canonicalised), and the
 * same seed gives the same digest.
 */
public final class TraceDigest {
    /** The digest of the empty stream (sha256 of zero bytes). */
    public static final String EMPTY =
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    private static final byte[] NEWLINE = {0x0A};

    private final MessageDigest md;
    private long count;

    public TraceDigest() {
        try {
            md = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }

    /** Hash one trace ({@code trace.toLine()} + newline). */
    public TraceDigest update(DecisionTrace trace) {
        return updateLine(trace.toLine());
    }

    /** Hash one already-canonical line (no newline) + newline. */
    public TraceDigest updateLine(String line) {
        md.update(line.getBytes(StandardCharsets.US_ASCII));
        md.update(NEWLINE);
        count++;
        return this;
    }

    /** Number of traces hashed so far. */
    public long count() {
        return count;
    }

    /** Lowercase hex SHA-256 over everything hashed so far (non-destructive). */
    public String hexDigest() {
        try {
            MessageDigest copy = (MessageDigest) md.clone();
            byte[] out = copy.digest();
            StringBuilder sb = new StringBuilder(64);
            for (byte b : out) {
                sb.append(Character.forDigit((b >> 4) & 0xF, 16));
                sb.append(Character.forDigit(b & 0xF, 16));
            }
            return sb.toString();
        } catch (CloneNotSupportedException e) {
            throw new IllegalStateException("SHA-256 digest not cloneable", e);
        }
    }

    /**
     * Digest of a trace JSONL file: every non-empty line is parsed as a
     * {@link DecisionTrace} and re-canonicalised, so a file that is not a
     * valid trace stream is an error, never a silently different hash.
     */
    public static TraceDigest ofJsonl(Path path) {
        TraceDigest d = new TraceDigest();
        try {
            for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
                if (!line.isEmpty()) {
                    d.update(DecisionTrace.fromLine(line));
                }
            }
        } catch (IOException e) {
            throw new IllegalStateException("cannot read trace file " + path, e);
        }
        return d;
    }
}

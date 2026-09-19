package com.iap.trace;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

/**
 * Trace sink writing one canonical JSON line per {@link DecisionTrace}
 * to a JSONL file ({@code iap.trace.sinks.JsonlTraceSink}).
 *
 * <p>Lines are buffered in memory and appended to the file by
 * {@link #flush()} — complete lines only, followed by an fsync — so the
 * durable file never holds a torn line and the caller decides the commit
 * points (the paper platform flushes at every checkpoint and at session
 * end, like the risk audit). The running {@link TraceDigest} is updated on
 * {@link #emit}, in emission order, and equals {@link TraceDigest#ofJsonl}
 * of the file once flushed.
 */
public final class JsonlTraceSink implements AutoCloseable {
    private final Path path;
    private final TraceDigest digest;
    private final StringBuilder pending = new StringBuilder(4096);
    private long pendingLines;

    /**
     * Open a sink. {@code append} keeps an existing file and continues the
     * given digest (a resumed session); otherwise the file is truncated and
     * the digest must be fresh.
     */
    public JsonlTraceSink(Path path, boolean append, TraceDigest digest) {
        this.path = path;
        this.digest = digest;
        try {
            if (path.getParent() != null) {
                Files.createDirectories(path.getParent());
            }
            if (!append) {
                if (digest.count() != 0) {
                    throw new IllegalArgumentException(
                            "a fresh trace sink needs an empty digest, got "
                                    + digest.count() + " traces");
                }
                Files.write(path, new byte[0]);
            } else if (!Files.exists(path)) {
                Files.write(path, new byte[0]);
            }
        } catch (IOException e) {
            throw new UncheckedIOException("cannot open trace file " + path, e);
        }
    }

    /** A fresh (truncating) sink with a new digest. */
    public JsonlTraceSink(Path path) {
        this(path, false, new TraceDigest());
    }

    /** The file this sink writes. */
    public Path path() {
        return path;
    }

    /** The running digest (updated on every emit). */
    public TraceDigest digest() {
        return digest;
    }

    /** Traces emitted so far (including a resumed prefix). */
    public long count() {
        return digest.count();
    }

    /** Lines emitted but not yet flushed to disk. */
    public long pendingLines() {
        return pendingLines;
    }

    /** Record one trace: canonical line into the buffer and the digest. */
    public void emit(DecisionTrace trace) {
        String line = trace.toLine();
        pending.append(line).append('\n');
        pendingLines++;
        digest.updateLine(line);
    }

    /** Append the buffered lines to the file and fsync (no-op when empty). */
    public void flush() {
        if (pendingLines == 0) {
            return;
        }
        byte[] bytes = pending.toString().getBytes(StandardCharsets.US_ASCII);
        try (FileChannel ch = FileChannel.open(path, StandardOpenOption.CREATE,
                StandardOpenOption.WRITE, StandardOpenOption.APPEND)) {
            ch.write(ByteBuffer.wrap(bytes));
            ch.force(true);
        } catch (IOException e) {
            throw new UncheckedIOException("cannot append trace file " + path, e);
        }
        pending.setLength(0);
        pendingLines = 0;
    }

    /** Flush; the sink stays usable (idempotent). */
    @Override
    public void close() {
        flush();
    }
}

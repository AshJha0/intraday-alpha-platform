package com.iap.lifecycle;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;

import com.iap.config.Json;
import com.iap.contracts.Trees;

/**
 * Append-only canonical-JSON-lines log of {@link LifecycleTransition}s
 * ({@code research/lifecycle_transitions.jsonl}; the adaptive study's
 * {@code lifecycle_log.jsonl} is a different record and is left alone).
 * Replaying the log from RESEARCH reproduces each alpha's state.
 */
public final class LifecycleTransitionLog {
    private final Path path;

    /** Open (creating, optionally truncating) the log. */
    public LifecycleTransitionLog(Path path, boolean truncate) {
        this.path = path;
        try {
            if (path.getParent() != null) {
                Files.createDirectories(path.getParent());
            }
            if (truncate || !Files.exists(path)) {
                Files.write(path, new byte[0]);
            }
        } catch (IOException e) {
            throw new IllegalStateException("cannot open lifecycle log " + path, e);
        }
    }

    /** The log file. */
    public Path path() {
        return path;
    }

    /** Append one canonical line. */
    public void append(LifecycleTransition transition) {
        try {
            Files.write(path, (transition.toLine() + "\n").getBytes(StandardCharsets.US_ASCII),
                    StandardOpenOption.APPEND);
        } catch (IOException e) {
            throw new IllegalStateException("cannot append lifecycle log " + path, e);
        }
    }

    /** Every logged transition, in file order, strictly parsed. */
    public List<LifecycleTransition> readAll() {
        List<LifecycleTransition> out = new ArrayList<>();
        try {
            for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
                if (!line.isBlank()) {
                    out.add(LifecycleTransition.fromTree(
                            Trees.obj(Json.parse(line, true), path.toString())));
                }
            }
        } catch (IOException e) {
            throw new IllegalStateException("cannot read lifecycle log " + path, e);
        }
        return out;
    }
}

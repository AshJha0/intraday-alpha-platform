package com.iap.replay;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;

import com.iap.core.MarketEvent;
import com.iap.orderbook.BookState;
import com.iap.orderbook.ConsolidatedBook;

/**
 * Deterministic event-time replay driving order books (API_CORE.md section 5,
 * mirroring {@code iap/replay/replay.py}). Consumes normalized events in
 * event-time order, routes each to its instrument's {@link ConsolidatedBook},
 * emits book-state snapshots every {@code snapshotEvery} events, keeps rolling
 * checkpoints every {@code checkpointEvery} events, and can be restored from
 * any checkpoint to bit-identical subsequent state. No wall clock; every
 * serialized map is walked in sorted key order.
 */
public final class ReplayEngine {
    /** Callback invoked when a periodic snapshot is emitted. */
    @FunctionalInterface
    public interface SnapshotCallback {
        void onSnapshot(long index, Snapshot snapshot);
    }

    /** Book states of every instrument/venue at one replay index. */
    public static final class Snapshot {
        public final long index; // events processed when taken (0 = final/manual)
        public final TreeMap<Long, TreeMap<Integer, BookState>> instruments;

        public Snapshot(long index, TreeMap<Long, TreeMap<Integer, BookState>> instruments) {
            this.index = index;
            this.instruments = instruments;
        }

        @Override
        public boolean equals(Object obj) {
            if (this == obj) {
                return true;
            }
            if (!(obj instanceof Snapshot o)) {
                return false;
            }
            return index == o.index && instruments.equals(o.instruments);
        }

        @Override
        public int hashCode() {
            return Objects.hash(index, instruments);
        }
    }

    /** Full engine state; {@link ReplayEngine#restore} rebuilds it identically. */
    public static final class Checkpoint {
        public final long eventsProcessed;
        public final long lastExchangeTs;
        public final long timeRegressions;
        public final int checkpointEvery;
        public final int snapshotEvery;
        public final TreeMap<Long, ConsolidatedBook.Checkpoint> books;

        public Checkpoint(long eventsProcessed, long lastExchangeTs, long timeRegressions,
                int checkpointEvery, int snapshotEvery,
                TreeMap<Long, ConsolidatedBook.Checkpoint> books) {
            this.eventsProcessed = eventsProcessed;
            this.lastExchangeTs = lastExchangeTs;
            this.timeRegressions = timeRegressions;
            this.checkpointEvery = checkpointEvery;
            this.snapshotEvery = snapshotEvery;
            this.books = books;
        }

        @Override
        public boolean equals(Object obj) {
            if (this == obj) {
                return true;
            }
            if (!(obj instanceof Checkpoint o)) {
                return false;
            }
            return eventsProcessed == o.eventsProcessed
                    && lastExchangeTs == o.lastExchangeTs
                    && timeRegressions == o.timeRegressions
                    && checkpointEvery == o.checkpointEvery
                    && snapshotEvery == o.snapshotEvery
                    && books.equals(o.books);
        }

        @Override
        public int hashCode() {
            return Objects.hash(eventsProcessed, lastExchangeTs, timeRegressions,
                    checkpointEvery, snapshotEvery, books);
        }
    }

    /** Summary statistics returned by {@link #run}. */
    public record Summary(long eventsProcessed, int instruments, long timeRegressions,
            int snapshots) {
    }

    public final int checkpointEvery;
    public final int snapshotEvery;
    public final int keepCheckpoints;

    private final TreeMap<Long, ConsolidatedBook> books = new TreeMap<>();
    private final List<Checkpoint> checkpoints = new ArrayList<>();
    private final List<Snapshot> snapshots = new ArrayList<>();
    private long eventsProcessed;
    private long timeRegressions;
    private long lastExchangeTs;

    public ReplayEngine() {
        this(0, 0, 4);
    }

    public ReplayEngine(int checkpointEvery, int snapshotEvery, int keepCheckpoints) {
        if (checkpointEvery < 0 || snapshotEvery < 0) {
            throw new IllegalArgumentException(
                    "checkpointEvery/snapshotEvery must be >= 0");
        }
        this.checkpointEvery = checkpointEvery;
        this.snapshotEvery = snapshotEvery;
        this.keepCheckpoints = keepCheckpoints;
    }

    // -------------------------------------------------------------- applying

    /** Get (or lazily create) the per-instrument consolidated book. */
    public ConsolidatedBook instrumentBook(long instrumentId) {
        ConsolidatedBook book = books.get(instrumentId);
        if (book == null) {
            book = new ConsolidatedBook(instrumentId);
            books.put(instrumentId, book);
        }
        return book;
    }

    /** Apply one event; tracks event-time monotonicity. */
    public void apply(MarketEvent ev) {
        if (ev.exchangeTs < lastExchangeTs) {
            timeRegressions++;
        }
        lastExchangeTs = ev.exchangeTs;
        instrumentBook(ev.instrumentId).apply(ev);
        eventsProcessed++;
    }

    /** Replay an event stream; returns summary stats. */
    public Summary run(Iterable<MarketEvent> events, SnapshotCallback onSnapshot) {
        for (MarketEvent ev : events) {
            apply(ev);
            if (snapshotEvery != 0 && eventsProcessed % snapshotEvery == 0) {
                Snapshot snap = new Snapshot(eventsProcessed, bookStates());
                snapshots.add(snap);
                if (onSnapshot != null) {
                    onSnapshot.onSnapshot(eventsProcessed, snap);
                }
            }
            if (checkpointEvery != 0 && eventsProcessed % checkpointEvery == 0) {
                checkpoints.add(checkpoint());
                if (checkpoints.size() > keepCheckpoints) {
                    checkpoints.remove(0);
                }
            }
        }
        return new Summary(eventsProcessed, books.size(), timeRegressions, snapshots.size());
    }

    /** Replay an event stream without a snapshot callback. */
    public Summary run(Iterable<MarketEvent> events) {
        return run(events, null);
    }

    // ---------------------------------------------------------- serialization

    /** Exact-integer per-venue state summaries (deterministic key order). */
    public TreeMap<Long, TreeMap<Integer, BookState>> bookStates() {
        TreeMap<Long, TreeMap<Integer, BookState>> out = new TreeMap<>();
        for (Map.Entry<Long, ConsolidatedBook> e : books.entrySet()) {
            TreeMap<Integer, BookState> venues = new TreeMap<>();
            e.getValue().venues().forEach((vid, book) -> venues.put(vid, book.stateSummary()));
            out.put(e.getKey(), venues);
        }
        return out;
    }

    /** Full engine state; {@link #restore} rebuilds it identically. */
    public Checkpoint checkpoint() {
        TreeMap<Long, ConsolidatedBook.Checkpoint> cps = new TreeMap<>();
        for (Map.Entry<Long, ConsolidatedBook> e : books.entrySet()) {
            cps.put(e.getKey(), e.getValue().checkpoint());
        }
        return new Checkpoint(eventsProcessed, lastExchangeTs, timeRegressions,
                checkpointEvery, snapshotEvery, cps);
    }

    /** Rebuild an engine from {@link #checkpoint()} output. */
    public static ReplayEngine restore(Checkpoint cp) {
        ReplayEngine engine = new ReplayEngine(cp.checkpointEvery, cp.snapshotEvery, 4);
        engine.eventsProcessed = cp.eventsProcessed;
        engine.lastExchangeTs = cp.lastExchangeTs;
        engine.timeRegressions = cp.timeRegressions;
        for (Map.Entry<Long, ConsolidatedBook.Checkpoint> e : cp.books.entrySet()) {
            engine.books.put(e.getKey(), ConsolidatedBook.restore(e.getValue()));
        }
        return engine;
    }

    // -------------------------------------------------------------- accessors

    public long eventsProcessed() {
        return eventsProcessed;
    }

    public long timeRegressions() {
        return timeRegressions;
    }

    public long lastExchangeTs() {
        return lastExchangeTs;
    }

    /** Rolling checkpoints taken by {@link #run} (latest last). */
    public List<Checkpoint> checkpoints() {
        return java.util.Collections.unmodifiableList(checkpoints);
    }

    /** Periodic snapshots taken by {@link #run}. */
    public List<Snapshot> snapshots() {
        return java.util.Collections.unmodifiableList(snapshots);
    }

    /** Instrument ids present, sorted. */
    public java.util.SortedMap<Long, ConsolidatedBook> instruments() {
        return java.util.Collections.unmodifiableSortedMap(books);
    }
}

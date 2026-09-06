package com.iap.replay;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.TreeSet;

import com.iap.core.MarketEvent;
import com.iap.orderbook.BookState;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Deterministic event-time replay driving order books (API_CORE.md section 5,
 * mirroring {@code iap/replay/replay.py}). Consumes normalized events in
 * event-time order, validates ids against an optional universe (instrument
 * to venue ids; unknown ids are dropped + counted), routes each to its
 * instrument's {@link ConsolidatedBook}, emits book-state snapshots every
 * {@code snapshotEvery} events (retaining the latest {@code keepSnapshots}),
 * keeps rolling checkpoints every {@code checkpointEvery} events (retaining
 * {@code keepCheckpoints}), and can be restored from any checkpoint to
 * bit-identical subsequent state. Checkpoints serialize to the
 * cross-language JSON document of API_CORE section 5 through
 * {@link CheckpointJson}. No wall clock; every serialized map is walked in
 * sorted key order.
 */
public final class ReplayEngine {
    /** Engine checkpoint schema version (API_CORE section 5). */
    public static final long VERSION = 2;

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
        public final long unknownInstrumentDropped;
        public final long unknownVenueDropped;
        public final int checkpointEvery;
        public final int snapshotEvery;
        public final int keepCheckpoints;
        public final int keepSnapshots;
        public final long snapshotsEmitted;
        public final int reorderWindow;
        /** instrument_id to allowed venue ids; null = no validation. */
        public final TreeMap<Long, TreeSet<Integer>> universe;
        public final TreeMap<Long, ConsolidatedBook.Checkpoint> books;

        public Checkpoint(long eventsProcessed, long lastExchangeTs, long timeRegressions,
                long unknownInstrumentDropped, long unknownVenueDropped,
                int checkpointEvery, int snapshotEvery, int keepCheckpoints,
                int keepSnapshots, long snapshotsEmitted, int reorderWindow,
                TreeMap<Long, TreeSet<Integer>> universe,
                TreeMap<Long, ConsolidatedBook.Checkpoint> books) {
            this.eventsProcessed = eventsProcessed;
            this.lastExchangeTs = lastExchangeTs;
            this.timeRegressions = timeRegressions;
            this.unknownInstrumentDropped = unknownInstrumentDropped;
            this.unknownVenueDropped = unknownVenueDropped;
            this.checkpointEvery = checkpointEvery;
            this.snapshotEvery = snapshotEvery;
            this.keepCheckpoints = keepCheckpoints;
            this.keepSnapshots = keepSnapshots;
            this.snapshotsEmitted = snapshotsEmitted;
            this.reorderWindow = reorderWindow;
            this.universe = universe;
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
                    && unknownInstrumentDropped == o.unknownInstrumentDropped
                    && unknownVenueDropped == o.unknownVenueDropped
                    && checkpointEvery == o.checkpointEvery
                    && snapshotEvery == o.snapshotEvery
                    && keepCheckpoints == o.keepCheckpoints
                    && keepSnapshots == o.keepSnapshots
                    && snapshotsEmitted == o.snapshotsEmitted
                    && reorderWindow == o.reorderWindow
                    && Objects.equals(universe, o.universe)
                    && books.equals(o.books);
        }

        @Override
        public int hashCode() {
            return Objects.hash(eventsProcessed, lastExchangeTs, timeRegressions,
                    unknownInstrumentDropped, unknownVenueDropped, checkpointEvery,
                    snapshotEvery, keepCheckpoints, keepSnapshots, snapshotsEmitted,
                    reorderWindow, universe, books);
        }
    }

    /** Summary statistics returned by {@link #run}. */
    public record Summary(long eventsProcessed, int instruments, long timeRegressions,
            long snapshots, long unknownInstrumentDropped, long unknownVenueDropped) {
    }

    public final int checkpointEvery;
    public final int snapshotEvery;
    public final int keepCheckpoints;
    public final int keepSnapshots;
    public final int reorderWindow;

    private final TreeMap<Long, TreeSet<Integer>> universe;
    private final TreeMap<Long, ConsolidatedBook> books = new TreeMap<>();
    private final List<Checkpoint> checkpoints = new ArrayList<>();
    private final List<Snapshot> snapshots = new ArrayList<>();
    private long eventsProcessed;
    private long snapshotsEmitted;
    private long timeRegressions;
    private long unknownInstrumentDropped;
    private long unknownVenueDropped;
    private long lastExchangeTs;

    public ReplayEngine() {
        this(0, 0, 4);
    }

    public ReplayEngine(int checkpointEvery, int snapshotEvery, int keepCheckpoints) {
        this(checkpointEvery, snapshotEvery, keepCheckpoints, 4, 0, null);
    }

    /**
     * @param universe instrument_id to allowed venue ids (null = accept all)
     */
    public ReplayEngine(int checkpointEvery, int snapshotEvery, int keepCheckpoints,
            int keepSnapshots, int reorderWindow, Map<Long, ? extends java.util.Set<Integer>> universe) {
        if (checkpointEvery < 0 || snapshotEvery < 0) {
            throw new IllegalArgumentException(
                    "checkpointEvery/snapshotEvery must be >= 0");
        }
        if (keepCheckpoints < 0 || keepSnapshots < 0) {
            throw new IllegalArgumentException(
                    "keepCheckpoints/keepSnapshots must be >= 0");
        }
        if (reorderWindow < 0 || reorderWindow > OrderBook.MAX_REORDER_WINDOW) {
            throw new IllegalArgumentException("reorderWindow out of range: " + reorderWindow);
        }
        this.checkpointEvery = checkpointEvery;
        this.snapshotEvery = snapshotEvery;
        this.keepCheckpoints = keepCheckpoints;
        this.keepSnapshots = keepSnapshots;
        this.reorderWindow = reorderWindow;
        if (universe == null) {
            this.universe = null;
        } else {
            this.universe = new TreeMap<>();
            for (Map.Entry<Long, ? extends java.util.Set<Integer>> e : universe.entrySet()) {
                this.universe.put(e.getKey(), new TreeSet<>(e.getValue()));
            }
        }
    }

    // -------------------------------------------------------------- applying

    /** Get (or lazily create) the per-instrument consolidated book. */
    public ConsolidatedBook instrumentBook(long instrumentId) {
        ConsolidatedBook book = books.get(instrumentId);
        if (book == null) {
            book = new ConsolidatedBook(instrumentId, reorderWindow);
            books.put(instrumentId, book);
        }
        return book;
    }

    /** Apply one event; counts it, validates the universe, tracks event-time monotonicity. */
    public void apply(MarketEvent ev) {
        if (ev.exchangeTs < lastExchangeTs) {
            timeRegressions++;
        }
        lastExchangeTs = ev.exchangeTs;
        eventsProcessed++;
        if (universe != null) {
            TreeSet<Integer> venues = universe.get(ev.instrumentId);
            if (venues == null) {
                unknownInstrumentDropped++;
                return;
            }
            if (!venues.contains(ev.venueId)) {
                unknownVenueDropped++;
                return;
            }
        }
        instrumentBook(ev.instrumentId).apply(ev);
    }

    /** Explicit sequence reset on every book (session roll). */
    public void resetSequences() {
        for (ConsolidatedBook cons : books.values()) {
            cons.resetSequences();
        }
    }

    /** Replay an event stream; returns summary stats. */
    public Summary run(Iterable<MarketEvent> events, SnapshotCallback onSnapshot) {
        for (MarketEvent ev : events) {
            apply(ev);
            if (snapshotEvery != 0 && eventsProcessed % snapshotEvery == 0) {
                Snapshot snap = new Snapshot(eventsProcessed, bookStates());
                snapshotsEmitted++;
                snapshots.add(snap);
                if (snapshots.size() > keepSnapshots) {
                    snapshots.remove(0);
                }
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
        return new Summary(eventsProcessed, books.size(), timeRegressions, snapshotsEmitted,
                unknownInstrumentDropped, unknownVenueDropped);
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
        TreeMap<Long, TreeSet<Integer>> uni = null;
        if (universe != null) {
            uni = new TreeMap<>();
            for (Map.Entry<Long, TreeSet<Integer>> e : universe.entrySet()) {
                uni.put(e.getKey(), new TreeSet<>(e.getValue()));
            }
        }
        return new Checkpoint(eventsProcessed, lastExchangeTs, timeRegressions,
                unknownInstrumentDropped, unknownVenueDropped, checkpointEvery,
                snapshotEvery, keepCheckpoints, keepSnapshots, snapshotsEmitted,
                reorderWindow, uni, cps);
    }

    /** Rebuild an engine from {@link #checkpoint()} output (all configuration carried). */
    public static ReplayEngine restore(Checkpoint cp) {
        ReplayEngine engine = new ReplayEngine(cp.checkpointEvery, cp.snapshotEvery,
                cp.keepCheckpoints, cp.keepSnapshots, cp.reorderWindow, cp.universe);
        engine.eventsProcessed = cp.eventsProcessed;
        engine.lastExchangeTs = cp.lastExchangeTs;
        engine.timeRegressions = cp.timeRegressions;
        engine.unknownInstrumentDropped = cp.unknownInstrumentDropped;
        engine.unknownVenueDropped = cp.unknownVenueDropped;
        engine.snapshotsEmitted = cp.snapshotsEmitted;
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

    public long unknownInstrumentDropped() {
        return unknownInstrumentDropped;
    }

    public long unknownVenueDropped() {
        return unknownVenueDropped;
    }

    public long snapshotsEmitted() {
        return snapshotsEmitted;
    }

    public long lastExchangeTs() {
        return lastExchangeTs;
    }

    /** The universe (read-only copy), or null when ids are not validated. */
    public TreeMap<Long, TreeSet<Integer>> universe() {
        return universe == null ? null : new TreeMap<>(universe);
    }

    /** Rolling checkpoints taken by {@link #run} (latest last). */
    public List<Checkpoint> checkpoints() {
        return java.util.Collections.unmodifiableList(checkpoints);
    }

    /** Retained periodic snapshots taken by {@link #run} (latest last). */
    public List<Snapshot> snapshots() {
        return java.util.Collections.unmodifiableList(snapshots);
    }

    /** Instrument ids present, sorted. */
    public java.util.SortedMap<Long, ConsolidatedBook> instruments() {
        return java.util.Collections.unmodifiableSortedMap(books);
    }
}

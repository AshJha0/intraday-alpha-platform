package com.iap.features;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.SortedMap;
import java.util.TreeMap;

import com.iap.core.EventType;
import com.iap.core.MarketEvent;
import com.iap.core.Side;
import com.iap.orderbook.ApplyStatus;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Event-driven native feature engine (API_FEATURES.md; conventions section
 * 6). Implements the pinned native 40 features plus the 5 auxiliary
 * alpha-input features ({@link Features}) with semantics identical to the
 * Python reference engine — the golden checkpoints in
 * tests/golden/expected_features.json must match at abs/rel 1e-9.
 *
 * <p>State-update semantics (pinned, API_FEATURES.md section 2):
 * <ul>
 *   <li>an exchange_ts below the instrument's last seen exchange_ts
 *       (cross-venue clock skew) is dropped + counted before the book sees
 *       it — never thrown, never re-ordered;</li>
 *   <li>events are applied to the per-venue books of a
 *       {@link ConsolidatedBook}; ONLY events the book reports
 *       {@link com.iap.orderbook.ApplyStatus#APPLIED} feed rolling state
 *       (duplicates, invalid sides, malformed payloads and events dropped
 *       while stale contribute nothing);</li>
 *   <li>the merged top-10 view is refreshed after APPLIED book-touching
 *       events (ADD/MODIFY/CANCEL/EXECUTE/QUOTE and the final record of a
 *       SNAPSHOT burst, trade_id == 0) and after any event that changed the
 *       set of stale venues (staleness refresh: view only, no samples),
 *       merging non-stale venues in ascending venue_id;</li>
 *   <li>a stale to fresh recovery CLEARS every rolling window and history and
 *       re-anchors warmup at the recovery timestamp
 *       (warmup_after_recovery);</li>
 *   <li>windows are half-open {@code (t - w, t]} on exchange_ts;</li>
 *   <li>mid-derived samples are recorded whenever the merged mid differs from
 *       the last RECORDED sample; depth samples at every two-sided refresh;
 *       OFI deltas at every refresh;</li>
 *   <li>a windowed feature is invalid until {@code t - warmTs >= w};</li>
 *   <li>every {@code x / (y + EPS)} ratio is invalid when {@code y <= 0};</li>
 *   <li>quantities above {@link #FEATURE_MAX_QTY} are not folded into any
 *       window;</li>
 *   <li>NaN never appears with valid == true (single value funnel).</li>
 * </ul>
 *
 * <p>Hot path: primitive ring buffers and preallocated merge scratch; no
 * steady-state allocation in the rolling machinery (conventions section 8).
 */
public final class FeatureEngine {
    /**
     * Largest quantity folded into a rolling window (API_FEATURES.md section
     * 2.2): beyond this a feed is malformed, not a market, and an int64
     * window sum could no longer be exact in every port.
     */
    public static final long FEATURE_MAX_QTY = 1L << 40;

    /** History retention (2x the longest lookback + margin, as the reference). */
    private static final long HIST_KEEP_NS = 660 * Features.NS_PER_SEC;
    private static final int DEPTH = OrderBook.DEPTH_LEVELS;

    private static final class InstState {
        final double tick;
        ConsolidatedBook cons;
        long firstTs = -1;
        /** warmup anchor: first event, or the last stale to fresh recovery. */
        long warmTs = -1;
        long lastTs;
        long recoveries;
        /** sorted ids of this instrument's venues whose book is stale. */
        List<Integer> staleVenues = new ArrayList<>();
        long lastEmit = -1;
        // merged top-10 view (refreshed on book-touching events)
        boolean bookOk;
        final long[] bidP = new long[DEPTH];
        final long[] bidQ = new long[DEPTH];
        int nBid;
        final long[] askP = new long[DEPTH];
        final long[] askQ = new long[DEPTH];
        int nAsk;
        final long[] prevBidP = new long[DEPTH];
        final long[] prevBidQ = new long[DEPTH];
        int nPrevBid;
        final long[] prevAskP = new long[DEPTH];
        final long[] prevAskQ = new long[DEPTH];
        int nPrevAsk;
        long[] scratchP = new long[8 * DEPTH];
        long[] scratchQ = new long[8 * DEPTH];
        /**
         * Per-venue cached top-10 depth, refreshed only for the venue whose
         * event triggered the refresh (pinned: interior SNAPSHOT records
         * must not leak a half-built book into the merged view).
         */
        TreeMap<Integer, long[][][]> venueCache = new TreeMap<>();
        long bestBidP;
        long bestBidQ;
        long bestAskP;
        long bestAskQ;
        long db1;
        long db3;
        long db5;
        long db10;
        long da1;
        long da3;
        long da5;
        long da10;
        long mid2;
        double mid;
        double logmid;
        long spreadTicks;
        double spreadBps;
        // rolling state
        TimeSeries hist2 = new TimeSeries();   // mid2 at mid changes
        TimeSeries histlog = new TimeSeries(); // ln(mid2) at mid changes
        DoubleRollingSum rv10s = new DoubleRollingSum(Features.W_10S);
        DoubleRollingSum rv1m = new DoubleRollingSum(Features.W_1M);
        DoubleRollingSum rv5m = new DoubleRollingSum(Features.W_5M);
        LongRollingSum ofi1s = new LongRollingSum(4, Features.W_1S);
        LongRollingSum ofi5s = new LongRollingSum(4, Features.W_5S);
        LongRollingSum ofi30s = new LongRollingSum(4, Features.W_30S);
        LongRollingSum depthAvg10s = new LongRollingSum(4, Features.W_10S);
        LongRollingSum tr1s = new LongRollingSum(3, Features.W_1S);
        LongRollingSum tr10s = new LongRollingSum(3, Features.W_10S);
        LongRollingSum tr1m = new LongRollingSum(3, Features.W_1M);
        // scratch (no per-event allocation)
        final long[] ofiContribs = new long[4];
        final long[] depthSample = new long[4];
        final long[] tradeSample = new long[3];

        InstState(long instrumentId, double tickSize) {
            this.tick = tickSize;
            this.cons = new ConsolidatedBook(instrumentId);
        }

        boolean warm(long t, long w) {
            return warmTs >= 0 && t - warmTs >= w;
        }

        private static long[][] copyLevels(long[][] src) {
            long[][] out = new long[src.length][];
            for (int i = 0; i < src.length; i++) {
                out[i] = src[i].clone();
            }
            return out;
        }

        /** Clear every rolling window / history; re-anchor warmup at t. */
        void resetRolling(long t) {
            warmTs = t;
            recoveries++;
            hist2 = new TimeSeries();
            histlog = new TimeSeries();
            rv10s = new DoubleRollingSum(Features.W_10S);
            rv1m = new DoubleRollingSum(Features.W_1M);
            rv5m = new DoubleRollingSum(Features.W_5M);
            ofi1s = new LongRollingSum(4, Features.W_1S);
            ofi5s = new LongRollingSum(4, Features.W_5S);
            ofi30s = new LongRollingSum(4, Features.W_30S);
            depthAvg10s = new LongRollingSum(4, Features.W_10S);
            tr1s = new LongRollingSum(3, Features.W_1S);
            tr10s = new LongRollingSum(3, Features.W_10S);
            tr1m = new LongRollingSum(3, Features.W_1M);
            nBid = 0;
            nAsk = 0;
            bookOk = false;
        }

        InstState copy(long instrumentId) {
            InstState o = new InstState(instrumentId, tick);
            o.cons = ConsolidatedBook.restore(cons.checkpoint());
            o.firstTs = firstTs;
            o.warmTs = warmTs;
            o.lastTs = lastTs;
            o.recoveries = recoveries;
            o.staleVenues = new ArrayList<>(staleVenues);
            o.lastEmit = lastEmit;
            o.bookOk = bookOk;
            System.arraycopy(bidP, 0, o.bidP, 0, DEPTH);
            System.arraycopy(bidQ, 0, o.bidQ, 0, DEPTH);
            o.nBid = nBid;
            System.arraycopy(askP, 0, o.askP, 0, DEPTH);
            System.arraycopy(askQ, 0, o.askQ, 0, DEPTH);
            o.nAsk = nAsk;
            System.arraycopy(prevBidP, 0, o.prevBidP, 0, DEPTH);
            System.arraycopy(prevBidQ, 0, o.prevBidQ, 0, DEPTH);
            o.nPrevBid = nPrevBid;
            System.arraycopy(prevAskP, 0, o.prevAskP, 0, DEPTH);
            System.arraycopy(prevAskQ, 0, o.prevAskQ, 0, DEPTH);
            o.nPrevAsk = nPrevAsk;
            o.bestBidP = bestBidP;
            o.bestBidQ = bestBidQ;
            o.bestAskP = bestAskP;
            o.bestAskQ = bestAskQ;
            o.db1 = db1;
            o.db3 = db3;
            o.db5 = db5;
            o.db10 = db10;
            o.da1 = da1;
            o.da3 = da3;
            o.da5 = da5;
            o.da10 = da10;
            o.mid2 = mid2;
            o.mid = mid;
            o.logmid = logmid;
            o.spreadTicks = spreadTicks;
            o.spreadBps = spreadBps;
            o.venueCache = new TreeMap<>();
            for (Map.Entry<Integer, long[][][]> e : venueCache.entrySet()) {
                long[][][] v = e.getValue();
                o.venueCache.put(e.getKey(), new long[][][] {
                    copyLevels(v[0]), copyLevels(v[1])});
            }
            o.hist2 = hist2.copy();
            o.histlog = histlog.copy();
            o.rv10s = rv10s.copy();
            o.rv1m = rv1m.copy();
            o.rv5m = rv5m.copy();
            o.ofi1s = ofi1s.copy();
            o.ofi5s = ofi5s.copy();
            o.ofi30s = ofi30s.copy();
            o.depthAvg10s = depthAvg10s.copy();
            o.tr1s = tr1s.copy();
            o.tr10s = tr10s.copy();
            o.tr1m = tr1m.copy();
            return o;
        }
    }

    private final TreeMap<Long, Double> tickSizes;
    private final long cadenceNs;
    private final TreeMap<Long, InstState> states = new TreeMap<>();
    private long eventsProcessed;
    private long eventsDropped;
    private long tsRegressionsDropped;
    private long oversizedQtyDropped;
    private long oversizedDepthSkipped;
    private long vectorsEmitted;

    /**
     * @param tickSizes instrument_id to tick_size (real price per tick)
     * @param cadenceNs 0 emits one vector after every event of the
     *     instrument; otherwise at most one vector per instrument per
     *     cadence interval (event time only — no wall clock)
     */
    public FeatureEngine(Map<Long, Double> tickSizes, long cadenceNs) {
        if (cadenceNs < 0) {
            throw new IllegalArgumentException("cadence_ns must be >= 0");
        }
        this.tickSizes = new TreeMap<>(tickSizes);
        this.cadenceNs = cadenceNs;
    }

    private FeatureEngine(FeatureEngine o) {
        this.tickSizes = new TreeMap<>(o.tickSizes);
        this.cadenceNs = o.cadenceNs;
        this.eventsProcessed = o.eventsProcessed;
        this.eventsDropped = o.eventsDropped;
        this.tsRegressionsDropped = o.tsRegressionsDropped;
        this.oversizedQtyDropped = o.oversizedQtyDropped;
        this.oversizedDepthSkipped = o.oversizedDepthSkipped;
        this.vectorsEmitted = o.vectorsEmitted;
        for (Map.Entry<Long, InstState> e : o.states.entrySet()) {
            this.states.put(e.getKey(), e.getValue().copy(e.getKey()));
        }
    }

    /** Deep-copy snapshot of the full engine state (checkpoint support). */
    public FeatureEngine snapshot() {
        return new FeatureEngine(this);
    }

    public long eventsProcessed() {
        return eventsProcessed;
    }

    public long vectorsEmitted() {
        return vectorsEmitted;
    }

    /** Events the book dropped/held — never folded into rolling state. */
    public long eventsDropped() {
        return eventsDropped;
    }

    /** Events dropped for an exchange_ts regression (fail closed). */
    public long tsRegressionsDropped() {
        return tsRegressionsDropped;
    }

    /** Applied events whose qty exceeded {@link #FEATURE_MAX_QTY}. */
    public long oversizedQtyDropped() {
        return oversizedQtyDropped;
    }

    /** Refreshes whose merged depth exceeded {@link #FEATURE_MAX_QTY}. */
    public long oversizedDepthSkipped() {
        return oversizedDepthSkipped;
    }

    /** Stale to fresh recoveries of one instrument (rolling-state resets). */
    public long recoveries(long instrumentId) {
        InstState st = states.get(instrumentId);
        return st == null ? 0L : st.recoveries;
    }

    /** Warmup anchor of one instrument (-1 when it has no events yet). */
    public long warmTs(long instrumentId) {
        InstState st = states.get(instrumentId);
        return st == null ? -1L : st.warmTs;
    }

    /** True when the instrument's merged book is currently two-sided. */
    public boolean bookOk(long instrumentId) {
        InstState st = states.get(instrumentId);
        return st != null && st.bookOk;
    }

    private InstState state(long instrumentId) {
        InstState st = states.get(instrumentId);
        if (st != null) {
            return st;
        }
        Double tick = tickSizes.get(instrumentId);
        if (tick == null) {
            throw new IllegalArgumentException(
                    "no tick size for instrument " + instrumentId);
        }
        st = new InstState(instrumentId, tick);
        states.put(instrumentId, st);
        return st;
    }

    /**
     * Apply one event. Returns true when a vector was emitted; the emitted
     * vector is written into {@code out} (caller-owned, no allocation).
     */
    public boolean apply(MarketEvent ev, FeatureVector out) {
        InstState st = state(ev.instrumentId);
        long t = ev.exchangeTs;
        if (st.firstTs >= 0 && t < st.lastTs) {
            // Cross-venue exchange_ts regression: dropped + counted before
            // the book sees it (pinned). Never thrown, never re-ordered.
            tsRegressionsDropped++;
            eventsDropped++;
            eventsProcessed++;
            return false;
        }
        ApplyStatus status = st.cons.apply(ev);
        if (st.firstTs < 0) {
            st.firstTs = t;
            st.warmTs = t;
        }
        st.lastTs = t;
        eventsProcessed++;

        // The merged view is a function of WHICH venues are stale, so the
        // trigger is a change of the stale SET, not of "any venue is stale".
        List<Integer> staleNow = new ArrayList<>();
        for (Map.Entry<Integer, OrderBook> e : st.cons.venues().entrySet()) {
            if (e.getValue().isStale()) {
                staleNow.add(e.getKey());
            }
        }
        boolean staleChanged = !staleNow.equals(st.staleVenues);
        boolean justRecovered = !st.staleVenues.isEmpty() && staleNow.isEmpty();
        st.staleVenues = staleNow;

        if (status != ApplyStatus.APPLIED) {
            eventsDropped++;
            if (staleChanged
                    && refreshBook(st, ev.venueId, t, justRecovered, false)) {
                oversizedDepthSkipped++;
            }
            return emitIfDue(st, ev.instrumentId, t, out);
        }

        int et = ev.eventType;
        if (ev.qty > FEATURE_MAX_QTY) {
            // Oversized quantity (pinned section 2.2): the book may hold it,
            // but no rolling window folds it in — an int64 window sum stays
            // exact. The merged view is still refreshed.
            oversizedQtyDropped++;
        } else if (et == EventType.TRADE) {
            // signed / buy / sell traded quantity (side BID = buy aggressor)
            long buy = ev.side == Side.BID ? ev.qty : 0;
            long sell = ev.qty - buy;
            st.tradeSample[0] = buy - sell;
            st.tradeSample[1] = buy;
            st.tradeSample[2] = sell;
            st.tr1s.add(t, st.tradeSample);
            st.tr10s.add(t, st.tradeSample);
            st.tr1m.add(t, st.tradeSample);
        }

        boolean bookTouch = et == EventType.ADD || et == EventType.MODIFY
                || et == EventType.CANCEL || et == EventType.EXECUTE
                || et == EventType.QUOTE
                || (et == EventType.SNAPSHOT && ev.tradeId == 0);
        if (bookTouch) {
            if (refreshBook(st, ev.venueId, t, justRecovered, true)) {
                oversizedDepthSkipped++;
            }
        } else if (staleChanged) {
            if (refreshBook(st, ev.venueId, t, justRecovered, false)) {
                oversizedDepthSkipped++;
            }
        }

        return emitIfDue(st, ev.instrumentId, t, out);
    }

    private boolean emitIfDue(InstState st, long instrumentId, long t,
            FeatureVector out) {
        if (cadenceNs == 0 || st.lastEmit < 0 || t - st.lastEmit >= cadenceNs) {
            // Evict expired samples from every window at emission time.
            st.rv10s.trim(t);
            st.rv1m.trim(t);
            st.rv5m.trim(t);
            st.ofi1s.trim(t);
            st.ofi5s.trim(t);
            st.ofi30s.trim(t);
            st.depthAvg10s.trim(t);
            st.tr1s.trim(t);
            st.tr10s.trim(t);
            st.tr1m.trim(t);
            st.hist2.trim(t - HIST_KEEP_NS);
            st.histlog.trim(t - HIST_KEEP_NS);
            emit(st, instrumentId, t, out);
            st.lastEmit = t;
            vectorsEmitted++;
            return true;
        }
        return false;
    }

    /** Apply a stream; collect a deep copy of every emitted vector. */
    public List<FeatureVector> run(Iterable<MarketEvent> events) {
        List<FeatureVector> out = new ArrayList<>();
        FeatureVector vec = new FeatureVector();
        for (MarketEvent ev : events) {
            if (apply(ev, vec)) {
                out.add(vec.copy());
            }
        }
        return out;
    }

    /**
     * Signed depth change within the best-k levels of one side (OFI building
     * block): sum over the union of prev/curr top-k prices of (curr - prev)
     * sizes, missing price = size 0.
     */
    private static long depthDelta(long[] prevP, long[] prevQ, int nPrev,
            long[] currP, long[] currQ, int nCurr, int k) {
        int np = Math.min(nPrev, k);
        int nc = Math.min(nCurr, k);
        long d = 0;
        for (int i = 0; i < nc; i++) {
            long prevQty = 0;
            for (int j = 0; j < np; j++) {
                if (prevP[j] == currP[i]) {
                    prevQty = prevQ[j];
                    break;
                }
            }
            d += currQ[i] - prevQty;
        }
        for (int j = 0; j < np; j++) {
            boolean seen = false;
            for (int i = 0; i < nc; i++) {
                if (currP[i] == prevP[j]) {
                    seen = true;
                    break;
                }
            }
            if (!seen) {
                d -= prevQ[j];
            }
        }
        return d;
    }

    /**
     * Merge one side over non-stale venues into (outP, outQ) from the per
     * venue CACHE (pinned: a venue's contribution changes only when one of
     * its own book-touching events refreshes it); returns the level count.
     */
    private int mergeSide(InstState st, int side, long[] outP, long[] outQ) {
        int n = 0;
        SortedMap<Integer, OrderBook> venues = st.cons.venues();
        for (Map.Entry<Integer, long[][][]> ce : st.venueCache.entrySet()) {
            OrderBook book = venues.get(ce.getKey());
            if (book == null || book.isStale()) {
                continue;
            }
            long[][] levels = ce.getValue()[side == Side.BID ? 0 : 1];
            for (long[] lvl : levels) {
                long p = lvl[0];
                long q = lvl[1];
                boolean found = false;
                for (int i = 0; i < n; i++) {
                    if (st.scratchP[i] == p) {
                        st.scratchQ[i] += q;
                        found = true;
                        break;
                    }
                }
                if (!found) {
                    if (n == st.scratchP.length) {
                        long[] np = new long[n * 2];
                        long[] nq = new long[n * 2];
                        System.arraycopy(st.scratchP, 0, np, 0, n);
                        System.arraycopy(st.scratchQ, 0, nq, 0, n);
                        st.scratchP = np;
                        st.scratchQ = nq;
                    }
                    st.scratchP[n] = p;
                    st.scratchQ[n] = q;
                    n++;
                }
            }
        }
        // Sort best-first (insertion sort; prices are unique after merging).
        boolean isBid = side == Side.BID;
        for (int i = 1; i < n; i++) {
            long p = st.scratchP[i];
            long q = st.scratchQ[i];
            int j = i - 1;
            while (j >= 0 && (isBid ? st.scratchP[j] < p : st.scratchP[j] > p)) {
                st.scratchP[j + 1] = st.scratchP[j];
                st.scratchQ[j + 1] = st.scratchQ[j];
                j--;
            }
            st.scratchP[j + 1] = p;
            st.scratchQ[j + 1] = q;
        }
        int take = Math.min(n, DEPTH);
        System.arraycopy(st.scratchP, 0, outP, 0, take);
        System.arraycopy(st.scratchQ, 0, outQ, 0, take);
        return take;
    }

    /**
     * Recompute the merged view. {@code samples == false} is a staleness
     * refresh: the view and bookOk are recomputed because the stale-venue set
     * changed, but no OFI / depth / mid sample is recorded.
     *
     * @return true when the merged depth was oversized (view unusable)
     */
    private boolean refreshBook(InstState st, int venueId, long t,
            boolean justRecovered, boolean samples) {
        if (justRecovered) {
            st.resetRolling(t);
        }
        OrderBook vb = st.cons.venues().get(venueId);
        if (vb != null) {
            st.venueCache.put(venueId, new long[][][] {
                vb.depth(Side.BID, DEPTH), vb.depth(Side.ASK, DEPTH)});
        }
        // Save the previous merged view (copy into the prev arrays).
        System.arraycopy(st.bidP, 0, st.prevBidP, 0, DEPTH);
        System.arraycopy(st.bidQ, 0, st.prevBidQ, 0, DEPTH);
        st.nPrevBid = st.nBid;
        System.arraycopy(st.askP, 0, st.prevAskP, 0, DEPTH);
        System.arraycopy(st.askQ, 0, st.prevAskQ, 0, DEPTH);
        st.nPrevAsk = st.nAsk;
        st.nBid = mergeSide(st, Side.BID, st.bidP, st.bidQ);
        st.nAsk = mergeSide(st, Side.ASK, st.askP, st.askQ);

        // Oversized merged depth (pinned section 2.2): a level above
        // FEATURE_MAX_QTY makes the merged view unusable — clear it, record
        // nothing, and let the next clean refresh re-baseline.
        boolean oversized = false;
        for (int i = 0; i < st.nBid; i++) {
            oversized |= st.bidQ[i] > FEATURE_MAX_QTY;
        }
        for (int i = 0; i < st.nAsk; i++) {
            oversized |= st.askQ[i] > FEATURE_MAX_QTY;
        }
        if (oversized) {
            st.nBid = 0;
            st.nAsk = 0;
            st.bookOk = false;
            return true;
        }

        // OFI contributions per level count (defined book_ok or not); the
        // sample is skipped when previous and current views are all empty, or
        // on the first refresh after a recovery (no previous depth).
        if (samples && !justRecovered
                && (st.nPrevBid != 0 || st.nPrevAsk != 0 || st.nBid != 0
                        || st.nAsk != 0)) {
            final int[] ks = {1, 3, 5, 10};
            for (int i = 0; i < 4; i++) {
                st.ofiContribs[i] = depthDelta(st.prevBidP, st.prevBidQ,
                        st.nPrevBid, st.bidP, st.bidQ, st.nBid, ks[i])
                        - depthDelta(st.prevAskP, st.prevAskQ, st.nPrevAsk,
                                st.askP, st.askQ, st.nAsk, ks[i]);
            }
            st.ofi1s.add(t, st.ofiContribs);
            st.ofi5s.add(t, st.ofiContribs);
            st.ofi30s.add(t, st.ofiContribs);
        }

        st.bookOk = st.nBid > 0 && st.nAsk > 0;
        if (!st.bookOk || !samples) {
            return false;
        }

        st.bestBidP = st.bidP[0];
        st.bestBidQ = st.bidQ[0];
        st.bestAskP = st.askP[0];
        st.bestAskQ = st.askQ[0];
        st.db1 = st.db3 = st.db5 = st.db10 = 0;
        for (int i = 0; i < st.nBid; i++) {
            long q = st.bidQ[i];
            if (i < 1) {
                st.db1 += q;
            }
            if (i < 3) {
                st.db3 += q;
            }
            if (i < 5) {
                st.db5 += q;
            }
            st.db10 += q;
        }
        st.da1 = st.da3 = st.da5 = st.da10 = 0;
        for (int i = 0; i < st.nAsk; i++) {
            long q = st.askQ[i];
            if (i < 1) {
                st.da1 += q;
            }
            if (i < 3) {
                st.da3 += q;
            }
            if (i < 5) {
                st.da5 += q;
            }
            st.da10 += q;
        }
        st.mid2 = st.bestBidP + st.bestAskP;
        st.mid = (double) st.mid2 * st.tick / 2.0;
        st.logmid = Math.log((double) st.mid2);
        st.spreadTicks = st.bestAskP - st.bestBidP;
        st.spreadBps = (double) st.spreadTicks * st.tick / st.mid * 1e4;

        // Depth sample at every two-sided refresh (ofi_norm denominators).
        st.depthSample[0] = st.db1;
        st.depthSample[1] = st.da1;
        st.depthSample[2] = st.db5;
        st.depthSample[3] = st.da5;
        st.depthAvg10s.add(t, st.depthSample);

        // Mid-change samples (returns / realized-vol inputs), compared with
        // the last RECORDED sample (pinned): a one-sided flicker that moves
        // the mid still yields a vol sample; a flicker back to the same mid
        // yields none.
        boolean hasLast = !st.hist2.isEmpty();
        if (!hasLast || st.hist2.last() != (double) st.mid2) {
            if (hasLast) {
                double dlm = st.logmid - st.histlog.last();
                double sq = dlm * dlm;
                st.rv10s.add(t, sq);
                st.rv1m.add(t, sq);
                st.rv5m.add(t, sq);
            }
            st.hist2.append(t, (double) st.mid2);
            st.histlog.append(t, st.logmid);
        }
        return false;
    }

    private void emit(InstState st, long iid, long t, FeatureVector out) {
        out.instrumentId = iid;
        out.timestamp = t;
        out.clear();

        boolean ok = st.bookOk;

        // ---- price family: returns from at-or-before mid-change samples --
        double past2 = ok ? st.hist2.atOrBefore(t - Features.W_1S) : Double.NaN;
        boolean has1s = !Double.isNaN(past2);
        put(out, Features.RET_SIMPLE_1S,
                has1s ? (double) st.mid2 / past2 - 1.0 : 0.0, has1s);
        double retLog10s = 0.0;
        boolean hasLog10s = false;
        final int[] retSlots = {Features.RET_LOG_1S, Features.RET_LOG_10S,
            Features.RET_LOG_1M};
        final long[] retH = {Features.W_1S, Features.W_10S, Features.W_1M};
        for (int i = 0; i < 3; i++) {
            double pastLog =
                    ok ? st.histlog.atOrBefore(t - retH[i]) : Double.NaN;
            boolean has = !Double.isNaN(pastLog);
            double v = has ? st.logmid - pastLog : 0.0;
            put(out, retSlots[i], v, has);
            if (retSlots[i] == Features.RET_LOG_10S) {
                retLog10s = v;
                hasLog10s = has;
            }
        }

        // realized vol (per sqrt-second); valid once the window is warm.
        boolean hasRv10 = st.warm(t, Features.W_10S);
        boolean hasRv1m = st.warm(t, Features.W_1M);
        boolean hasRv5m = st.warm(t, Features.W_5M);
        double rv10 = hasRv10 ? rvol(st.rv10s, Features.W_10S) : 0.0;
        double rv1m = hasRv1m ? rvol(st.rv1m, Features.W_1M) : 0.0;
        double rv5m = hasRv5m ? rvol(st.rv5m, Features.W_5M) : 0.0;
        put(out, Features.RVOL_W10S, rv10, hasRv10);
        put(out, Features.RVOL_W1M, rv1m, hasRv1m);
        put(out, Features.RVOL_W5M, rv5m, hasRv5m);

        // EPS guard (section 4): the denominator is undefined when the vol
        // window holds no mid-change SAMPLE (exact integer count, not
        // `rvol > 0`: a float sum drifts and the test would flip per port).
        boolean hasRva = hasLog10s && hasRv1m && st.rv1m.count() > 0;
        put(out, Features.RET_VOL_ADJ_10S,
                hasRva ? retLog10s / (rv1m + Features.EPS) : 0.0, hasRva);
        boolean hasVrr = hasRv1m && hasRv5m && st.rv5m.count() > 0;
        put(out, Features.VOL_REGIME_RATIO,
                hasVrr ? rv1m / (rv5m + Features.EPS) : 0.0, hasVrr);

        // ---- microstructure ---------------------------------------------
        put(out, Features.MID_PRICE, st.mid, ok);
        boolean hasMicro = ok && (st.bestBidQ + st.bestAskQ) > 0;
        double micro = 0.0;
        if (hasMicro) {
            micro = ((double) st.bestBidP * (double) st.bestAskQ
                    + (double) st.bestAskP * (double) st.bestBidQ)
                    / (double) (st.bestBidQ + st.bestAskQ) * st.tick;
        }
        put(out, Features.MICROPRICE, micro, hasMicro);
        boolean hasDev = hasMicro && st.mid != 0.0;
        put(out, Features.MICRO_MID_DEV_BPS,
                hasDev ? (micro - st.mid) / st.mid * 1e4 : 0.0, hasDev);
        put(out, Features.SPREAD_TICKS, (double) st.spreadTicks, ok);
        put(out, Features.SPREAD_BPS, st.spreadBps, ok);

        put(out, Features.DEPTH_BID_L1, (double) st.db1, ok);
        put(out, Features.DEPTH_ASK_L1, (double) st.da1, ok);
        put(out, Features.DEPTH_BID_L5, (double) st.db5, ok);
        put(out, Features.DEPTH_ASK_L5, (double) st.da5, ok);
        put(out, Features.DEPTH_BID_L10, (double) st.db10, ok);
        put(out, Features.DEPTH_ASK_L10, (double) st.da10, ok);

        final int[] imbSlots = {Features.IMBALANCE_L1, Features.IMBALANCE_L3,
            Features.IMBALANCE_L5, Features.IMBALANCE_L10};
        final long[] imbB = {st.db1, st.db3, st.db5, st.db10};
        final long[] imbA = {st.da1, st.da3, st.da5, st.da10};
        for (int i = 0; i < 4; i++) {
            boolean has = ok && (imbB[i] + imbA[i]) > 0;
            put(out, imbSlots[i],
                    has ? (double) (imbB[i] - imbA[i]) / (double) (imbB[i] + imbA[i])
                        : 0.0,
                    has);
        }

        // ---- order flow -------------------------------------------------
        final LongRollingSum[] ofis = {st.ofi1s, st.ofi5s, st.ofi30s};
        final long[] ofiW = {Features.W_1S, Features.W_5S, Features.W_30S};
        // slot layout: OFI_L{K}_W{W} = OFI_L1_W1S + ki*3 + wi
        for (int ki = 0; ki < 4; ki++) {
            for (int wi = 0; wi < 3; wi++) {
                boolean warm = st.warm(t, ofiW[wi]);
                put(out, Features.OFI_L1_W1S + ki * 3 + wi,
                        warm ? (double) ofis[wi].sum(ki) : 0.0, warm);
            }
        }
        // ofi_norm_l{1,5}: ofi / (mean two-sided depth over 10s + EPS).
        boolean davgOk = st.warm(t, Features.W_10S) && st.depthAvg10s.count() > 0;
        // aux slots: ofi_norm_l1_w1s, ofi_norm_l5_w1s, ofi_norm_l5_w5s
        final int[] normSlots = {Features.OFI_NORM_L1_W1S,
            Features.OFI_NORM_L5_W1S, Features.OFI_NORM_L5_W5S};
        final int[] normOfiIdx = {0, 2, 2};      // l1 -> tuple 0, l5 -> tuple 2
        final int[] normWi = {0, 0, 1};          // w1s, w1s, w5s
        for (int i = 0; i < 3; i++) {
            double denom = 0.0;
            if (davgOk) {
                double denomSum = normOfiIdx[i] == 0
                        ? (double) (st.depthAvg10s.sum(0) + st.depthAvg10s.sum(1))
                        : (double) (st.depthAvg10s.sum(2) + st.depthAvg10s.sum(3));
                denom = denomSum / (double) st.depthAvg10s.count();
            }
            // Exact INTEGER guard: a float `> 0` test would flip between
            // ports on accumulation drift.
            long depthSum = normOfiIdx[i] == 0
                    ? st.depthAvg10s.sum(0) + st.depthAvg10s.sum(1)
                    : st.depthAvg10s.sum(2) + st.depthAvg10s.sum(3);
            boolean has = st.warm(t, ofiW[normWi[i]]) && davgOk && depthSum > 0;
            double v = has ? (double) ofis[normWi[i]].sum(normOfiIdx[i])
                    / (denom + Features.EPS) : 0.0;
            put(out, normSlots[i], v, has);
        }

        final LongRollingSum[] trs = {st.tr1s, st.tr10s, st.tr1m};
        final long[] trW = {Features.W_1S, Features.W_10S, Features.W_1M};
        for (int wi = 0; wi < 3; wi++) {
            boolean warm = st.warm(t, trW[wi]);
            put(out, Features.SIGNED_VOLUME_W1S + wi,
                    warm ? (double) trs[wi].sum(0) : 0.0, warm);
            long tot = trs[wi].sum(1) + trs[wi].sum(2);
            boolean has = warm && tot > 0;
            put(out, Features.TRADE_IMBALANCE_W1S + wi,
                    has ? (double) (trs[wi].sum(1) - trs[wi].sum(2)) / (double) tot
                        : 0.0,
                    has);
        }
    }

    private static double rvol(DoubleRollingSum win, long w) {
        return Math.sqrt(Math.max(win.sum(), 0.0) / ((double) w / 1e9));
    }

    /**
     * Single value funnel (API_FEATURES.md section 1): a non-finite value can
     * never be emitted with valid == true.
     */
    private static void put(FeatureVector out, int slot, double v, boolean ok) {
        boolean good = ok && !Double.isNaN(v) && !Double.isInfinite(v);
        out.values[slot] = good ? v : Double.NaN;
        out.valid[slot] = good;
    }
}

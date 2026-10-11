package com.iap.sor;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.execution.ExecutionSimulator;
import com.iap.execution.VenueSpec;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Smart order routing (spec section 17: multi-venue routing research),
 * mirroring the C++ reference ({@code cpp/include/iap/sor/sor.hpp})
 * exactly. Deterministic venue selection over the per-venue books of one
 * instrument:
 *
 * <ul>
 *   <li><b>Eligible venue</b>: a candidate whose book exists, is not stale,
 *       whose status is TRADING and whose configured latency_mean_ns is
 *       within {@link SorOptions#maxVenueLatencyNs()}. A stale, halted or
 *       missing venue book is NEVER routed to, whatever the alternative.</li>
 *   <li>{@link #routeAggressive}: among eligible venues quoting the opposite
 *       side, the most favorable displayed best price (lowest ask for a buy
 *       / highest bid for a sell). Ties break to the lower
 *       taker_fee_per_share, then the lower commission_per_million, then
 *       the lower venue_id.</li>
 *   <li>{@link #routePassive}: among eligible venues quoting OUR side, the
 *       highest maker rebate when {@code preferRebate} (ties: lower
 *       commission, then lower venue_id); otherwise the lowest venue_id
 *       quoting our side.</li>
 *   <li><b>No route (0)</b>: no eligible venue quotes the needed side. The
 *       caller MUST NOT submit (the engines skip the child and count
 *       {@code sor_no_route_total}). An empty candidate list throws.</li>
 * </ul>
 *
 * <p>All iteration is in ascending venue_id order — same candidates + same
 * books = same route.
 */
public final class SmartOrderRouter {
    /** Sentinel: no eligible venue. */
    public static final int NO_ROUTE = 0;

    private final TreeMap<Integer, VenueSpec> venues;
    private final SorOptions options;

    public SmartOrderRouter(Map<Integer, VenueSpec> venues) {
        this(venues, SorOptions.DEFAULT);
    }

    public SmartOrderRouter(Map<Integer, VenueSpec> venues, SorOptions options) {
        this.venues = new TreeMap<>(venues);
        this.options = options;
    }

    public SorOptions options() {
        return options;
    }

    private OrderBook eligible(ConsolidatedBook book, int vid) {
        OrderBook vb = book.venues().get(vid);
        if (!ExecutionSimulator.venueOpen(vb)) {
            return null;
        }
        VenueSpec spec = venues.get(vid);
        if (spec != null && spec.latencyMeanNs() > options.maxVenueLatencyNs()) {
            return null;
        }
        return vb;
    }

    public int routeAggressive(ConsolidatedBook book, int side,
            List<Integer> candidates) {
        int[] sorted = sortedCandidates(candidates);
        boolean have = false;
        int bestVid = NO_ROUTE;
        long bestPrice = 0;
        double bestFee = 0.0;
        double bestComm = 0.0;
        for (int vid : sorted) {
            OrderBook vb = eligible(book, vid);
            if (vb == null) {
                continue;
            }
            long[] quote = side == 0 ? vb.bestAsk() : vb.bestBid();
            if (quote == null) {
                continue;
            }
            VenueSpec spec = venues.get(vid);
            double fee = spec == null ? 0.0 : spec.takerFeePerShare();
            double comm = spec == null ? 0.0 : spec.commissionPerMillion();
            boolean better = !have
                    || (side == 0 ? quote[0] < bestPrice : quote[0] > bestPrice)
                    || (quote[0] == bestPrice
                            && (fee < bestFee || (fee == bestFee && comm < bestComm)));
            if (better) {
                have = true;
                bestVid = vid;
                bestPrice = quote[0];
                bestFee = fee;
                bestComm = comm;
            }
        }
        return have ? bestVid : NO_ROUTE;
    }

    public int routePassive(ConsolidatedBook book, int side,
            List<Integer> candidates) {
        int[] sorted = sortedCandidates(candidates);
        boolean have = false;
        int bestVid = NO_ROUTE;
        double bestRebate = 0.0;
        double bestComm = 0.0;
        for (int vid : sorted) {
            OrderBook vb = eligible(book, vid);
            if (vb == null) {
                continue;
            }
            long[] quote = side == 0 ? vb.bestBid() : vb.bestAsk();
            if (quote == null) {
                continue;
            }
            if (!options.preferRebate()) {
                return vid; // lowest eligible venue id quoting our side
            }
            VenueSpec spec = venues.get(vid);
            double rebate = spec == null ? 0.0 : spec.makerRebatePerShare();
            double comm = spec == null ? 0.0 : spec.commissionPerMillion();
            if (!have || rebate > bestRebate
                    || (rebate == bestRebate && comm < bestComm)) {
                have = true;
                bestVid = vid;
                bestRebate = rebate;
                bestComm = comm;
            }
        }
        return have ? bestVid : NO_ROUTE;
    }

    private static int[] sortedCandidates(List<Integer> candidates) {
        if (candidates.isEmpty()) {
            throw new IllegalArgumentException("SOR: empty candidate venue list");
        }
        int[] out = new int[candidates.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = candidates.get(i);
        }
        java.util.Arrays.sort(out);
        return out;
    }
}

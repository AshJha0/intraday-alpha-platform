package com.iap.sor;

import java.util.List;
import java.util.Map;
import java.util.TreeMap;

import com.iap.execution.VenueSpec;
import com.iap.orderbook.ConsolidatedBook;
import com.iap.orderbook.OrderBook;

/**
 * Smart order routing (spec section 17: multi-venue routing research).
 * Deterministic venue selection over the per-venue books of one instrument:
 *
 * <ul>
 *   <li>{@link #routeAggressive}: the venue whose displayed opposite-side
 *       best price is most favorable (lowest ask for a buy / highest bid
 *       for a sell) among the candidates quoting that side. Ties break to
 *       the lower taker fee, then to the lower venue_id. Falls back to the
 *       lowest candidate venue_id when no candidate quotes the side.</li>
 *   <li>{@link #routePassive}: among candidates quoting OUR side, the venue
 *       with the highest maker rebate (prefer_rebate,
 *       configs/execution.json sor block); ties break to the lower
 *       venue_id; same fallback.</li>
 * </ul>
 *
 * <p>Stale venue books are never routed to. All iteration is in ascending
 * venue_id order — same candidates + same books = same route.
 */
public final class SmartOrderRouter {
    private final TreeMap<Integer, VenueSpec> venues;

    public SmartOrderRouter(Map<Integer, VenueSpec> venues) {
        this.venues = new TreeMap<>(venues);
    }

    public int routeAggressive(ConsolidatedBook book, int side,
            List<Integer> candidates) {
        int[] sorted = sortedCandidates(candidates);
        boolean have = false;
        int bestVid = 0;
        long bestPrice = 0;
        double bestFee = 0.0;
        for (int vid : sorted) {
            OrderBook vb = book.venues().get(vid);
            if (vb == null || vb.isStale()) {
                continue;
            }
            long[] quote = side == 0 ? vb.bestAsk() : vb.bestBid();
            if (quote == null) {
                continue;
            }
            VenueSpec spec = venues.get(vid);
            double fee = spec == null ? 0.0 : spec.takerFeePerShare();
            boolean better = !have
                    || (side == 0 ? quote[0] < bestPrice : quote[0] > bestPrice)
                    || (quote[0] == bestPrice && fee < bestFee);
            if (better) {
                have = true;
                bestVid = vid;
                bestPrice = quote[0];
                bestFee = fee;
            }
        }
        return have ? bestVid : fallback(candidates);
    }

    public int routePassive(ConsolidatedBook book, int side,
            List<Integer> candidates) {
        int[] sorted = sortedCandidates(candidates);
        boolean have = false;
        int bestVid = 0;
        double bestRebate = 0.0;
        for (int vid : sorted) {
            OrderBook vb = book.venues().get(vid);
            if (vb == null || vb.isStale()) {
                continue;
            }
            long[] quote = side == 0 ? vb.bestBid() : vb.bestAsk();
            if (quote == null) {
                continue;
            }
            VenueSpec spec = venues.get(vid);
            double rebate = spec == null ? 0.0 : spec.makerRebatePerShare();
            if (!have || rebate > bestRebate) {
                have = true;
                bestVid = vid;
                bestRebate = rebate;
            }
        }
        return have ? bestVid : fallback(candidates);
    }

    private static int[] sortedCandidates(List<Integer> candidates) {
        int[] out = new int[candidates.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = candidates.get(i);
        }
        java.util.Arrays.sort(out);
        return out;
    }

    private static int fallback(List<Integer> candidates) {
        if (candidates.isEmpty()) {
            throw new IllegalArgumentException("SOR: empty candidate venue list");
        }
        int min = candidates.get(0);
        for (int vid : candidates) {
            min = Math.min(min, vid);
        }
        return min;
    }
}

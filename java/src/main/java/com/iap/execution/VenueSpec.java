package com.iap.execution;

import java.nio.file.Path;
import java.util.Map;
import java.util.TreeMap;

import com.iap.config.Json;

/**
 * One venue's execution profile (configs/venues.json): fees per share (EQ,
 * negative fee = maker rebate) / commission per million notional (FX) plus
 * the venue latency leg (mean + jitter bound for the pinned SplitMix64
 * uniform draw).
 */
public record VenueSpec(
        int venueId,       // u16
        String name,
        boolean isFx,
        double takerFeePerShare,
        double makerRebatePerShare,
        double commissionPerMillion,
        long latencyMeanNs,
        long latencyJitterNs) {

    /** Load every venue from configs/venues.json, keyed by venue_id. */
    public static TreeMap<Integer, VenueSpec> loadVenues(Path path) {
        Map<String, Object> root = Json.object(Json.parseFile(path));
        TreeMap<Integer, VenueSpec> out = new TreeMap<>();
        for (Object entry : Json.array(root.get("venues"))) {
            Map<String, Object> v = Json.object(entry);
            Map<String, Object> lat = Json.object(v.get("latency"));
            int vid = (int) Json.asLong(v.get("venue_id"));
            out.put(vid, new VenueSpec(
                    vid,
                    (String) v.get("venue"),
                    "FX".equals(v.get("asset_class")),
                    v.containsKey("taker_fee_per_share")
                            ? Json.asDouble(v.get("taker_fee_per_share")) : 0.0,
                    v.containsKey("maker_rebate_per_share")
                            ? Json.asDouble(v.get("maker_rebate_per_share")) : 0.0,
                    v.containsKey("commission_per_million")
                            ? Json.asDouble(v.get("commission_per_million")) : 0.0,
                    Json.asLong(lat.get("mean_ns")),
                    Json.asLong(lat.get("jitter_ns"))));
        }
        if (out.isEmpty()) {
            throw new IllegalStateException("no venues in " + path);
        }
        return out;
    }
}

package com.iap.execution;

import java.util.Map;
import java.util.TreeMap;

/**
 * Execution-simulator configuration: latency legs, deterministic seed
 * (SplitMix64 jitter stream), linear-impact coefficient
 * (configs/execution.json cost_model), instrument reference data and venue
 * profiles (configs/venues.json). Treated as immutable once handed to a
 * simulator.
 */
public final class ExecConfig {
    public final LatencyConfig latency;
    public final long seed;
    public final double impactCoeffBpsPerPctAdv;
    public final TreeMap<Long, InstrumentSpec> instruments;
    public final TreeMap<Integer, VenueSpec> venues;

    public ExecConfig(LatencyConfig latency, long seed,
            double impactCoeffBpsPerPctAdv,
            Map<Long, InstrumentSpec> instruments,
            Map<Integer, VenueSpec> venues) {
        this.latency = latency;
        this.seed = seed;
        this.impactCoeffBpsPerPctAdv = impactCoeffBpsPerPctAdv;
        this.instruments = new TreeMap<>(instruments);
        this.venues = new TreeMap<>(venues);
    }

    /** Venue profile; throws on an unknown venue_id. */
    public VenueSpec venue(int venueId) {
        VenueSpec v = venues.get(venueId);
        if (v == null) {
            throw new IllegalArgumentException("unknown venue_id " + venueId);
        }
        return v;
    }

    /** Instrument reference data; throws on an unknown instrument_id. */
    public InstrumentSpec instrument(long instrumentId) {
        InstrumentSpec i = instruments.get(instrumentId);
        if (i == null) {
            throw new IllegalArgumentException(
                    "unknown instrument_id " + instrumentId);
        }
        return i;
    }
}

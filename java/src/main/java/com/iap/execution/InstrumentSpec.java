package com.iap.execution;

/** Instrument reference data the simulator needs. */
public record InstrumentSpec(
        long instrumentId, // u32
        double tickSize,
        double lotSize,
        double adv) {      // average daily volume, base units
}

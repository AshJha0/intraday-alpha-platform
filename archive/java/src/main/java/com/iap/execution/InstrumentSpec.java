package com.iap.execution;

/**
 * Instrument reference data the simulator, backtester and risk engine
 * need. {@code qtyUnit} is the real base units per qty unit — the FX
 * {@code lot_size} (1 qty unit = 1,000 base ccy) and 1 for EQUITY/ETF
 * whose qty is already in shares (PLATFORM_CONVENTIONS.md §1); every
 * notional is {@code qty * qtyUnit * priceTicks * tickSize} in
 * {@code quoteCcy}.
 */
public record InstrumentSpec(
        long instrumentId, // u32
        double tickSize,
        double qtyUnit,
        double adv,        // average daily volume, base units
        String quoteCcy) {
    public InstrumentSpec {
        if (!(tickSize > 0.0) || !(qtyUnit > 0.0) || !(adv > 0.0)) {
            throw new IllegalArgumentException(
                    "tick_size, qty_unit and adv must be > 0 for instrument "
                            + instrumentId);
        }
        if (quoteCcy == null || quoteCcy.isEmpty()) {
            throw new IllegalArgumentException(
                    "quote_ccy must be non-empty for instrument " + instrumentId);
        }
    }

    /** USD-quoted instrument (equity shares or a USD-quoted FX pair). */
    public InstrumentSpec(long instrumentId, double tickSize, double qtyUnit,
            double adv) {
        this(instrumentId, tickSize, qtyUnit, adv, "USD");
    }

    /** Risk-engine reference view of this instrument. */
    public com.iap.risk.InstrumentRef riskRef() {
        return new com.iap.risk.InstrumentRef(tickSize, qtyUnit, quoteCcy);
    }
}

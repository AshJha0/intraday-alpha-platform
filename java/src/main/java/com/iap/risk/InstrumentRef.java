package com.iap.risk;

/**
 * Per-instrument reference data the risk engine needs (mirrors the Rust
 * {@code risk::InstrumentRef}): real price per tick, real base units per
 * qty unit ({@code lot_size} for FX, 1 for EQUITY/ETF — conventions §1)
 * and the currency of prices / P&amp;L.
 */
public record InstrumentRef(double tickSize, double qtyUnit, String quoteCcy) {
    public InstrumentRef {
        // Finite AND > 0: NaN fails `> 0`, +Infinity must fail too (an
        // infinite tick or unit makes every notional Infinity or NaN).
        if (!(Double.isFinite(tickSize) && tickSize > 0.0)
                || !(Double.isFinite(qtyUnit) && qtyUnit > 0.0)) {
            throw new IllegalArgumentException(
                    "tick_size and qty_unit must be finite and > 0");
        }
        if (quoteCcy == null || quoteCcy.isEmpty()) {
            throw new IllegalArgumentException("quote_ccy must be non-empty");
        }
    }

    /** A USD equity: qty in shares, prices in USD. */
    public static InstrumentRef equity(double tickSize) {
        return new InstrumentRef(tickSize, 1.0, "USD");
    }
}

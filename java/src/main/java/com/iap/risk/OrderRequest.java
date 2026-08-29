package com.iap.risk;

/**
 * Strategy order request (schemas/order_request.schema.json), mirroring the
 * Rust {@code venue::OrderRequest} field-for-field. Order type codes:
 * MARKET=1 LIMIT=2 IOC=3 FOK=4 PEG=5 MID=6.
 */
public record OrderRequest(long orderId, long instrumentId, int side,
        long qty, long priceTicks, int orderType, int venueId,
        String strategyId, double urgency, long timestamp) {
    /** MARKET order type code. */
    public static final int MARKET = 1;
    /** LIMIT order type code. */
    public static final int LIMIT = 2;
    /** IOC order type code. */
    public static final int IOC = 3;
    /** FOK order type code. */
    public static final int FOK = 4;
    /** PEG order type code. */
    public static final int PEG = 5;
    /** MID order type code. */
    public static final int MID = 6;

    /**
     * Contract-level validation ({@code null} when valid, else the reason)
     * — venue- and risk-independent, only the schema's own rules (mirrors
     * {@code venue::order_validation_error}).
     */
    public String validationError() {
        if (side < 0 || side > 1) {
            return "side must be 0 or 1: " + side;
        }
        if (orderType < MARKET || orderType > MID) {
            return "unknown order_type: " + orderType;
        }
        if (qty <= 0) {
            return "qty must be > 0: " + qty;
        }
        if (!(Double.isFinite(urgency) && urgency >= 0.0 && urgency <= 1.0)) {
            return "urgency must be in [0, 1]: " + urgency;
        }
        switch (orderType) {
            case MARKET -> {
                if (priceTicks != 0) {
                    return "MARKET order must carry price_ticks 0: " + priceTicks;
                }
            }
            case LIMIT -> {
                if (priceTicks <= 0) {
                    return "LIMIT order needs price_ticks > 0: " + priceTicks;
                }
            }
            // IOC/FOK may be priced (limit-style) or unpriced (market-style);
            // PEG/MID carry no price (the venue derives it).
            case IOC, FOK -> {
                if (priceTicks < 0) {
                    return "price_ticks must be >= 0: " + priceTicks;
                }
            }
            default -> {
                if (priceTicks != 0) {
                    return "PEG/MID orders carry price_ticks 0: " + priceTicks;
                }
            }
        }
        return null;
    }
}

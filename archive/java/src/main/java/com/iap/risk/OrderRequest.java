package com.iap.risk;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;

/**
 * Strategy order request (schemas/order/order_request.schema.json), mirroring the
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
     * Rust {@code {}} ({@code Display}) of an f64, for byte parity of the
     * reason text with the reference ({@code rust/venue/src/messages.rs})
     * and the Python port ({@code rust_display_f64}): the shortest
     * round-trip digits in plain decimal notation (never scientific),
     * integral values without a fraction ({@code 2}, not {@code 2.0}),
     * {@code -0} for negative zero, {@code NaN} / {@code inf} /
     * {@code -inf}. {@link Double#toString} (shortest round-trip digits
     * since JDK 19) supplies the digits; only the layout differs.
     *
     * <p>One exception: {@code Double.toString} never prints fewer than two
     * significant digits, and when ONE digit already round-trips it prints
     * the two-digit decimal closest to the exact value instead — which for
     * the smallest subnormals is not the shortest one followed by a zero
     * ({@code Double.MIN_VALUE} is {@code 4.9E-324}, the reference prints
     * {@code 5e-324}). So the one-digit decimal nearest the exact value is
     * tried first and used whenever it round-trips.
     */
    public static String rustDisplay(double v) {
        if (Double.isNaN(v)) {
            return "NaN";
        }
        if (Double.isInfinite(v)) {
            return v > 0 ? "inf" : "-inf";
        }
        if (v == 0.0) {
            return Double.doubleToRawLongBits(v) < 0 ? "-0" : "0";
        }
        BigDecimal oneDigit = new BigDecimal(v)
                .round(new MathContext(1, RoundingMode.HALF_EVEN));
        String plain = (oneDigit.doubleValue() == v
                ? oneDigit : new BigDecimal(Double.toString(v))).toPlainString();
        if (plain.indexOf('.') >= 0) {
            int end = plain.length();
            while (plain.charAt(end - 1) == '0') {
                end--;
            }
            if (plain.charAt(end - 1) == '.') {
                end--;
            }
            plain = plain.substring(0, end);
        }
        return plain;
    }

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
            return "urgency must be in [0, 1]: " + rustDisplay(urgency);
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

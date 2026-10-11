package com.iap.contracts;

import java.math.BigDecimal;
import java.math.RoundingMode;

/**
 * The handful of Python format specs the cross-language text contracts use
 * ({@code explain()} lines, lifecycle reasons), reproduced byte-exactly:
 * {@code :.Nf} rounds the EXACT binary value half-to-even (never the
 * double's shortest decimal), a negative value that rounds to zero keeps
 * its minus sign, {@code +} forces a sign and {@code ,} groups thousands.
 */
public final class PyFormat {
    private PyFormat() {
    }

    /** Python {@code f"{v:.{decimals}f}"} (optionally with the {@code +} flag). */
    public static String fixed(double v, int decimals, boolean forceSign) {
        if (!Double.isFinite(v)) {
            throw new IllegalArgumentException("fixed(): non-finite value " + v);
        }
        boolean negative = Double.doubleToRawLongBits(v) < 0;
        String magnitude = new BigDecimal(Math.abs(v))
                .setScale(decimals, RoundingMode.HALF_EVEN).toPlainString();
        if (negative) {
            return "-" + magnitude;
        }
        return forceSign ? "+" + magnitude : magnitude;
    }

    /** Python {@code f"{v:.{decimals}f}"}. */
    public static String fixed(double v, int decimals) {
        return fixed(v, decimals, false);
    }

    /** Python {@code f"{n:,d}"} / {@code f"{n:+,d}"}. */
    public static String grouped(long n, boolean forceSign) {
        String digits = Long.toString(Math.abs(n));
        StringBuilder sb = new StringBuilder(digits.length() + 8);
        if (n < 0) {
            sb.append('-');
        } else if (forceSign) {
            sb.append('+');
        }
        int lead = digits.length() % 3;
        if (lead > 0) {
            sb.append(digits, 0, lead);
        }
        for (int i = lead; i < digits.length(); i += 3) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(digits, i, i + 3);
        }
        return sb.toString();
    }
}

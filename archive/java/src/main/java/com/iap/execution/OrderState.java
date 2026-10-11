package com.iap.execution;

/** Child-order lifecycle states. */
public enum OrderState {
    /** Submitted, in flight to the venue. */
    PENDING,
    /** Resting passively at the venue. */
    ACTIVE,
    FILLED,
    /** Includes IOC/FOK/MARKET unfilled remainders. */
    CANCELLED,
}

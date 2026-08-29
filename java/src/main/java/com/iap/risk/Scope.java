package com.iap.risk;

/** Decision scope (schemas/risk_event.schema.json enum). */
public enum Scope {
    /** Whole-firm scope ({@code scope_id} is ""). */
    GLOBAL,
    /** One strategy ({@code scope_id} = strategy id). */
    STRATEGY,
    /** One instrument ({@code scope_id} = decimal instrument_id). */
    INSTRUMENT,
    /** One venue ({@code scope_id} = decimal venue_id). */
    VENUE;

    /** Parse a schema scope string. */
    public static Scope parse(String s) {
        return switch (s) {
            case "GLOBAL" -> GLOBAL;
            case "STRATEGY" -> STRATEGY;
            case "INSTRUMENT" -> INSTRUMENT;
            case "VENUE" -> VENUE;
            default -> throw new IllegalArgumentException("bad scope " + s);
        };
    }
}

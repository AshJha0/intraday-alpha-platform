package com.iap.lifecycle;

/**
 * The seven alpha promotion states (ordered integer ids 0..6; NAMES on the
 * wire — {@code schemas/alpha/lifecycle_transition.schema.json}):
 * RESEARCH → CANDIDATE → VALIDATING → PAPER → ACTIVE ⇄ WATCH → RETIRED.
 * ACTIVE / WATCH / RETIRED are the live sub-machine of
 * {@link com.iap.adaptive.LifecycleGauge}.
 */
public enum LifecycleState {
    RESEARCH(0), CANDIDATE(1), VALIDATING(2), PAPER(3), ACTIVE(4), WATCH(5),
    RETIRED(6);

    private final int index;

    LifecycleState(int index) {
        this.index = index;
    }

    /** The pinned integer id ({@code state_index} in the registry). */
    public int index() {
        return index;
    }

    /** Parse a wire name; unknown names are an {@link IllegalArgumentException}. */
    public static LifecycleState parse(String name, String where) {
        for (LifecycleState s : values()) {
            if (s.name().equals(name)) {
                return s;
            }
        }
        throw new IllegalArgumentException(where + ": unknown lifecycle state '"
                + name + "'");
    }
}

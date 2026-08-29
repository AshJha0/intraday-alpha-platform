"""Venue family (spec §10, "Venue").

Computed from the per-venue books of the instrument's ConsolidatedBook.
"Active" means the venue book has both sides and is not stale.  All venue
iteration is in sorted venue_id order (deterministic).

- ``venue_count_active``       = number of active venues
- ``venue_depth_hhi``          = Herfindahl index of the per-venue shares of
                                 two-sided top-10 depth (1 = single venue)
- ``venue_update_hhi_w10s``    = HHI of per-venue event-count shares over 10s
- ``venue_update_share_top_w10s`` = largest per-venue event-count share over 10s
- ``venue_staleness_{max,min}_ms`` = max/min over venues seen of
                                 (t - venue last event exchange_ts) in ms
- ``venue_staleness_spread_ms``= max - min staleness
- ``venue_imbalance_divergence`` = max - min of per-venue L1 imbalance
                                 (Qb-Qa)/(Qb+Qa) across active venues
                                 (0 with a single active venue)
- ``venue_best_depth_share_{side}`` = largest single-venue size at the
                                 consolidated best price / consolidated best
                                 size (1 = one venue owns the touch)

Validity: staleness and counts are valid once any event was seen; depth/
imbalance features need book_ok and >= 1 active venue; update shares need
10s warmup and >= 1 event in the window.
"""

from __future__ import annotations

from typing import List

from iap.features._famutil import put
from iap.features.spec import WINDOW_NS, FeatureSpec, mkspec

FAMILY = "venue"


def specs() -> List[FeatureSpec]:
    """Registry entries for the venue family (pinned order)."""
    out: List[FeatureSpec] = []
    out.append(mkspec("venue_count_active_v1", FAMILY,
                      "Number of venues with a two-sided, non-stale book."))
    out.append(mkspec("venue_depth_hhi_v1", FAMILY,
                      "HHI of per-venue shares of two-sided top-10 depth."))
    out.append(mkspec("venue_update_hhi_w10s_v1", FAMILY,
                      "HHI of per-venue event-count shares over 10s.",
                      window="10s"))
    out.append(mkspec("venue_update_share_top_w10s_v1", FAMILY,
                      "Largest per-venue event-count share over 10s.",
                      window="10s"))
    out.append(mkspec("venue_staleness_max_ms_v1", FAMILY,
                      "Max over venues of ms since that venue's last event."))
    out.append(mkspec("venue_staleness_min_ms_v1", FAMILY,
                      "Min over venues of ms since that venue's last event."))
    out.append(mkspec("venue_staleness_spread_ms_v1", FAMILY,
                      "Staleness spread: max - min venue staleness in ms.",
                      depends_on=("venue_staleness_max_ms_v1",
                                  "venue_staleness_min_ms_v1")))
    out.append(mkspec("venue_imbalance_divergence_v1", FAMILY,
                      "Max - min of per-venue L1 imbalance across active venues."))
    for side in ("bid", "ask"):
        out.append(mkspec(
            f"venue_best_depth_share_{side}_v1", FAMILY,
            f"Largest single-venue share of the consolidated best-{side} size.",
            side=side))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 10 venue values for the current emission."""
    rows = st.venue_rows  # [(vid, bid10, ask10, stale)] sorted by vid
    active = [(vid, b, a) for vid, b, a, stale in rows if b and a and not stale]
    put(values, valid, float(len(active)), True)

    hhi = None
    if st.book_ok and active:
        depths = [sum(q for _, q in b) + sum(q for _, q in a)
                  for _, b, a in active]
        tot = sum(depths)
        if tot > 0:
            hhi = sum((d / tot) ** 2 for d in depths)
    put(values, valid, hhi, hhi is not None)

    shares = st.venue_updates.shares() if st.warm(WINDOW_NS["10s"]) else []
    put(values, valid, sum(s * s for s in shares) if shares else None, bool(shares))
    put(values, valid, max(shares) if shares else None, bool(shares))

    stale_ms = [(st.t - ts) / 1e6 for _, ts in sorted(st.venue_last_ts.items())]
    put(values, valid, max(stale_ms) if stale_ms else None, bool(stale_ms))
    put(values, valid, min(stale_ms) if stale_ms else None, bool(stale_ms))
    put(values, valid,
        (max(stale_ms) - min(stale_ms)) if stale_ms else None, bool(stale_ms))

    div = None
    if st.book_ok and active:
        imbs = []
        for _, b, a in active:
            qb, qa = b[0][1], a[0][1]
            if qb + qa > 0:
                imbs.append((qb - qa) / (qb + qa))
        if imbs:
            div = max(imbs) - min(imbs)
    put(values, valid, div, div is not None)

    for side_idx, best_p, best_q in ((1, st.bid_p, st.bid_q),
                                     (2, st.ask_p, st.ask_q)):
        share = None
        if st.book_ok and best_q > 0:
            best_venue = 0
            for row in rows:
                if row[3]:  # stale
                    continue
                for p, q in row[side_idx]:
                    if p == best_p:
                        best_venue = max(best_venue, q)
                        break
            share = best_venue / best_q
        put(values, valid, share, share is not None)

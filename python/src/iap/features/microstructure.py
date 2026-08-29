"""Microstructure family (spec §10, "Microstructure").

State source: the consolidated book over non-stale venue books, top-10 depth
per side, refreshed after every book-touching event (ADD/MODIFY/CANCEL/
EXECUTE/QUOTE and the final record of a SNAPSHOT burst).

Instantaneous formulas (b_k / a_k = summed size of the best k bid/ask price
levels; Pb/Pa = best bid/ask in ticks; Qb/Qa = best sizes; tick = tick_size):

- ``mid_price``        = (Pb + Pa) * tick / 2
- ``microprice``       = (Pb*Qa + Pa*Qb) / (Qb + Qa) * tick
                         (size-weighted expected next mid, Stoikov microprice)
- ``micro_mid_dev_bps``= (microprice - mid) / mid * 1e4
- ``spread_ticks``     = Pa - Pb          (can be <= 0 across venues)
- ``spread_bps``       = (Pa - Pb) * tick / mid * 1e4
- ``depth_{side}_lk``  = summed size of the k best levels of that side
- ``depth_total_lk``   = depth_bid_lk + depth_ask_lk
- ``imbalance_lk``     = (b_k - a_k) / (b_k + a_k)
- ``order_count_{side}_l1`` = number of resting orders at the best price
                         (summed across non-stale venues quoting that price)
- ``avg_order_size_{side}_l1`` = Q_side / order_count_side_l1

Windowed formulas (window w, samples taken at every book refresh with a
two-sided book; window is the half-open event-time interval (t-w, t]):

- ``*_avg_w``          = mean of the sampled instantaneous value over w
- ``queue_depletion_rate_{side}_w``   = (summed L1 depletion qty over w) / w_seconds
- ``queue_replenish_rate_{side}_w``   = (summed L1 replenishment qty over w) / w_seconds

L1 depletion/replenishment per book refresh (pinned): compare the previous
best level (P', Q') to the current (P, Q) on the same side:
same price  -> dQ = Q - Q'; dQ < 0 adds -dQ to depletion, else dQ to
replenishment; price improved -> Q to replenishment; price worsened -> Q' to
depletion; side appearing -> Q to replenishment; side vanishing -> Q' to
depletion.

Validity: instantaneous features need book_ok; windowed features need the
window fully elapsed since the instrument's first event (warmup) and at
least one sample (rates are valid with zero events once warm).
"""

from __future__ import annotations

from typing import List

from iap.features._famutil import put
from iap.features.spec import WINDOW_NS, FeatureSpec, mkspec

FAMILY = "micro"

AVG_WINDOWS = ("1s", "10s")
QUEUE_WINDOWS = ("1s", "10s")
IMB_LEVELS = (1, 3, 5, 10)
DEPTH_LEVELS = (1, 5, 10)


def specs() -> List[FeatureSpec]:
    """Registry entries for the microstructure family (pinned order)."""
    out: List[FeatureSpec] = []
    out.append(mkspec("mid_price_v1", FAMILY,
                      "Consolidated mid price: (best_bid + best_ask)/2 * tick_size."))
    out.append(mkspec("microprice_v1", FAMILY,
                      "Size-weighted microprice: (Pb*Qa + Pa*Qb)/(Qb+Qa) * tick_size."))
    out.append(mkspec("micro_mid_dev_bps_v1", FAMILY,
                      "Microprice deviation from mid in bps: (microprice-mid)/mid*1e4.",
                      depends_on=("microprice_v1", "mid_price_v1")))
    out.append(mkspec("spread_ticks_v1", FAMILY,
                      "Consolidated quoted spread in ticks: best_ask - best_bid."))
    out.append(mkspec("spread_bps_v1", FAMILY,
                      "Quoted spread in bps of mid: spread_ticks*tick_size/mid*1e4.",
                      depends_on=("spread_ticks_v1", "mid_price_v1")))
    for k in DEPTH_LEVELS:
        for side in ("bid", "ask"):
            out.append(mkspec(f"depth_{side}_l{k}_v1", FAMILY,
                              f"Summed {side} size over the best {k} price level(s).",
                              levels=k, side=side))
    for k in DEPTH_LEVELS:
        out.append(mkspec(f"depth_total_l{k}_v1", FAMILY,
                          f"Two-sided depth over the best {k} level(s): bid + ask.",
                          depends_on=(f"depth_bid_l{k}_v1", f"depth_ask_l{k}_v1"),
                          levels=k))
    for w in AVG_WINDOWS:
        for k in DEPTH_LEVELS:
            for side in ("bid", "ask"):
                out.append(mkspec(
                    f"depth_{side}_l{k}_avg_w{w}_v1", FAMILY,
                    f"Mean of depth_{side}_l{k} sampled at book refreshes over {w}.",
                    depends_on=(f"depth_{side}_l{k}_v1",),
                    levels=k, side=side, window=w))
    for k in IMB_LEVELS:
        out.append(mkspec(f"imbalance_l{k}_v1", FAMILY,
                          f"Depth imbalance over best {k} level(s): (b-a)/(b+a).",
                          levels=k))
    for w in AVG_WINDOWS:
        for k in IMB_LEVELS:
            out.append(mkspec(
                f"imbalance_l{k}_avg_w{w}_v1", FAMILY,
                f"Mean of imbalance_l{k} sampled at book refreshes over {w}.",
                depends_on=(f"imbalance_l{k}_v1",),
                levels=k, window=w))
    for w in AVG_WINDOWS:
        out.append(mkspec(f"spread_ticks_avg_w{w}_v1", FAMILY,
                          f"Mean quoted spread in ticks over {w}.",
                          depends_on=("spread_ticks_v1",), window=w))
    for w in QUEUE_WINDOWS:
        for side in ("bid", "ask"):
            out.append(mkspec(
                f"queue_depletion_rate_{side}_w{w}_v1", FAMILY,
                f"L1 {side} queue depletion rate over {w}: depleted qty per second.",
                side=side, window=w))
    for w in QUEUE_WINDOWS:
        for side in ("bid", "ask"):
            out.append(mkspec(
                f"queue_replenish_rate_{side}_w{w}_v1", FAMILY,
                f"L1 {side} queue replenishment rate over {w}: added qty per second.",
                side=side, window=w))
    for side in ("bid", "ask"):
        out.append(mkspec(f"order_count_{side}_l1_v1", FAMILY,
                          f"Resting order count at the best {side} price "
                          f"(non-stale venues quoting that price).",
                          side=side))
    for side in ("bid", "ask"):
        out.append(mkspec(f"avg_order_size_{side}_l1_v1", FAMILY,
                          f"Best-{side} size divided by its resting order count.",
                          depends_on=(f"depth_{side}_l1_v1",
                                      f"order_count_{side}_l1_v1"),
                          side=side))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 52 microstructure values for the current emission."""
    ok = st.book_ok
    tick = st.tick
    mid = st.mid if ok else None
    # instantaneous top-of-book
    put(values, valid, mid, ok)
    micro = None
    if ok and (st.bid_q + st.ask_q) > 0:
        micro = (st.bid_p * st.ask_q + st.ask_p * st.bid_q) / (st.bid_q + st.ask_q) * tick
    put(values, valid, micro, micro is not None)
    put(values, valid,
        (micro - mid) / mid * 1e4 if (micro is not None and mid) else None,
        micro is not None and bool(mid))
    put(values, valid, st.spread_ticks if ok else None, ok)
    put(values, valid, st.spread_bps if ok else None, ok)
    dvals = {("bid", 1): st.db1, ("ask", 1): st.da1,
             ("bid", 5): st.db5, ("ask", 5): st.da5,
             ("bid", 10): st.db10, ("ask", 10): st.da10}
    for k in DEPTH_LEVELS:
        for side in ("bid", "ask"):
            put(values, valid, dvals[(side, k)] if ok else None, ok)
    for k in DEPTH_LEVELS:
        put(values, valid,
            (dvals[("bid", k)] + dvals[("ask", k)]) if ok else None, ok)
    # windowed depth averages (depthavg sums layout:
    # [db1,da1,db5,da5,db10,da10,imb1,imb3,imb5,imb10,spr,dtot])
    idx = {(1, "bid"): 0, (1, "ask"): 1, (5, "bid"): 2, (5, "ask"): 3,
           (10, "bid"): 4, (10, "ask"): 5}
    for w in AVG_WINDOWS:
        win = st.depthavg[w]
        wok = st.warm(WINDOW_NS[w]) and win.count > 0
        for k in DEPTH_LEVELS:
            for side in ("bid", "ask"):
                put(values, valid,
                    win.sums[idx[(k, side)]] / win.count if wok else None, wok)
    for k in IMB_LEVELS:
        b = dvals.get(("bid", k)) if k in (1, 5, 10) else None
        if k == 3:
            b, a = st.db3, st.da3
        else:
            a = dvals[("ask", k)]
        imb = (b - a) / (b + a) if ok and (b + a) > 0 else None
        put(values, valid, imb, imb is not None)
    imb_idx = {1: 6, 3: 7, 5: 8, 10: 9}
    for w in AVG_WINDOWS:
        win = st.depthavg[w]
        wok = st.warm(WINDOW_NS[w]) and win.count > 0
        for k in IMB_LEVELS:
            put(values, valid,
                win.sums[imb_idx[k]] / win.count if wok else None, wok)
    for w in AVG_WINDOWS:
        win = st.depthavg[w]
        wok = st.warm(WINDOW_NS[w]) and win.count > 0
        put(values, valid, win.sums[10] / win.count if wok else None, wok)
    # queue rates (queue sums layout: [dep_b, rep_b, dep_a, rep_a])
    for w in QUEUE_WINDOWS:
        win = st.queue[w]
        w_s = WINDOW_NS[w] / 1e9
        wok = st.warm(WINDOW_NS[w])
        for i in (0, 2):  # dep_b, dep_a
            put(values, valid, win.sums[i] / w_s if wok else None, wok)
    for w in QUEUE_WINDOWS:
        win = st.queue[w]
        w_s = WINDOW_NS[w] / 1e9
        wok = st.warm(WINDOW_NS[w])
        for i in (1, 3):  # rep_b, rep_a
            put(values, valid, win.sums[i] / w_s if wok else None, wok)
    # order counts / average sizes at L1
    put(values, valid, st.oc_bid1 if ok else None, ok and st.oc_bid1 > 0)
    put(values, valid, st.oc_ask1 if ok else None, ok and st.oc_ask1 > 0)
    put(values, valid,
        st.bid_q / st.oc_bid1 if ok and st.oc_bid1 > 0 else None,
        ok and st.oc_bid1 > 0)
    put(values, valid,
        st.ask_q / st.oc_ask1 if ok and st.oc_ask1 > 0 else None,
        ok and st.oc_ask1 > 0)

"""Order-flow family (spec §10, "Order flow").

Multi-level order-flow imbalance (OFI), signed trade volume, trade
imbalance, and event intensities, all over pinned event-time windows
(half-open (t-w, t]).

OFI contribution at each book refresh (pinned; multi-level generalization of
Cont-Kukanov-Stoikov): for level count k and side s, let prev_k / curr_k be
the {price -> size} maps of the best k levels of side s before and after the
event.  Then

    delta_s(k) = sum over p in union(prev_k, curr_k) of
                 (curr_k.get(p, 0) - prev_k.get(p, 0))

and the event's contribution is  e(k) = delta_bid(k) - delta_ask(k).
``ofi_lk_w`` is the sum of e(k) over the window (integer, exact).

- ``ofi_norm_lk_w``     = ofi_lk_w / (mean two-sided depth_total_lk over 10s + EPS);
                        INVALID when that mean depth is 0 (EPS guard, §4)
- ``signed_volume_w``   = sum of +qty (buy aggressor, side=BID) / -qty (sell) TRADEs
- ``trade_imbalance_w`` = (buy_qty - sell_qty) / (buy_qty + sell_qty)
- ``trade_count_w``     = number of TRADE events in w
- ``*_intensity_w``     = event count / w_seconds for the given event type
  (add = ADD, cancel = CANCEL, modify = MODIFY, execute = EXECUTE, trade = TRADE)
- ``add_qty_w`` / ``cancel_qty_w`` = summed event qty of ADD / CANCEL events
- ``cancel_add_ratio_w``= cancel_count / add_count  (valid when add_count > 0)

Validity: window fully elapsed (warmup); trade_imbalance additionally needs
traded volume > 0; ofi_norm needs the 10s depth average to exist.
"""

from __future__ import annotations

from typing import List

from iap.features._famutil import put
from iap.features.spec import EPS, WINDOW_NS, FeatureSpec, mkspec

FAMILY = "flow"

OFI_LEVELS = (1, 3, 5, 10)
OFI_WINDOWS = ("1s", "5s", "30s")
OFI_NORM_LEVELS = (1, 5)
TRADE_WINDOWS = ("1s", "10s", "1m")
EV_WINDOWS = ("1s", "10s", "1m")
SHORT_WINDOWS = ("1s", "10s")


def specs() -> List[FeatureSpec]:
    """Registry entries for the order-flow family (pinned order)."""
    out: List[FeatureSpec] = []
    for k in OFI_LEVELS:
        for w in OFI_WINDOWS:
            out.append(mkspec(
                f"ofi_l{k}_w{w}_v1", FAMILY,
                f"Order-flow imbalance over the best {k} level(s), summed over {w}: "
                f"sum of per-event (bid depth delta - ask depth delta) within top {k}.",
                levels=k, window=w))
    for k in OFI_NORM_LEVELS:
        for w in OFI_WINDOWS:
            out.append(mkspec(
                f"ofi_norm_l{k}_w{w}_v1", FAMILY,
                f"ofi_l{k}_w{w} normalized by the 10s mean two-sided depth over "
                f"the best {k} level(s): ofi / (mean depth_total_l{k} + EPS).",
                depends_on=(f"ofi_l{k}_w{w}_v1", f"depth_bid_l{k}_avg_w10s_v1",
                            f"depth_ask_l{k}_avg_w10s_v1"),
                levels=k, window=w))
    for w in TRADE_WINDOWS:
        out.append(mkspec(
            f"signed_volume_w{w}_v1", FAMILY,
            f"Signed traded volume over {w}: +qty for buy-aggressor TRADEs "
            f"(side=BID), -qty for sells.", window=w))
    for w in TRADE_WINDOWS:
        out.append(mkspec(
            f"trade_imbalance_w{w}_v1", FAMILY,
            f"Trade imbalance over {w}: (buy_qty - sell_qty)/(buy_qty + sell_qty).",
            window=w))
    for w in TRADE_WINDOWS:
        out.append(mkspec(f"trade_count_w{w}_v1", FAMILY,
                          f"Number of TRADE events in {w}.", window=w))
    for w in TRADE_WINDOWS:
        out.append(mkspec(f"trade_intensity_w{w}_v1", FAMILY,
                          f"TRADE events per second over {w}.",
                          depends_on=(f"trade_count_w{w}_v1",), window=w))
    for w in EV_WINDOWS:
        out.append(mkspec(f"add_intensity_w{w}_v1", FAMILY,
                          f"ADD events per second over {w}.", window=w))
    for w in EV_WINDOWS:
        out.append(mkspec(f"cancel_intensity_w{w}_v1", FAMILY,
                          f"CANCEL events per second over {w}.", window=w))
    for w in SHORT_WINDOWS:
        out.append(mkspec(f"modify_intensity_w{w}_v1", FAMILY,
                          f"MODIFY events per second over {w}.", window=w))
    for w in EV_WINDOWS:
        out.append(mkspec(f"execute_intensity_w{w}_v1", FAMILY,
                          f"EXECUTE events per second over {w}.", window=w))
    for w in SHORT_WINDOWS:
        out.append(mkspec(f"add_qty_w{w}_v1", FAMILY,
                          f"Summed qty of ADD events over {w}.", window=w))
    for w in SHORT_WINDOWS:
        out.append(mkspec(f"cancel_qty_w{w}_v1", FAMILY,
                          f"Summed qty of CANCEL events over {w}.", window=w))
    for w in SHORT_WINDOWS:
        out.append(mkspec(f"cancel_add_ratio_w{w}_v1", FAMILY,
                          f"cancel_count / add_count over {w} (valid when adds > 0).",
                          window=w))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 47 order-flow values for the current emission."""
    ofi_idx = {1: 0, 3: 1, 5: 2, 10: 3}
    for k in OFI_LEVELS:
        for w in OFI_WINDOWS:
            wok = st.warm(WINDOW_NS[w])
            put(values, valid, st.ofi[w].sums[ofi_idx[k]] if wok else None, wok)
    da10 = st.depthavg["10s"]
    d_idx = {1: (0, 1), 5: (2, 3)}
    for k in OFI_NORM_LEVELS:
        bi, ai = d_idx[k]
        for w in OFI_WINDOWS:
            v = None
            if st.warm(WINDOW_NS[w]) and st.warm(WINDOW_NS["10s"]) and da10.count > 0:
                # exact integer depth total: a float `> 0` test would flip
                # between languages on accumulation drift
                if da10.sums[bi] + da10.sums[ai] > 0:
                    denom = (da10.sums[bi] + da10.sums[ai]) / da10.count
                    v = st.ofi[w].sums[ofi_idx[k]] / (denom + EPS)
            put(values, valid, v, v is not None)
    # trades sums layout: [signed, qty, buy, sell, eff_bps_sum, eff_n]
    for w in TRADE_WINDOWS:
        wok = st.warm(WINDOW_NS[w])
        put(values, valid, st.trades[w].sums[0] if wok else None, wok)
    for w in TRADE_WINDOWS:
        tw = st.trades[w]
        tot = tw.sums[2] + tw.sums[3]
        wok = st.warm(WINDOW_NS[w]) and tot > 0
        put(values, valid, (tw.sums[2] - tw.sums[3]) / tot if wok else None, wok)
    for w in TRADE_WINDOWS:
        wok = st.warm(WINDOW_NS[w])
        put(values, valid, st.trades[w].count if wok else None, wok)
    for w in TRADE_WINDOWS:
        wok = st.warm(WINDOW_NS[w])
        put(values, valid,
            st.trades[w].count / (WINDOW_NS[w] / 1e9) if wok else None, wok)
    # evstats sums layout: [add_c, add_q, can_c, can_q, mod_c, exe_c, exe_q]
    def _rate(w: str, i: int):
        wok = st.warm(WINDOW_NS[w])
        return (st.evstats[w].sums[i] / (WINDOW_NS[w] / 1e9), wok) if wok else (None, False)

    for w in EV_WINDOWS:
        v, wok = _rate(w, 0)
        put(values, valid, v, wok)
    for w in EV_WINDOWS:
        v, wok = _rate(w, 2)
        put(values, valid, v, wok)
    for w in SHORT_WINDOWS:
        v, wok = _rate(w, 4)
        put(values, valid, v, wok)
    for w in EV_WINDOWS:
        v, wok = _rate(w, 5)
        put(values, valid, v, wok)
    for w in SHORT_WINDOWS:
        wok = st.warm(WINDOW_NS[w])
        put(values, valid, st.evstats[w].sums[1] if wok else None, wok)
    for w in SHORT_WINDOWS:
        wok = st.warm(WINDOW_NS[w])
        put(values, valid, st.evstats[w].sums[3] if wok else None, wok)
    for w in SHORT_WINDOWS:
        ev = st.evstats[w]
        wok = st.warm(WINDOW_NS[w]) and ev.sums[0] > 0
        put(values, valid, ev.sums[2] / ev.sums[0] if wok else None, wok)

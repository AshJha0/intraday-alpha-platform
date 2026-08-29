"""Execution family (spec §10, "Execution").

Proxies that feed execution decisions; every input is an already-registered
feature quantity, combined by pinned formulas:

- ``fill_prob_{side}_h{h}`` = 1 - exp(-dep_rate_side_w10s * h_s / Q_side_L1)
                              — probability proxy that an order resting at the
                              back of the current L1 queue is reached within
                              horizon h if the observed 10s depletion rate
                              persists (exponential queue-clearing model).
- ``half_spread_cost_bps``  = spread_bps / 2 — cost of crossing immediately.
- ``expected_impact_bps``   = half_spread_bps * sqrt(1 + traded_volume_w10s /
                              (quoted_depth_total + EPS)) — square-root-law
                              flavored impact proxy scaling with recent
                              activity vs standing depth.
- ``alpha_decay_proxy``     = rvol_w10s / (rvol_w1m + EPS) — how front-loaded
                              recent price movement is; > 1 means information
                              is arriving faster than the 1m norm, so alpha
                              decays quickly.
- ``urgency_score``         = |imbalance_l1| * alpha_decay_proxy /
                              (1 + spread_ticks) — pressure to act now: strong
                              signed book pressure, fast-moving prices, and a
                              tight spread all raise urgency.

Validity: all need book_ok; fill probabilities and impact need 10s warmup,
alpha decay and urgency need 1m warmup.
"""

from __future__ import annotations

from math import exp
from typing import List

from iap.features._famutil import put
from iap.features.spec import EPS, WINDOW_NS, FeatureSpec, mkspec

FAMILY = "exec"

FILL_HORIZONS = ("1s", "10s")


def specs() -> List[FeatureSpec]:
    """Registry entries for the execution family (pinned order)."""
    out: List[FeatureSpec] = []
    for h in FILL_HORIZONS:
        for side in ("bid", "ask"):
            out.append(mkspec(
                f"fill_prob_{side}_h{h}_v1", FAMILY,
                f"P(back-of-L1-{side}-queue order fills within {h}): "
                f"1 - exp(-depletion_rate_{side}_w10s * h_s / Q_{side}_L1).",
                depends_on=(f"queue_depletion_rate_{side}_w10s_v1",
                            f"depth_{side}_l1_v1"),
                side=side, horizon=h))
    out.append(mkspec("half_spread_cost_bps_v1", FAMILY,
                      "Immediate crossing cost: spread_bps / 2.",
                      depends_on=("spread_bps_v1",)))
    out.append(mkspec(
        "expected_impact_bps_v1", FAMILY,
        "Impact proxy: half_spread_bps * sqrt(1 + traded_volume_w10s / "
        "(quoted_depth_total + EPS)).",
        depends_on=("half_spread_cost_bps_v1", "traded_volume_w10s_v1",
                    "quoted_depth_total_v1")))
    out.append(mkspec(
        "alpha_decay_proxy_v1", FAMILY,
        "Information arrival speed: rvol_w10s / (rvol_w1m + EPS).",
        depends_on=("rvol_w10s_v1", "rvol_w1m_v1")))
    out.append(mkspec(
        "urgency_score_v1", FAMILY,
        "Urgency to act: |imbalance_l1| * alpha_decay_proxy / (1 + spread_ticks).",
        depends_on=("imbalance_l1_v1", "alpha_decay_proxy_v1",
                    "spread_ticks_v1")))
    return out


def compute(st, values: List[float], valid: List[bool]) -> None:
    """Append the 8 execution values for the current emission."""
    warm10 = st.warm(WINDOW_NS["10s"])
    for h in FILL_HORIZONS:
        h_s = WINDOW_NS[h] / 1e9
        for dep_i, q in ((0, st.bid_q), (2, st.ask_q)):
            v = None
            if st.book_ok and warm10 and q > 0:
                rate = st.queue["10s"].sums[dep_i] / 10.0
                v = 1.0 - exp(-rate * h_s / q)
            put(values, valid, v, v is not None)
    ok = st.book_ok
    half = st.spread_bps / 2.0 if ok else None
    put(values, valid, half, ok)
    v = None
    if ok and warm10:
        vol10 = st.trades["10s"].sums[1]
        v = half * (1.0 + vol10 / (st.db10 + st.da10 + EPS)) ** 0.5
    put(values, valid, v, v is not None)
    decay = None
    rv10, rv1m = st.rvol("10s"), st.rvol("1m")
    if rv10 is not None and rv1m is not None:
        decay = rv10 / (rv1m + EPS)
    put(values, valid, decay, decay is not None)
    v = None
    if ok and decay is not None and (st.db1 + st.da1) > 0:
        denom = 1.0 + st.spread_ticks  # can be <= 0 on a crossed/locked book
        if denom > 0:
            imb1 = (st.db1 - st.da1) / (st.db1 + st.da1)
            v = abs(imb1) * decay / denom
    put(values, valid, v, v is not None)

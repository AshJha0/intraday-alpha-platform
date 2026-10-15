"""Shared fixtures of the sor_v2 goldens (rules 1 and 2)."""

from __future__ import annotations

from iap.core.events import SessionStatus
from iap.execution.calibration import VERSION as CAL_VERSION
from iap.execution.calibration import ExecCalibration
from iap.execution.config import SorOptions
from iap.execution.sor_v2 import CostAwareRouter, RouterOptions, hazard_fill_prob
from iap.execution.types import VenueSpec
from iap.execution.venues_model import FeeLedger, FeeTier, VenueModel

from . import SEC, SLICE_VENUE, EventBuilder, venue_spec_row

INSTRUMENT = 7
T0 = 1_787_578_200_000_000_000
TICK = 0.01


def venue_models(all_in_fees: bool = False) -> dict[int, VenueModel]:
    """Three equity venues: 1 (dear taker, fast), 2 (cheap taker, slow),
    3 (tiered fees, mid latency, toxic)."""
    v1 = VenueSpec(
        venue_id=1,
        name="V1",
        taker_fee_per_share=0.015 if all_in_fees else 0.003,
        maker_rebate_per_share=0.002,
        latency_mean_ns=100_000,
        latency_jitter_ns=0,
    )
    v2 = VenueSpec(
        venue_id=2,
        name="V2",
        taker_fee_per_share=0.0010,
        maker_rebate_per_share=0.0025,
        latency_mean_ns=300_000,
        latency_jitter_ns=10_000,
    )
    v3 = VenueSpec(
        venue_id=3,
        name="V3",
        taker_fee_per_share=0.0030,
        maker_rebate_per_share=0.0015,
        latency_mean_ns=150_000,
        latency_jitter_ns=0,
    )
    return {
        1: VenueModel(v1, p_fill_touch=0.6, toxicity_bps={"1s": 0.4, "100ms": 0.1}),
        2: VenueModel(v2, p_fill_touch=0.35, toxicity_bps={"1s": -0.2}, latency_ns=250_000),
        3: VenueModel(
            v3,
            p_fill_touch=0.5,
            toxicity_bps={"1s": 1.6},
            tiers=(
                FeeTier(0, 0.0030, 0.0015),
                FeeTier(5_000, 0.0020, 0.0020),
                FeeTier(20_000, 0.0012, 0.0028),
            ),
        ),
    }


def model_row(m: VenueModel) -> dict:
    return {
        "spec": venue_spec_row(m.spec),
        "p_fill_touch": m.p_fill_touch,
        "toxicity_bps": dict(sorted(m.toxicity_bps.items())),
        "latency_ns": m.latency_ns,
        "tiers": [
            [t.min_monthly_shares, t.taker_fee_per_share, t.maker_rebate_per_share] for t in m.tiers
        ],
    }


def options_row(o: RouterOptions) -> dict:
    return {
        "tox_horizon": o.tox_horizon,
        "latency_penalty_bps_per_ms": o.latency_penalty_bps_per_ms,
        "miss_cost_bps": o.miss_cost_bps,
        "stagger": o.stagger,
        "sweep_rank": o.sweep_rank,
        "sweep_levels": o.sweep_levels,
        # inf (the default: no cap) is not JSON: null stands for it
        "max_toxicity_bps": None if o.max_toxicity_bps == float("inf") else o.max_toxicity_bps,
        "tox_scale_bps": o.tox_scale_bps,
        "lot": o.lot,
    }


def options_from_row(row: dict) -> RouterOptions:
    kw = dict(row)
    if kw["max_toxicity_bps"] is None:
        kw["max_toxicity_bps"] = float("inf")
    return RouterOptions(**kw)


def sor_options_row(s: SorOptions) -> dict:
    return {"prefer_rebate": s.prefer_rebate, "max_venue_latency_ns": s.max_venue_latency_ns}


def three_venue_books(halt_v3: bool = False, gap_v2: bool = False) -> EventBuilder:
    """Displayed depth on the three venues (bids / asks at 100.00 +- ticks)."""
    b = EventBuilder(INSTRUMENT, T0)
    # venue 1: asks 10001x300 10002x500 10003x1000 10005x200; bids 9999x400 9998x600 9996x900
    b.add(1, 1, 1, 10001, 300, 11)
    b.add(1, 2, 1, 10002, 500, 12)
    b.add(1, 3, 1, 10003, 1000, 13)
    b.add(1, 4, 1, 10005, 200, 14)
    b.add(1, 5, 0, 9999, 400, 15)
    b.add(1, 6, 0, 9998, 600, 16)
    b.add(1, 7, 0, 9996, 900, 17)
    # venue 2: asks 10002x200 10002x150 (two orders, one level) 10003x400; bids 9999x100 9997x700
    b.add(2, 1, 1, 10002, 200, 21, skip_sequence=1 if gap_v2 else 0)
    b.add(2, 2, 1, 10002, 150, 22)
    b.add(2, 3, 1, 10003, 400, 23)
    b.add(2, 4, 0, 9999, 100, 24)
    b.add(2, 5, 0, 9997, 700, 25)
    # venue 3: asks 10001x50 10004x2000; bids 10000x250 9999x800 9998x100
    b.add(3, 1, 1, 10001, 50, 31)
    b.add(3, 2, 1, 10004, 2000, 32)
    b.add(3, 3, 0, 10000, 250, 33)
    b.add(3, 4, 0, 9999, 800, 34)
    b.add(3, 5, 0, 9998, 100, 35)
    if halt_v3:
        b.status(3, 6, SessionStatus.HALT)
    return b


def ledger_with_fills(models, fills: list[list]) -> FeeLedger:
    ledger = FeeLedger(models)
    for vid, qty, ts, maker in fills:
        ledger.record(vid, qty, ts, maker)
    return ledger


def router(models, options, sor_options, fills) -> CostAwareRouter:
    return CostAwareRouter(models, options, sor_options, ledger_with_fills(models, fills))


#: fills recorded on the ledger before the queries: venue 3 crosses its 5,000
#: tier in month 1; an earlier-month fill proves the month roll-over.
LEDGER_FILLS = [
    [3, 4_000, T0, False],
    [3, 1_500, T0 + SEC, True],
    [3, 30_000, T0 - 40 * 86_400 * SEC, False],
    [1, 2_000, T0, False],
]


def slice_models() -> dict[int, VenueModel]:
    """The slice's one venue (its venues.json: fee 0.003, rebate 0.002, latency 0)."""
    spec = VenueSpec(
        venue_id=SLICE_VENUE,
        name="XNAS",
        taker_fee_per_share=0.003,
        maker_rebate_per_share=0.002,
        latency_mean_ns=0,
        latency_jitter_ns=0,
    )
    return {SLICE_VENUE: VenueModel(spec, p_fill_touch=0.45, toxicity_bps={"1s": 0.8})}


# ------------------------------------------------- X-4 depletion read (rule 2)

#: A calibration document per venue carrying the X-4 split: pooled, execution
#: and cancel depletion rates per queue bucket; a missing / null cell is None.
QUEUE_EDGES = [0, 500, 2000]


def calibration_docs() -> dict[str, dict]:
    def cell(h, e, c):
        out = {}
        if h is not None:
            out["hazard_per_s"] = h
        if e is not None:
            out["exec_hazard_per_s"] = e
        if c is not None:
            out["cancel_hazard_per_s"] = c
        return out

    return {
        "1": {
            "queue_edges": QUEUE_EDGES,
            "queue_depletion": {
                "bid": {
                    "0": cell(2.0, 1.2, 0.8),
                    "1-500": cell(0.9, 0.5, 0.4),
                    "501-2000": cell(0.3, 0.12, 0.18),
                    ">2000": cell(0.05, 0.02, 0.03),
                },
                "ask": {
                    "0": cell(1.8, 1.0, 0.8),
                    "1-500": cell(0.8, 0.45, 0.35),
                    "501-2000": cell(0.25, None, None),
                    ">2000": {},
                },
            },
        },
        "2": {
            "queue_edges": QUEUE_EDGES,
            "queue_depletion": {
                "bid": {"0": cell(1.0, 0.7, 0.3), "1-500": cell(0.4, 0.3, 0.1)},
                "ask": {
                    "0": cell(1.1, 0.6, 0.5),
                    "1-500": cell(0.5, 0.2, 0.3),
                    "501-2000": cell(0.2, 0.1, 0.1),
                    ">2000": cell(0.01, 0.0, 0.01),
                },
            },
        },
        "3": {
            "queue_edges": QUEUE_EDGES,
            "queue_depletion": {"bid": {}, "ask": {"0": cell(3.0, 2.5, 0.5)}},
        },
        str(SLICE_VENUE): {
            "queue_edges": [0, 300, 1000, 5000],
            "queue_depletion": {
                "bid": {
                    "0": cell(4.0, 3.1, 0.9),
                    "1-300": cell(2.2, 1.4, 0.8),
                    "301-1000": cell(0.9, 0.55, 0.35),
                    "1001-5000": cell(0.3, 0.16, 0.14),
                    ">5000": cell(0.08, 0.03, 0.05),
                },
                "ask": {
                    "0": cell(3.8, 2.9, 0.9),
                    "1-300": cell(2.0, 1.3, 0.7),
                    "301-1000": cell(0.85, 0.5, 0.35),
                    "1001-5000": cell(0.28, 0.15, 0.13),
                    ">5000": cell(0.07, None, 0.05),
                },
            },
        },
    }


def fill_prob_from(docs: dict[str, dict], source: str, horizon_s: float):
    cals = {
        int(k): ExecCalibration.from_dict(
            dict(v, schema="iap.exec_calibration", version=CAL_VERSION)
        )
        for k, v in docs.items()
    }

    def rate(vid: int, side: int, q: int):
        cal = cals.get(vid)
        if cal is None:
            return None
        if source == "exec":
            return cal.queue_exec_depletion_hazard(side, q)
        if source == "cancel":
            return cal.queue_cancel_depletion_hazard(side, q)
        return cal.queue_depletion_hazard(side, q)

    return hazard_fill_prob(rate, horizon_s)

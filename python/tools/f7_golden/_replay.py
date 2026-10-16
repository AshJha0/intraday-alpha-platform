"""Shared replay fixtures of the simulator / scheduler goldens (rules 3, 4)."""

from __future__ import annotations

from iap.contracts.versions import canonical_json
from iap.core.codec import read_jsonl, sha256_bytes
from iap.execution import (
    AlgoType,
    ExecConfig,
    ExecPolicy,
    ExecutionReplay,
    InstrumentSpec,
    LatencyConfig,
    ParentOrder,
    PassiveParams,
    VenueSpec,
    load_venues,
)

from . import (
    GOLDEN_DIR,
    REPO,
    SLICE_ADV,
    SLICE_INSTRUMENT,
    SLICE_PROVENANCE,
    SLICE_TICK,
    SLICE_VENUE,
    instrument_row,
    load_slice,
    venue_spec_row,
)

SEC = 1_000_000_000
GOLDEN_EVENTS = "golden:events_eq_mbo.jsonl"
SLICE_EVENTS = "slice"
SLICE_T0 = 1_548_858_600_000_000_000  # window start of the real slice (SLICE.md)


def golden_venues() -> dict[int, VenueSpec]:
    return load_venues(REPO / "configs" / "venues" / "venues.json")


def slice_venues() -> dict[int, VenueSpec]:
    return {
        SLICE_VENUE: VenueSpec(
            venue_id=SLICE_VENUE,
            name="XNAS",
            taker_fee_per_share=0.003,
            maker_rebate_per_share=0.002,
            latency_mean_ns=0,
            latency_jitter_ns=0,
        )
    }


def config_row(cfg: ExecConfig) -> dict:
    return {
        "latency": {
            "decision_ns": cfg.latency.decision_ns,
            "risk_ns": cfg.latency.risk_ns,
            "wire_ns": cfg.latency.wire_ns,
        },
        "seed": cfg.seed,
        "impact_coeff_bps_per_pct_adv": cfg.impact_coeff_bps_per_pct_adv,
        "impact_model": cfg.impact_model,
        "impact_exponent": cfg.impact_exponent,
        "permanent_impact_bps_per_pct_adv": cfg.permanent_impact_bps_per_pct_adv,
        "instruments": [instrument_row(i) for _, i in sorted(cfg.instruments.items())],
        "venues": [venue_spec_row(v) for _, v in sorted(cfg.venues.items())],
    }


def config_from_row(row: dict) -> ExecConfig:
    return ExecConfig(
        latency=LatencyConfig(**row["latency"]),
        seed=row["seed"],
        impact_coeff_bps_per_pct_adv=row["impact_coeff_bps_per_pct_adv"],
        impact_model=row["impact_model"],
        impact_exponent=row["impact_exponent"],
        permanent_impact_bps_per_pct_adv=row["permanent_impact_bps_per_pct_adv"],
        instruments={r["instrument_id"]: InstrumentSpec(**r) for r in row["instruments"]},
        venues={r["venue_id"]: VenueSpec(**r) for r in row["venues"]},
    )


def parent_row(p: ParentOrder) -> dict:
    return {
        "parent_id": p.parent_id,
        "instrument_id": p.instrument_id,
        "venue_id": p.venue_id,
        "side": p.side,
        "qty": p.qty,
        "algo": int(p.algo),
        "start_ts": p.start_ts,
        "end_ts": p.end_ts,
        "slices": p.slices,
        "participation": p.participation,
        "risk_aversion": p.risk_aversion,
        "max_child_qty": p.max_child_qty,
        "policy": int(p.policy),
        "urgency": p.urgency,
        "passive": {
            "max_rest_ns": p.passive.max_rest_ns,
            "max_reprices": p.passive.max_reprices,
            "max_behind_fraction": p.passive.max_behind_fraction,
            "improve_min_spread_ticks": p.passive.improve_min_spread_ticks,
            "end_margin_ns": p.passive.end_margin_ns,
        },
        "pov_exact": p.pov_exact,
    }


def parent_from_row(r: dict) -> ParentOrder:
    kw = dict(r)
    kw["algo"] = AlgoType(kw["algo"])
    kw["policy"] = ExecPolicy(kw["policy"])
    kw["passive"] = PassiveParams(**kw["passive"])
    return ParentOrder(**kw)


def events_for(spec: str):
    if spec == GOLDEN_EVENTS:
        return read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")
    if spec == SLICE_EVENTS:
        return load_slice()
    raise ValueError(spec)


def golden_t0() -> int:
    return read_jsonl(GOLDEN_DIR / "events_eq_mbo.jsonl")[0].exchange_ts


def golden_config(**kw) -> ExecConfig:
    return ExecConfig(
        seed=20260829,
        impact_coeff_bps_per_pct_adv=2.0,
        venues=golden_venues(),
        instruments={1: InstrumentSpec(1, 0.01, 1.0, 38_000_000.0)},
        **kw,
    )


def slice_config(**kw) -> ExecConfig:
    return ExecConfig(
        seed=20260829,
        impact_coeff_bps_per_pct_adv=2.0,
        venues=slice_venues(),
        instruments={
            SLICE_INSTRUMENT: InstrumentSpec(SLICE_INSTRUMENT, SLICE_TICK, 1.0, SLICE_ADV)
        },
        **kw,
    )


def scenario(
    label: str, cfg: ExecConfig, events: str, parents: list[ParentOrder], *, digest: bool = False
) -> dict:
    """``digest``: the fills and children arrays are recorded as the SHA-256 of
    their canonical JSON (the real-slice scenarios: thousands of rows), the
    parent reports and counters in full."""
    return {
        "label": label,
        "config": config_row(cfg),
        "events": events,
        "parents": [parent_row(p) for p in parents],
        "digest": digest,
    }


def _fill_row(f) -> list:
    d = f.to_dict()
    return [
        d["fill_id"],
        d["order_id"],
        d["parent_id"],
        d["instrument_id"],
        d["venue_id"],
        d["side"],
        d["price_ticks"],
        d["qty"],
        d["ts"],
        d["liquidity"],
        d["fee"],
        d["impact_cost"],
    ]


def _report_row(r) -> dict:
    return {
        "filled_qty": r.filled_qty,
        "unfilled_qty": r.unfilled_qty,
        "children": r.children,
        "notional": r.notional,
        "avg_price": r.avg_price,
        "fees": r.fees,
        "rebates": r.rebates,
        "impact": r.impact,
        "total_cost": r.total_cost,
    }


def _child_row(o) -> list:
    return [
        o.order_id,
        o.parent_id,
        o.venue_id,
        o.side,
        int(o.type),
        o.limit_ticks,
        o.qty,
        o.decision_ts,
        o.expire_ts,
        int(o.state),
        o.remaining,
        int(o.cancel_reason),
    ]


def _stats_row(s) -> dict:
    return {
        "posts": s.posts,
        "reprices": s.reprices,
        "rest_extensions": s.rest_extensions,
        "crosses_timeout": s.crosses_timeout,
        "crosses_behind": s.crosses_behind,
        "crosses_immediate": s.crosses_immediate,
    }


def run_scenario(s: dict, *, with_passive: bool) -> dict:
    cfg = config_from_row(s["config"])
    parents = [parent_from_row(r) for r in s["parents"]]
    res = ExecutionReplay(cfg, parents).run(events_for(s["events"]))
    out = {
        "events_processed": res.events_processed,
        "sor_no_route": res.sor_no_route,
        "fills": [_fill_row(f) for f in res.fills],
        "parents": {str(k): _report_row(r) for k, r in sorted(res.parents.items())},
        "children": [_child_row(o) for pid in sorted(res.children) for o in res.children[pid]],
    }
    if with_passive:
        out["passive"] = {str(k): _stats_row(v) for k, v in sorted(res.passive.items())}
    if s["digest"]:
        for key in ("fills", "children"):
            rows = out.pop(key)
            out[key + "_count"] = len(rows)
            out[key + "_sha256"] = sha256_bytes(canonical_json(rows).encode("ascii"))
    return out


SLICE_INPUTS = dict(SLICE_PROVENANCE, window_start_ts=SLICE_T0)

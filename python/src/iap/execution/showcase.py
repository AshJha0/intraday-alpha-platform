"""v2.1 Track 2 showcase: parent algos, venue routing and one impact fit on a seeded tape.

Everything here is a demonstration on SYNTHETIC data and carries no evidence
class.  It reuses the real simulator (:class:`~iap.execution.replay.ExecutionReplay`
over :class:`~iap.execution.simulator.ExecutionSimulator`), the pinned
router (:mod:`iap.execution.sor`) and the cost-aware router
(:mod:`iap.execution.sor_v2`); only the tape is new.

The tape (:func:`synthetic_tape`) is two lit equity venues with the fees of
``configs/venues/venues.json`` (XV1, XV2).  Once a second the consolidated
mid moves by a planted permanent impact on signed market flow plus noise::

    mid_{t+1} = mid_t + LAMBDA_TRUE * signed_volume_t + sigma * z_t

and each venue re-quotes one level per side around the new mid (XV1 a
2-tick spread and deeper size, XV2 a spread that is sometimes 1 tick and
sometimes 3, with less size).  Half a second later one market TRADE of
``|signed_volume_t|`` prints at the touch of a seeded venue.  The draws use
:class:`random.Random` with a fixed seed, so every table is identical run
to run.

Run ``python -m iap.execution.showcase all`` (about 2 s).
"""

from __future__ import annotations

import argparse
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace

from iap.core.events import EventType, MarketEvent
from iap.execution.algos import AlgoType, ParentOrder
from iap.execution.config import ExecConfig
from iap.execution.replay import ExecReplayResult, ExecutionReplay
from iap.execution.sor_v2 import CostAwareRouter
from iap.execution.types import InstrumentSpec, VenueSpec
from iap.execution.venues_model import VenueModel

SEED = 20261027
T0 = 1_790_000_000_000_000_000
SEC = 1_000_000_000
INSTRUMENT = 7
TICK = 0.01
START_MID_TICKS = 10_000.0  #: $100.00
#: planted permanent impact, ticks of mid per share of signed market flow
LAMBDA_TRUE = 0.0004
SIGMA_TICKS = 0.6  #: per-second mid noise, ticks
ADV = 2_000_000.0  #: shares, for the simulator's impact adder only
SESSION_S = 1800  #: tape length, seconds

#: XV1 / XV2 as in configs/venues/venues.json (latency jitter kept, seeded).
VENUES = {
    1: VenueSpec(1, "XV1", False, 0.003, 0.002, 0.0, 150_000, 50_000),
    2: VenueSpec(2, "XV2", False, 0.0028, 0.0015, 0.0, 220_000, 80_000),
}


def exec_config() -> ExecConfig:
    return ExecConfig(
        seed=SEED,
        venues=VENUES,
        instruments={INSTRUMENT: InstrumentSpec(INSTRUMENT, TICK, 1.0, ADV)},
    )


@dataclass(frozen=True)
class Tape:
    events: list[MarketEvent]
    mids: list[float]  #: consolidated mid (ticks) in force during second i
    signed_volume: list[int]  #: market flow printed in second i (+ buy, - sell)

    def mid_at(self, ts: int) -> float:
        i = min(max((ts - T0) // SEC, 0), len(self.mids) - 1)
        return self.mids[int(i)]

    def volume_between(self, start_ts: int, end_ts: int) -> int:
        a = max((start_ts - T0) // SEC, 0)
        b = min((end_ts - T0) // SEC, len(self.signed_volume))
        return sum(abs(v) for v in self.signed_volume[int(a) : int(b)])


def synthetic_tape(seed: int = SEED, seconds: int = SESSION_S) -> Tape:
    """The seeded two-venue tape (module docstring)."""
    rng = random.Random(seed)
    events: list[MarketEvent] = []
    seq = {1: 0, 2: 0}
    resting: dict[int, list[tuple[int, int, int, int]]] = {1: [], 2: []}
    next_oid = 1

    def push(
        venue: int, ts: int, etype: EventType, side: int, px: int, qty: int, oid: int, tid: int = 0
    ) -> None:
        seq[venue] += 1
        events.append(
            MarketEvent(
                event_id=len(events) + 1,
                instrument_id=INSTRUMENT,
                venue_id=venue,
                exchange_ts=ts,
                receive_ts=ts,
                sequence=seq[venue],
                event_type=int(etype),
                side=side,
                price_ticks=px,
                qty=qty,
                order_id=oid,
                trade_id=tid,
            )
        )

    mid = START_MID_TICKS
    mids: list[float] = []
    flow: list[int] = []
    touch: dict[int, tuple[int, int]] = {}
    for i in range(seconds):
        ts = T0 + i * SEC
        mids.append(mid)
        # re-quote both venues around the current mid
        for v in (1, 2):
            for side, px, qty, oid in resting[v]:
                push(v, ts + v, EventType.CANCEL, side, px, qty, oid)
            resting[v] = []
            if v == 1:
                half_lo, half_hi, size = 1, 1, 3000 + 100 * rng.randrange(0, 11)
            else:
                wide = rng.random() < 0.5
                half_lo, half_hi = (2, 2) if wide else (0, 1)
                size = 1200 + 100 * rng.randrange(0, 9)
            centre = math.floor(mid)
            bid = centre - half_lo
            ask = centre + half_hi
            if ask <= bid:
                ask = bid + 1
            for side, px in ((0, bid), (1, ask)):
                push(v, ts + 10 + v, EventType.ADD, side, px, size, next_oid)
                resting[v].append((side, px, size, next_oid))
                next_oid += 1
            touch[v] = (bid, ask)
        # one market print half a second later
        sign = 1 if rng.random() < 0.5 else -1
        qty = 100 * rng.randrange(1, 21)
        venue = 1 if rng.random() < 0.6 else 2
        px = touch[venue][1] if sign > 0 else touch[venue][0]
        push(venue, ts + SEC // 2, EventType.TRADE, 0 if sign > 0 else 1, px, qty, 0, 10_000 + i)
        flow.append(sign * qty)
        mid = mid + LAMBDA_TRUE * sign * qty + SIGMA_TICKS * rng.gauss(0.0, 1.0)
    return Tape(events=events, mids=mids, signed_volume=flow)


# ------------------------------------------------------------------ TCA --


@dataclass(frozen=True)
class TcaRow:
    label: str
    qty: int
    filled: int
    arrival_mid: float  #: ticks
    avg_px: float  #: ticks, 0 if nothing filled
    slippage_bps: float  #: cost vs arrival mid, + = worse
    impact_bps: float  #: simulator impact adder, bps of filled notional
    fees_bps: float  #: fees minus rebates, bps of filled notional
    fill_rate: float
    participation: float  #: filled / market volume in the window
    children: int


def tca_row(label: str, p: ParentOrder, res: ExecReplayResult, tape: Tape) -> TcaRow:
    fills = [f for f in res.fills if f.parent_id == p.parent_id]
    filled = sum(f.qty for f in fills)
    arrival = tape.mid_at(p.start_ts)
    s = 1.0 if p.side == 0 else -1.0
    if filled:
        avg = sum(f.price_ticks * f.qty for f in fills) / filled
        notional = avg * TICK * filled
        slip = s * (avg - arrival) / arrival * 1e4
        impact = sum(f.impact_cost for f in fills) / notional * 1e4
        fees = sum(f.fee for f in fills) / notional * 1e4
    else:
        avg = slip = impact = fees = 0.0
    mkt = tape.volume_between(p.start_ts, p.end_ts)
    return TcaRow(
        label=label,
        qty=p.qty,
        filled=filled,
        arrival_mid=arrival,
        avg_px=avg,
        slippage_bps=slip,
        impact_bps=impact,
        fees_bps=fees,
        fill_rate=filled / p.qty,
        participation=filled / mkt if mkt else 0.0,
        children=res.parents[p.parent_id].children,
    )


def _parent(
    pid: int,
    algo: AlgoType,
    start_s: int,
    dur_s: int,
    qty: int,
    side: int = 0,
    venue: int = 1,
    **kw,
) -> ParentOrder:
    return ParentOrder(
        parent_id=pid,
        instrument_id=INSTRUMENT,
        venue_id=venue,
        side=side,
        qty=qty,
        algo=algo,
        start_ts=T0 + start_s * SEC + SEC // 4,
        end_ts=T0 + (start_s + dur_s) * SEC + SEC // 4,
        **{"slices": 10, "max_child_qty": 1000, **kw},
    )


# ---------------------------------------------------------- 1. algos ----

ALGO_PARENT_QTY = 6000
ALGO_START_S = 60
ALGO_DUR_S = 600


def run_algos(tape: Tape | None = None) -> list[TcaRow]:
    """One buy parent (6,000 shares, 10 minutes, XV1) worked by TWAP, VWAP and IS."""
    tape = tape or synthetic_tape()
    rows = []
    for label, algo, kw in (
        ("TWAP", AlgoType.TWAP, {}),
        ("VWAP", AlgoType.VWAP, {}),
        ("IS (risk aversion 1.0)", AlgoType.IS, {"risk_aversion": 1.0}),
        ("IS (risk aversion 3.0)", AlgoType.IS, {"risk_aversion": 3.0}),
    ):
        p = _parent(1, algo, ALGO_START_S, ALGO_DUR_S, ALGO_PARENT_QTY, **kw)
        res = ExecutionReplay(exec_config(), [p], md_delay_ns=0).run(tape.events)
        rows.append(tca_row(label, p, res, tape))
    return rows


# ------------------------------------------------------------ 2. SOR ----

SOR_PARENTS = 24
SOR_QTY = 8000
SOR_DUR_S = 60


def sor_parents(venue_id: int) -> list[ParentOrder]:
    """24 aggressive IS parents of 8,000 shares in 4 slices over 1 minute, alternating side.

    One child per slice (``max_child_qty`` = the parent), so the first child
    (about 3,100 shares) is larger than XV2's displayed touch."""
    return [
        _parent(
            k + 1,
            AlgoType.IS,
            30 + 70 * k,
            SOR_DUR_S,
            SOR_QTY,
            side=k % 2,
            venue=venue_id,
            slices=4,
            max_child_qty=SOR_QTY,
        )
        for k in range(SOR_PARENTS)
    ]


#: router-facing venue model for the cost-aware router: fill probability at
#: the touch and a toxicity charge per venue (assumed, synthetic).
VENUE_MODELS = {
    1: VenueModel(VENUES[1], p_fill_touch=0.6, toxicity_bps={"1s": 0.3}),
    2: VenueModel(VENUES[2], p_fill_touch=0.35, toxicity_bps={"1s": 0.8}),
}

#: an alternative model that believes XV2 fills far more often (0.9 vs 0.35)
#: with equal toxicity: it shows the route is set by the fill model.
XV2_FAVOURING_MODELS = {
    1: VenueModel(VENUES[1], p_fill_touch=0.35, toxicity_bps={"1s": 0.3}),
    2: VenueModel(VENUES[2], p_fill_touch=0.9, toxicity_bps={"1s": 0.3}),
}


@dataclass(frozen=True)
class SorRow:
    label: str
    parents: int
    filled: int
    target: int
    cost_bps: float  #: qty-weighted slippage vs arrival + fees + impact
    slippage_bps: float
    fees_bps: float
    share_xv2: float  #: filled share on XV2


def _sor_summary(
    label: str, parents: Sequence[ParentOrder], res: ExecReplayResult, tape: Tape
) -> SorRow:
    rows = [tca_row(label, p, res, tape) for p in parents]
    filled = sum(r.filled for r in rows)
    w = [r.filled / filled for r in rows] if filled else [0.0] * len(rows)
    slip = sum(wi * r.slippage_bps for wi, r in zip(w, rows, strict=True))
    fees = sum(wi * r.fees_bps for wi, r in zip(w, rows, strict=True))
    imp = sum(wi * r.impact_bps for wi, r in zip(w, rows, strict=True))
    xv2 = sum(f.qty for f in res.fills if f.venue_id == 2)
    return SorRow(
        label=label,
        parents=len(parents),
        filled=filled,
        target=sum(p.qty for p in parents),
        cost_bps=slip + fees + imp,
        slippage_bps=slip,
        fees_bps=fees,
        share_xv2=xv2 / filled if filled else 0.0,
    )


def run_sor(tape: Tape | None = None) -> list[SorRow]:
    """The same 24 parents under each router: aggressive IS, then passive TWAP (2,000 shares)."""
    tape = tape or synthetic_tape()
    out = []
    cfg = exec_config()
    naive = sor_parents(1)
    out.append(
        _sor_summary(
            "naive: all on XV1",
            naive,
            ExecutionReplay(cfg, naive, md_delay_ns=0).run(tape.events),
            tape,
        )
    )
    routed = sor_parents(0)
    out.append(
        _sor_summary(
            "pinned SOR (best price, fee tie-break)",
            routed,
            ExecutionReplay(cfg, routed, md_delay_ns=0).run(tape.events),
            tape,
        )
    )
    router = CostAwareRouter(VENUE_MODELS)
    res = ExecutionReplay(cfg, routed, router=router, md_delay_ns=0).run(tape.events)
    out.append(_sor_summary("cost-aware SOR (all-in cost)", routed, res, tape))
    router = CostAwareRouter(VENUE_MODELS)
    res = ExecutionReplay(cfg, routed, router=router, router_sweep=True, md_delay_ns=0).run(
        tape.events
    )
    out.append(_sor_summary("cost-aware sweep (both venues' depth)", routed, res, tape))
    # passive: TWAP children join the touch; here the fill probability matters
    for label, venue, models in (
        ("passive naive: all on XV1", 1, None),
        ("passive pinned SOR (best rebate)", 0, None),
        ("passive cost-aware SOR (fill prob, toxicity)", 0, VENUE_MODELS),
        ("passive cost-aware SOR, XV2-favouring fill model", 0, XV2_FAVOURING_MODELS),
    ):
        ps = [replace(p, algo=AlgoType.TWAP, qty=2000, venue_id=venue) for p in sor_parents(0)]
        kw = {"router": CostAwareRouter(models)} if models else {}
        res = ExecutionReplay(cfg, ps, md_delay_ns=0, **kw).run(tape.events)
        out.append(_sor_summary(label, ps, res, tape))
    return out


# ------------------------------------------------------ 3. calibration ----


@dataclass(frozen=True)
class ImpactFit:
    lambda_hat: float  #: ticks per share
    se: float
    lambda_true: float
    n: int
    r2: float
    bps_per_pct_adv: float  #: lambda_hat expressed as bps of mid per 1 % of ADV


def fit_impact(tape: Tape | None = None) -> ImpactFit:
    """OLS through the origin of the one-second mid change on signed flow."""
    tape = tape or synthetic_tape()
    x = [float(v) for v in tape.signed_volume[:-1]]
    y = [tape.mids[i + 1] - tape.mids[i] for i in range(len(tape.mids) - 1)]
    sxx = sum(a * a for a in x)
    lam = sum(a * b for a, b in zip(x, y, strict=True)) / sxx
    resid = [b - lam * a for a, b in zip(x, y, strict=True)]
    n = len(x)
    s2 = sum(r * r for r in resid) / (n - 1)
    se = math.sqrt(s2 / sxx)
    syy = sum(b * b for b in y)
    r2 = 1.0 - sum(r * r for r in resid) / syy
    bps = lam * (ADV / 100.0) / START_MID_TICKS * 1e4
    return ImpactFit(lam, se, LAMBDA_TRUE, n, r2, bps)


# ---------------------------------------------------------------- CLI ----


def _algo_table(rows: Sequence[TcaRow]) -> str:
    lines = [
        "| algo | filled / qty | children | avg px ($) | slippage vs arrival (bps) | impact adder (bps) | fees net (bps) | fill rate | participation |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.label} | {r.filled} / {r.qty} | {r.children} | {r.avg_px * TICK:.4f} | {r.slippage_bps:.2f} "
            f"| {r.impact_bps:.2f} | {r.fees_bps:.2f} | {r.fill_rate:.1%} | {r.participation:.1%} |"
        )
    return "\n".join(lines)


def _sor_table(rows: Sequence[SorRow]) -> str:
    lines = [
        "| router | filled / target | slippage (bps) | fees (bps) | all-in cost (bps) | share on XV2 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.label} | {r.filled} / {r.target} | {r.slippage_bps:.2f} | {r.fees_bps:.2f} | {r.cost_bps:.2f} | {r.share_xv2:.1%} |"
        )
    return "\n".join(lines)


def _fit_text(f: ImpactFit) -> str:
    return (
        f"lambda_hat = {f.lambda_hat:.6f} ticks/share (SE {f.se:.6f}); planted {f.lambda_true:.6f}; "
        f"n = {f.n} one-second intervals; R^2 = {f.r2:.3f}; "
        f"= {f.bps_per_pct_adv:.2f} bps of mid per 1 % of ADV"
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m iap.execution.showcase", description=__doc__.splitlines()[0]
    )
    ap.add_argument("what", choices=("algos", "sor", "impact", "all"), nargs="?", default="all")
    args = ap.parse_args(argv)
    tape = synthetic_tape()
    print("SYNTHETIC tape, seed", SEED, "- no evidence class.\n")
    if args.what in ("algos", "all"):
        print(f"Parent: buy {ALGO_PARENT_QTY} shares on XV1 over {ALGO_DUR_S} s, 10 slices\n")
        print(_algo_table(run_algos(tape)), "\n")
    if args.what in ("sor", "all"):
        print(f"{SOR_PARENTS} IS parents x {SOR_QTY} shares, {SOR_DUR_S} s each\n")
        print(_sor_table(run_sor(tape)), "\n")
    if args.what in ("impact", "all"):
        print(_fit_text(fit_impact(tape)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ImpactFit",
    "SorRow",
    "Tape",
    "TcaRow",
    "exec_config",
    "fit_impact",
    "main",
    "run_algos",
    "run_sor",
    "synthetic_tape",
]

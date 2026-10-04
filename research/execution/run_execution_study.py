#!/usr/bin/env python3
"""Execution-quality study: the same parent orders worked aggressively and
passively on the bundled dataset, and the MVP session re-run under each
child execution policy.

    python3 research/execution/run_execution_study.py

writes ``research/execution/EXECUTION_REPORT.md`` and
``research/execution/execution_study.json`` (step ``execution`` of
``tools/regenerate_dataset_artifacts.py``; the committed copies are produced
by the CI ``regenerate`` job).

Part A — bundled equities (``data/normalized/eq_*.normalized.jsonl``, every
instrument, every session). FX is left out: its feed is quote-driven, and
the simulator's queue model (rule 4) fills resting orders from EXECUTE
events and marketable ADDs, which a quote feed does not carry.

- Parent orders: per (session, instrument) a back-to-back sequence of
  10-minute windows from five minutes after the first event; side and size
  (300..1500 shares, multiples of 100) from one SplitMix64 stream, algos
  cycling TWAP / VWAP / POV / IS, 5 slices, POV participation 10 %,
  SOR-routed over XV1 / XV2. Every policy works exactly these parents.
- Policies: ``aggressive`` (every child MARKET), ``native`` (the platform
  default: TWAP / VWAP join the touch and wait, POV / IS cross) and
  ``passive`` (POST -> REST -> REPRICE / CROSS, ``PassiveParams`` defaults)
  at urgency 0.2 / 0.5 / 0.8.
- Costs per parent, in the quote currency and in bps of ``qty * arrival
  mid``; positive = worse than the arrival mid:
  ``shortfall`` = Perold implementation shortfall (``iap.tca.order_tca``,
  decision = arrival = the window start) = trading cost of what filled +
  ``opportunity``, the UNFILLED quantity priced at the arrival-to-end mid
  move — unfilled passive quantity is not free;
  ``spread`` = half-spread paid (+) or earned (-) at each fill;
  ``fees_net`` = taker fees - maker rebates; ``sim_impact`` = the
  simulator's linear impact charge (rule 6, taker fills only);
  ``completion`` = what finishing the unfilled quantity at the end of the
  window would cost on top of the mid move: the half-spread at the window
  end plus the venue-1 taker fee, per unfilled share;
  ``all_in = shortfall + fees_net + sim_impact + completion``.
- Markouts per policy and liquidity flag over the default horizons
  (``iap.tca.markout``; null when undefined), pooled over instruments.
- Passive orders: fill rate, time to fill and the fill rate by queue
  position at entry.

Part B — the MVP session (``configs/mvp/mvp.json``) run three times on the
same captured feed: the pinned NATIVE configuration, ``aggressive`` and
``passive`` (``execution.child_policy`` override; the passive parameters
are scaled to the MVP's one-second parent window). The pinned golden run is
not touched: the overrides produce separate run ids.

Deterministic: same dataset + same code => same JSON bytes.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

from iap.core.codec import read_jsonl  # noqa: E402
from iap.core.rng import SplitMix64  # noqa: E402
from iap.execution import (  # noqa: E402
    AlgoType,
    ExecPolicy,
    ExecutionReplay,
    Liquidity,
    OrderType,
    ParentOrder,
    PassiveParams,
    load_exec_config,
    load_sor_options,
)
from iap.tca.fills import MAKER, TAKER, stamp_fill  # noqa: E402
from iap.tca.fills import ParentOrder as TcaParent  # noqa: E402
from iap.tca.markout import (  # noqa: E402
    DEFAULT_HORIZONS_NS,
    MarkoutFill,
    PassiveOrder,
    build_gated_timeline,
    fill_measures,
    passive_order_stats,
)
from iap.tca.tca import order_tca  # noqa: E402

OUT_DIR = REPO / "research" / "execution"
SEC = 1_000_000_000
SEED = 20260829
WINDOW_NS = 600 * SEC
WARMUP_NS = 300 * SEC
SLICES = 5
POV_PARTICIPATION = 0.10
ALGOS = (AlgoType.TWAP, AlgoType.VWAP, AlgoType.POV, AlgoType.IS)
MIN_FILLS = 30  #: a markout cell with fewer defined fills reports null

#: name -> (policy, urgency)
POLICIES: dict[str, tuple[ExecPolicy, float]] = {
    "aggressive": (ExecPolicy.AGGRESSIVE, 1.0),
    "native": (ExecPolicy.NATIVE, 0.5),
    "passive_u0.2": (ExecPolicy.PASSIVE, 0.2),
    "passive_u0.5": (ExecPolicy.PASSIVE, 0.5),
    "passive_u0.8": (ExecPolicy.PASSIVE, 0.8),
}
HEADLINE = "passive_u0.5"

#: MVP passive parameters: the parent window is one second, so the rest
#: time is 400 ms at urgency 0 and nothing rests in the last 100 ms.
MVP_PASSIVE = {
    "max_rest_ns": 400_000_000,
    "max_reprices": 1,
    "max_behind_fraction": 0.1,
    "improve_min_spread_ticks": 3,
    "end_margin_ns": 100_000_000,
}

COST_KEYS = (
    "shortfall",
    "trading",
    "opportunity",
    "spread",
    "price_vs_mid_residual",
    "timing",
    "fees_net",
    "sim_impact",
    "completion",
    "all_in",
)


def make_parents(events, instrument_id: int, rng: SplitMix64) -> list[ParentOrder]:
    """The pinned parent set of one (session, instrument) stream."""
    first, last = events[0].exchange_ts, events[-1].exchange_ts
    out: list[ParentOrder] = []
    start = first + WARMUP_NS
    k = 0
    while start + WINDOW_NS <= last - WARMUP_NS:
        side = rng.randint(0, 1)
        qty = 100 * rng.randint(3, 15)
        out.append(
            ParentOrder(
                parent_id=k + 1,
                instrument_id=instrument_id,
                venue_id=0,
                side=side,
                qty=qty,
                algo=ALGOS[k % len(ALGOS)],
                start_ts=start,
                end_ts=start + WINDOW_NS,
                slices=SLICES,
                participation=POV_PARTICIPATION,
                risk_aversion=1.0,
            )
        )
        start += WINDOW_NS
        k += 1
    return out


def _mean_se(values: list[float]) -> dict[str, float | int | None]:
    n = len(values)
    if n < 2:
        return {"n": n, "mean": None, "se": None, "t": None}
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    se = math.sqrt(var / n)
    return {"n": n, "mean": mean, "se": se, "t": mean / se if se > 0 else None}


def parent_costs(parent, fills, timeline, tick, taker_fee) -> dict[str, float]:
    """Cost decomposition of one parent (module docstring), currency."""
    order = TcaParent(
        order_id=parent.parent_id,
        instrument_id=parent.instrument_id,
        side=parent.side,
        qty_target=parent.qty,
        decision_ts=parent.start_ts,
        arrival_ts=parent.start_ts,
        end_ts=parent.end_ts,
    )
    fees_net = sim_impact = 0.0
    maker_qty = 0
    for f in fills:
        liq = MAKER if f.liquidity == Liquidity.MAKER else TAKER
        order.fills.append(stamp_fill(timeline, f.ts, f.price_ticks * tick, f.qty, f.side, liq))
        fees_net += f.fee
        sim_impact += f.impact_cost
        if f.liquidity == Liquidity.MAKER:
            maker_qty += f.qty
    rec = order_tca(order, timeline)
    perold = rec["perold"]
    unfilled = parent.qty - order.qty_filled
    i_end = timeline.prevailing(parent.end_ts)
    completion = unfilled * (timeline.half_spread(i_end) + taker_fee)
    shortfall = float(perold["total_is"])
    return {
        "notional": parent.qty * float(rec["decision_mid"]),
        "qty": parent.qty,
        "filled_qty": order.qty_filled,
        "maker_qty": maker_qty,
        "shortfall": shortfall,
        "trading": float(perold["trading_cost"]),
        "opportunity": float(perold["opportunity_cost"]),
        "spread": float(rec["spread_cost"]),
        "price_vs_mid_residual": float(rec["impact_cost"]),
        "timing": float(rec["timing_cost"]),
        "fees_net": fees_net,
        "sim_impact": sim_impact,
        "completion": completion,
        "all_in": shortfall + fees_net + sim_impact + completion,
    }


def summarise(rows: list[dict[str, float]]) -> dict[str, object]:
    """Notional-weighted bps of each cost over a set of parents, with the
    per-parent mean and its standard error for the all-in cost."""
    notional = sum(r["notional"] for r in rows)
    qty = sum(r["qty"] for r in rows)
    filled = sum(r["filled_qty"] for r in rows)
    out: dict[str, object] = {
        "n_parents": len(rows),
        "notional": notional,
        "qty": qty,
        "filled_qty": filled,
        "fill_rate": filled / qty if qty else None,
        "maker_share_of_filled": sum(r["maker_qty"] for r in rows) / filled if filled else None,
    }
    for key in COST_KEYS:
        total = sum(r[key] for r in rows)
        out[f"{key}_ccy"] = total
        out[f"{key}_bps"] = 1e4 * total / notional if notional else None
    out["all_in_bps_per_parent"] = _mean_se([1e4 * r["all_in"] / r["notional"] for r in rows])
    return out


def paired(a: list[dict[str, float]], b: list[dict[str, float]]) -> dict[str, object]:
    """Per-parent difference ``b - a`` in bps (same parents, same order)."""
    out: dict[str, object] = {}
    for key in ("all_in", "shortfall", "spread", "fees_net", "opportunity"):
        out[f"{key}_bps"] = _mean_se(
            [
                1e4 * (y[key] / y["notional"] - x[key] / x["notional"])
                for x, y in zip(a, b, strict=True)
            ]
        )
    return out


def markout_cells(samples: dict[str, dict[str, list[float]]]) -> dict[str, object]:
    """{horizon: {n, markout_bps, se, price_impact_bps, se, effective_half_spread_bps}}."""
    out: dict[str, object] = {}
    for name in DEFAULT_HORIZONS_NS:
        cell = samples[name]
        n = len(cell["markout"])
        row: dict[str, object] = {"n": n}
        for m in ("markout", "effective_half_spread", "realised_half_spread", "price_impact"):
            stat = _mean_se(cell[m]) if n >= MIN_FILLS else {"mean": None, "se": None}
            row[f"{m}_bps"] = stat["mean"]
            row[f"{m}_se_bps"] = stat["se"]
        out[name] = row
    return out


def _new_samples() -> dict[str, dict[str, list[float]]]:
    return {
        name: {
            m: []
            for m in ("markout", "effective_half_spread", "realised_half_spread", "price_impact")
        }
        for name in DEFAULT_HORIZONS_NS
    }


def dataset_study(data_dir: Path) -> dict[str, object]:
    base_cfg = load_exec_config(REPO / "configs")
    sor = load_sor_options(REPO / "configs" / "execution" / "execution.json")
    taker_fee = base_cfg.venue(1).taker_fee_per_share
    files = sorted(data_dir.glob("eq_*.normalized.jsonl"))
    if not files:
        raise SystemExit(
            f"no eq_*.normalized.jsonl under {data_dir}: run `python -m iap.marketdata`"
        )
    rng = SplitMix64(SEED)
    costs: dict[str, list[dict[str, float]]] = {p: [] for p in POLICIES}
    by_algo: dict[str, dict[str, list[dict[str, float]]]] = {
        p: {a.name: [] for a in ALGOS} for p in POLICIES
    }
    marks: dict[str, dict[str, dict]] = {
        p: {MAKER: _new_samples(), TAKER: _new_samples()} for p in POLICIES
    }
    passive_orders: dict[str, list[PassiveOrder]] = {p: [] for p in POLICIES}
    transitions: dict[str, dict[str, int]] = {}
    n_events = 0
    sessions = []
    for path in files:
        events = read_jsonl(path)
        n_events += len(events)
        sessions.append(path.name)
        per_instrument: dict[int, list] = {}
        for ev in events:
            per_instrument.setdefault(ev.instrument_id, []).append(ev)
        for iid in sorted(per_instrument):
            evs = per_instrument[iid]
            tick = base_cfg.instrument(iid).tick_size
            timeline = build_gated_timeline(evs, iid, tick)
            parents = make_parents(evs, iid, rng)
            for name, (policy, urgency) in POLICIES.items():
                worked = [replace(p, policy=policy, urgency=urgency) for p in parents]
                res = ExecutionReplay(base_cfg, worked, sor).run(evs)
                fills_of: dict[int, list] = {p.parent_id: [] for p in worked}
                for f in res.fills:
                    fills_of[f.parent_id].append(f)
                for p in worked:
                    row = parent_costs(p, fills_of[p.parent_id], timeline, tick, taker_fee)
                    costs[name].append(row)
                    by_algo[name][p.algo.name].append(row)
                for f in res.fills:
                    liq = MAKER if f.liquidity == Liquidity.MAKER else TAKER
                    mf = MarkoutFill(f.ts, f.price_ticks * tick, f.qty, f.side, liq, f.venue_id)
                    for hname, h in DEFAULT_HORIZONS_NS.items():
                        x = fill_measures(mf, timeline, h)
                        if x is None:
                            continue
                        for m, v in x.items():
                            marks[name][liq][hname][m].append(1e4 * v / mf.price)
                order_fills: dict[int, list] = {}
                for f in res.fills:
                    order_fills.setdefault(f.order_id, []).append(f)
                for kids in res.children.values():
                    for o in kids:
                        if o.type != OrderType.LIMIT:
                            continue
                        fl = order_fills.get(o.order_id, [])
                        passive_orders[name].append(
                            PassiveOrder(
                                qty=o.qty,
                                filled_qty=o.qty - o.remaining,
                                rest_ts=o.arrival_ts,
                                first_fill_ts=fl[0].ts if fl else None,
                                last_fill_ts=fl[-1].ts if fl else None,
                                entry_ahead_qty=o.entry_ahead_qty,
                            )
                        )
                if res.passive:
                    tot = transitions.setdefault(name, {})
                    for s in res.passive.values():
                        for key, value in s.to_dict().items():
                            tot[key] = tot.get(key, 0) + value
    policies: dict[str, object] = {}
    for name in POLICIES:
        policies[name] = {
            "costs": summarise(costs[name]),
            "by_algo": {a: summarise(rows) for a, rows in by_algo[name].items()},
            "markouts": {
                "passive_fills": markout_cells(marks[name][MAKER]),
                "aggressive_fills": markout_cells(marks[name][TAKER]),
            },
            "passive_orders": passive_order_stats(passive_orders[name], min_orders=MIN_FILLS)
            if passive_orders[name]
            else None,
            "transitions": transitions.get(name),
        }
    return {
        "sessions": sessions,
        "n_events": n_events,
        "n_parents": len(costs["aggressive"]),
        "window_ns": WINDOW_NS,
        "slices": SLICES,
        "pov_participation": POV_PARTICIPATION,
        "seed": SEED,
        "min_fills": MIN_FILLS,
        "passive_params": {
            k: getattr(PassiveParams(), k) for k in PassiveParams.__dataclass_fields__
        },
        "policies": policies,
        "paired_vs_aggressive": {
            name: paired(costs["aggressive"], costs[name])
            for name in POLICIES
            if name != "aggressive"
        },
    }


def mvp_study(scratch: Path) -> dict[str, object]:
    from iap.mvp.config import load_config
    from iap.mvp.feed import generate_feed
    from iap.mvp.session import run_session

    cfg = load_config(REPO / "configs" / "mvp" / "mvp.json", repo_root=REPO)
    if scratch.exists():
        shutil.rmtree(scratch)
    feed = generate_feed(cfg, scratch / "feed")
    runs = {
        "native": cfg,
        "aggressive": cfg.with_overrides(child_policy="aggressive"),
        "passive": cfg.with_overrides(child_policy="passive", passive=MVP_PASSIVE),
    }
    out: dict[str, object] = {"passive_params": MVP_PASSIVE, "runs": {}}
    for name, run_cfg in runs.items():
        res = run_session(run_cfg, feed, scratch / name)
        pnl = res.report["pnl"]
        ex = res.report["execution"]
        eng = res.engine
        maker = sum(f.qty for f in eng.sim.simulator.fills if f.liquidity == Liquidity.MAKER)
        out["runs"][name] = {  # type: ignore[index]
            "run_id": run_cfg.run_id,
            "pnl_total": pnl["total"],
            "gross": pnl["gross"],
            "spread_cost": pnl["spread_cost"],
            "fees": pnl["fees"],
            "rebates": pnl["rebates"],
            "fees_net": pnl["fees_net"],
            "impact": pnl["impact"],
            "execution_cost": pnl["execution_cost"],
            "traded_qty": pnl["traded_qty"],
            "maker_qty": maker,
            "n_parent_orders": ex["n_parent_orders"],
            "fills": eng.counters.fills,
            "child_orders_submitted": eng.counters.child_orders_submitted,
            "user_cancels": eng.sim.simulator.counters.user_cancels,
            "expired_orders": eng.sim.simulator.counters.expired_orders,
            "transitions": eng.passive_stats.to_dict() if name == "passive" else None,
        }
    shutil.rmtree(scratch)
    return out


# ------------------------------------------------------------------ report


def _f(v, digits: int = 2) -> str:
    if v is None:
        return "null"
    return f"{v:,.{digits}f}"


def render(doc: dict) -> str:
    ds = doc["dataset"]
    pol = ds["policies"]
    names = list(pol)
    out: list[str] = []
    out += [
        "# Execution quality: aggressive vs passive execution of the same parent orders",
        "",
        "Generated by `research/execution/run_execution_study.py` (step `execution` of "
        "`tools/regenerate_dataset_artifacts.py`); numbers in `execution_study.json`. "
        "Definitions: API_PORTFOLIO_TCA.md §2.9 (markouts), API_TRADING.md §2 (policies).",
        "",
        "## 1. Setup",
        "",
        f"- Data: {', '.join('`' + s + '`' for s in ds['sessions'])} — {ds['n_events']:,} events, "
        "11 synthetic equities on two venues (seeded generator). FX is excluded: a "
        "quote-driven feed carries no trades for the queue model to fill resting orders from.",
        f"- Parents: {ds['n_parents']:,} per policy — back-to-back {ds['window_ns'] // SEC // 60}-minute "
        f"windows, side and size (300–1,500 shares) from SplitMix64({ds['seed']}), algos cycling "
        f"TWAP / VWAP / POV / IS, {ds['slices']} slices, POV participation "
        f"{ds['pov_participation']:.0%}, SOR-routed. Every policy works the same parents.",
        "- Policies: `aggressive` = every child MARKET; `native` = the platform default "
        "(TWAP / VWAP join the touch and wait, POV / IS cross); `passive_uX` = POST → REST → "
        "REPRICE / CROSS at urgency X with the default `PassiveParams` "
        f"(`{json.dumps(ds['passive_params'])}`).",
        "- Costs are in bps of `qty × arrival mid`, summed over parents (notional-weighted); "
        "positive = worse than the arrival mid. `opportunity` prices the UNFILLED quantity at "
        "the arrival-to-end mid move; `completion` adds what crossing it at the end of the "
        "window would cost (end half-spread + taker fee). "
        "`all-in = shortfall + fees_net + sim_impact + completion`.",
        "",
        "## 2. Cost decomposition (bps of arrival notional)",
        "",
        "| policy | fill rate | maker share | shortfall | of which opportunity | spread paid (+) / earned (−) | fees net | sim impact | completion | **all-in** |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for n in names:
        c = pol[n]["costs"]
        out.append(
            f"| `{n}` | {_f(100 * c['fill_rate'], 1)}% | {_f(100 * c['maker_share_of_filled'], 1)}% "
            f"| {_f(c['shortfall_bps'])} | {_f(c['opportunity_bps'])} | {_f(c['spread_bps'])} "
            f"| {_f(c['fees_net_bps'])} | {_f(c['sim_impact_bps'], 4)} | {_f(c['completion_bps'])} "
            f"| **{_f(c['all_in_bps'])}** |"
        )
    out += [
        "",
        "Paired difference per parent against `aggressive` (same parent, policy − aggressive; "
        "mean bps ± standard error over parents, t = mean / se; negative = cheaper):",
        "",
        "| policy | all-in | t | shortfall | spread | fees net | opportunity |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for n, d in ds["paired_vs_aggressive"].items():
        out.append(
            f"| `{n}` | {_f(d['all_in_bps']['mean'])} ± {_f(d['all_in_bps']['se'])} "
            f"| {_f(d['all_in_bps']['t'], 1)} | {_f(d['shortfall_bps']['mean'])} "
            f"| {_f(d['spread_bps']['mean'])} | {_f(d['fees_net_bps']['mean'])} "
            f"| {_f(d['opportunity_bps']['mean'])} |"
        )
    out += ["", f"All-in cost by algo (bps), `aggressive` / `native` / `{HEADLINE}`:", ""]
    out += ["| algo | aggressive | native | " + HEADLINE + " |", "|---|---:|---:|---:|"]
    for a in (x.name for x in ALGOS):
        out.append(
            f"| {a} | {_f(pol['aggressive']['by_algo'][a]['all_in_bps'])} "
            f"| {_f(pol['native']['by_algo'][a]['all_in_bps'])} "
            f"| {_f(pol[HEADLINE]['by_algo'][a]['all_in_bps'])} |"
        )
    out += [
        "",
        "## 3. Markout curves (bps of fill price, mean ± se, n)",
        "",
        "`markout = s·(mid(t+h) − p)`: positive = the fill looks good `h` later. A passive "
        "fill starts at +half-spread and loses what adverse selection takes; an aggressive "
        "fill starts at −half-spread. `null` = fewer than "
        f"{ds['min_fills']} defined fills (or none: windows that run into a halt, a quote gap "
        "or the end of the session are undefined, never zero).",
        "",
        "| policy | fills | " + " | ".join(DEFAULT_HORIZONS_NS) + " |",
        "|---|---|" + "---:|" * len(DEFAULT_HORIZONS_NS),
    ]
    for n in ("aggressive", "native", HEADLINE):
        for label, key in (
            ("passive (MAKER)", "passive_fills"),
            ("aggressive (TAKER)", "aggressive_fills"),
        ):
            cells = pol[n]["markouts"][key]
            if all(cells[h]["n"] == 0 for h in cells):
                continue
            row = " | ".join(
                f"null (n={cells[h]['n']})"
                if cells[h]["markout_bps"] is None
                else f"{_f(cells[h]['markout_bps'])} ± {_f(cells[h]['markout_se_bps'])} ({cells[h]['n']})"
                for h in DEFAULT_HORIZONS_NS
            )
            out.append(f"| `{n}` | {label} | {row} |")
    mk = pol[HEADLINE]["markouts"]["passive_fills"]
    out += [
        "",
        f"Adverse selection on the passive fills of `{HEADLINE}` "
        "(`effective half-spread = realised half-spread + price impact`; a maker's effective "
        "half-spread is negative = earned, its price impact negative = the mid moved against it):",
        "",
        "| horizon | n | half-spread earned | adverse selection | kept (= markout) |",
        "|---|---:|---:|---:|---:|",
    ]
    for h in DEFAULT_HORIZONS_NS:
        c = mk[h]
        if c["markout_bps"] is None:
            out.append(f"| {h} | {c['n']} | null | null | null |")
        else:
            out.append(
                f"| {h} | {c['n']} | {_f(-c['effective_half_spread_bps'])} "
                f"| {_f(-c['price_impact_bps'])} ± {_f(c['price_impact_se_bps'])} "
                f"| {_f(c['markout_bps'])} |"
            )
    po = pol[HEADLINE]["passive_orders"]
    out += [
        "",
        f"## 4. Passive orders of `{HEADLINE}`: fill rate, time to fill, queue position",
        "",
        "| queue ahead at entry (shares) | orders | fill rate (qty) | orders with a fill | fully filled | mean time to first fill (s) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    rows = [("all", po["all"])] + list(po["by_queue_ahead_at_entry"].items())
    for label, c in rows:
        ttf = c["time_to_first_fill_ns"]["mean"]
        out.append(
            f"| {label} | {c['n_orders']} | "
            + (
                "null | null | null"
                if c["fill_rate_qty"] is None
                else f"{_f(100 * c['fill_rate_qty'], 1)}% | {_f(100 * c['fill_rate_orders'], 1)}% "
                f"| {_f(100 * c['full_fill_rate'], 1)}%"
            )
            + f" | {_f(None if ttf is None else ttf / 1e9, 1)} |"
        )
    tr = pol[HEADLINE]["transitions"]
    out += [
        "",
        f"State-machine transitions of `{HEADLINE}`: "
        + ", ".join(f"{k} {v:,}" for k, v in tr.items())
        + ".",
        "",
        "## 5. The MVP session under each child policy",
        "",
        "`python -m iap.mvp` on the pinned configuration (`native`, the golden run) and with "
        "`execution.child_policy` overridden; same captured feed, separate run ids. Passive "
        f"parameters for the one-second parent window: `{json.dumps(doc['mvp']['passive_params'])}`.",
        "",
        "| run | P&L total | gross | spread cost | fees net | impact | execution cost | traded qty | maker qty | parents | fills |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for n, r in doc["mvp"]["runs"].items():
        out.append(
            f"| `{n}` | {_f(r['pnl_total'])} | {_f(r['gross'])} | {_f(r['spread_cost'])} "
            f"| {_f(r['fees_net'])} | {_f(r['impact'], 4)} | {_f(r['execution_cost'])} "
            f"| {r['traded_qty']:,} | {r['maker_qty']:,} | {r['n_parent_orders']} | {r['fills']} |"
        )
    mv = doc["mvp"]["runs"]
    out += [
        "",
        "USD. The three runs are closed loops: different fills give different positions and "
        "therefore different later parents, so the rows are three sessions, not one session "
        "re-priced. `execution cost = spread cost + fees net + impact`; "
        f"per traded share it is {_f(mv['native']['execution_cost'] / max(mv['native']['traded_qty'], 1), 4)} "
        f"(native), {_f(mv['aggressive']['execution_cost'] / max(mv['aggressive']['traded_qty'], 1), 4)} "
        f"(aggressive) and {_f(mv['passive']['execution_cost'] / max(mv['passive']['traded_qty'], 1), 4)} "
        "(passive).",
        "",
    ]
    out += [doc["verdict_md"], ""]
    return "\n".join(out)


def verdict(doc: dict) -> str:
    """The honest reading, written from the numbers."""
    pol = doc["dataset"]["policies"]
    agg = pol["aggressive"]["costs"]
    pas = pol[HEADLINE]["costs"]
    d = doc["dataset"]["paired_vs_aggressive"][HEADLINE]["all_in_bps"]
    cheaper = d["mean"] < 0
    significant = d["t"] is not None and abs(d["t"]) >= 2.0
    mv = doc["mvp"]["runs"]
    lines = ["## 6. Verdict and what is a simulator assumption", ""]
    lines.append(
        f"**On this simulator, `{HEADLINE}` costs {_f(pas['all_in_bps'])} bps all-in against "
        f"{_f(agg['all_in_bps'])} bps for `aggressive`** — a paired difference of "
        f"{_f(d['mean'])} ± {_f(d['se'])} bps per parent (t = {_f(d['t'], 1)}, "
        f"n = {d['n']}): passive execution is "
        + ("cheaper" if cheaper else "NOT cheaper")
        + (
            ", and the difference is larger than its noise."
            if significant
            else ", but the difference is within its noise."
        )
    )
    lines.append("")
    lines.append(
        f"Where it comes from: the spread line moves from {_f(agg['spread_bps'])} bps paid to "
        f"{_f(pas['spread_bps'])} bps, fees net from {_f(agg['fees_net_bps'])} to "
        f"{_f(pas['fees_net_bps'])} bps (maker rebates on {_f(100 * pas['maker_share_of_filled'], 1)}% "
        f"of the filled quantity), while opportunity + completion for the unfilled quantity is "
        f"{_f(pas['opportunity_bps'] + pas['completion_bps'])} bps "
        f"(fill rate {_f(100 * pas['fill_rate'], 1)}% against {_f(100 * agg['fill_rate'], 1)}%)."
    )
    lines.append("")
    lines.append(
        f"MVP session: P&L {_f(mv['native']['pnl_total'])} USD (native, the pinned run), "
        f"{_f(mv['aggressive']['pnl_total'])} (aggressive), {_f(mv['passive']['pnl_total'])} "
        f"(passive); execution cost {_f(mv['native']['execution_cost'])} / "
        f"{_f(mv['aggressive']['execution_cost'])} / {_f(mv['passive']['execution_cost'])} USD. "
        + (
            "The session still loses money under every policy: the alpha does not cover even the cheapest execution."
            if max(mv[k]["pnl_total"] for k in mv) < 0
            else "At least one policy ends the session above zero; one seeded 15-minute session is not evidence of a profitable strategy."
        )
    )
    lines.append("")
    lines.append(
        f"`native` loses least because it trades least ({mv['native']['traded_qty']:,} shares "
        f"against {mv['aggressive']['traded_qty']:,} and {mv['passive']['traded_qty']:,}): its "
        "TWAP children join the touch and mostly expire unfilled, and the position it never "
        "took costs nothing in a session whose alpha is worth a fraction of a basis point. "
        "Per traded share the three policies cost about the same; passive is the cheapest of "
        "the two that actually complete their orders."
    )
    lines += [
        "",
        "What part of the passive result is the simulator rather than the market:",
        "",
        "1. **Queue model optimism.** Our resting order never changes the replayed book: nobody "
        "sees it, nobody cancels in front of it, joins behind it or fades when it improves the "
        "touch. Posting one tick inside a wide spread puts us first in a queue of one, and the "
        "next aggressor that would have hit the old touch is assumed to hit us instead (rule 4 "
        "trade-through). On a real venue the improved quote changes what others do, and the "
        "flow that reaches it is more informed than the average print.",
        "2. **No hidden liquidity and no reaction.** Aggressive children walk displayed depth "
        "only and pay the pinned linear impact; passive fills carry no impact charge at all. "
        "Synthetic order flow is not conditioned on our orders, so the adverse selection "
        "measured above is the generator's, not a market's: real passive fills are selected "
        "by the counterparty, and markouts after them are typically worse than these.",
        "3. **Cancels and reprices.** A cancel travels the latency path (rule 7) and can lose "
        "the race to a fill — that part is modelled — but there are no cancel rejects, no "
        "exchange throttles, no minimum resting time and no fee for message traffic.",
        "4. **Fees.** Maker rebates and taker fees are the configured venue schedule; a real "
        "rebate tier depends on volume and is not guaranteed.",
        "5. **Sample.** Two seeded sessions of one generator. The standard errors treat "
        "parents as independent; they share a market, so the t statistics are upper bounds.",
        "",
        "What would hold on a real venue: the accounting (a maker fill earns the half-spread "
        "and the rebate; unfilled quantity must be priced), the decomposition "
        "(effective = realised + price impact) and the fact that queue position at entry "
        "drives the fill rate. What would not transfer without re-measurement: the size of the "
        "saving. The policy therefore stays opt-in (`native` remains the default) and the "
        "markout tables are the tool for checking it against real fills.",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, default=REPO / "data" / "normalized")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--skip-mvp", action="store_true", help="dataset part only (tests)")
    args = ap.parse_args()
    doc: dict[str, object] = {
        "x-version": 1,
        "description": (
            "Execution-quality study (research/execution/run_execution_study.py): the same "
            "parent orders worked under each child execution policy on the bundled equities, "
            "and the MVP session per policy. Costs in bps of qty * arrival mid, positive = "
            "worse; markouts in bps of the fill price, null = undefined or too few fills."
        ),
        "dataset": dataset_study(args.data_dir),
    }
    if args.skip_mvp:
        print(json.dumps(doc["dataset"]["policies"][HEADLINE]["costs"], indent=1))  # type: ignore[index]
        return 0
    doc["mvp"] = mvp_study(REPO / "data" / "execution_study_scratch")
    doc["verdict_md"] = verdict(doc)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with open(args.out_dir / "execution_study.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    with open(args.out_dir / "EXECUTION_REPORT.md", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(doc))
    print(f"wrote {args.out_dir / 'EXECUTION_REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

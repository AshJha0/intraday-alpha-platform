"""B8 diagnosis: do the IEX fills' bad markouts come from walking IEX's depth?

Re-runs the B8 paired benchmark (same parents, same seed, same flags as
``run_b8_real.cmd``) with a recorder on ``ExecutionReplay._issue_child``. For
every marketable child it keeps, at decision time: the routed venue, the
opposite-side displayed depth of every lit venue (10 levels), the
consolidated mid, and, under ``sor``, the router's all-in cost per venue.
After the replay each child is joined to its fills: the levels it walked,
its filled qty, and its cost vs the decision mid split into the touch part
(touch - mid) and the walk part (fill VWAP - touch).

    set PYTHONPATH=python/src
    python research/execution/b8/diagnose_depth_walk.py --symbol AAPL \\
        --out research/execution/b8/diagnosis [--policies pin_nasdaq,pin_iex,sor] \\
        [--data-root data/real]

Research tool only: nothing in ``iap`` imports it, and the replay it runs
is unchanged (the recorder only reads).
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

from iap.execution import replay as replay_mod
from iap.execution.types import OrderType
from iap.research.execution_benchmark import run_benchmark

LEVELS = 10
_children: list[dict] = []
_fills: dict[int, list] = defaultdict(list)
_label: dict[int, str] = {}

_orig_issue = replay_mod.ExecutionReplay._issue_child
_orig_run = replay_mod.ExecutionReplay.run


def _policy_of(rep) -> str:
    if rep._router is not None:
        return "sor_sweep" if getattr(rep, "_router_sweep", False) else "sor"
    vids = {ps.order.venue_id for ps in rep._parents}
    names = {rep._config.venues[v].name for v in vids if v}
    return "pin_" + ("iex" if names == {"IEXG"} else "nasdaq" if names == {"XNAS"} else "?")


def _issue(self, ps, child_qty, decision_ts, passive, post=False):
    p = ps.order
    book = self._sim.instrument_book(p.instrument_id)
    opp = 1 - p.side
    depth = {
        self._config.venues[v].name: (vb.depth(opp, LEVELS) if vb is not None else [])
        for v in self._sor_candidates
        for vb in [book.books.get(v)]
    }
    bb, ba = book.best_bid(), book.best_ask()
    mid = None if bb is None or ba is None else 0.5 * (bb[0] + ba[0])
    tick = self._config.instrument(p.instrument_id).tick_size
    costs = None
    if self._router is not None and not passive and mid is not None:
        costs = {
            self._config.venues[v].name: c
            for v, c in self._router.aggressive_costs(
                book, p.side, decision_ts, tick, self._sor_candidates
            ).items()
        }
    n0 = len(ps.child_ids)
    ok = _orig_issue(self, ps, child_qty, decision_ts, passive, post)
    if not ok or len(ps.child_ids) == n0 or mid is None:
        return ok
    o = self._sim.orders[ps.child_ids[-1]]
    _children.append(
        {
            "rep": id(self),
            "order_id": o.order_id,
            "side": p.side,
            "qty": o.qty,
            "venue": self._config.venues[o.venue_id].name,
            "type": o.type.name,
            "limit": o.limit_ticks if o.type != OrderType.MARKET else None,
            "nbb_same": (bb if p.side == 0 else ba)[0],
            "mid": mid,
            "tick": tick,
            "depth": depth,
            "costs": costs,
            "ts": decision_ts,
        }
    )
    return ok


def _run(self, events):
    res = _orig_run(self, events)
    _label[id(self)] = _policy_of(self)
    for f in res.fills:
        _fills[id(self)].append(f)
    return res


def _summ(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs),
        "p50": xs[len(xs) // 2],
        "p90": xs[min(len(xs) - 1, int(0.9 * len(xs)))],
    }


def analyse() -> dict:
    by_rep_order = {(r, f.order_id): [] for r, fs in _fills.items() for f in fs}
    for r, fs in _fills.items():
        for f in fs:
            by_rep_order[(r, f.order_id)].append(f)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in _children:
        pol = _label.get(c["rep"], "?")
        fs = by_rep_order.get((c["rep"], c["order_id"]), [])
        s = 1.0 if c["side"] == 0 else -1.0
        own = c["depth"].get(c["venue"], [])
        other = {k: v for k, v in c["depth"].items() if k != c["venue"]}
        touch = own[0] if own else None
        row = {
            "qty": c["qty"],
            "touch_size": touch[1] if touch else 0,
            "size_over_touch": c["qty"] / touch[1] if touch and touch[1] > 0 else float("inf"),
            "depth10": sum(q for _, q in own),
            "exceeds_depth10": c["qty"] > sum(q for _, q in own),
            "levels_walked": len({f.price_ticks for f in fs}),
            "filled": sum(f.qty for f in fs),
        }
        if fs and touch:
            vwap = sum(f.price_ticks * f.qty for f in fs) / row["filled"]
            row["touch_bps"] = 1e4 * s * (touch[0] - c["mid"]) / c["mid"]
            row["walk_bps"] = 1e4 * s * (vwap - touch[0]) / c["mid"]
            row["total_bps"] = row["touch_bps"] + row["walk_bps"]
        # touch comparison against the other venue(s)
        for name, d in other.items():
            if touch and d:
                ot = d[0][0]
                rel = s * (touch[0] - ot)  # >0: routed venue's touch is worse
                row["touch_vs_other"] = "equal" if rel == 0 else ("worse" if rel > 0 else "better")
                row["other_touch_size"] = d[0][1]
                row["other_venue"] = name
        if c["costs"]:
            row["costs"] = c["costs"]
        if c["limit"] is not None:
            # >0: our limit sits behind the consolidated same-side best (ticks)
            row["behind_nbbo_ticks"] = s * (c["nbb_same"] - c["limit"])
            row["limit_vs_mid_bps"] = 1e4 * s * (c["limit"] - c["mid"]) / c["mid"]
        row["n_fills"] = len(fs)
        row["maker_qty"] = sum(f.qty for f in fs if f.liquidity.name == "MAKER")
        if fs:
            vw = sum(f.price_ticks * f.qty for f in fs) / row["filled"]
            row["fill_vs_decision_mid_bps"] = 1e4 * s * (vw - c["mid"]) / c["mid"]
        groups[(pol, c["venue"], c["type"])].append(row)
    out = {}
    for (pol, ven, typ), rows in sorted(groups.items()):
        withcost = [r for r in rows if "total_bps" in r]
        tvo = defaultdict(int)
        for r in rows:
            tvo[r.get("touch_vs_other", "n/a")] += 1
        filled_rows = [r for r in rows if r["filled"]]
        out[f"{pol}/{ven}/{typ}"] = {
            "filled_qty": sum(r["filled"] for r in rows),
            "maker_qty": sum(r["maker_qty"] for r in rows),
            "fill_vs_decision_mid_bps_qty_weighted": (
                sum(r["fill_vs_decision_mid_bps"] * r["filled"] for r in filled_rows)
                / max(1, sum(r["filled"] for r in filled_rows))
            ),
            "behind_nbbo_ticks": _summ(
                [r["behind_nbbo_ticks"] for r in rows if "behind_nbbo_ticks" in r]
            ),
            "limit_vs_mid_bps": _summ(
                [r["limit_vs_mid_bps"] for r in rows if "limit_vs_mid_bps" in r]
            ),
            "children": len(rows),
            "child_qty": _summ([r["qty"] for r in rows]),
            "touch_size": _summ([r["touch_size"] for r in rows]),
            "share_qty_gt_touch": sum(r["qty"] > r["touch_size"] for r in rows) / len(rows),
            "share_qty_gt_depth10": sum(r["exceeds_depth10"] for r in rows) / len(rows),
            "levels_walked": _summ([r["levels_walked"] for r in rows]),
            "share_walked_ge2": sum(r["levels_walked"] >= 2 for r in rows) / len(rows),
            "fill_ratio": sum(r["filled"] for r in rows) / max(1, sum(r["qty"] for r in rows)),
            "touch_bps": _summ([r["touch_bps"] for r in withcost]),
            "walk_bps": _summ([r["walk_bps"] for r in withcost]),
            "total_bps_qty_weighted": (
                sum(r["total_bps"] * r["filled"] for r in withcost)
                / max(1, sum(r["filled"] for r in withcost))
            ),
            "walk_bps_qty_weighted": (
                sum(r["walk_bps"] * r["filled"] for r in withcost)
                / max(1, sum(r["filled"] for r in withcost))
            ),
            "touch_vs_other": dict(tvo),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--policies", default="pin_nasdaq,pin_iex,sor")
    ap.add_argument("--data-root", type=Path, required=True)
    ap.add_argument("--calibration", type=Path, default=Path("research/execution/b8/calibration"))
    ap.add_argument(
        "--venues", type=Path, default=Path("research/execution/b8/venues_xnas_iexg.json")
    )
    a = ap.parse_args()
    replay_mod.ExecutionReplay._issue_child = _issue  # type: ignore[method-assign]
    replay_mod.ExecutionReplay.run = _run  # type: ignore[method-assign]
    D = a.data_root
    run_benchmark(
        D / "r7_20251128",
        second_data_dir=D / "iex" / "iex_20251128",
        more_pairs=((D / "r7_20251208", D / "iex" / "iex_20251208"),),
        venues=a.venues,
        venue_calibration=a.calibration,
        policies=tuple(a.policies.split(",")),
        baseline_policy=a.policies.split(",")[0],
        symbols=(a.symbol,),
        timestamp=False,
    )
    doc = {"symbol": a.symbol, "levels_recorded": LEVELS, "groups": analyse()}
    a.out.mkdir(parents=True, exist_ok=True)
    p = a.out / f"depth_walk_{a.symbol}.json"
    p.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

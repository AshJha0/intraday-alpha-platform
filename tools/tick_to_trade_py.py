#!/usr/bin/env python3
"""Tick-to-trade latency of the PYTHON reference path (v1.12, plan item X6).

The Python counterpart of ``cpp/bench/bench_tick_to_trade.cpp``: per event,
one IAP1 v2 frame -> ``decode_iap1`` (CRC verified) -> ``OrderBook.apply`` ->
``FeatureEngine.apply`` (48 native features, cadence 0) -> linear_z_v1 scoring
of EQ01 + EQ03 + EQ06 from the fitted ``configs/strategies/alpha_params.json``
-> the same bench-local direction vote and minimal pre-trade check.  Timed
with ``time.perf_counter_ns``; the first pass is warm-up and discarded.

The Python path is the readable REFERENCE implementation, not a hot path
(docs/POLYGLOT.md): expect it to be two to three orders of magnitude slower
than the C++ figures.  Per-row alpha scoring here is a direct transcription of
the pinned linear_z_v1 formula (the ``iap.alpha`` models score DataFrames in
batch, which has no per-event entry point); not included, as in C++: network,
kernel, order encoding / wire send, the platform risk engine, logging.

    PYTHONPATH=python/src python3 tools/tick_to_trade_py.py [--passes 3] [--genday 20000]

Prints a markdown table (events/s and p50/p90/p99/p99.9/max in microseconds).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.core.codec import decode_iap1, encode_iap1, read_jsonl  # noqa: E402
from iap.core.events import EventType, MarketEvent  # noqa: E402
from iap.core.rng import SplitMix64  # noqa: E402
from iap.features.context import build_contexts  # noqa: E402
from iap.features.engine import FeatureEngine  # noqa: E402
from iap.features.registry import feature_index  # noqa: E402
from iap.orderbook.book import OrderBook  # noqa: E402

STAGES = ("decode", "book", "features", "alpha", "decision")
EPS = 1e-12
THRESHOLD = 0.5
CLIP, MAX_POS, MAX_NOTIONAL, BAND_BPS = 100, 5_000, 250_000.0, 500.0


def generate_equity_day(seed: int, n: int) -> list[MarketEvent]:
    """Same generator as bench_tick_to_trade.cpp (same draws, same seed)."""
    rng = SplitMix64(seed)
    out: list[MarketEvent] = []
    live: list[list[int]] = []  # [order_id, side, px, qty]
    state = {"ts": 1787578200000000000, "seq": 0}
    oid, tid, mid = 1, 1, 2450

    def push(etype: int, side: int, px: int, qty: int, o: int, t: int) -> None:
        state["seq"] += 1
        state["ts"] += 1_000_000 + rng.below(4_000_000)
        ts, seq = state["ts"], state["seq"]
        recv = ts + 150_000 + rng.below(50_000)
        out.append(MarketEvent(seq, 1, 1, ts, recv, seq, etype, side, px, qty, o, t))

    push(int(EventType.STATUS), 0, 0, 1, 0, 0)
    while len(out) < n:
        u = rng.uniform()
        if rng.uniform() < 0.02:
            mid += -1 if rng.uniform() < 0.5 else 1
        mid = max(mid, 100)
        if len(live) < 20 or u < 0.45:
            side = 0 if rng.uniform() < 0.5 else 1
            off = 1 + rng.below(10)
            px = mid - off if side == 0 else mid + off
            # never cross the resting opposite side (a crossing ADD executes)
            for o in live:
                if side == 0 and o[1] == 1 and px >= o[2]:
                    px = o[2] - 1
                if side == 1 and o[1] == 0 and px <= o[2]:
                    px = o[2] + 1
            px = max(px, 1)
            qty = 100 * (1 + rng.below(10))
            live.append([oid, side, px, qty])
            push(int(EventType.ADD), side, px, qty, oid, 0)
            oid += 1
        elif u < 0.80:
            k = rng.below(len(live))
            o = live[k]
            live[k] = live[-1]
            live.pop()
            push(int(EventType.CANCEL), o[1], o[2], o[3], o[0], 0)
        elif u < 0.92:
            k = rng.below(len(live))
            o = live[k]
            q = min(o[3], 100)
            push(int(EventType.EXECUTE), o[1], o[2], q, o[0], tid)
            tid += 1
            o[3] -= q
            if o[3] == 0:
                live[k] = live[-1]
                live.pop()
        else:
            side = 0 if rng.uniform() < 0.5 else 1
            push(int(EventType.TRADE), side, mid, 100 * (1 + rng.below(5)), 0, tid)
            tid += 1
    return out


def _alpha_inputs() -> list[tuple[dict, list[tuple[float, int]]]]:
    params = json.loads((ROOT / "configs/strategies/alpha_params.json").read_text())["params"]
    idx = feature_index()
    weights = {
        "EQ01": [(1.0, "micro_mid_dev_bps_v1")],
        "EQ03": [
            (0.5, "ofi_norm_l1_w1s_v1"),
            (0.3, "ofi_norm_l5_w1s_v1"),
            (0.2, "ofi_norm_l5_w5s_v1"),
        ],
        "EQ06": [(1.0, "ret_vol_adj_10s_v1")],
    }
    return [(params[a], [(w, idx[f]) for w, f in weights[a]]) for a in ("EQ01", "EQ03", "EQ06")]


def _score(p: dict, inputs: list[tuple[float, int]], vec) -> tuple[float, float]:
    raw = 0.0
    for w, i in inputs:
        if not vec.validity[i]:
            return 0.0, 0.0
        raw += w * vec.values[i]
    if p.get("dead") or not p["sigma"] > 0 or p["beta"] == 0 or not math.isfinite(raw):
        return 0.0, 0.0
    z = max(-p["z_clip"], min(p["z_clip"], (raw - p["mu"]) / (p["sigma"] + EPS)))
    return p["beta"] * z, min(1.0, abs(z) / p["conf_scale"])


def run_pass(frames: list[bytes], contexts, alphas, samples: list[list[int]] | None) -> None:
    book = OrderBook(1, 1)
    fe = FeatureEngine(contexts, cadence_ns=0)
    pos = 0
    clock = time.perf_counter_ns
    for frame in frames:
        t0 = clock()
        ev = decode_iap1(frame)[0]
        t1 = clock()
        book.apply(ev)
        t2 = clock()
        vec = fe.apply(ev)
        t3 = clock()
        vote, wsum = 0.0, 0.0
        if vec is not None:
            for p, inputs in alphas:
                er, conf = _score(p, inputs, vec)
                vote += (1.0 if er > 0 else -1.0 if er < 0 else 0.0) * conf
                wsum += conf
        t4 = clock()
        s = vote / 3.0
        if wsum > 0 and abs(s) > THRESHOLD:
            bid, ask = book.best_bid(), book.best_ask()
            if bid and ask:
                buy = s > 0
                px = ask[0] if buy else bid[0]
                mid = 0.5 * (bid[0] + ask[0])
                new_pos = pos + (CLIP if buy else -CLIP)
                ok = (
                    abs(new_pos) <= MAX_POS
                    and px * 0.01 * CLIP <= MAX_NOTIONAL
                    and 1e4 * abs(px - mid) / mid <= BAND_BPS
                )
                if ok:
                    pos = new_pos
        t5 = clock()
        if samples is not None:
            samples[0].append(t5 - t0)
            for k, (a, b) in enumerate(((t0, t1), (t1, t2), (t2, t3), (t3, t4), (t4, t5))):
                samples[k + 1].append(b - a)


def pct(xs: list[int], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))] / 1000.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--passes", type=int, default=3, help="timed passes over the golden vector")
    ap.add_argument("--genday", type=int, default=20_000, help="generated-day events (0 = skip)")
    args = ap.parse_args(argv)
    contexts = build_contexts(ROOT / "configs")
    alphas = _alpha_inputs()
    workloads = [
        (
            "golden eq_mbo (2000 ev)",
            read_jsonl(ROOT / "tests/golden/events_eq_mbo.jsonl"),
            args.passes,
        )
    ]
    if args.genday:
        workloads.append(
            (
                f"generated day (seed 20261012, {args.genday} ev)",
                generate_equity_day(20261012, args.genday),
                1,
            )
        )
    print(
        "| path (Python reference), workload | events/s | p50 us | p90 us | p99 us | p99.9 us | max us |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|")
    for name, events, passes in workloads:
        frames = [encode_iap1([ev]) for ev in events]
        run_pass(frames, contexts, alphas, None)  # warm-up
        samples: list[list[int]] = [[] for _ in range(len(STAGES) + 1)]
        for _ in range(passes):
            run_pass(frames, contexts, alphas, samples)
        for label, xs in zip(("tick-to-trade (end to end)", *STAGES), samples, strict=True):
            eps = 1e9 * len(xs) / sum(xs) if label.startswith("tick") else float("nan")
            rate = f"{eps:,.0f}" if label.startswith("tick") else ""
            print(
                f"| {label}, {name} | {rate} | {pct(xs, 0.5):.1f} | {pct(xs, 0.9):.1f} | "
                f"{pct(xs, 0.99):.1f} | {pct(xs, 0.999):.1f} | {max(xs) / 1000:.1f} |"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Benchmark the native-feature backends: Python reference vs Rust (pyo3).

Usage (from python/ with PYTHONPATH=src; needs the iap_features_rs wheel):

    python tools/bench_native_features.py [FILE ...] [--cadence-ms 0]
        [--max-events N] [--repeat 3] [--summary $GITHUB_STEP_SUMMARY]

Default inputs: the four golden event vectors in tests/golden. For each
input it replays the events through

- ``python``: the reference FeatureEngine (it computes all 205 registry
  features; there is no native-only Python engine) + selection of the 45
  native slots -- what a Python caller pays today;
- ``rust``: ``iap_features_rs.replay_bytes`` on pre-encoded IAP1 bytes
  (decode + replay of the 45 slots + numpy hand-off);

reports the share of rows on which the two agree (informational), and
events/s (best of ``--repeat``) and the speed-up as a markdown table.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
from iap.core.codec import encode_iap1, read_iap1, read_jsonl
from iap.features.context import build_contexts
from iap.features.native import _from_ext, compute_native, rust_extension

_REPO = Path(__file__).resolve().parents[2]
_GOLDEN = _REPO / "tests" / "golden"
_DEFAULT = [
    _GOLDEN / n
    for n in (
        "events_eq_mbo.jsonl",
        "events_fx_quote.jsonl",
        "events_eq_anomalies.jsonl",
        "events_fx_anomalies.jsonl",
    )
]


def _best(fn, repeat: int):
    best, out = float("inf"), None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t0)
    return best, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--configs", default=str(_REPO / "configs"))
    ap.add_argument("--cadence-ms", type=int, default=0)
    ap.add_argument("--max-events", type=int, default=0, help="0 = whole file")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--summary", default=None, help="append the markdown table to this file")
    args = ap.parse_args(argv)

    ext = rust_extension()
    if ext is None:
        print("iap_features_rs is not installed", file=sys.stderr)
        return 2
    contexts = build_contexts(args.configs)
    ticks = {i: c.tick_size for i, c in contexts.items()}
    cadence_ns = args.cadence_ms * 1_000_000

    lines = [
        f"### Native features: Python vs Rust (pyo3), cadence {args.cadence_ms} ms",
        "",
        "| input | events | rows | python ev/s | rust ev/s | speed-up | rows agreeing |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    tot_ev = tot_py = tot_rs = 0.0
    for path in args.files or _DEFAULT:
        events = read_iap1(path) if path.suffix == ".iap1" else read_jsonl(path)
        if args.max_events:
            events = events[: args.max_events]
        data = encode_iap1(events)
        t_py, py = _best(
            lambda ev=events: compute_native(ev, contexts, cadence_ns, "python"), args.repeat
        )
        t_rs, d = _best(lambda b=data: ext.replay_bytes(b, ticks, cadence_ns, "iap1"), args.repeat)
        rs = _from_ext(d)
        # Informational (the parity contract is tests/test_features_rust_backend.py):
        # share of rows whose 45 slots agree (validity exact, values 1e-9).
        if np.array_equal(py.timestamp, rs.timestamp):
            same_v = py.validity == rs.validity
            a = np.where(py.validity, py.values, 0.0)
            b = np.where(rs.validity, rs.values, 0.0)
            ok = same_v & (np.abs(a - b) <= 1e-9 + 1e-9 * np.abs(a))
            agree = f"{ok.all(axis=1).mean():.2%}"
        else:
            agree = "rows differ"
        n = len(events)
        tot_ev += n
        tot_py += t_py
        tot_rs += t_rs
        lines.append(
            f"| {path.name} | {n} | {len(py.timestamp)} | {n / t_py:,.0f} | "
            f"{n / t_rs:,.0f} | {t_py / t_rs:.1f}x | {agree} |"
        )
    lines.append(
        f"| **total** | {int(tot_ev)} | | {tot_ev / tot_py:,.0f} | "
        f"{tot_ev / tot_rs:,.0f} | **{tot_py / tot_rs:.1f}x** | |"
    )
    lines += [
        "",
        "python = full reference engine (205 features) + native-slot selection; "
        "rust = IAP1 decode + 45-slot replay + numpy hand-off. Best of "
        f"{args.repeat}. rows agreeing = all 45 slots equal (validity exact, values "
        "1e-9); informational, the parity contract is the pytest suite.",
        "",
    ]
    text = "\n".join(lines)
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""(Re)generate tests/golden/expected_adaptive.json and
research/baselines/signal_eq01.json (the adaptability-layer goldens).

Run from python/ with PYTHONPATH=src:

    PYTHONPATH=src python3 tools/make_golden_adaptive.py

Pins (see /API_ADAPTIVE.md for the normative formulas):

1. **PSI + KS on pinned synthetic distributions** — samples drawn from the
   pinned SplitMix64 stream (seed 20260830, one continuous stream):
   4000 uniforms (baseline), then 2000 uniforms (same-distribution case),
   then 2000 values ``0.25 + 0.75*u`` (shifted case).  The captured
   baseline, both PSI values and both KS (D, p) pairs are pinned at 1e-10.
2. **signal_eq01 baseline** — the EQ01 signal distribution on the golden
   EQ frame (events_eq_mbo.jsonl, instrument 1): expected_return values of
   rows with confidence > 0, scored with the day-1-fitted params from
   configs/strategies/alpha_params.json.  Serialized to
   research/baselines/signal_eq01.json (the file the Java live-metric
   agent consumes).  Self-PSI (exactly 0) and second-half PSI/KS pinned
   as cross-language parity targets.
3. **Drift-trigger decision sequence** — a pinned stateful input sequence
   through DriftTriggeredPolicy (thresholds from configs/strategies.json)
   with exact expected booleans, covering both threshold edges, the
   min-refit-gap and None (silent monitor) inputs.
4. **Lifecycle transition sequence** — a constructed rolling-IC path
   through the pinned lifecycle gates with exact expected states,
   covering WATCH entry, neutral-zone counter reset, retirement,
   post-retirement recovery to WATCH, re-activation, relapse and a block of
   UNINFORMATIVE evaluations (a frozen IC window re-read six times, which
   must move nothing).
5. **Rolling realized IC** — EQ01 on the golden EQ frame: the mid series
   (one sample per book_ok refresh) and the confident signal rows are
   embedded, together with the rolling IC at pinned evaluation times.
   A live port (Java ``com.iap.adaptive.RollingIc``) must reproduce the
   research label — prevailing mid at-or-before ``t + horizon`` — and the
   mean bucket IC at 1e-10 from the embedded series alone.

BEFORE writing, every PSI and KS value is re-derived by an independent
brute-force implementation (explicit loops, no iap.adaptive code) and
compared at 1e-12; the file is only written when all agree.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python" / "src"))

import numpy as np  # noqa: E402

from iap.adaptive import (  # noqa: E402
    DriftTriggeredPolicy,
    LifecycleConfig,
    LifecycleTracker,
    PSI_EPS,
    RefitContext,
    capture_baseline,
    ks_test,
    load_adaptive_config,
    psi,
)
from iap.alpha import load_params_file  # noqa: E402
from iap.alpha.goldenframes import build_golden_frame  # noqa: E402
from iap.core.rng import SplitMix64  # noqa: E402

GOLDEN_DIR = REPO / "tests" / "golden"
BASELINES_DIR = REPO / "research" / "baselines"

SYNTH_SEED = 20260830
N_BASE, N_CUR = 4000, 2000
NS_15M = 900_000_000_000


# -- independent brute force (no iap.adaptive code) -------------------------

def bf_psi(edges, expected_frac, values) -> float:
    counts = [0] * 10
    n = 0
    for v in values:
        if not math.isfinite(v):
            continue
        n += 1
        b = 0
        for e in edges:  # count edges strictly less than v (side='left')
            if e < v:
                b += 1
        counts[b] += 1
    total = 0.0
    for i in range(10):
        a = max(counts[i] / n, PSI_EPS)
        e = max(expected_frac[i], PSI_EPS)
        total += (a - e) * math.log(a / e)
    return total


def bf_ks(a, b):
    a = [v for v in a if math.isfinite(v)]
    b = [v for v in b if math.isfinite(v)]
    d = 0.0
    for v in list(a) + list(b):
        fa = sum(1 for x in a if x <= v) / len(a)
        fb = sum(1 for x in b if x <= v) / len(b)
        d = max(d, abs(fa - fb))
    en = math.sqrt(len(a) * len(b) / (len(a) + len(b)))
    lam = (en + 0.12 + 0.11 / en) * d
    p = 2.0 * sum((-1.0) ** (j - 1) * math.exp(-2.0 * j * j * lam * lam)
                  for j in range(1, 101))
    return d, min(1.0, max(0.0, p))


def check(label: str, got: float, bf: float, tol: float = 1e-12) -> None:
    if abs(got - bf) > tol:
        raise RuntimeError(f"brute-force mismatch {label}: {got!r} vs {bf!r}")


def _rolling_ic_case(frame, model, cfg) -> dict:
    """Rolling-IC golden: embedded mid series + signals + pinned ICs.

    The mid series is exactly the one the label layer uses (one sample per
    book_ok refresh); the signal rows are EQ01's confident emissions.  The
    expected values come from the reference ``rolling_ic_z`` over the
    matured rows of each window — the same code path research uses.
    """
    from iap.adaptive.drift import ICBaseline, rolling_ic_z  # noqa: E402
    from iap.validation.metrics import HORIZONS_NS  # noqa: E402

    horizon = model.horizon
    h_ns = HORIZONS_NS[horizon]
    window_ns = int(cfg["ic_window_ns"])
    bucket_ns = int(cfg["ic_bucket_ns"])
    min_buckets = int(cfg["min_ic_buckets"])

    ts = frame["exchange_ts"].to_numpy(dtype=np.int64)
    mid = frame["mid_price_v1"].to_numpy(dtype=float)
    ok = np.isfinite(mid) & (mid > 0.0)
    mid_ts, mid_v = ts[ok], mid[ok]

    sc = model.score({1: frame})[1]
    er = sc["expected_return"].to_numpy(dtype=float)
    conf = sc["confidence"].to_numpy(dtype=float)
    sig_ok = (conf > 0.0) & np.isfinite(er) & ok
    sig_ts, sig_v, sig_mid = ts[sig_ok], er[sig_ok], mid[sig_ok]

    # research label: prevailing mid at-or-before t + h (at_or_before sweep)
    idx = np.searchsorted(mid_ts, sig_ts + h_ns, side="right") - 1
    have = idx >= 0
    fwd = np.where(have, mid_v[np.maximum(idx, 0)], np.nan)
    observed = (sig_ts + h_ns) <= mid_ts[-1]  # stream seen through t+h
    ret = np.where(have & observed, fwd / sig_mid - 1.0, np.nan)

    # a dummy baseline: only `rolling_ic` (the mean live bucket IC) is pinned
    base = ICBaseline(name="golden", alpha_id="EQ01", source="golden",
                      ic_mean=0.0, ic_std=1.0, n_buckets_baseline=8,
                      bucket_ns=bucket_ns, horizon=horizon)
    t_lo, t_hi = int(ts[0]), int(ts[-1])
    evals = []
    for k in range(1, 6):
        t_eval = t_lo + (t_hi - t_lo) * k // 5
        m = (sig_ts >= t_eval - window_ns) & (sig_ts + h_ns <= t_eval)
        res = rolling_ic_z(base, sig_ts[m], sig_v[m], ret[m], min_buckets)
        evals.append({
            "t": int(t_eval),
            "n_matured": int(np.sum(m & np.isfinite(ret))),
            "n_buckets": int(res.n_buckets),
            "rolling_ic": res.rolling_ic,
        })
    return {
        "description": (
            "EQ01 on the golden EQ frame. Feed mid_series through onMid (one "
            "sample per book_ok refresh) and signals through onSignal, then "
            "read ic(t) at each evaluation time; tolerance 1e-10, null = NaN."
        ),
        "alpha_id": "EQ01",
        "instrument_id": 1,
        "horizon": horizon,
        "horizon_ns": int(h_ns),
        "window_ns": window_ns,
        "bucket_ns": bucket_ns,
        "min_buckets": min_buckets,
        "mid_series": [[int(a), float(b)] for a, b in zip(mid_ts, mid_v)],
        "signals": [[int(a), float(b), float(c)]
                    for a, b, c in zip(sig_ts, sig_v, sig_mid)],
        "evaluations": evals,
    }


def main() -> int:
    cfg = load_adaptive_config(REPO / "configs" / "strategies.json")
    dt_cfg = cfg["policies"]["drift_triggered"]

    # -- 1. synthetic PSI/KS ------------------------------------------------
    rng = SplitMix64(SYNTH_SEED)
    base = np.array([rng.uniform() for _ in range(N_BASE)])
    same = np.array([rng.uniform() for _ in range(N_CUR)])
    shifted = np.array([0.25 + 0.75 * rng.uniform() for _ in range(N_CUR)])

    baseline = capture_baseline(
        base, "feature", "golden_synthetic_uniform",
        source=f"SplitMix64 seed {SYNTH_SEED}, {N_BASE} uniforms",
    )
    cases = {}
    for name, cur in (("same_dist", same), ("shifted", shifted)):
        p_val = psi(baseline, cur)
        d, kp = ks_test(base, cur)
        check(f"psi[{name}]", p_val,
              bf_psi(baseline.edges, baseline.expected_frac, cur))
        bf_d, bf_p = bf_ks(base, cur)
        check(f"ks_d[{name}]", d, bf_d)
        check(f"ks_p[{name}]", kp, bf_p)
        cases[name] = {"n": int(cur.size), "psi": p_val, "ks_d": d, "ks_p": kp}
        print(f"synthetic {name}: psi={p_val:.10f} ks_d={d:.10f} ks_p={kp:.3e}")

    # -- 2. signal_eq01 baseline from the golden EQ frame -------------------
    frame = build_golden_frame(
        GOLDEN_DIR / "events_eq_mbo.jsonl", REPO / "configs", 1
    )
    models = load_params_file(REPO / "configs" / "strategies" / "alpha_params.json")
    sc = models["EQ01"].score({1: frame})[1]
    er = sc["expected_return"].to_numpy(dtype=float)
    conf = sc["confidence"].to_numpy(dtype=float)
    sig = er[conf > 0.0]
    eq01_baseline = capture_baseline(
        sig, "signal", "signal_eq01", alpha_id="EQ01",
        source=(
            "EQ01 expected_return on the golden EQ frame "
            "(tests/golden/events_eq_mbo.jsonl, instrument 1, cadence 0), "
            "rows with confidence > 0, params from "
            "configs/strategies/alpha_params.json"
        ),
    )
    eq01_baseline.save(BASELINES_DIR / "signal_eq01.json")
    psi_self = psi(eq01_baseline, sig)
    check("psi_self", psi_self, bf_psi(eq01_baseline.edges,
                                       eq01_baseline.expected_frac, sig))
    half = sig[sig.size // 2:]
    psi_half = psi(eq01_baseline, half)
    check("psi_half", psi_half, bf_psi(eq01_baseline.edges,
                                       eq01_baseline.expected_frac, half))
    d_half, p_half = ks_test(sig, half)
    bf_d, bf_p = bf_ks(sig, half)
    check("ks_d_half", d_half, bf_d)
    check("ks_p_half", p_half, bf_p)
    print(f"signal_eq01: n={sig.size} psi_self={psi_self} "
          f"psi_half={psi_half:.10f} ks_d_half={d_half:.10f}")

    # -- 3. drift-trigger decision sequence ---------------------------------
    policy = DriftTriggeredPolicy(
        psi_threshold=float(dt_cfg["psi_threshold"]),
        ic_z_threshold=float(dt_cfg["ic_z_threshold"]),
        min_refit_gap_ns=int(dt_cfg["min_refit_gap_ns"]),
    )
    steps = [
        {"now_ns": 900_000_000_000, "psi": {"signal": 0.5}, "ic_z": None},
        {"now_ns": 1_800_000_000_000, "psi": {}, "ic_z": -5.0},
        {"now_ns": 3_600_000_000_000, "psi": {"signal": 0.25}, "ic_z": -2.0},
        {"now_ns": 4_500_000_000_000, "psi": {"signal": 0.2500000001}, "ic_z": None},
        {"now_ns": 5_400_000_000_000, "psi": {"signal": 3.0}, "ic_z": -9.0},
        {"now_ns": 8_100_000_000_000, "psi": {"signal": None}, "ic_z": -1.99},
        {"now_ns": 9_000_000_000_000, "psi": {"signal": 0.1}, "ic_z": -2.0000001},
        {"now_ns": 12_600_000_000_000,
         "psi": {"signal": 0.1, "feature:x": 0.26}, "ic_z": 0.5},
        {"now_ns": 16_200_000_000_000, "psi": {"signal": 0.24}, "ic_z": None},
        {"now_ns": 17_100_000_000_000, "psi": {}, "ic_z": None},
    ]
    last_fit = 0
    expected_bools = []
    for s in steps:
        dec = policy.should_refit(RefitContext(
            now_ns=s["now_ns"], last_fit_ns=last_fit,
            psi_by_series=s["psi"], ic_z=s["ic_z"],
        ))
        expected_bools.append(bool(dec.refit))
        if dec.refit:
            last_fit = s["now_ns"]
    if expected_bools != [False, False, False, True, False,
                          False, True, True, False, False]:
        raise RuntimeError(f"trigger sequence changed: {expected_bools}")
    print("trigger sequence:", expected_bools)

    # -- 4. lifecycle transition sequence -----------------------------------
    lc = LifecycleConfig.from_config(cfg["lifecycle"])
    ic_path = [0.02, -0.01, -0.02, 0.002, -0.01, -0.01, -0.01, None,
               -0.01, -0.01, -0.001, 0.01, 0.004, 0.01, 0.02, 0.005,
               0.005, 0.03, 0.06, -0.01,
               # round-3: a breach followed by SIX re-reads of the SAME
               # frozen IC window (no new matured rows) must NOT retire the
               # alpha — uninformative evaluations move nothing.
               -0.02, -0.02, -0.02, -0.02, -0.02, -0.02, -0.02]
    ic_informative = [True] * 20 + [True] + [False] * 6
    tracker = LifecycleTracker(alpha_id="GOLDEN", config=lc, policy="golden")
    states = []
    for k, (v, inf) in enumerate(zip(ic_path, ic_informative)):
        states.append(tracker.update((k + 1) * NS_15M, v, informative=inf))
    expected_states = [
        "ACTIVE", "WATCH", "WATCH", "WATCH", "WATCH", "WATCH", "WATCH",
        "WATCH", "WATCH", "WATCH", "RETIRED", "RETIRED", "RETIRED",
        "RETIRED", "RETIRED", "WATCH", "WATCH", "WATCH", "ACTIVE", "WATCH",
        "WATCH", "WATCH", "WATCH", "WATCH", "WATCH", "WATCH", "WATCH",
    ]
    if states != expected_states:
        raise RuntimeError(f"lifecycle sequence changed: {states}")
    print(f"lifecycle sequence ok ({len(tracker.transitions)} transitions)")

    # -- 5. rolling realized IC on the golden EQ frame ----------------------
    rolling = _rolling_ic_case(frame, models["EQ01"], cfg)
    print(f"rolling ic: {len(rolling['evaluations'])} evaluations, "
          f"{len(rolling['mid_series'])} mid samples, "
          f"{len(rolling['signals'])} signal rows")

    blob = {
        "x-version": 1,
        "description": (
            "Adaptability-layer goldens (iap.adaptive; normative contract "
            "/API_ADAPTIVE.md). PSI/KS float tolerance 1e-10; trigger "
            "booleans and lifecycle states exact. Regenerate with "
            "python/tools/make_golden_adaptive.py (brute-force validated "
            "at 1e-12 before writing)."
        ),
        "psi_ks": {
            "recipe": {
                "rng": "SplitMix64, one continuous stream",
                "seed": SYNTH_SEED,
                "baseline": f"{N_BASE} x uniform()",
                "same_dist": f"{N_CUR} x uniform()",
                "shifted": f"{N_CUR} x (0.25 + 0.75*uniform())",
            },
            "baseline": baseline.to_dict(),
            "cases": cases,
        },
        "signal_eq01": {
            "baseline_file": "research/baselines/signal_eq01.json",
            "n": int(sig.size),
            "psi_self": psi_self,
            "second_half": {
                "n": int(half.size), "psi": psi_half,
                "ks_d": d_half, "ks_p": p_half,
            },
        },
        "drift_trigger": {
            "policy": policy.describe(),
            "initial_last_fit_ns": 0,
            "note": "stateful: a True decision sets last_fit_ns = now_ns",
            "steps": steps,
            "expected": expected_bools,
        },
        "rolling_ic": rolling,
        "lifecycle": {
            "config": {
                "watch_ic_gate": lc.watch_ic_gate,
                "reactivate_ic_gate": lc.reactivate_ic_gate,
                "retire_breach_evals": lc.retire_breach_evals,
                "reactivate_evals": lc.reactivate_evals,
            },
            "ts_step_ns": NS_15M,
            "ic_path": ic_path,
            "ic_informative": ic_informative,
            "expected_states": expected_states,
            "expected_transition_count": len(tracker.transitions),
            "expected_transitions": [
                {"from": t.from_state, "to": t.to_state,
                 "eval_index": t.eval_index}
                for t in tracker.transitions
            ],
        },
    }
    out = GOLDEN_DIR / "expected_adaptive.json"
    out.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} and {BASELINES_DIR / 'signal_eq01.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

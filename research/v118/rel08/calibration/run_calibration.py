"""REL-08 v2 synthetic-null calibration runner (plan: ../REL08_V2_CALIBRATION_PLAN_REV2.md; execution protocol:
../REL08_V2_EXECUTION_PROTOCOL.md).

Reads no real data (every frame is generated in memory), writes only under --out.  Launch only after the reviewer authorises it.

    python run_calibration.py all      --freeze execution_manifest.json --out DIR [--workers 2]
    python run_calibration.py cell     --freeze ... --out DIR [--cells N1S1,...] [--r-from 0] [--reps 200]   (partial; same guards)
    python run_calibration.py power    --freeze ... --out DIR
    python run_calibration.py finalize --freeze ... --out DIR       # result.json + COMPLETED.json iff every unit is complete
    python run_calibration.py verify   --freeze ... --out DIR       # verify stored replications (resume policy)
    python run_calibration.py manifest                               # the machine-readable accounting (JSON)
    python run_calibration.py timing                                 # 4 screens (seeds 20269000+i), prints seconds only

Every run stage first recomputes the frozen identity in this checkout and refuses on any difference; every stored replication is stamped with
the identity, its unit and a content hash; resume verifies them and refuses (study VOID) on any invalid, duplicate or foreign record; a
COMPLETED study is never rerun.  The checkpoint at 100 per gating cell is OPERATIONAL ONLY (progress, integrity, resources).
Seeds: dataset seed = 20261100 + 1000 * cell_index + r; bootstrap seed = the rule's.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from iap.research import rel08_screen as rs
from iap.research import rel08_screen_v2 as v2

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "rel08_v2_dryrun", HERE.parent / "dryrun" / "rel08_v2_dryrun.py"
)
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)


def _load_sibling(name: str):
    spec = importlib.util.spec_from_file_location(f"rel08_v2_{name}", HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"rel08_v2_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


ig = _load_sibling("integrity")
fz = _load_sibling("freeze")
REPO = HERE.parents[3]

MODELS = ("N1", "N2", "N3", "N4")
SPREADS = {"S1": 1.0, "S4": 4.0}
CELLS = [f"{m}{s}" for m in MODELS for s in SPREADS]  # N1S1, N1S4, N2S1, ... (plan order)
BASE_SEED = 20261100
POWER_BASE_SEED = (
    20268500  # REV2: moved from 20268000, which overlapped the N4S4 cell range (20268100-20268299)
)
TIMING_BASE_SEED = 20269000
GATING_CELLS = [c for c in CELLS if not c.startswith("N4")]  # N1-N3 x S1, S4
R_FULL = (
    200  # confirmatory replications per null cell (fixed sample; decided only from the full 200)
)
R_CHECKPOINT = 100  # NON-DECISIONAL checkpoint after the first 100 of each gating cell
POWER_REPS = 30
POWER_CASES = [("revert", 6.0), ("revert", 12.0), ("revert", 24.0), ("lead", 0.3), ("lead", 0.6)]
BOOK = {"alpha_bps": 20.0, "risk_aversion": 100.0, "solver_iters": 20}
DRAWS = 9999
SESSIONS = 6


# ------------------------------------------------------------------ data --


def _ar1(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    e = rng.normal(0.0, math.sqrt(1 - phi**2), n)
    x = np.zeros(n)
    x[0] = rng.normal()
    for k in range(1, n):
        x[k] = phi * x[k - 1] + e[k]
    return x


def make_null(model: str, seed: int, spread: float) -> rs.ScreenData:
    """Six synthetic one-day sessions, no predictable component, N1-N4 of the plan (section 2)."""
    if model == "N1" or model == "N3":
        return dr.make_data("null", 0.0, seed, days=SESSIONS, spread=spread)
    # N2 / N4: regenerate with scale processes (same structure as dr.make_data)
    rng = np.random.default_rng(seed)
    days = SESSIONS
    rows_day = dr.SESSION_MIN * 60 // dr.STEP_S
    n_rows = rows_day * days
    ts = np.concatenate(
        [
            dr.DAY0 + d * dr.NS_DAY + np.arange(rows_day, dtype=np.int64) * dr.STEP_S * dr.NS_S
            for d in range(days)
        ]
    )
    vol_mult = np.ones(n_rows)
    spr_mult = np.ones(n_rows)
    shock = np.zeros(n_rows)
    if model == "N4":
        for d in range(days):
            sl = slice(d * rows_day, (d + 1) * rows_day)
            vol_mult[sl] = math.exp(rng.normal(0.0, 0.4))
            spr_mult[sl] = math.exp(rng.normal(0.0, 0.4))
            shock[sl] = rng.normal(0.0, 1e-3) / rows_day  # mean-zero daily common shock (~10 bps)
    if model == "N2":
        vol_mult = np.exp(0.3 * _ar1(rng, n_rows, 0.95))
    common_ret = rng.normal(0.0, 1.5e-4, n_rows) * vol_mult + shock
    noise_sd = 1e-4 if model == "N2" else 0.0

    def frame(logp: np.ndarray, beta: float, lo: float, hi: float) -> pd.DataFrame:
        b = np.full(n_rows, beta)
        for d in range(days):
            b[d * rows_day : d * rows_day + 10 * 60 // dr.STEP_S] = np.nan
        if noise_sd:
            logp = logp + rng.normal(0.0, noise_sd, n_rows)
        return pd.DataFrame(
            {
                "exchange_ts": ts,
                "mid_price_v1": 50.0 * np.exp(logp),
                "spread_bps_v1": rng.uniform(lo, hi, n_rows) * spr_mult,
                "beta_w5m_v1": b,
                "traded_volume_w10s_v1": rng.gamma(2.0, 1000.0, n_rows),
                "ret_log_1m_v1": np.zeros(n_rows),
                "ref_ret_1m_v1": np.zeros(n_rows),
            }
        )

    frames: dict[int, pd.DataFrame] = {}
    inst: dict[int, Any] = {}
    ref_of: dict[int, int] = {}
    sector_of: dict[int, str] = {}
    for iid, sym in ((100, "SPY"), (101, "QQQ")):
        etf = np.cumsum(common_ret + rng.normal(0, 3e-5, n_rows) * vol_mult)
        frames[iid] = frame(etf, 1.0, 0.5, 1.5)
        inst[iid] = dr.bb.Instrument(iid, sym, "ETF", 5e7, 100, 0.01)
    for i in range(1, dr.N_EQ + 1):
        beta = 0.6 + 0.8 * rng.random()
        idio = rng.normal(0.0, 3e-4, n_rows) * vol_mult
        frames[i] = frame(np.cumsum(beta * common_ret + idio), beta, 1.0 * spread, 4.0 * spread)
        inst[i] = dr.bb.Instrument(i, f"N{i}", "EQUITY", 3e6, 100, 0.01)
        ref_of[i] = 101 if i % 2 else 100
        sector_of[i] = dr.SECTORS[i % len(dr.SECTORS)]
    return rs.ScreenData(
        frames=frames,
        instruments=inst,
        cost_model=dr.CostModel(2.0, 0.003, 2.5),
        ref_of=ref_of,
        sector_of=sector_of,
    )


class _SignedScores:
    """N3: multiply each name's score by a slowly varying random sign (half-life 20 min), independent
    of returns; installed around ``rs.compute_scores`` for the length of one screen."""

    def __init__(self, seed: int):
        self.seed = seed
        self._orig = rs.compute_scores

    def __enter__(self):
        orig, seed = self._orig, self.seed

        def patched(data, ids, score, h, grid, max_age, names_min, alt=False):
            sc, miss = orig(data, ids, score, h, grid, max_age, names_min, alt=alt)
            rng = np.random.default_rng(np.random.SeedSequence([seed, 3]))
            phi = 0.5 ** (1.0 / 20.0)  # 1-minute columns approx.; persistence, not a calibration
            sg = np.sign(np.stack([_ar1(rng, sc.shape[1], phi) for _ in ids]))
            sg[sg == 0] = 1.0
            return sc * sg, miss

        rs.compute_scores = patched

    def __exit__(self, *exc):
        rs.compute_scores = self._orig


# ------------------------------------------------------------- one screen --


def _rule(draws: int):
    rule = dr.base_rule(BOOK, draws)
    rule.doc["sufficiency_v2"] = {
        "min_rebalances_per_session": 2,
        "sessions_min_qualifying": 5,
        "sessions_total": SESSIONS,
    }
    return rule


def screen(data: rs.ScreenData, rule, model: str, seed: int) -> dict[str, Any]:
    days = sorted(int(d) for d in data.session_close())
    if model == "N3":
        with _SignedScores(seed):
            out = v2.run_screen_v2(data, rule, days, short_circuit=True)
    else:
        out = v2.run_screen_v2(data, rule, days, short_circuit=True)
    rows = []
    for st, dec in zip(out["variants"], out["decisions"], strict=True):
        net = st.get("net_bps") or {}
        rows.append(
            {
                "id": st["id"],
                "outcome": dec["outcome"],
                "reason": dec["reason"],
                "criteria": dec["criteria"],
                "sessions_qualifying": st.get("sessions_qualifying"),
                "max_gap_min": st.get("max_gap_min"),
                "n_usable": st.get("n_usable"),
                "net_mean": net.get("mean"),
                "net_lower": net.get("lower"),
                "net_se": net.get("se"),
                "mde": net.get("mde"),
                "ess": net.get("ess"),
                "ic_mean": (st.get("rank_ic") or {}).get("mean"),
                "error": st.get("error"),
            }
        )
    return {"seed": seed, "variants": rows}


def _job(args: tuple) -> dict[str, Any]:
    kind, model, spread, seed, amp, draws = args
    t0 = time.time()
    if kind == "null":
        data = make_null(model, seed, spread)
    else:
        data = dr.make_data(kind, amp, seed, days=SESSIONS, spread=spread)
    res = screen(data, _rule(draws), model if kind == "null" else "N1", seed)
    res.update(
        {"model": model, "kind": kind, "amp": amp, "spread": spread, "sec": time.time() - t0}
    )
    return res


# ------------------------------------------------------------------- main --


def _load(p: Path) -> list[dict[str, Any]]:
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []


def _write(p: Path, recs: list[dict[str, Any]]) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(recs, default=float), encoding="utf-8")
    tmp.replace(p)


def frozen_identity(root: Path, manifest_path: Path) -> str:
    """The frozen identity, after recomputing it in this checkout; refuses on any difference."""
    ok, now, frozen = fz.identity_matches(root, manifest_path)
    if not ok:
        raise SystemExit(
            f"REFUSED: this checkout's identity {now[:16]} differs from the frozen {frozen[:16]} (run freeze.py verify)"
        )
    return frozen


def _run_unit(
    out: Path, unit: str, file: str, jobs: list[tuple], workers: int, identity: str
) -> None:
    path = out / file
    recs = _load(path)
    done = {r["seed"] for r in recs if r.get("unit") == unit}
    jobs = [j for j in jobs if j[3] not in done]
    if not jobs:
        return
    if workers <= 1:
        it = map(_job, jobs)
    else:
        ex = ProcessPoolExecutor(max_workers=workers)
        it = ex.map(_job, jobs)
    for rec in it:
        recs.append(ig.stamp(rec, identity, unit))
        _write(path, recs)
        print(unit, sum(r.get("unit") == unit for r in recs), flush=True)


def run_cells(
    out: Path, cells: list[str], r_from: int, r_to: int, workers: int, identity: str
) -> None:
    """Replications r_from .. r_to - 1 of each cell (resumable by seed; stamped and verified)."""
    for cell in cells:
        ci = CELLS.index(cell)
        model, sp = cell[:2], SPREADS[cell[2:]]
        jobs = [
            ("null", model, sp, BASE_SEED + 1000 * ci + r, 0.0, DRAWS) for r in range(r_from, r_to)
        ]
        _run_unit(out, cell, f"cell_{cell}.json", jobs, workers, identity)


def run_power(out: Path, reps: int, workers: int, identity: str) -> None:
    for ki, (kind, amp) in enumerate(POWER_CASES):
        jobs = [(kind, "N1", 1.0, POWER_BASE_SEED + 100 * ki + r, amp, DRAWS) for r in range(reps)]
        _run_unit(out, f"power_{kind}_{amp}", "power.json", jobs, workers, identity)


def manifest() -> dict[str, Any]:
    """Machine-readable accounting of every cell, replication count and seed the runner executes."""
    cells = []
    for ci, cell in enumerate(CELLS):
        gating = cell in GATING_CELLS
        cells.append(
            {
                "cell": cell,
                "model": cell[:2],
                "spread_regime": cell[2:],
                "spread_multiplier": SPREADS[cell[2:]],
                "role": "gating" if gating else "non-gating (stress)",
                "replications": R_FULL,
                "seed_first": BASE_SEED + 1000 * ci,
                "seed_last": BASE_SEED + 1000 * ci + R_FULL - 1,
                "checkpoint_replications_non_decisional": R_CHECKPOINT if gating else None,
            }
        )
    power = []
    for ki, (kind, amp) in enumerate(POWER_CASES):
        power.append(
            {
                "kind": kind,
                "amp": amp,
                "role": "non-gating (power-side sanity check)",
                "spread_multiplier": 1.0,
                "replications": POWER_REPS,
                "seed_first": POWER_BASE_SEED + 100 * ki,
                "seed_last": POWER_BASE_SEED + 100 * ki + POWER_REPS - 1,
            }
        )
    n_gating = sum(c["replications"] for c in cells if c["role"] == "gating")
    n_null = sum(c["replications"] for c in cells)
    n_power = sum(c["replications"] for c in power)
    return {
        "x-version": 1,
        "plan": "REL08_V2_CALIBRATION_PLAN_REV2.md",
        "sessions_per_screen": SESSIONS,
        "variants_per_screen": 7,
        "bootstrap_draws": DRAWS,
        "book": dict(BOOK),
        "bootstrap_seed": 20261020,
        "null_cells": cells,
        "power_cells": power,
        "sensitivity_cells": [],
        "timing_seeds": [TIMING_BASE_SEED + i for i in range(4)],
        "totals": {
            "gating_screens": n_gating,
            "null_screens": n_null,
            "power_screens": n_power,
            "all_screens": n_null + n_power,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "stage", choices=("all", "cell", "power", "finalize", "verify", "timing", "manifest")
    )
    ap.add_argument("--out", type=Path, default=HERE / "results_exec")
    ap.add_argument("--freeze", type=Path, default=None, help="execution_manifest.json")
    ap.add_argument("--root", type=Path, default=REPO)
    ap.add_argument("--cells", default=",".join(CELLS))
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--r-from", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args(argv)
    if a.stage == "manifest":
        print(json.dumps(manifest(), indent=1))
        return 0
    if a.stage == "timing":
        t0 = time.time()  # 4 screens; outputs discarded unread, only seconds are printed
        jobs = [("null", "N1", 1.0, TIMING_BASE_SEED + i, 0.0, DRAWS) for i in range(4)]
        with ProcessPoolExecutor(max_workers=max(1, a.workers)) as ex:
            secs = [round(r["sec"]) for r in ex.map(_job, jobs)]
        print(f"per-screen s {secs}; elapsed {time.time() - t0:.0f} s for 4 screens")
        return 0
    if a.freeze is None:
        raise SystemExit("REFUSED: --freeze execution_manifest.json is required")
    m = manifest()
    identity = frozen_identity(a.root, a.freeze)
    out = a.out
    if a.stage == "verify":
        v = ig.verify_outputs(out, m, identity)
        print(json.dumps(v, indent=1))
        return 0 if not v["problems"] else 1
    if a.stage == "finalize":
        res = ig.finalize(out, m, identity)
        print(res["gate"]["verdict"])
        return 0
    ram = fz.available_ram_gb()
    if ram is not None and ram < fz.MIN_FREE_RAM_GB:
        raise SystemExit(f"REFUSED: free RAM {ram:.1f} GB < {fz.MIN_FREE_RAM_GB} GB")
    started = time.time()
    counts = ig.guard_start(out, m, identity)
    ig.audit(
        out,
        "start",
        identity,
        {"stage": a.stage, "workers": a.workers, "valid_counts_at_start": counts},
    )
    if a.stage == "cell":
        cells = a.cells.split(",")
        if any(c not in CELLS for c in cells):
            raise SystemExit(f"cells must be among {CELLS}")
        run_cells(out, cells, a.r_from, a.reps or R_FULL, a.workers, identity)
    elif a.stage == "power":
        run_power(out, POWER_REPS, a.workers, identity)
    else:  # all: the whole fixed-sample plan; no step depends on any outcome
        run_cells(out, GATING_CELLS, 0, R_CHECKPOINT, a.workers, identity)
        if not (out / "checkpoint_100_operational.json").is_file():
            ig.checkpoint_operational(out, m, identity, fz.available_ram_gb(), started)
        run_cells(out, GATING_CELLS, R_CHECKPOINT, R_FULL, a.workers, identity)
        run_cells(out, [c for c in CELLS if c not in GATING_CELLS], 0, R_FULL, a.workers, identity)
        run_power(out, POWER_REPS, a.workers, identity)
        ig.finalize(out, m, identity)
    return 0


if __name__ == "__main__":
    sys.exit(main())

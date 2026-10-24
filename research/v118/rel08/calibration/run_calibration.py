"""REL-08 v2 synthetic-null calibration runner (plan: ../REL08_V2_CALIBRATION_PLAN.md).

PREPARED, NOT RUN.  Reads no real data (every frame is generated in memory), writes only under --out.
Run only after the plan PR and the implementation (iap.research.rel08_screen_v2) are in main.

    python run_calibration.py cell   --out DIR [--cells N1S1,...] [--reps 200] [--workers 2]
    python run_calibration.py power  --out DIR [--reps 30]
    python run_calibration.py timing                      # 3 replications of N1S1, prints elapsed only
    python run_calibration.py report --out DIR            # tables from the per-cell JSON

Seeds (plan section 3): dataset seed = 20261100 + 1000 * cell_index + r; bootstrap seed = the rule's.
Resumable: a finished (cell, r) record is never recomputed; the cells and seeds are fixed in this file.
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

MODELS = ("N1", "N2", "N3", "N4")
SPREADS = {"S1": 1.0, "S4": 4.0}
CELLS = [f"{m}{s}" for m in MODELS for s in SPREADS]  # N1S1, N1S4, N2S1, ... (plan order)
BASE_SEED = 20261100
POWER_BASE_SEED = 20268000
TIMING_BASE_SEED = 20269000
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
            out = v2.run_screen_v2(data, rule, days)
    else:
        out = v2.run_screen_v2(data, rule, days)
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


def run_cells(out: Path, cells: list[str], reps: int, workers: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for cell in cells:
        ci = CELLS.index(cell)
        model, sp = cell[:2], SPREADS[cell[2:]]
        path = out / f"cell_{cell}.json"
        recs = _load(path)
        done = {r["seed"] for r in recs}
        jobs = [
            ("null", model, sp, BASE_SEED + 1000 * ci + r, 0.0, DRAWS)
            for r in range(reps)
            if BASE_SEED + 1000 * ci + r not in done
        ]
        if workers <= 1:
            it = map(_job, jobs)
        else:
            ex = ProcessPoolExecutor(max_workers=workers)
            it = ex.map(_job, jobs)
        for rec in it:
            rec["cell"] = cell
            recs.append(rec)
            _write(path, recs)
            print(cell, len(recs), flush=True)


def run_power(out: Path, reps: int, workers: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cases = [("revert", a) for a in (6.0, 12.0, 24.0)] + [("lead", a) for a in (0.3, 0.6)]
    path = out / "power.json"
    recs = _load(path)
    done = {(r["kind"], r["amp"], r["seed"]) for r in recs}
    for ki, (kind, amp) in enumerate(cases):
        jobs = [
            (kind, "N1", 1.0, POWER_BASE_SEED + 100 * ki + r, amp, DRAWS)
            for r in range(reps)
            if (kind, amp, POWER_BASE_SEED + 100 * ki + r) not in done
        ]
        with ProcessPoolExecutor(max_workers=max(1, workers)) as ex:
            for rec in ex.map(_job, jobs):
                recs.append(rec)
                _write(path, recs)
                print("power", kind, amp, len(recs), flush=True)


def cp_upper(k: int, n: int, conf: float = 0.95) -> float:
    """One-sided Clopper-Pearson upper bound (bisection on the binomial CDF)."""
    if n == 0:
        return float("nan")
    if k >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        cdf = sum(math.comb(n, i) * mid**i * (1 - mid) ** (n - i) for i in range(k + 1))
        lo, hi = (mid, hi) if cdf > 1 - conf else (lo, mid)
    return hi


def report(out: Path) -> str:
    lines = [
        "| cell | reps | family events | family rate | family CP upper | variant continue | variant C3 pass | pooled variant C3 upper |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for cell in CELLS:
        recs = _load(out / f"cell_{cell}.json")
        if not recs:
            continue
        n = len(recs)
        fam = sum(
            any(v["outcome"] in ("continue", "passed_not_expanded") for v in r["variants"])
            for r in recs
        )
        vc = sum(
            v["outcome"] in ("continue", "passed_not_expanded") for r in recs for v in r["variants"]
        )
        c3 = sum(bool(v["criteria"]["C3"]) for r in recs for v in r["variants"])
        nv = sum(len(r["variants"]) for r in recs)
        lines.append(
            f"| {cell} | {n} | {fam} | {fam / n:.4f} | {cp_upper(fam, n):.4f} | {vc} | {c3} | {cp_upper(c3, nv):.4f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("stage", choices=("cell", "power", "timing", "report"))
    ap.add_argument("--out", type=Path, default=HERE / "results")
    ap.add_argument("--cells", default=",".join(CELLS))
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args(argv)
    if a.stage == "cell":
        cells = a.cells.split(",")
        if any(c not in CELLS for c in cells):
            raise SystemExit(f"cells must be among {CELLS}")
        run_cells(a.out, cells, a.reps or 200, a.workers)
    elif a.stage == "power":
        run_power(a.out, a.reps or 30, a.workers)
    elif a.stage == "timing":
        t0 = time.time()
        for i in range(3):  # outputs discarded unread; only the elapsed time is printed
            _job(("null", "N1", 1.0, TIMING_BASE_SEED + i, 0.0, DRAWS))
        print(f"elapsed {time.time() - t0:.0f} s for 3 replications")
    else:
        print(report(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

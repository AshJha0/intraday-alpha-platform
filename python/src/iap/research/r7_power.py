"""Power analysis for the DRAFT R7 confirmatory pre-registration (v1.15).

Before R7-CONFIRM-1 is signed, this answers: on the 7 tier-1 holdout
sessions (and on tier 1 + tier 2 = 47 sessions), what is the chance that
each drafted hypothesis passes the drafted gate if its true effect equals
the in-sample estimate, and what is the smallest effect it would pass with
80 % probability (the minimum detectable effect, MDE)?

It reads only already-computed summary statistics:
``research/r7/r7_power_inputs.json`` (extracted from the in-sample and v1.8
holdout result files; each source pinned by sha256) and the committed
ledger ``research/experiments.json`` (Bonferroni threshold, distinct
experiments for the deflated-Sharpe N).  No market data is read.

**Gate modelled (the draft's):** HAC t >= the ledger Bonferroni threshold in
force AND deflated Sharpe > 0.95, both on the holdout sessions.

**Model (assumptions, stated in the report).**

* Per-session effect ``x_s ~ N(mu, sigma_s^2)`` independently across
  sessions: between-session variance is the dominant source of
  uncertainty.  ``sigma_s`` for an EQ alpha is the sd of its in-sample
  walk-forward fold ICs scaled to one session (``sqrt(test sessions /
  folds)``); for the auction cell it is the session-clustered SE times
  ``sqrt(active sessions)``.
* The HAC t the gate computes uses 5-minute buckets without day clusters,
  so its standard error is the WITHIN-session one: ``se_hac(S) =
  |IC_in / t_in| * sqrt(S_in / S)`` (it shrinks with buckets, i.e. with
  sessions).  The estimate itself varies by both: ``IC_hat ~ N(mu,
  sigma_s^2 / S + se_hac(S)^2)``.  HAC power = ``P(IC_hat >= thr *
  se_hac)``.
* Deflated Sharpe is modelled on the per-session series (``SR = mu /
  sigma_s``, ``n = S``, ``N`` = ledger distinct experiments + 1, ``V = 1 /
  (S - 1)``, normal moments): the pass needs ``SR_hat >= SR*`` with
  ``PSR(SR*; SR0) = 0.95``; ``SR_hat ~ N(SR, (1 + SR^2 / 2) / (S - 1))``.
* H1 and H3 statistics are session-clustered (one value per session, as
  registered for the auction study): ``t = mean / (sigma_s / sqrt(S))``.
* Joint power is at most the smaller of the two components (reported as
  ``joint_upper``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import NormalDist, stdev

from iap.validation.deflated import expected_max_sharpe, probabilistic_sharpe_ratio

R7_POWER_VERSION = 1
TIER1_SESSIONS = 7
TIER2_SESSIONS = 7 + 40
TARGET_POWER = 0.8
DSR_LEVEL = 0.95
#: H1 placeholder grid: standardised per-session effect mu / sigma_s.
H1_PLACEHOLDER_D = (0.25, 0.5, 1.0, 2.0)

_N = NormalDist()


def hac_power(mu: float, sigma_s: float, se1: float, sessions: int, thr: float) -> float:
    """``P(IC_hat >= thr * se_hac(S))`` with ``se_hac = se1 / sqrt(S)``
    (``se1`` = within-session SE at one session) and ``IC_hat ~ N(mu,
    sigma_s^2/S + se_hac^2)``."""
    se = se1 / math.sqrt(sessions)
    sd = math.sqrt(sigma_s**2 / sessions + se**2)
    return 1.0 - _N.cdf((thr * se - mu) / sd)


def hac_mde(sigma_s: float, se1: float, sessions: int, thr: float, power: float = TARGET_POWER):
    se = se1 / math.sqrt(sessions)
    sd = math.sqrt(sigma_s**2 / sessions + se**2)
    return thr * se + _N.inv_cdf(power) * sd


def dsr_threshold_sr(sessions: int, n_trials: int, level: float = DSR_LEVEL) -> float:
    """Per-session Sharpe ``SR*`` at which ``PSR(SR*; SR0) = level``."""
    sr0 = expected_max_sharpe(n_trials, 1.0 / (sessions - 1))
    lo, hi = sr0, sr0 + 50.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if probabilistic_sharpe_ratio(mid, sr0, sessions) >= level:
            hi = mid
        else:
            lo = mid
    return hi


def dsr_power(sr: float, sessions: int, n_trials: int) -> float:
    x = dsr_threshold_sr(sessions, n_trials)
    sd = math.sqrt((1.0 + sr**2 / 2.0) / (sessions - 1))
    return 1.0 - _N.cdf((x - sr) / sd)


def dsr_mde_sr(sessions: int, n_trials: int, power: float = TARGET_POWER) -> float:
    lo, hi = 0.0, 100.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if dsr_power(mid, sessions, n_trials) >= power:
            hi = mid
        else:
            lo = mid
    return hi


def _cell(mu, sigma_s, se1, sessions, thr, n_trials) -> dict:
    hp = hac_power(mu, sigma_s, se1, sessions, thr)
    dp = dsr_power(mu / sigma_s, sessions, n_trials) if sigma_s > 0 else float("nan")
    mde_h = hac_mde(sigma_s, se1, sessions, thr)
    mde_d = dsr_mde_sr(sessions, n_trials) * sigma_s
    return {
        "sessions": sessions,
        "power_hac": hp,
        "power_dsr": dp,
        "joint_upper": min(hp, dp),
        "mde_hac": mde_h,
        "mde_dsr": mde_d,
        "mde_joint": max(mde_h, mde_d),
    }


def _clustered_cell(mu, sigma_s, sessions, thr, n_trials) -> dict:
    """Session-clustered statistic (one value per session): ``t = mean /
    (sigma_s / sqrt(S)) ~ N(mu sqrt(S) / sigma_s, 1)``; DSR as in :func:`_cell`."""
    root = math.sqrt(sessions)
    hp = 1.0 - _N.cdf(thr - mu * root / sigma_s)
    dp = dsr_power(mu / sigma_s, sessions, n_trials)
    mde_h = (thr + _N.inv_cdf(TARGET_POWER)) * sigma_s / root
    mde_d = dsr_mde_sr(sessions, n_trials) * sigma_s
    return {
        "sessions": sessions,
        "power_hac": hp,
        "power_dsr": dp,
        "joint_upper": min(hp, dp),
        "mde_hac": mde_h,
        "mde_dsr": mde_d,
        "mde_joint": max(mde_h, mde_d),
    }


def _recommend(cells: dict[int, dict], effect: float) -> str:
    t1, t2 = cells[TIER1_SESSIONS], cells[TIER2_SESSIONS]
    if effect <= 0:
        return "do not test as drafted (in-sample effect has the wrong sign)"
    if t1["joint_upper"] >= TARGET_POWER:
        return "test now (tier 1)"
    if t2["joint_upper"] >= TARGET_POWER:
        return "wait for tier 2"
    return "underpowered even on tier 2"


def ledger_gate(experiments_path: str | Path) -> dict:
    d = json.loads(Path(experiments_path).read_text())
    return {
        "total_looks": int(d["total_experiments"]),
        "t_threshold": float(d["bonferroni_t_threshold"]),
        "dsr_n_trials": int(d["distinct_experiments"]) + 1,
    }


def analyse(inputs: dict, gate: dict, h1: dict | None = None) -> dict:
    thr, n_tr = gate["t_threshold"], gate["dsr_n_trials"]
    sessions = (TIER1_SESSIONS, TIER2_SESSIONS)
    out: dict = {"version": R7_POWER_VERSION, "gate": gate, "hypotheses": {}}

    h4 = {}
    for a, v in inputs["H4"]["alphas"].items():
        ins = v["insample"]
        k = (ins["sessions"] - 1) / ins["test_folds"]  # test sessions per fold
        sigma_s = stdev(ins["fold_ic"]) * math.sqrt(k)
        se1 = abs(ins["gate_ic"] / ins["gate_t"]) * math.sqrt(ins["sessions"])
        row = {"horizon": v["horizon"], "sigma_session": sigma_s, "se_hac_1session": se1}
        for label, mu in (
            ("insample", ins["gate_ic"]),
            ("v18_holdout", v["holdout_v18"]["ic"]),
        ):
            cells = {s: _cell(mu, sigma_s, se1, s, thr, n_tr) for s in sessions}
            row[label] = {"effect": mu, "cells": cells}
        row["recommendation"] = _recommend(row["insample"]["cells"], ins["gate_ic"])
        row["recommendation_if_decayed"] = _recommend(
            row["v18_holdout"]["cells"], v["holdout_v18"]["ic"]
        )
        h4[a] = row
    out["hypotheses"]["H4"] = h4

    h3in = inputs["H3"]
    sig3 = h3in["se_bps"] * math.sqrt(h3in["active_sessions"])
    act = h3in["active_sessions"] / h3in["insample_test_sessions"]
    h3 = {"sigma_session_bps": sig3, "active_fraction": act, "cells": {}}
    for s in sessions:
        sa = max(3, round(s * act))
        c = _clustered_cell(h3in["mean_net_bps"], sig3, sa, thr, n_tr)
        c["sessions"] = s
        c["active_sessions"] = sa
        n = h3in["decisions_per_session"] * s
        r = thr + _N.inv_cdf(TARGET_POWER)
        c["mde_ic_decisions"] = n
        c["mde_ic"] = r / math.sqrt(n - 2 + r * r)
        h3["cells"][s] = c
    h3["effect_bps"] = h3in["mean_net_bps"]
    h3["recommendation"] = _recommend(h3["cells"], h3in["mean_net_bps"])
    out["hypotheses"]["H3"] = h3

    if h1:
        sig1 = h1["session_sd_bps"]
        cells = {s: _clustered_cell(h1["mean_edge_bps"], sig1, s, thr, n_tr) for s in sessions}
        out["hypotheses"]["H1"] = {
            "placeholder": False,
            "effect_bps": h1["mean_edge_bps"],
            "sigma_session_bps": sig1,
            "cells": cells,
            "recommendation": _recommend(cells, h1["mean_edge_bps"]),
        }
    else:
        grid = {}
        for d in H1_PLACEHOLDER_D:
            grid[d] = {s: _clustered_cell(d, 1.0, s, thr, n_tr) for s in sessions}
        out["hypotheses"]["H1"] = {
            "placeholder": True,
            "note": "maker_real summary not written; effect as standardised d = mu / sigma_s",
            "grid": grid,
            "mde_d": {s: grid[H1_PLACEHOLDER_D[0]][s]["mde_joint"] for s in sessions},
            "recommendation": "PENDING maker_real summary (rerun with --h1-mean-bps/--h1-sd-bps)",
        }
    return out


def prereg_status(r7_dir: str | Path) -> dict:
    """Which R7-CONFIRM-1 prereg file exists and whether it is signed.

    Prefers a signed non-DRAFT ``r7_confirm_prereg*.json``; falls back to the DRAFT.
    """
    r7 = Path(r7_dir)
    for f in sorted(r7.glob("r7_confirm_prereg*.json")):
        if "DRAFT" in f.name:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        signed = d.get("signed") is True and isinstance(d.get("signature"), dict)
        return {"file": f"research/r7/{f.name}", "signed": signed}
    draft = r7 / "r7_confirm_prereg_DRAFT.json"
    if draft.exists():
        return {"file": f"research/r7/{draft.name}", "signed": False}
    return {"file": None, "signed": False}


def _prereg_line(pre: dict | None) -> str:
    if not pre or not pre.get("file"):
        return "No R7-CONFIRM-1 pre-registration file found in `research/r7/`:"
    state = "signed and registered" if pre["signed"] else "DRAFT, unsigned"
    return f"Recorded in `{pre['file']}` ({state}):"


def _p(x: float) -> str:
    return f"{x:.2f}"


def render_markdown(res: dict) -> str:
    g = res["gate"]
    t1, t2 = TIER1_SESSIONS, TIER2_SESSIONS
    L = [
        "# R7 power analysis (pre-registration R7-CONFIRM-1"
        + (", signed)" if (res.get("prereg") or {}).get("signed") else ", DRAFT)"),
        "",
        "Generated by `tools/r7_power.py` (`iap.research.r7_power`, version "
        f"{res['version']}); rerun it, do not edit the tables by hand. Inputs: "
        "`research/r7/r7_power_inputs.json` (summary statistics extracted from the "
        "in-sample and v1.8 holdout result files, each source pinned by sha256; no "
        "market data read) and the committed ledger `research/experiments.json`.",
        "",
        "This is a planning aid for the owner before sign-off. It does not change the "
        "drafted hypotheses, takes no look and charges nothing to the ledger.",
        "",
        "## Gate modelled",
        "",
        f"- HAC t >= {g['t_threshold']:.3f}: the Bonferroni threshold of the committed "
        f"ledger ({g['total_looks']:,} looks). The R7 looks themselves will raise it "
        "slightly; a threshold fixed in the pre-registration (`threshold_policy="
        '"prereg"`) would freeze it.',
        f"- Deflated Sharpe > {DSR_LEVEL}: N = {g['dsr_n_trials']} (ledger distinct "
        "experiments + 1), V = 1/(S-1), normal moments, on the per-session series.",
        f"- Tier 1 = {t1} holdout sessions; tier 2 = {t2} (7 + 40 approved dates).",
        "- Power = P(pass | true effect); MDE = smallest effect passed with 80 % "
        "probability. `joint <=` is the smaller of the two component powers (an upper "
        "bound on passing both).",
        "",
        "## Assumptions",
        "",
        "1. **Between-session variance dominant.** Per-session effects are independent "
        "draws around the true effect; their sd is taken from the in-sample "
        "walk-forward fold ICs (4 folds over 6 test sessions, scaled by sqrt(1.5) to "
        "one session) for EQ, and from the session-clustered SE for the auction cell. "
        "Four folds give a very rough sd: treat every number as order of magnitude.",
        "2. **HAC.** The gate's t uses 5-minute buckets without day clusters, so its SE "
        "is the within-session one and shrinks as 1/sqrt(sessions) (calibrated from the "
        "in-sample IC / t). The estimate's real spread also includes the "
        "between-session term, which the HAC SE ignores; the model includes both.",
        "3. **Effect sizes.** Primary: the in-sample (2019-20, 7 sessions) gate IC. "
        "The v1.8 2026 holdout found the microstructure ICs at a third to a half of "
        "in-sample, so a second column uses the v1.8 holdout IC as a decayed effect.",
        "4. **Deflated Sharpe on a statistic.** The EQ taker strategies made no trade "
        "at 1x cost in-sample or in the v1.8 holdout, so a P&L DSR is undefined; the "
        "table models DSR on the per-session IC series, which the owner adopted for H4.",
        "",
        "## Owner decisions (2026-10-11)",
        "",
        _prereg_line(res.get("prereg")),
        "",
        "- **H4 tier 1 (7 sessions) = EQ10 @1s and EQ05 @1s only.** They are the two "
        "with joint power >= 0.8 at their in-sample IC.",
        "- **EQ01, EQ02, EQ04 deferred to tier 1 + tier 2 (47 sessions)** under a later "
        "pre-registration: joint power on 7 sessions 0.26 / 0.49 / 0.04 at the "
        "in-sample IC, about 1.00 on 47.",
        "- **EQ07 dropped:** the drafted sign (+) contradicts the in-sample IC and the "
        "v1.8 holdout; flipping it after seeing results would fit seen data. It may "
        "return as a new idea with its own pre-registration.",
        "- **H3 (auction) dropped from R7:** underpowered even on 47 sessions; needs a "
        "redesign (pooled / longer sample).",
        "- **DSR series for H4:** the per-session IC series (defined every session); "
        "net-of-cost P&L is reported alongside, descriptive only. H1 keeps its PENDING "
        "placeholder; its DSR series is decided after the maker summary and a rerun.",
        "",
        "The tables below are the analysis the decisions were taken on; they still "
        "list every drafted hypothesis.",
        "",
        "## H4 (EQ alphas, pooled uncrossed IC > 0)",
        "",
        "| alpha | effect (IC) | sd/session | S | power HAC | power DSR | joint <= "
        "| MDE HAC | MDE DSR | MDE joint |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a, r in res["hypotheses"]["H4"].items():
        for label in ("insample", "v18_holdout"):
            blk = r[label]
            for s in (t1, t2):
                c = blk["cells"][s]
                tag = "in-sample" if label == "insample" else "v1.8 decayed"
                L.append(
                    f"| {a} @{r['horizon']} ({tag}) | {blk['effect']:+.4f} | "
                    f"{r['sigma_session']:.4f} | {s} | {_p(c['power_hac'])} | "
                    f"{_p(c['power_dsr'])} | {_p(c['joint_upper'])} | {c['mde_hac']:.4f} | "
                    f"{c['mde_dsr']:.4f} | {c['mde_joint']:.4f} |"
                )
    L += ["", "| alpha | recommendation (in-sample effect) | if decayed to v1.8 |", "|---|---|---|"]
    for a, r in res["hypotheses"]["H4"].items():
        L.append(f"| {a} | {r['recommendation']} | {r['recommendation_if_decayed']} |")

    h3 = res["hypotheses"]["H3"]
    L += [
        "",
        "## H3 (AUC01 closing cross)",
        "",
        "No in-sample IC exists for AUC01: the in-sample statistic is the "
        "session-clustered mean net return of `C-300s` "
        f"({h3['effect_bps']:+.1f} bp, sd/session {h3['sigma_session_bps']:.1f} bp, "
        f"traded on {h3['active_fraction']:.0%} of test sessions). Power is computed for "
        "that statistic on the expected active sessions. The drafted IC test has 3 "
        "decisions (one per symbol) per session; `MDE IC` is the IC the HAC gate would "
        "need on those decisions alone.",
        "",
        "| S | active S | power t-gate | power DSR | joint <= | MDE (bp) | MDE IC (n decisions) |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in (t1, t2):
        c = h3["cells"][s]
        L.append(
            f"| {s} | {c['active_sessions']} | {_p(c['power_hac'])} | {_p(c['power_dsr'])} | "
            f"{_p(c['joint_upper'])} | {c['mde_joint']:.1f} | {c['mde_ic']:.2f} "
            f"({c['mde_ic_decisions']}) |"
        )
    L += ["", f"Recommendation: **{h3['recommendation']}**."]

    h1 = res["hypotheses"]["H1"]
    L += ["", "## H1 (maker)", ""]
    if h1["placeholder"]:
        L += [
            "**PLACEHOLDER**: the maker_real in-sample summary does not exist yet. The "
            "table gives power for a standardised per-session effect d = mean edge / "
            "per-session sd; rerun `tools/r7_power.py --h1-mean-bps M --h1-sd-bps SD` "
            "once the summary is written.",
            "",
            "| d | S | power t-gate | power DSR | joint <= |",
            "|---:|---:|---:|---:|---:|",
        ]
        for d, cells in h1["grid"].items():
            for s in (t1, t2):
                c = cells[s]
                L.append(
                    f"| {d} | {s} | {_p(c['power_hac'])} | {_p(c['power_dsr'])} | "
                    f"{_p(c['joint_upper'])} |"
                )
        L += [
            "",
            f"MDE (d, 80 %): {h1['mde_d'][t1]:.2f} on tier 1, {h1['mde_d'][t2]:.2f} on tier 2.",
        ]
    else:
        L += [
            f"Effect {h1['effect_bps']:+.2f} bp, sd/session {h1['sigma_session_bps']:.2f} bp.",
            "",
            "| S | power t-gate | power DSR | joint <= | MDE (bp) |",
            "|---:|---:|---:|---:|---:|",
        ]
        for s in (t1, t2):
            c = h1["cells"][s]
            L.append(
                f"| {s} | {_p(c['power_hac'])} | {_p(c['power_dsr'])} | "
                f"{_p(c['joint_upper'])} | {c['mde_joint']:.2f} |"
            )
    L += ["", f"Recommendation: **{h1['recommendation']}**.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Cross-sectional design (v1.15 multi-symbol plan): N symbols x S sessions.
# ---------------------------------------------------------------------------

#: Symbols in the in-sample artefacts the session sd is calibrated on.
REF_SYMBOLS = 3
MULTI_N = (3, 8, 16)
MULTI_RHO = (0.2, 0.5, 0.8)
MULTI_ALPHAS = ("EQ01", "EQ02", "EQ04")


def cross_section_var_ratio(n: int, rho: float, n_ref: int = REF_SYMBOLS) -> float:
    """Variance of an equal-weight N-symbol pooled per-session IC relative to
    the ``n_ref``-symbol pool, when every symbol's per-session IC deviation
    has the same variance and pairwise correlation ``rho`` (a common market
    factor): ``Var_N = sigma_1^2 (rho + (1 - rho) / N)``.  With ``rho = 1``
    more symbols add nothing; with ``rho = 0`` the variance falls as 1/N."""
    if not 0.0 <= rho <= 1.0:
        raise ValueError("rho must be in [0, 1]")
    if n < 1 or n_ref < 1:
        raise ValueError("symbol counts must be >= 1")
    return (rho + (1.0 - rho) / n) / (rho + (1.0 - rho) / n_ref)


def multi_symbol_cell(
    mu: float,
    sigma_ref: float,
    se1_ref: float,
    n_symbols: int,
    rho: float,
    sessions: int,
    thr: float,
    n_trials: int,
    n_ref: int = REF_SYMBOLS,
) -> dict:
    """Power / MDE of the drafted gate for the pooled IC over ``n_symbols``.

    ``sigma_ref`` and ``se1_ref`` are the per-session sd and the one-session
    within-session HAC SE measured on the ``n_ref``-symbol pool; both are
    rescaled by ``sqrt(cross_section_var_ratio)`` (the same ``rho`` is assumed
    for the between-session and the within-session bucket noise).  ``mu`` is
    the pooled IC of the N-symbol universe."""
    f = math.sqrt(cross_section_var_ratio(n_symbols, rho, n_ref))
    c = _cell(mu, sigma_ref * f, se1_ref * f, sessions, thr, n_trials)
    c.update({"n_symbols": n_symbols, "rho": rho, "sd_scale": f, "sigma_session": sigma_ref * f})
    return c


def breakeven_rho(
    mu: float,
    sigma_ref: float,
    se1_ref: float,
    n_symbols: int,
    sessions: int,
    thr: float,
    n_trials: int,
    power: float = TARGET_POWER,
) -> float | None:
    """Largest ``rho`` at which the joint (upper-bound) power at ``n_symbols``
    reaches ``power``; ``None`` if not even ``rho = 0`` reaches it."""

    def ok(r: float) -> bool:
        c = multi_symbol_cell(mu, sigma_ref, se1_ref, n_symbols, r, sessions, thr, n_trials)
        return c["joint_upper"] >= power

    if not ok(0.0):
        return None
    if ok(1.0):
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def symbol_dispersion(ic_by_symbol: dict[str, float]) -> dict:
    v = list(ic_by_symbol.values())
    return {
        "by_symbol": dict(sorted(ic_by_symbol.items())),
        "mean": sum(v) / len(v),
        "sd": stdev(v) if len(v) > 1 else float("nan"),
        "min": min(v),
        "max": max(v),
    }


def _h4_scales(ins: dict) -> tuple[float, float]:
    k = (ins["sessions"] - 1) / ins["test_folds"]
    sigma_s = stdev(ins["fold_ic"]) * math.sqrt(k)
    se1 = abs(ins["gate_ic"] / ins["gate_t"]) * math.sqrt(ins["sessions"])
    return sigma_s, se1


def analyse_multi_symbol(
    inputs: dict,
    gate: dict,
    alphas: tuple[str, ...] = MULTI_ALPHAS,
    ns: tuple[int, ...] = MULTI_N,
    rhos: tuple[float, ...] = MULTI_RHO,
    sessions: int = TIER1_SESSIONS,
) -> dict:
    thr, n_tr = gate["t_threshold"], gate["dsr_n_trials"]
    out: dict = {
        "version": R7_POWER_VERSION,
        "gate": gate,
        "sessions": sessions,
        "ref_symbols": REF_SYMBOLS,
        "ns": list(ns),
        "rhos": list(rhos),
        "dsr_mde_sr": dsr_mde_sr(sessions, n_tr),
        "alphas": {},
    }
    for a in alphas:
        v = inputs["H4"]["alphas"][a]
        ins = v["insample"]
        sigma_s, se1 = _h4_scales(ins)
        disp = ins.get("oos_ic_by_symbol")
        row: dict = {
            "horizon": v["horizon"],
            "sigma_session_ref": sigma_s,
            "se_hac_1session_ref": se1,
            "dispersion": symbol_dispersion(disp) if disp else None,
        }
        for label, mu in (("insample", ins["gate_ic"]), ("v18_holdout", v["holdout_v18"]["ic"])):
            cells = [
                multi_symbol_cell(mu, sigma_s, se1, n, rho, sessions, thr, n_tr)
                for rho in rhos
                for n in ns
            ]
            row[label] = {
                "effect": mu,
                "cells": cells,
                "breakeven_rho": {
                    n: breakeven_rho(mu, sigma_s, se1, n, sessions, thr, n_tr) for n in ns
                },
                "rho0_max_n": multi_symbol_cell(
                    mu, sigma_s, se1, max(ns), 0.0, sessions, thr, n_tr
                ),
            }
        out["alphas"][a] = row
    return out


def render_multi_symbol_markdown(res: dict) -> str:
    g, s, nref = res["gate"], res["sessions"], res["ref_symbols"]
    L = [
        "# R7 multi-symbol power (cross-sectional design)",
        "",
        "Generated by `tools/r7_power.py --multi-symbol` (`iap.research.r7_power`, "
        f"version {res['version']}); rerun it, do not edit the tables by hand. Same "
        "inputs and gate as `R7_POWER.md`; no market data read. The plan and the "
        "recommendation are in `R7_MULTI_SYMBOL_PLAN.md`.",
        "",
        f"Gate: HAC t >= {g['t_threshold']:.3f} AND deflated Sharpe > {DSR_LEVEL} "
        f"(N = {g['dsr_n_trials']}) on the per-session pooled IC series, S = {s} "
        f"holdout sessions. On {s} sessions the DSR leg alone needs a per-session "
        f"Sharpe (mean / sd of the pooled IC) of {res['dsr_mde_sr']:.2f} for 80 % power.",
        "",
        "Model: IC of symbol i in session s = mu + c_s + e_is with Var(c_s + e_is) = "
        "sigma_1^2 and correlation rho across symbols (common market factor). The "
        "N-symbol pooled per-session series has sd sigma_N = sigma_ref * sqrt((rho + "
        f"(1-rho)/N) / (rho + (1-rho)/{nref})), calibrated on the measured {nref}-symbol "
        "(AAPL, MSFT, QQQ) fold sd; the within-session HAC SE is rescaled the same way. "
        "mu (the pooled IC of the new universe) is set to the 3-symbol value; the "
        "dispersion table shows how far a single symbol can sit from it.",
        "",
        "## Cross-symbol dispersion (in-sample 2019-20, OOS IC per symbol)",
        "",
        "| alpha | AAPL | MSFT | QQQ | mean | sd across symbols |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for a, r in res["alphas"].items():
        d = r["dispersion"]
        if d is None:
            continue
        b = d["by_symbol"]
        L.append(
            f"| {a} @{r['horizon']} | "
            + " | ".join(f"{b.get(k, float('nan')):+.4f}" for k in ("AAPL", "MSFT", "QQQ"))
            + f" | {d['mean']:+.4f} | {d['sd']:.4f} |"
        )
    L += [
        "",
        f"## Power and MDE on {s} sessions",
        "",
        "| alpha | effect (IC) | rho | N | sd/session | power HAC | power DSR | joint <= "
        "| MDE joint |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a, r in res["alphas"].items():
        for label, tag in (("insample", "in-sample"), ("v18_holdout", "v1.8 decayed")):
            blk = r[label]
            for c in blk["cells"]:
                L.append(
                    f"| {a} @{r['horizon']} | {tag} {blk['effect']:+.4f} | {c['rho']:.1f} | "
                    f"{c['n_symbols']} | {c['sigma_session']:.4f} | {_p(c['power_hac'])} | "
                    f"{_p(c['power_dsr'])} | {_p(c['joint_upper'])} | {c['mde_joint']:.4f} |"
                )
    nmax = max(res["ns"])
    L += [
        "",
        "## Break-even rho (largest rho at which joint power reaches 0.80)",
        "",
        "| alpha | effect (IC) | "
        + " | ".join(f"N = {n}" for n in res["ns"])
        + f" | joint at N = {nmax}, rho = 0 |",
        "|---|---|" + "---:|" * (len(res["ns"]) + 1),
    ]
    for a, r in res["alphas"].items():
        for label, tag in (("insample", "in-sample"), ("v18_holdout", "v1.8 decayed")):
            blk = r[label]
            be = [blk["breakeven_rho"][n] for n in res["ns"]]
            L.append(
                f"| {a} @{r['horizon']} | {tag} {blk['effect']:+.4f} | "
                + " | ".join("never" if x is None else f"{x:.2f}" for x in be)
                + f" | {_p(blk['rho0_max_n']['joint_upper'])} |"
            )
    L.append("")
    return "\n".join(L)

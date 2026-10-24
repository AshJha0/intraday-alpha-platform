"""R5 real-data dependence audit: the runner for ``R5_DEPENDENCE_PLAN.md`` (v1.19 / v2.0 stage 1).

NOTHING HERE HAS BEEN RUN ON REAL DATA.  The plan is predeclared in
``research/v119/R5_DEPENDENCE_PLAN.md``; this file is the code that will run it
later, plus a synthetic self-test.  It never opens a market-data file: it
consumes a *session-value panel* (one value per session and hypothesis, the
same session statistics the confirmatory gate reads for the registered labels)
that a separate, guarded producer writes, and it refuses a panel that names a
session the holdout registry does not list as ``in_sample`` (or ``spent`` by
the prereg the panel names).  Sealed (``unread``) sessions are refused outright.

    # synthetic self-test (no registry, no ledger, no data)
    python research/v119/r5_dependence.py selftest --out <dir>
    # the audit (later; one ledger look; registry checked first)
    python research/v119/r5_dependence.py run --panel <panel.json> --out research/v119

The result document has two separated parts with separate conclusions (I3):

* **Part A, diagnostics**: lag 1-5 autocorrelations with intervals, the
  common-factor loading (first principal component), the stationarity split
  (2019-21 vs 2025-26), and the session-block permutation diagnostic.
  Descriptive; it can reveal a violation; it never establishes domain
  membership, and non-detection is never called evidence of independence.
* **Part B, affirmative evidence for domain membership**: the R0 section 4
  bands (bias-corrected excess kurtosis with its 95 % interval, the
  design-independence guarantees from the manifest, the lag-1 refusal
  threshold, the nonlinear rank diagnostic, G).  The conclusion is exactly one
  row of plan 5.12's table and is drawn from part B only.  Part B is an input
  to the X0 domain justification memo, not by itself evidence of D-IND
  eligibility (R0-2).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

from iap.core.rng import SplitMix64  # noqa: E402

PLAN_REL = "research/v119/R5_DEPENDENCE_PLAN.md"
RESULT_REL = "research/v119/R5_DEPENDENCE_RESULT.md"
RESULT_JSON_REL = "research/v119/r5_dependence_result.json"
LEDGER_KIND = "r5_dependence_audit"
LEDGER_ALPHA = "R5"

#: predeclared constants (plan sections 3-5); changing one is a new plan revision
LAGS = (1, 2, 3, 4, 5)
Z95 = 1.959963984540054
N_PERMUTATIONS = 2000
PERMUTATION_SEED = 20301900
SELFTEST_SEED = 20301901
#: R0 section 4 bands (proposed numbers; the H1 prereg fixes them before T0)
LAG1_REFUSAL = 0.3
KURTOSIS_HIGH_SIDE = 1.0
NONLINEAR_LEVEL = 0.05
MIN_G = 10
#: the planning heuristic for "low power": lag-1 SE 1/sqrt(G) above 0.15
LOW_POWER_G = 44
#: the stationarity split (plan section 3.4)
EARLY_YEARS = (2019, 2020, 2021)
LATE_YEARS = (2025, 2026)
#: companion screen (plan section 4, NOT an R0 band): the same bands at a Sidak
#: level over the K x 5 (lags) or K (kurtosis) tests, reported beside the
#: per-hypothesis R0 screen for the X0 memo; the conclusion never reads it
FAMILY_LEVEL = 0.05
#: the four permitted conclusions (plan 5.12), in the order the rule applies them
ROWS = (
    "missing or incomplete evidence",
    "material dependence detected",
    "dependence not detected, low power",
    "stronger evidence supporting the assumptions",
)
PERMITTED = {
    "material dependence detected": "the synthetic F3 claim may stand; real-data transfer prohibited",
    "dependence not detected, low power": (
        "the synthetic F3 claim may stand; real-data transfer remains unproven"
    ),
    "stronger evidence supporting the assumptions": (
        "real-data use may be considered only after a separate inference validation"
    ),
    "missing or incomplete evidence": "no real-data FWER claim",
}


class R5Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        print(f"REFUSED: {msg}", file=sys.stderr)
        super().__init__(2)


# ------------------------------------------------------------- statistics --
def autocorrelation(x: np.ndarray, lag: int) -> float:
    """Sample autocorrelation at ``lag`` (mean-corrected, denominator the full sum of squares)."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if lag <= 0 or lag >= n:
        return float("nan")
    d = x - x.mean()
    den = float((d * d).sum())
    if den <= 0.0:
        return float("nan")
    return float((d[lag:] * d[:-lag]).sum() / den)


def bartlett_interval(r: float, n: int, z: float = Z95) -> tuple[float, float]:
    """``r +- z / sqrt(n)``: the large-sample band for a white-noise autocorrelation."""
    half = z / math.sqrt(n) if n > 0 else float("inf")
    return r - half, r + half


def sidak_z(m: int, level: float = FAMILY_LEVEL) -> float:
    """Two-sided normal quantile for a Sidak-adjusted family of ``m`` tests at ``level``."""
    from scipy import stats

    per_test = 1.0 - (1.0 - level) ** (1.0 / max(int(m), 1))
    return float(stats.norm.isf(per_test / 2.0))


def rank_autocorrelation(x: np.ndarray, lag: int) -> float:
    """Spearman autocorrelation at ``lag`` (ranks of ``x[lag:]`` against ranks of ``x[:-lag]``)."""
    x = np.asarray(x, dtype=float)
    if lag <= 0 or lag >= x.size - 1:
        return float("nan")
    from scipy import stats

    a, b = x[lag:], x[:-lag]
    if np.all(a == a[0]) or np.all(b == b[0]):
        return float("nan")
    return float(stats.spearmanr(a, b).statistic)


def rank_autocorrelation_p(x: np.ndarray, lag: int) -> float:
    """Two-sided p of the Spearman autocorrelation at ``lag`` (t approximation, n - lag - 2 df)."""
    x = np.asarray(x, dtype=float)
    n = x.size - lag
    r = rank_autocorrelation(x, lag)
    if not math.isfinite(r) or n <= 2:
        return float("nan")
    if abs(r) >= 1.0:
        return 0.0
    from scipy import stats

    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    return float(2.0 * stats.t.sf(abs(t), n - 2))


def excess_kurtosis(x: np.ndarray) -> tuple[float, float, float]:
    """Bias-corrected sample excess kurtosis (G2) with a 95 % interval from the
    normal-theory standard error ``sqrt(24 n (n-1)^2 / ((n-3)(n-2)(n+3)(n+5)))``."""
    x = np.asarray(x, dtype=float)
    n = x.size
    if n < 4:
        return float("nan"), float("nan"), float("nan")
    d = x - x.mean()
    m2 = float((d * d).sum()) / n
    if m2 <= 0.0:
        return float("nan"), float("nan"), float("nan")
    m4 = float((d**4).sum()) / n
    g2 = m4 / (m2 * m2) - 3.0
    big_g2 = ((n - 1) / ((n - 2) * (n - 3))) * ((n + 1) * g2 + 6.0)
    se = math.sqrt(24.0 * n * (n - 1) ** 2 / ((n - 3) * (n - 2) * (n + 3) * (n + 5)))
    return big_g2, big_g2 - Z95 * se, big_g2 + Z95 * se


def first_principal_component(panel: np.ndarray) -> dict[str, Any]:
    """PC1 of the standardised G x K panel: share of variance, loadings and the
    mean off-diagonal correlation (the equicorrelation estimate)."""
    g, k = panel.shape
    if g < 3 or k < 2:
        return {"share": float("nan"), "loadings": [], "mean_offdiag_corr": float("nan")}
    z = panel - panel.mean(axis=0)
    sd = z.std(axis=0, ddof=1)
    keep = sd > 0
    if keep.sum() < 2:
        return {"share": float("nan"), "loadings": [], "mean_offdiag_corr": float("nan")}
    z = z[:, keep] / sd[keep]
    corr = (z.T @ z) / (g - 1)
    w, v = np.linalg.eigh(corr)
    share = float(w[-1] / w.sum())
    load = v[:, -1]
    if load.sum() < 0:
        load = -load
    off = corr[~np.eye(corr.shape[0], dtype=bool)]
    return {
        "share": share,
        "loadings": [float(x) for x in load],
        "mean_offdiag_corr": float(off.mean()),
        "columns_used": int(keep.sum()),
    }


def _years(sessions: Sequence[str]) -> np.ndarray:
    return np.array([int(str(s)[:4]) for s in sessions])


def stationarity_split(panel: np.ndarray, sessions: Sequence[str]) -> dict[str, Any]:
    """Early (2019-21) vs late (2025-26) sessions per hypothesis: means, sds,
    Welch t and df, variance ratio.  Descriptive: both periods are tiny."""
    years = _years(sessions)
    early = np.isin(years, EARLY_YEARS)
    late = np.isin(years, LATE_YEARS)
    out: dict[str, Any] = {
        "n_early": int(early.sum()),
        "n_late": int(late.sum()),
        "unclassified_sessions": [
            s for s, y in zip(sessions, years, strict=True) if y not in EARLY_YEARS + LATE_YEARS
        ],
        "per_hypothesis": [],
    }
    for j in range(panel.shape[1]):
        a, b = panel[early, j], panel[late, j]
        row: dict[str, Any] = {"hypothesis": j}
        if a.size >= 2 and b.size >= 2:
            va, vb = float(a.var(ddof=1)), float(b.var(ddof=1))
            se2 = va / a.size + vb / b.size
            row.update(
                mean_early=float(a.mean()),
                mean_late=float(b.mean()),
                sd_early=math.sqrt(va),
                sd_late=math.sqrt(vb),
                welch_t=float((b.mean() - a.mean()) / math.sqrt(se2)) if se2 > 0 else float("nan"),
                welch_df=(
                    float(
                        se2**2
                        / ((va / a.size) ** 2 / (a.size - 1) + (vb / b.size) ** 2 / (b.size - 1))
                    )
                    if se2 > 0
                    else float("nan")
                ),
                variance_ratio=float(vb / va) if va > 0 else float("nan"),
            )
        else:
            row["not_estimable"] = "fewer than two sessions in a period"
        out["per_hypothesis"].append(row)
    return out


def mean_lag1(panel: np.ndarray) -> float:
    """The permutation statistic: the mean over hypotheses of the lag-1 autocorrelation."""
    vals = [autocorrelation(panel[:, j], 1) for j in range(panel.shape[1])]
    vals = [v for v in vals if math.isfinite(v)]
    return float(np.mean(vals)) if vals else float("nan")


def block_permutation(
    panel: np.ndarray, n_perm: int = N_PERMUTATIONS, seed: int = PERMUTATION_SEED
) -> dict[str, Any]:
    """Session-block permutation diagnostic: the whole session (all K values
    together) is the unit; the session ORDER is permuted, which preserves every
    within-session and cross-hypothesis structure and breaks only the temporal
    order.  Reports the share of permutations whose mean lag-1 autocorrelation
    is at least the observed one.  Failing to reject with few sessions is WEAK
    evidence and is never described as proof of independence."""
    g = panel.shape[0]
    observed = mean_lag1(panel)
    rng = SplitMix64(seed)
    count = 0
    draws = np.empty(n_perm)
    idx = np.arange(g)
    for i in range(n_perm):
        # Fisher-Yates with the pinned stream
        perm = idx.copy()
        for a in range(g - 1, 0, -1):
            b = rng.below(a + 1)
            perm[a], perm[b] = perm[b], perm[a]
        s = mean_lag1(panel[perm])
        draws[i] = s
        if s >= observed - 1e-12:
            count += 1
    return {
        "statistic": "mean over hypotheses of the lag-1 autocorrelation",
        "observed": observed,
        "n_permutations": n_perm,
        "seed": seed,
        "share_at_least_observed": count / n_perm,
        "permutation_mean": float(draws.mean()),
        "permutation_sd": float(draws.std(ddof=1)),
        "reading": (
            "a diagnostic: a small share may reveal temporal dependence; a large share with "
            f"G = {g} sessions is weak evidence and does not establish independence"
        ),
    }


# ------------------------------------------------------------------ parts --
def part_a(panel: np.ndarray, sessions: Sequence[str], hypotheses: Sequence[str]) -> dict[str, Any]:
    g, k = panel.shape
    per_h = []
    for j in range(k):
        x = panel[:, j]
        lags = {}
        for h in LAGS:
            r = autocorrelation(x, h)
            lo, hi = bartlett_interval(r, g) if math.isfinite(r) else (float("nan"), float("nan"))
            lags[str(h)] = {
                "r": r,
                "lo": lo,
                "hi": hi,
                "excludes_zero": bool(math.isfinite(r) and (lo > 0 or hi < 0)),
            }
        per_h.append({"hypothesis": hypotheses[j], "lags": lags})
    pooled = {}
    for h in LAGS:
        rs = [autocorrelation(panel[:, j], h) for j in range(k)]
        rs = [r for r in rs if math.isfinite(r)]
        pooled[str(h)] = {
            "mean_r": float(np.mean(rs)) if rs else float("nan"),
            "sd_r": float(np.std(rs, ddof=1)) if len(rs) > 1 else float("nan"),
            "n_hypotheses": len(rs),
        }
    return {
        "G": g,
        "K": k,
        "lag_se_white_noise": 1.0 / math.sqrt(g),
        "autocorrelation": {"per_hypothesis": per_h, "pooled": pooled},
        "common_factor": first_principal_component(panel),
        "stationarity": stationarity_split(panel, sessions),
        "block_permutation": block_permutation(panel),
        "reading": (
            "Part A is descriptive. It can reveal a violation of the independence or stationarity "
            "assumptions; it cannot establish domain membership, and not detecting dependence "
            f"with G = {g} sessions is not evidence of independence."
        ),
    }


def part_b(
    panel: np.ndarray, hypotheses: Sequence[str], design: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The R0 section 4 screen.  Every element is reported with the band it is
    judged against; a missing element is a missing element (refusal), never a pass."""
    g, k = panel.shape
    elements: dict[str, Any] = {}
    # kurtosis
    rows = []
    for j in range(k):
        g2, lo, hi = excess_kurtosis(panel[:, j])
        rows.append(
            {
                "hypothesis": hypotheses[j],
                "excess_kurtosis": g2,
                "lo": lo,
                "hi": hi,
                "disqualifying": bool(math.isfinite(lo) and lo > KURTOSIS_HIGH_SIDE),
            }
        )
    zk = sidak_z(k)
    fam_k = []
    for r in rows:
        if math.isfinite(r["excess_kurtosis"]):
            se = (r["hi"] - r["excess_kurtosis"]) / Z95
            fam_k.append(bool(r["excess_kurtosis"] - zk * se > KURTOSIS_HIGH_SIDE))
        else:
            fam_k.append(False)
    elements["kurtosis"] = {
        "band": (
            "disqualifying if the 95 % interval excludes 0 on the high side by more than "
            f"+{KURTOSIS_HIGH_SIDE:g}"
        ),
        "role": "screen only; passing is not evidence of normality",
        "per_hypothesis": rows,
        "disqualifying": any(r["disqualifying"] for r in rows),
        "available": all(math.isfinite(r["excess_kurtosis"]) for r in rows),
        "family_adjusted": {
            "level": FAMILY_LEVEL,
            "m": k,
            "disqualifying": any(fam_k),
            "hypotheses": [hypotheses[j] for j in range(k) if fam_k[j]],
        },
    }
    # design independence (from the manifest; a declaration, never inferred from data)
    need = (
        "non_overlapping_sessions",
        "no_label_crossing_session_boundary",
        "within_session_conditioning",
    )
    if isinstance(design, Mapping) and all(design.get(n) in (True, False) for n in need):
        elements["design_independence"] = {
            "band": "any overlapping sessions, label crossing a session boundary or conditioning across sessions disqualifies",
            "role": "design guarantee for what it covers and nothing more",
            "declared": {n: bool(design[n]) for n in need},
            "manifest": design.get("manifest"),
            "disqualifying": not all(bool(design[n]) for n in need),
            "available": True,
        }
    else:
        elements["design_independence"] = {
            "band": "any overlapping sessions, label crossing a session boundary or conditioning across sessions disqualifies",
            "role": "design guarantee for what it covers and nothing more",
            "declared": None,
            "disqualifying": None,
            "available": False,
            "missing": "no design manifest supplied (X1); a missing element is a refusal",
        }
    # serial dependence screen
    lag_rows = []
    zf = sidak_z(k * len(LAGS))
    for j in range(k):
        x = panel[:, j]
        r1 = autocorrelation(x, 1)
        excl, excl_fam = [], []
        for h in LAGS:
            r = autocorrelation(x, h)
            if math.isfinite(r):
                lo, hi = bartlett_interval(r, g)
                if lo > 0 or hi < 0:
                    excl.append(h)
                lo, hi = bartlett_interval(r, g, zf)
                if lo > 0 or hi < 0:
                    excl_fam.append(h)
        lag_rows.append(
            {
                "hypothesis": hypotheses[j],
                "lag1": r1,
                "lags_excluding_zero": excl,
                "lags_excluding_zero_family_adjusted": excl_fam,
                "disqualifying": bool((math.isfinite(r1) and r1 > LAG1_REFUSAL) or excl),
            }
        )
    fam_s = [bool(r["lags_excluding_zero_family_adjusted"]) for r in lag_rows]
    elements["serial_dependence"] = {
        "band": f"lag-1 point estimate above {LAG1_REFUSAL:g}, or any lag 1-5 interval excluding 0",
        "role": "screen only; a low estimate is not evidence for independence",
        "per_hypothesis": lag_rows,
        "disqualifying": any(r["disqualifying"] for r in lag_rows),
        "available": all(math.isfinite(r["lag1"]) for r in lag_rows),
        "expected_false_flags_under_independence": {
            "per_hypothesis_lag_tests": k * len(LAGS),
            "nominal_rate_per_test": 0.05,
            "expected_hypotheses_flagged": float(k * (1.0 - 0.95 ** len(LAGS))),
            "note": (
                "the R0 band is per hypothesis and per lag; across K hypotheses and 5 lags it "
                "flags independent data often (see the family-adjusted companion)"
            ),
        },
        "family_adjusted": {
            "level": FAMILY_LEVEL,
            "m": k * len(LAGS),
            "disqualifying": any(fam_s),
            "hypotheses": [hypotheses[j] for j in range(k) if fam_s[j]],
        },
    }
    # nonlinear dependence: rank autocorrelation of the squared values
    nl_rows = []
    m_nl = k * len(LAGS)
    fam_level_nl = 1.0 - (1.0 - FAMILY_LEVEL) ** (1.0 / m_nl)
    for j in range(k):
        x2 = panel[:, j] ** 2
        ps = {str(h): rank_autocorrelation_p(x2, h) for h in LAGS}
        rej = [h for h in LAGS if math.isfinite(ps[str(h)]) and ps[str(h)] < NONLINEAR_LEVEL]
        rej_fam = [h for h in LAGS if math.isfinite(ps[str(h)]) and ps[str(h)] < fam_level_nl]
        nl_rows.append(
            {
                "hypothesis": hypotheses[j],
                "p_by_lag": ps,
                "rejecting_lags": rej,
                "rejecting_lags_family_adjusted": rej_fam,
                "disqualifying": bool(rej),
            }
        )
    fam_n = [bool(r["rejecting_lags_family_adjusted"]) for r in nl_rows]
    elements["nonlinear_dependence"] = {
        "band": (
            "a rank-based test on squared session values (lag 1-5) rejecting at "
            f"{NONLINEAR_LEVEL:.0%}"
        ),
        "role": "screen only",
        "per_hypothesis": nl_rows,
        "disqualifying": any(r["disqualifying"] for r in nl_rows),
        "available": all(any(math.isfinite(p) for p in r["p_by_lag"].values()) for r in nl_rows),
        "family_adjusted": {
            "level": FAMILY_LEVEL,
            "m": m_nl,
            "disqualifying": any(fam_n),
            "hypotheses": [hypotheses[j] for j in range(k) if fam_n[j]],
        },
    }
    elements["common_factor_loading"] = {
        "band": "never disqualifying",
        "role": "affects power, not the domain (Holm)",
        "disqualifying": False,
        "available": True,
    }
    elements["G"] = {
        "band": f"below the power-table minimum or below {MIN_G} is a hard refusal",
        "value": g,
        "disqualifying": g < MIN_G,
        "available": True,
        "low_power": g < LOW_POWER_G,
        "lag1_se": 1.0 / math.sqrt(g),
    }
    return {"elements": elements}


def conclude(b: Mapping[str, Any]) -> dict[str, Any]:
    """Exactly one row of plan 5.12, from part B only, in the predeclared order:
    missing -> detected -> low power -> stronger evidence."""
    els = b["elements"]
    missing = [n for n, e in els.items() if not e.get("available")]
    if missing:
        row = ROWS[0]
        why = f"missing or unavailable element(s): {missing}"
    elif any(e.get("disqualifying") for e in els.values()):
        row = ROWS[1]
        why = "disqualifying condition(s): " + ", ".join(
            n for n, e in els.items() if e.get("disqualifying")
        )
    elif els["G"]["low_power"]:
        row = ROWS[2]
        why = (
            f"no disqualifying condition detected at G = {els['G']['value']} (lag-1 SE "
            f"{els['G']['lag1_se']:.2f}); not detecting dependence at this G is weak evidence"
        )
    else:
        row = ROWS[3]
        why = f"no disqualifying condition detected at G = {els['G']['value']} >= {LOW_POWER_G}"
    return {
        "row": row,
        "permitted_conclusion": PERMITTED[row],
        "why": why,
        "from": "part B only",
        "never": (
            "This conclusion is not evidence of D-IND eligibility by itself (R0-2) and does not "
            "authorise PROMOTE; passing F3 and not detecting dependence in R5 does not authorise PROMOTE."
        ),
    }


# ------------------------------------------------------------- the panel --
def load_panel(path: Path) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("sessions", "hypotheses", "values"):
        if key not in doc:
            raise R5Refused(f"panel lacks {key!r}")
    values = np.asarray(doc["values"], dtype=float)
    if values.ndim != 2 or values.shape != (len(doc["sessions"]), len(doc["hypotheses"])):
        raise R5Refused("panel values must be a G x K matrix matching sessions and hypotheses")
    if not np.all(np.isfinite(values)):
        raise R5Refused("panel carries non-finite values")
    sessions = [str(s) for s in doc["sessions"]]
    if sessions != sorted(sessions):
        raise R5Refused("sessions must be in registered date order")
    if len(set(sessions)) != len(sessions):
        raise R5Refused("duplicated session")
    doc["values"] = values
    doc["sessions"] = sessions
    return doc


def check_registry(
    sessions: Sequence[str],
    symbols: Sequence[str] | None,
    prereg_id: str | None,
    registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Every session must be ``in_sample`` for the symbols, or ``spent`` by
    ``prereg_id``; an ``unread`` (sealed) session or an unregistered one is refused."""
    recs = registry.get("sessions") or {}
    out = []
    for iso in sessions:
        entries = recs.get(iso)
        if not entries:
            raise R5Refused(f"session {iso} is not in the holdout registry")

        def covers(e: Mapping[str, Any]) -> bool:
            syms = e.get("symbols")
            if syms == "*" or not symbols:
                return True
            return isinstance(syms, list) and set(symbols) <= set(syms)

        ok = None
        for e in entries:
            if e.get("state") == "in_sample" and covers(e):
                ok = "in_sample"
                break
            if (
                e.get("state") == "spent"
                and covers(e)
                and prereg_id
                and prereg_id in (e.get("spent_by") or [])
            ):
                ok = f"spent by {prereg_id}"
                break
        if ok is None:
            states = sorted({str(e.get("state")) for e in entries})
            raise R5Refused(
                f"session {iso} is {states} for {list(symbols or ['*'])} in the holdout registry: "
                "only in_sample sessions, or sessions spent by the named prereg, may enter R5"
            )
        out.append({"session": iso, "access": ok})
    return out


# -------------------------------------------------------------- rendering --
def _f(x: Any, nd: int = 3) -> str:
    if isinstance(x, float):
        return "nan" if not math.isfinite(x) else f"{x:.{nd}f}"
    return str(x)


def render(res: Mapping[str, Any]) -> str:
    a, b, c = res["part_a"], res["part_b"], res["conclusion"]
    L = []
    L.append(f"# R5 real-data dependence audit: result ({res['label']})")
    L.append("")
    L.append(
        f"Plan: `{PLAN_REL}` (sha256 `{res['plan_sha256'][:12]}`). Panel: {res['panel']['source']} "
        f"(G = {a['G']} sessions, K = {a['K']} hypotheses; panel sha256 `{res['panel']['sha256'][:12]}`). "
        f"Generated {res['generated_utc']}. {res['ledger_note']}"
    )
    L.append("")
    L.append(
        "## Part A: diagnostics (descriptive; may reveal a violation; never establishes membership)"
    )
    L.append("")
    L.append(
        f"White-noise standard error of an autocorrelation at G = {a['G']}: {_f(a['lag_se_white_noise'])}."
    )
    L.append("")
    L.append("| lag | mean r over hypotheses | sd of r | hypotheses |")
    L.append("|---|---|---|---|")
    for h, row in a["autocorrelation"]["pooled"].items():
        L.append(f"| {h} | {_f(row['mean_r'])} | {_f(row['sd_r'])} | {row['n_hypotheses']} |")
    L.append("")
    L.append("| hypothesis | r1 [95 %] | r2 | r3 | r4 | r5 | lags excluding 0 |")
    L.append("|---|---|---|---|---|---|---|")
    for row in a["autocorrelation"]["per_hypothesis"]:
        lg = row["lags"]
        r1 = lg["1"]
        excl = [h for h, v in lg.items() if v["excludes_zero"]]
        L.append(
            f"| {row['hypothesis']} | {_f(r1['r'])} [{_f(r1['lo'])}, {_f(r1['hi'])}] | {_f(lg['2']['r'])} | "
            f"{_f(lg['3']['r'])} | {_f(lg['4']['r'])} | {_f(lg['5']['r'])} | {excl or '-'} |"
        )
    cf = a["common_factor"]
    L.append("")
    L.append(
        f"Common factor: first principal component share {_f(cf['share'])}, mean off-diagonal correlation "
        f"{_f(cf['mean_offdiag_corr'])} (affects power, not the domain)."
    )
    st = a["stationarity"]
    L.append("")
    L.append(
        f"Stationarity split: {st['n_early']} early (2019-21) vs {st['n_late']} late (2025-26) sessions"
        + (f"; unclassified {st['unclassified_sessions']}" if st["unclassified_sessions"] else "")
        + ". Welch t and variance ratios per hypothesis are in the JSON; both periods are tiny and the split is descriptive."
    )
    bp = a["block_permutation"]
    L.append("")
    L.append(
        f"Session-block permutation diagnostic ({bp['n_permutations']} permutations, seed {bp['seed']}): observed "
        f"{bp['statistic']} {_f(bp['observed'])}; share of permutations at least as large {_f(bp['share_at_least_observed'])}. "
        f"{bp['reading']}."
    )
    L.append("")
    L.append(f"**Part A conclusion.** {a['reading']}")
    L.append("")
    L.append("## Part B: affirmative evidence for domain membership (R0 section 4 bands)")
    L.append("")
    L.append("| element | band | available | disqualifying | role |")
    L.append("|---|---|---|---|---|")
    for name, e in b["elements"].items():
        L.append(
            f"| {name} | {e['band']} | {e.get('available')} | {e.get('disqualifying')} | {e.get('role', '')} |"
        )
    fam = {n: e["family_adjusted"] for n, e in b["elements"].items() if "family_adjusted" in e}
    exp = b["elements"]["serial_dependence"]["expected_false_flags_under_independence"]
    L.append("")
    L.append(
        "Companion (not an R0 band; for the X0 memo): the same screens at a Sidak-adjusted family "
        f"level of {FAMILY_LEVEL:g} over the K x 5 (or K) tests flag "
        + "; ".join(f"{n}: {e['hypotheses'] or 'none'}" for n, e in fam.items())
        + f". Per-hypothesis flags expected under independence at K = {a['K']}: about "
        f"{exp['expected_hypotheses_flagged']:.1f} hypotheses for the serial screen alone; the "
        "conclusion below reads the R0 per-hypothesis screen as written (fail-closed)."
    )
    kt = b["elements"]["kurtosis"]["per_hypothesis"]
    L.append("")
    L.append("| hypothesis | excess kurtosis (bias-corrected) [95 %] | disqualifying |")
    L.append("|---|---|---|")
    for r in kt:
        L.append(
            f"| {r['hypothesis']} | {_f(r['excess_kurtosis'])} [{_f(r['lo'])}, {_f(r['hi'])}] | {r['disqualifying']} |"
        )
    L.append("")
    L.append(
        "**Part B conclusion.** Part B is an input to the X0 domain justification memo, not by itself evidence "
        "of D-IND eligibility (R0-2). Passing every screen establishes only that no known disqualifying "
        "condition was detected."
    )
    L.append("")
    L.append("## Conclusion (exactly one row of plan 5.12, drawn from part B only)")
    L.append("")
    L.append("| R5 finding | permitted conclusion |")
    L.append("|---|---|")
    L.append(f"| **{c['row']}** | {c['permitted_conclusion']} |")
    L.append("")
    L.append(f"Why: {c['why']}. {c['never']}")
    L.append("")
    return "\n".join(L)


# ----------------------------------------------------------------- driver --
def audit(panel: Mapping[str, Any], *, label: str, ledger_note: str) -> dict[str, Any]:
    values = panel["values"]
    sessions = panel["sessions"]
    hyps = [str(h) for h in panel["hypotheses"]]
    a = part_a(values, sessions, hyps)
    b = part_b(values, hyps, panel.get("design"))
    c = conclude(b)
    plan = ROOT / PLAN_REL
    return {
        "schema": "iap.r5_dependence_result/v1",
        "label": label,
        "generated_utc": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest() if plan.is_file() else "",
        "panel": {
            "source": str(panel.get("source", "unspecified")),
            "sha256": hashlib.sha256(
                json.dumps(
                    {"s": sessions, "h": hyps, "v": values.tolist()}, sort_keys=True
                ).encode()
            ).hexdigest(),
            "sessions": list(sessions),
            "hypotheses": hyps,
        },
        "ledger_note": ledger_note,
        "part_a": a,
        "part_b": b,
        "conclusion": c,
    }


def synthetic_panel(g: int = 16, k: int = 16, seed: int = SELFTEST_SEED) -> dict[str, Any]:
    """Independent normal session values with a fake 2019-21 / 2025-26 calendar
    and a complete design declaration: the self-test input (no data)."""
    rng = SplitMix64(seed)
    vals = np.array([[rng.normal() for _ in range(k)] for _ in range(g)])
    early = [f"2019-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(g // 2)]
    late = [f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(g - g // 2)]
    return {
        "source": f"synthetic self-test (SplitMix64 seed {seed}, independent N(0, 1))",
        "sessions": early + late,
        "hypotheses": [f"H{j + 1:02d}" for j in range(k)],
        "values": vals,
        "design": {
            "non_overlapping_sessions": True,
            "no_label_crossing_session_boundary": True,
            "within_session_conditioning": True,
            "manifest": "synthetic (no X1 manifest)",
        },
    }


def _write(out: Path, res: Mapping[str, Any], *, suffix: str) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    md = out / f"R5_DEPENDENCE_RESULT{suffix}.md"
    js = out / f"r5_dependence_result{suffix}.json"
    md.write_text(render(res), encoding="utf-8", newline="\n")
    js.write_text(
        json.dumps(res, indent=1, sort_keys=True, allow_nan=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return md, js


def cmd_selftest(a: argparse.Namespace) -> int:
    panel = synthetic_panel()
    res = audit(
        panel,
        label="SYNTHETIC SELF-TEST; no real data; not a result",
        ledger_note="No ledger look (synthetic).",
    )
    md, js = _write(Path(a.out), res, suffix="_SELFTEST")
    print(f"self-test row: {res['conclusion']['row']}; wrote {md} and {js}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from iap.research.holdout_registry import load_registry

    panel = load_panel(Path(a.panel))
    registry = load_registry(root=ROOT)
    access = check_registry(
        panel["sessions"], panel.get("symbols"), panel.get("prereg_id"), registry
    )
    note = (
        "One ledger look recorded."
        if not a.no_ledger
        else "Ledger look NOT recorded (--no-ledger: test only)."
    )
    res = audit(panel, label="REAL DATA; one look", ledger_note=note)
    res["registry_access"] = access
    if not a.no_ledger:
        from iap.validation.ledger import ExperimentLedger

        ledger = ExperimentLedger(ROOT / "research" / "experiments.json", data_family="real")
        ledger.record(
            LEDGER_ALPHA,
            LEDGER_KIND,
            {"plan_sha256": res["plan_sha256"], "panel_sha256": res["panel"]["sha256"]},
            {"row": res["conclusion"]["row"]},
        )
        ledger.save()
    md, js = _write(Path(a.out), res, suffix="")
    print(f"R5 row: {res['conclusion']['row']}; wrote {md} and {js}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("selftest", help="synthetic self-test: no registry, no ledger, no data")
    s.add_argument("--out", required=True)
    r = sub.add_parser("run", help="the audit on a guarded session-value panel (later)")
    r.add_argument("--panel", required=True)
    r.add_argument("--out", default=str(ROOT / "research" / "v119"))
    r.add_argument("--no-ledger", action="store_true", help="tests only")
    a = ap.parse_args(argv)
    return cmd_selftest(a) if a.cmd == "selftest" else cmd_run(a)


if __name__ == "__main__":
    raise SystemExit(main())

"""The confirmation framework's procedure/domain profiles (v2.0 stage 10, U13; G1).

Specification: ``research/v2/CONFIRMATION_FRAMEWORK.md`` (the framework) and
``python/tests/spec/CONFIRMATION_PROFILES.md`` (the profiles PF-1, PF-2 and the deferred
PF-3, their synthetic domains, the size and power acceptance and the refusal fixtures).
Evidence: ``research/v2/confirmation_evidence/<profile>_RESULTS.json`` written once by
``python/tests/spec/pf_suite.py --write``; this module READS it and nothing else decides
what is in a profile's operating domain.

R0 (``iap.validation.confirmatory``) stays frozen for H1.  Every other registration names a
profile here and is decided by :func:`decide`, the only other PROMOTE constructor in the
package (the R1 enumeration test lists both).

**Observation unit.**  One series per hypothesis of daily marked-to-market portfolio returns
on the registered capital, in registered date order, ``T`` sessions, ``K`` hypotheses.

**Procedures (candidates; each validated per profile and per synthetic cell).**

* ``P-HAC-L<lag>``: one-sided t of the mean with a Newey-West long-run variance (Bartlett
  kernel, the profile's PRE-FIXED lag, the finite-sample factor ``T / (T - lag - 1)``),
  referred to Student t with ``min(T - 1, floor(3 T / (2 lag)))`` degrees of freedom (the
  fixed-smoothing t-approximation for the Bartlett kernel, :func:`hac_reference_df`).
* ``P-CBB``: circular block bootstrap of the mean with a FIXED block length that the
  registration fixes from the declared dependence diagnostic computed on DEVELOPMENT data
  only (:func:`block_length_rule`); ``ceil(T / L)`` whole circular blocks per resample,
  ``B`` resamples, the centred resample means scaled by ``sqrt(T / (T - L))``, one-sided
  percentile p-value ``(1 + #{scaled(m*_b - m) >= m}) / (B + 1)``.

Holm step-down over the registered family at one-sided ``alpha_op = 0.020`` (claim FWER
<= 0.025), the economic screen on the registered capital with the common margin, and the
registration record of the framework.  **Fail closed**: :func:`decide` refuses, in this
order and before any statistic, an unknown or deferred profile; a profile without
committed evidence; a registration that names another profile, an unregistered statistic,
a horizon outside the profile, an unvalidated procedure, a block length that is not the
declared rule applied to the recorded development diagnostic, a missing economic screen
or refusal-condition list, a spent window or looks on the confirmation dataset; a series
read by any identity other than the registered runner (real data must come through
:func:`read_series`, which goes through :mod:`iap.core.guard`); a series whose shape,
length or finiteness is wrong; and a series whose declared diagnostics (lag-1
autocorrelation, lag-1 autocorrelation of squares, excess kurtosis) fall outside the
procedure's operating domain.  A ``refused: <reason>`` outcome is terminal.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from iap.core import guard
from iap.validation import confirmatory as _c

__all__ = [
    "ALPHA_OP",
    "FWER_CLAIM",
    "GARCH_ALPHA",
    "GARCH_BETA",
    "GARCH_EXCESS_KURTOSIS",
    "GARCH_SQ_ACF1",
    "KURTOSIS_BAND",
    "KURTOSIS_T3_CAP",
    "N_POWER",
    "N_SIZE",
    "POWER_PASS_DETECTIONS",
    "PROFILES",
    "RUNNER_ACCOUNT",
    "SEED_BASE",
    "SIZE_PASS_K",
    "STATISTIC",
    "Profile",
    "ProfileVerdict",
    "SeriesRead",
    "acf",
    "block_length_rule",
    "cbb_pvalue",
    "cbb_pvalues_many",
    "current_identity",
    "decide",
    "diagnostics",
    "evidence_path",
    "generator_acf1",
    "hac_pvalue",
    "hac_pvalues_many",
    "hac_reference_df",
    "holm_many",
    "newey_west_lrv",
    "operating_domain",
    "planted_effect",
    "read_series",
    "registration_sha256",
]

# ----------------------------------------------------------------- constants --
ALPHA_OP = 0.020
FWER_CLAIM = 0.025
STATISTIC = "daily_mtm_portfolio_return"
N_SIZE = 20_000
SIZE_PASS_K = 463  # one-sided 95 % Clopper-Pearson upper bound 0.02498 <= 0.025 (R0 form)
N_POWER = 5_000
POWER_PASS_DETECTIONS = 4_047  # one-sided 95 % lower bound 0.800 (R0 form)
POWER_TARGET = 0.90  # theoretical power of the planted effect under the iid normal reference
K_STUDY = 4  # hypotheses per synthetic dataset (independent: the least favourable case for Holm)
SEED_BASE = 20_320_000  # cell seed = SEED_BASE + 10_000 * profile_index + 100 * kind + cell_index (PF-1 at 20330000, PF-2 at 20340000)
DEV_SEED_OFFSET = 5_000  # the development series that fixes the block length of a cell
RESAMPLES = 999
GARCH_ALPHA = 0.08
GARCH_BETA = 0.90
# GARCH(1,1) with normal innovations, alpha 0.08, beta 0.90 (unit unconditional variance):
# excess kurtosis 3 (1 - (a+b)^2) / (1 - (a+b)^2 - 2 a^2) - 3 and the lag-1 autocorrelation
# of squares a (1 - a b - b^2) / (1 - 2 a b - b^2) (Bollerslev 1986), rounded here.
GARCH_EXCESS_KURTOSIS = 1.4328
GARCH_SQ_ACF1 = 0.2052
KURTOSIS_BAND = 1.0
KURTOSIS_T3_CAP = 30.0
DEPENDENCE_BAND_MULT = 2.0  # band = 2 / sqrt(T) on an autocorrelation estimate (domain screens)
LJUNG_BOX_LEVEL = 0.05  # the block-length rule's tail test level
RUNNER_ACCOUNT = "iap-runner"
EVIDENCE_DIR = Path("research") / "v2" / "confirmation_evidence"
_ROOT = Path(__file__).resolve().parents[4]
_TOKEN_KEY = object()
PROMOTE = _c.PROMOTE
REJECT = _c.REJECT
REFUSED = _c.REFUSED
DIAGNOSTICS = ("acf1", "sq_acf1", "excess_kurtosis")


# ------------------------------------------------------------------ profiles --
@dataclass(frozen=True)
class Profile:
    """A procedure/domain profile: the observation unit, horizon class, candidate
    procedures, the synthetic domain grid and the T grid of its study."""

    id: str
    index: int
    title: str
    horizon: tuple[int, int]  # holding horizon in sessions, inclusive
    T_grid: tuple[int, ...]
    procedures: Mapping[str, Mapping[str, Any]]
    ar_grid: tuple[float, ...]
    overlap_h: tuple[int, ...]
    deferred: bool = False
    statistic: str = STATISTIC

    def generators(self) -> list[tuple[str, dict[str, Any]]]:
        """The synthetic null generators of the profile's domain, in cell order."""
        out: list[tuple[str, dict[str, Any]]] = [("iid_normal", {})]
        out += [("ar1", {"rho": r}) for r in self.ar_grid]
        out += [("garch11", {"alpha": GARCH_ALPHA, "beta": GARCH_BETA})]
        out += [("t3", {})]
        out += [("overlap_ma", {"h": h}) for h in self.overlap_h]
        return out


def _hac(lag: int) -> dict[str, Any]:
    return {"kind": "hac", "lag": int(lag), "reference": "student_t_df_min(T-1,3T/2lag)"}


def _cbb(floor: int, cap: int, max_lag: int) -> dict[str, Any]:
    return {
        "kind": "cbb",
        "floor": int(floor),
        "cap": int(cap),
        "multiplier": 2,
        "max_lag": int(max_lag),
        "resamples": RESAMPLES,
    }


PROFILES: dict[str, Profile] = {
    "PF-1": Profile(
        id="PF-1",
        index=1,
        title="daily marked-to-market portfolio return of a 1-5 session horizon book",
        horizon=(1, 5),
        T_grid=(120, 250),
        procedures={"P-HAC-L5": _hac(5), "P-CBB": _cbb(5, 20, 20)},
        ar_grid=(0.1, 0.2, 0.3, 0.5),
        overlap_h=(2, 3, 5),
    ),
    "PF-2": Profile(
        id="PF-2",
        index=2,
        title="daily marked-to-market portfolio return of a 20-60 session horizon book with overlapping positions",
        horizon=(20, 60),
        T_grid=(250, 500),
        procedures={"P-HAC-L20": _hac(20), "P-HAC-L60": _hac(60), "P-CBB": _cbb(10, 60, 60)},
        ar_grid=(0.1, 0.2, 0.3, 0.5),
        overlap_h=(20, 40, 60),
    ),
    "PF-3": Profile(
        id="PF-3",
        index=3,
        title="the combined multi-horizon book (deferred until C0-C2 exist)",
        horizon=(1, 60),
        T_grid=(),
        procedures={},
        ar_grid=(),
        overlap_h=(),
        deferred=True,
    ),
}


def generator_acf1(generator: str, params: Mapping[str, Any]) -> float:
    """The theoretical lag-1 autocorrelation of a synthetic null generator."""
    if generator == "ar1":
        return float(params["rho"])
    if generator == "overlap_ma":
        h = int(params["h"])
        return (h - 1) / h
    if generator in ("iid_normal", "garch11", "t3"):
        return 0.0
    raise ValueError(f"unknown generator {generator!r}")


# ---------------------------------------------------------------- statistics --
def acf(x: np.ndarray, lag: int) -> float:
    """Sample autocorrelation at ``lag`` (biased estimator, mean removed)."""
    x = np.asarray(x, dtype=float)
    d = x - x.mean()
    s0 = float(np.dot(d, d))
    if s0 <= 0.0 or lag >= x.size:
        return float("nan")
    return float(np.dot(d[lag:], d[:-lag]) / s0)


def newey_west_lrv(x: Sequence[float] | np.ndarray, lag: int, *, correction: bool = True) -> float:
    """Newey-West long-run variance of ``x`` (Bartlett kernel, ``lag`` lags):
    ``gamma_0 + 2 sum_h (1 - h / (lag + 1)) gamma_h`` with biased autocovariances, times
    the declared finite-sample factor ``T / (T - lag - 1)`` (the expected downward bias of
    the biased autocovariances under independence is ``(lag + 1) / T``; ``correction=False``
    turns it off).  ``statsmodels`` ``OLS(x, 1).fit(cov_type="HAC", cov_kwds={"maxlags": lag,
    "use_correction": False}).bse ** 2 * T`` is the uncorrected number."""
    x = np.asarray(x, dtype=float)
    n = int(x.size)
    if n < 2:
        return float("nan")
    d = x - x.mean()
    s0 = float(np.dot(d, d))
    for h in range(1, int(lag) + 1):
        if h >= n:
            break
        s0 += 2.0 * (1.0 - h / (lag + 1.0)) * float(np.dot(d[h:], d[:-h]))
    if correction and n > lag + 1:
        s0 *= n / (n - lag - 1.0)
    return s0 / n


def hac_reference_df(T: int, lag: int) -> int:
    """Degrees of freedom of the HAC t's Student-t reference: ``min(T - 1, floor(3 T /
    (2 lag)))``, the fixed-smoothing t-approximation for the Bartlett kernel (the Bartlett
    long-run variance estimator at bandwidth ``b = lag / T`` has asymptotic variance about
    ``(4 / 3) b`` times the squared long-run variance, i.e. the variability of a chi-square
    with ``3 / (2 b)`` degrees of freedom; Sun 2014); ``T - 1`` at lag 0."""
    T = int(T)
    if lag <= 0:
        return max(T - 1, 1)
    return max(1, min(T - 1, int(math.floor(3.0 * T / (2.0 * lag)))))


def hac_pvalue(x: Sequence[float] | np.ndarray, lag: int) -> dict[str, float]:
    """One-sided (greater) HAC t of the mean and its p-value on Student t with
    :func:`hac_reference_df` degrees of freedom."""
    from scipy import stats

    x = np.asarray(x, dtype=float)
    n = int(x.size)
    lrv = newey_west_lrv(x, lag)
    if not (n >= 2 and math.isfinite(lrv) and lrv > 0.0):
        return {
            "T": n,
            "mean": float(x.mean()) if n else float("nan"),
            "lrv": lrv,
            "t": float("nan"),
            "p": float("nan"),
        }
    t = float(x.mean()) / math.sqrt(lrv / n)
    df = hac_reference_df(n, lag)
    return {
        "T": n,
        "mean": float(x.mean()),
        "lrv": lrv,
        "t": t,
        "df": df,
        "p": float(stats.t.sf(t, df)),
    }


def hac_pvalues_many(x: np.ndarray, lag: int) -> np.ndarray:
    """Vectorised :func:`hac_pvalue`: ``x`` of shape ``(n, T, K)`` -> p of shape ``(n, K)``
    (NaN where the long-run variance is not positive)."""
    from scipy import stats

    n_, T, _k = x.shape
    m = x.mean(axis=1)
    d = x - m[:, None, :]
    s0 = (d * d).sum(axis=1)
    for h in range(1, int(lag) + 1):
        if h >= T:
            break
        s0 = s0 + 2.0 * (1.0 - h / (lag + 1.0)) * (d[:, h:, :] * d[:, :-h, :]).sum(axis=1)
    if T > lag + 1:
        s0 = s0 * (T / (T - lag - 1.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(s0 > 0.0, m / np.sqrt(s0 / (T * T)), np.nan)
    return np.where(np.isfinite(t), stats.t.sf(np.nan_to_num(t), hac_reference_df(T, lag)), np.nan)


def _circular_block_means(x: np.ndarray, L: int) -> np.ndarray:
    """Means of the ``T`` circular blocks of length ``L`` of ``x`` (axis 1 is time)."""
    T = x.shape[1]
    ext = np.concatenate([x, x[:, : L - 1, ...]], axis=1) if L > 1 else x
    cs = np.cumsum(ext, axis=1, dtype=float)
    cs = np.concatenate([np.zeros_like(cs[:, :1, ...]), cs], axis=1)
    return (cs[:, L : L + T, ...] - cs[:, :T, ...]) / L


def cbb_pvalues_many(
    x: np.ndarray,
    L: int,
    resamples: int,
    rng: np.random.Generator,
    *,
    sub_chunk_elements: int = 8_000_000,
) -> np.ndarray:
    """Vectorised circular block bootstrap p-values: ``x`` of shape ``(n, T, K)`` -> p of
    shape ``(n, K)``.  Each dataset and hypothesis draws its own ``resamples`` x
    ``ceil(T / L)`` block starts from ``rng`` (``integers(0, T)`` of shape ``(datasets,
    K, B * nb)`` per sub-chunk, in dataset order, then hypothesis order); the resample statistic is the mean of the ``ceil(T / L) * L``
    values; the centred resample means are scaled by the declared finite-sample factor
    ``sqrt(T / (T - L))`` (the circular block bootstrap's variance of the mean is biased
    down by ``1 - L / T`` under independence); p = ``(1 + #{scaled(m*_b - m) >= m}) /
    (B + 1)``.  NaN where the series has no variance."""
    n_, T, K = x.shape
    L = int(L)
    nb = -(-T // L)
    scale = math.sqrt(T / (T - L)) if T > L else 1.0
    cb = np.ascontiguousarray(np.transpose(_circular_block_means(x, L), (0, 2, 1)))  # (n, K, T)
    m = x.mean(axis=1)  # (n, K)
    out = np.empty((n_, K))
    step = max(1, int(sub_chunk_elements // (K * resamples * nb)))
    for i0 in range(0, n_, step):
        i1 = min(n_, i0 + step)
        idx = rng.integers(0, T, size=(i1 - i0, K, resamples * nb))
        boots = (
            np.take_along_axis(cb[i0:i1], idx, axis=2)
            .reshape(i1 - i0, K, resamples, nb)
            .mean(axis=3)
        )
        dev = (boots - m[i0:i1, :, None]) * scale
        out[i0:i1] = (1.0 + np.count_nonzero(dev >= m[i0:i1, :, None], axis=2)) / (resamples + 1.0)
    var = x.var(axis=1)
    return np.where(var > 0.0, out, np.nan)


def cbb_pvalue(
    x: Sequence[float] | np.ndarray, block_length: int, *, resamples: int = RESAMPLES, seed: int
) -> dict[str, float]:
    """One series: :func:`cbb_pvalues_many` on ``x`` alone with ``default_rng(seed)``."""
    x = np.asarray(x, dtype=float)
    n = int(x.size)
    if n < 2 or not (0 < int(block_length) <= n):
        return {
            "T": n,
            "mean": float(x.mean()) if n else float("nan"),
            "block_length": int(block_length),
            "p": float("nan"),
        }
    p = cbb_pvalues_many(
        x[None, :, None], int(block_length), int(resamples), np.random.default_rng(int(seed))
    )
    return {
        "T": n,
        "mean": float(x.mean()),
        "block_length": int(block_length),
        "resamples": int(resamples),
        "p": float(p[0, 0]),
    }


def holm_many(p: np.ndarray, alpha: float) -> np.ndarray:
    """Vectorised Holm step-down: ``p`` of shape ``(n, K)`` -> rejection mask ``(n, K)``
    (NaN = 1).  Row by row identical to :func:`iap.validation.confirmatory.holm`."""
    pf = np.where(np.isfinite(p), p, 1.0)
    n_, K = pf.shape
    order = np.argsort(pf, axis=1, kind="stable")
    sp = np.take_along_axis(pf, order, axis=1)
    thr = alpha / (K - np.arange(K))
    ok = np.cumprod(sp <= thr[None, :], axis=1).astype(bool)
    rej = np.zeros_like(ok)
    np.put_along_axis(rej, order, ok, axis=1)
    return rej


def block_length_rule(
    dev: Sequence[float] | np.ndarray, *, max_lag: int, floor: int, cap: int, multiplier: int = 2
) -> dict[str, Any]:
    """The declared block-length rule, computed on DEVELOPMENT data only: ``L_dep`` is the
    smallest lag ``L`` such that the Ljung-Box statistic of the tail lags ``L + 1 ..
    max_lag``, ``T (T + 2) sum_k acf_k^2 / (T - k)``, is at or below the 95 % chi-square
    quantile with ``max_lag - L`` degrees of freedom (a joint test of no residual
    dependence beyond ``L``; a per-lag ``2 / sqrt(T)`` band raises a false alarm at most
    lags of a 60-lag window and was rejected at the pilot stage); the block length is
    ``clip(multiplier * L_dep, floor, cap)``.  Refuses a development series shorter than ``2 * max_lag + 2``.  The
    record returned (``acf``, ``band``, ``L_dep``, ``block_length``, ``T_dev``) is what a
    registration binds; :func:`decide` recomputes the block length from it."""
    x = np.asarray(dev, dtype=float)
    T = int(x.size)
    if T < 2 * max_lag + 2:
        raise ValueError(
            f"development series of {T} sessions is too short to diagnose {max_lag} lags"
        )
    if not np.all(np.isfinite(x)) or x.var() <= 0.0:
        raise ValueError("development series must be finite with positive variance")
    a = [acf(x, lag) for lag in range(1, max_lag + 1)]
    band = DEPENDENCE_BAND_MULT / math.sqrt(T)
    rec = {"acf": [float(v) for v in a], "band": band, "T_dev": T, "max_lag": int(max_lag)}
    rec.update(_block_from_acf(rec, floor=floor, cap=cap, multiplier=multiplier))
    return rec


def _block_from_acf(
    rec: Mapping[str, Any], *, floor: int, cap: int, multiplier: int
) -> dict[str, Any]:
    from scipy import stats

    a = [float(v) for v in rec["acf"]]
    T = int(rec["T_dev"])
    max_lag = len(a)
    L_dep = max_lag
    q_tail = []
    for L in range(0, max_lag):
        q = T * (T + 2.0) * sum(a[k - 1] ** 2 / (T - k) for k in range(L + 1, max_lag + 1))
        q_tail.append(q)
        if q <= float(stats.chi2.ppf(1.0 - LJUNG_BOX_LEVEL, max_lag - L)):
            L_dep = L
            break
    return {
        "L_dep": int(L_dep),
        "ljung_box_tail": [float(q) for q in q_tail],
        "block_length": int(min(cap, max(floor, multiplier * L_dep))),
    }


def planted_effect(
    T: int, K: int = K_STUDY, *, power: float = POWER_TARGET, alpha: float = ALPHA_OP
) -> float:
    """The planted mean (in sd units per session) of a power cell: theoretical power
    ``power`` under the iid normal reference at Bonferroni level ``alpha / K`` with ``T - 1``
    df (the one noncentral-t inversion of :func:`iap.validation.confirmatory.mde`)."""
    return float(_c.mde(int(T), alpha / K, power, procedure="P-IND"))


def diagnostics(x: np.ndarray) -> dict[str, float]:
    """The declared domain diagnostics of one series: lag-1 autocorrelation, lag-1
    autocorrelation of squares, bias-corrected excess kurtosis."""
    from scipy import stats

    x = np.asarray(x, dtype=float)
    return {
        "acf1": acf(x, 1),
        "sq_acf1": acf(x * x, 1),
        "excess_kurtosis": float(stats.kurtosis(x, fisher=True, bias=False)),
    }


# ------------------------------------------------------------------ evidence --
def evidence_path(profile_id: str, root: Path | str | None = None) -> Path:
    return Path(root or _ROOT) / EVIDENCE_DIR / f"{profile_id}_RESULTS.json"


def operating_domain(profile_id: str, root: Path | str | None = None) -> dict[str, Any] | None:
    """The committed operating domain of a profile, read from its evidence file: per
    procedure the generator cells (name, params) whose size cells pass at EVERY T of the
    profile's grid, the smallest validated T, and whether the reference (iid normal) cells
    pass both size and power acceptance (a procedure whose reference power cells fail is
    underpowered and has no operating domain).  ``None`` when there is no evidence (then every decision is refused)."""
    path = evidence_path(profile_id, root)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        doc.get("profile") != profile_id
        or doc.get("size_pass_k") != SIZE_PASS_K
        or doc.get("n_size") != N_SIZE
    ):
        return None
    prof = PROFILES.get(profile_id)
    if prof is None or prof.deferred:
        return None
    cells = [c for c in doc.get("cells", []) if c.get("kind") == "size"]
    power_ref = [
        c
        for c in doc.get("cells", [])
        if c.get("kind") == "power" and c.get("generator") == "iid_normal"
    ]
    out: dict[str, Any] = {}
    for proc in prof.procedures:
        power_ok = bool(power_ref) and all(
            c.get("procedures", {}).get(proc, {}).get("result") == "pass" for c in power_ref
        )
        passed: dict[str, set[int]] = {}
        failed: set[str] = set()
        for cell in cells:
            key = _gen_key(cell["generator"], cell["params"])
            r = cell.get("procedures", {}).get(proc)
            if r is None:
                failed.add(key)
                continue
            if r.get("result") == "pass":
                passed.setdefault(key, set()).add(int(cell["T"]))
            else:
                failed.add(key)
        gens = [
            (cell["generator"], dict(cell["params"]))
            for cell in cells
            if _gen_key(cell["generator"], cell["params"]) not in failed
            and passed.get(_gen_key(cell["generator"], cell["params"]), set()) >= set(prof.T_grid)
        ]
        uniq: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for g, p in gens:
            if _gen_key(g, p) not in seen:
                seen.add(_gen_key(g, p))
                uniq.append((g, p))
        if not power_ok:
            uniq = []  # an underpowered procedure has no operating domain (framework section 2)
        out[proc] = {
            "generators": uniq,
            "T_min": min(prof.T_grid),
            "reference_passes": ("iid_normal", {}) in uniq and power_ok,
            "reference_power_passes": power_ok,
            "acf1_max": max([generator_acf1(g, p) for g, p in uniq], default=0.0),
            "garch_in_domain": any(g == "garch11" for g, _ in uniq),
            "t3_in_domain": any(g == "t3" for g, _ in uniq),
        }
    return out


def _gen_key(generator: str, params: Mapping[str, Any]) -> str:
    return generator + json.dumps(dict(params), sort_keys=True)


def _bounds(dom: Mapping[str, Any], T: int) -> dict[str, float]:
    band = DEPENDENCE_BAND_MULT / math.sqrt(T)
    kurt = 0.0
    if dom["garch_in_domain"]:
        kurt = GARCH_EXCESS_KURTOSIS
    if dom["t3_in_domain"]:
        kurt = KURTOSIS_T3_CAP
    return {
        "acf1": float(dom["acf1_max"]) + band,
        "sq_acf1": (GARCH_SQ_ACF1 if dom["garch_in_domain"] else 0.0) + band,
        "excess_kurtosis": kurt + KURTOSIS_BAND,
    }


# ---------------------------------------------------------- the series read --
def current_identity() -> str:
    """The process identity (the runner's own rule: USERNAME / USER / LOGNAME, then getpass)."""
    for var in ("USERNAME", "USER", "LOGNAME"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    import getpass

    return getpass.getuser()


@dataclass(frozen=True)
class SeriesRead:
    """A series read through :func:`read_series`: the values, the path, its sha256, the
    identity that read it and the guard purpose.  Constructible only by :func:`read_series`."""

    values: np.ndarray = field(repr=False, compare=False)
    path: str
    sha256: str
    identity: str
    purpose: str
    _key: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _TOKEN_KEY:
            raise PermissionError(
                "a SeriesRead is produced only by iap.validation.profiles.read_series"
            )


def read_series(
    path: Path | str, registration: Mapping[str, Any], *, identity: str | None = None
) -> SeriesRead:
    """Read the confirmation series file (JSON ``{"values": [[...]]}`` of shape ``(T, K)``)
    through the data-read choke point :func:`iap.core.guard.check` with the purpose
    ``confirmation:<profile>:<registration sha256>``.  The guard's refusal propagates."""
    p = Path(path)
    purpose = f"confirmation:{registration.get('profile')}:{registration_sha256(registration)[:16]}"
    guard.check(p, purpose)
    raw = p.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    values = np.asarray(doc["values"], dtype=float)
    who = identity if identity is not None else current_identity()
    return SeriesRead(
        values, p.as_posix(), hashlib.sha256(raw).hexdigest(), who, purpose, _TOKEN_KEY
    )


def registration_sha256(registration: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(registration, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# ------------------------------------------------------------------- verdict --
class ProfileToken:
    """The PROMOTE token of the profile gate; constructible only by :func:`decide`."""

    __slots__ = ("sha256",)

    def __init__(self, key: object, sha256: str) -> None:
        if key is not _TOKEN_KEY:
            raise PermissionError(
                "PROMOTE under a profile is constructed only by iap.validation.profiles.decide"
            )
        self.sha256 = str(sha256)

    def __repr__(self) -> str:
        return f"ProfileToken({self.sha256[:12]})"


@dataclass(frozen=True)
class ProfileVerdict:
    outcome: str
    reason: str
    profile: str
    procedure: str
    T: int
    K: int
    alpha_op: float
    hypotheses: tuple[str, ...]
    promoted: tuple[str, ...] = ()
    statistics: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    bounds: dict[str, float] = field(default_factory=dict)
    economic: tuple[dict[str, Any], ...] = ()
    read: dict[str, Any] = field(default_factory=dict)
    inference_sha256: str = ""
    token: ProfileToken | None = field(default=None, compare=False, repr=False)

    @property
    def refused(self) -> bool:
        return self.outcome.startswith(REFUSED)

    @property
    def is_promote(self) -> bool:
        return self.outcome == PROMOTE and self.token is not None

    def to_json(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "token"}
        for key in ("hypotheses", "promoted"):
            d[key] = list(d[key])
        for key in ("statistics", "diagnostics", "economic"):
            d[key] = [dict(s) for s in d[key]]
        d["gate"] = "confirmation_profile"
        return d


def _refuse(reason: str, profile_id: str, reg: Mapping[str, Any], **extra: Any) -> ProfileVerdict:
    fam = reg.get("family") if isinstance(reg, Mapping) else None
    hyps = tuple(str(h) for h in fam) if isinstance(fam, (list, tuple)) else ()
    return ProfileVerdict(
        outcome=f"{REFUSED}: {reason}",
        reason=reason,
        profile=str(profile_id),
        procedure=str(reg.get("procedure", "")) if isinstance(reg, Mapping) else "",
        T=0,
        K=len(hyps),
        alpha_op=ALPHA_OP,
        hypotheses=hyps,
        **extra,
    )


def _registration_checks(
    prof: Profile, dom: Mapping[str, Any], reg: Mapping[str, Any]
) -> str | None:
    if not isinstance(reg, Mapping):
        return "registration record missing"
    if reg.get("profile") != prof.id:
        return "registration names another profile"
    if reg.get("statistic") != prof.statistic:
        return "unregistered statistic"
    h = reg.get("horizon_sessions")
    if (
        not isinstance(h, int)
        or isinstance(h, bool)
        or not (prof.horizon[0] <= h <= prof.horizon[1])
    ):
        return "horizon outside the profile"
    fam = reg.get("family")
    if not isinstance(fam, (list, tuple)) or not fam or len(set(map(str, fam))) != len(fam):
        return "empty or duplicated family"
    proc = reg.get("procedure")
    if proc not in prof.procedures:
        return "procedure not a candidate of the profile"
    if not dom.get(proc, {}).get("reference_passes") or not dom[proc]["generators"]:
        return "procedure outside validated domain"
    if float(reg.get("alpha_op", -1)) != ALPHA_OP:
        return "alpha_op differs from the framework"
    ss = reg.get("sample_size")
    if (
        not isinstance(ss, Mapping)
        or not isinstance(ss.get("T_min"), int)
        or ss["T_min"] < dom[proc]["T_min"]
    ):
        return "sample-size rule missing or below the validated T"
    if float(ss.get("cap", 0)) != 1.25:
        return "sample-size cap is not the framework's 1.25"
    rc = reg.get("refusal_conditions")
    if not isinstance(rc, (list, tuple)) or tuple(rc) != DIAGNOSTICS:
        return "refusal conditions do not name the profile's diagnostics"
    spec = prof.procedures[proc]
    if spec["kind"] == "hac":
        if reg.get("hac_lag") != spec["lag"]:
            return "HAC lag is not the profile's pre-fixed lag"
    else:
        bl = reg.get("block_length")
        if not isinstance(bl, Mapping) or not isinstance(bl.get("diagnostic"), Mapping):
            return "block length without its development diagnostic"
        diag = bl["diagnostic"]
        if diag.get("source") != "development" or not isinstance(
            diag.get("development_sha256"), str
        ):
            return "block-length diagnostic not bound to development data"
        try:
            want = _block_from_acf(
                diag, floor=spec["floor"], cap=spec["cap"], multiplier=spec["multiplier"]
            )
        except (KeyError, TypeError, ValueError):
            return "block-length diagnostic malformed"
        if (
            len(diag["acf"]) != spec["max_lag"]
            or int(diag.get("T_dev", 0)) < 2 * spec["max_lag"] + 2
        ):
            return "block-length diagnostic does not cover the profile's lags"
        if bl.get("block_length") != want["block_length"]:
            return "block length is not the declared rule applied to the development diagnostic"
    es = reg.get("economic_screen")
    if (
        not isinstance(es, Mapping)
        or not (float(es.get("capital_usd", 0)) > 0)
        or "hurdle_bps" not in es
        or "margin" not in es
    ):
        return "economic screen missing (capital, hurdle, common margin)"
    r = reg.get("registry")
    if (
        not isinstance(r, Mapping)
        or r.get("window_state") != "fresh"
        or int(r.get("confirmation_dataset_looks", 1)) != 0
    ):
        return "window spent or looks on the confirmation dataset"
    data = reg.get("data")
    if not isinstance(data, Mapping) or data.get("origin") not in ("synthetic", "real"):
        return "data origin not declared"
    return None


def _series_checks(
    prof: Profile, reg: Mapping[str, Any], series: Any
) -> tuple[str | None, np.ndarray, dict[str, Any]]:
    origin = reg["data"]["origin"]
    read: dict[str, Any] = {}
    if isinstance(series, SeriesRead):
        read = {
            "path": series.path,
            "sha256": series.sha256,
            "identity": series.identity,
            "purpose": series.purpose,
        }
        if origin == "real" and series.identity != RUNNER_ACCOUNT:
            return "series read by an identity other than the registered runner", np.empty(0), read
        values = series.values
    else:
        if origin == "real":
            return (
                "real series not read through read_series by the registered runner",
                np.empty(0),
                read,
            )
        values = np.asarray(series, dtype=float)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.shape[1] != len(reg["family"]):
        return "series shape differs from the family", values, read
    T = values.shape[0]
    if T < int(reg["sample_size"]["T_min"]):
        return "fewer sessions than registered", values, read
    if not np.all(np.isfinite(values)):
        return "non-finite values", values, read
    if np.any(values.var(axis=0) <= 0.0):
        return "zero variance", values, read
    return None, values, read


def decide(
    profile_id: str,
    series: Any,
    registration: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    seed: int | None = None,
) -> ProfileVerdict:
    """The profile gate: ``series`` is a :class:`SeriesRead` (required for real data) or
    an array of shape ``(T, K)`` (synthetic only); ``registration`` the framework's
    registration record.  ``seed`` fixes the bootstrap's resample stream (``P-CBB``);
    a registration under P-CBB names its own ``bootstrap_seed``.  Refusals in the order of
    the module docstring; the outcome is PROMOTE for the family when Holm rejects at
    least one hypothesis whose economic screen passes, else REJECT."""
    reg = registration if isinstance(registration, Mapping) else {}
    prof = PROFILES.get(str(profile_id))
    if prof is None:
        return _refuse("unknown profile", str(profile_id), reg)
    if not isinstance(registration, Mapping):
        return _refuse("registration record missing", prof.id, reg)
    if prof.deferred:
        return _refuse(f"profile not validated ({prof.id} deferred)", prof.id, reg)
    dom = operating_domain(prof.id, root)
    if dom is None:
        return _refuse("profile not validated (no committed evidence)", prof.id, reg)
    reason = _registration_checks(prof, dom, reg)
    if reason:
        return _refuse(reason, prof.id, reg)
    reason, values, read = _series_checks(prof, reg, series)
    if reason:
        return _refuse(reason, prof.id, reg, read=read)
    proc = str(reg["procedure"])
    T, K = values.shape
    hyps = tuple(str(h) for h in reg["family"])
    bounds = _bounds(dom[proc], T)
    diags = []
    for j in range(K):
        d = diagnostics(values[:, j])
        d["hypothesis"] = hyps[j]
        diags.append(d)
        for name in DIAGNOSTICS:
            if not (d[name] <= bounds[name]):
                return _refuse(
                    f"dependence assumptions unsupported ({name} {d[name]:.3f} above the validated bound {bounds[name]:.3f})",
                    prof.id,
                    reg,
                    diagnostics=tuple(diags),
                    bounds=bounds,
                    read=read,
                )
    spec = prof.procedures[proc]
    rows: list[dict[str, Any]] = []
    ps = np.empty(K)
    for j in range(K):
        if spec["kind"] == "hac":
            st = hac_pvalue(values[:, j], spec["lag"])
        else:
            s = seed if seed is not None else reg["block_length"].get("bootstrap_seed")
            if s is None:
                return _refuse("bootstrap seed not registered", prof.id, reg, read=read)
            st = cbb_pvalue(
                values[:, j],
                reg["block_length"]["block_length"],
                resamples=spec["resamples"],
                seed=int(s) + j,
            )
        if not math.isfinite(st["p"]):
            return _refuse("degenerate statistic", prof.id, reg, read=read)
        st["hypothesis"] = hyps[j]
        rows.append(st)
        ps[j] = st["p"]
    h = _c.holm(ps, ALPHA_OP)
    es = reg["economic_screen"]
    hurdle = float(es["hurdle_bps"]) * (1.0 + float(es["margin"]))
    econ = []
    promoted = []
    for j, row in enumerate(rows):
        row["p_holm"] = float(h["adjusted"][j])
        row["rejected"] = bool(h["rejected"][j])
        mean_bps = float(values[:, j].mean())
        passes = mean_bps >= hurdle
        econ.append(
            {
                "hypothesis": hyps[j],
                "mean_bps": mean_bps,
                "hurdle_bps_with_margin": hurdle,
                "passes": passes,
            }
        )
        if row["rejected"] and passes:
            promoted.append(hyps[j])
    outcome = PROMOTE if promoted else REJECT
    obj = {
        "gate": "confirmation_profile",
        "profile": prof.id,
        "procedure": proc,
        "registration_sha256": registration_sha256(reg),
        "read": read,
        "statistics": rows,
        "economic": econ,
        "promoted": promoted,
        "outcome": outcome,
    }
    sha = hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=float).encode()
    ).hexdigest()
    return ProfileVerdict(
        outcome=outcome,
        reason=f"Holm at alpha_op {ALPHA_OP:g} over K = {K} with the economic screen: {len(promoted)} promotion(s)",
        profile=prof.id,
        procedure=proc,
        T=int(T),
        K=int(K),
        alpha_op=ALPHA_OP,
        hypotheses=hyps,
        promoted=tuple(promoted),
        statistics=tuple(rows),
        diagnostics=tuple(diags),
        bounds=bounds,
        economic=tuple(econ),
        read=read,
        inference_sha256=sha,
        token=ProfileToken(_TOKEN_KEY, sha) if outcome == PROMOTE else None,
    )

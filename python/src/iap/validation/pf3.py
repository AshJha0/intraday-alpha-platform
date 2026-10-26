"""PF-3, the combined multi-horizon book: the U13 confirmation procedure for Track C
(v2.0 stage 10; plan section 15.6 and V2-C6).

Specification: ``research/v2/U13_CONFIRMATION_PROTOCOL.md``.  Evidence:
``research/v2/confirmation_evidence/PF-3_STUDY.json``, written once by
``python/tests/spec/pf3_suite.py --write``; this module READS it and nothing else decides
what is in the procedure's operating domain.

**Status: pre-freeze.**  :func:`evaluate` returns an inference record, never a PROMOTE
verdict.  ``iap.validation.profiles.decide`` keeps refusing PF-3 until the reviewer panel
freezes this protocol; the panel's freeze PR wires :func:`evaluate` into that constructor.
R0 (``iap.validation.confirmatory``) and PF-1/PF-2 are untouched.

**Procedure** ``P-SCBB``: a studentized circular block bootstrap of the mean (bootstrap-t).
The block length ``L`` is the declared rule of :func:`iap.validation.profiles.block_length_rule`
(Ljung-Box tail test, multiplier 2, floor 5, cap 60, ``max_lag`` 60) applied to the
DEVELOPMENT series of the registered book before registration.  With ``cb`` the ``T``
circular block means of length ``L`` and ``m`` the sample mean, the statistic is
``t = m / s`` with ``s^2 = g0 * max(1, 1 + 2 g1 / g0) / nb``, ``nb = ceil(T / L)``,
``g0 = mean((cb - m)^2)`` and ``g1 = mean((cb_t - m)(cb_{t+L} - m))`` the covariance of
abutting blocks (the adjacent-block correction: dependence longer than ``L`` makes
abutting blocks covary and the uncorrected ``s`` too small; the floor at 1 keeps the
noise of ``g1`` from shrinking ``s`` under independence).  Each of ``B``
resamples draws ``nb`` block means ``b*`` uniformly from ``cb`` and forms
``t* = (mean(b*) - m) / (sd(b*, ddof=1) / sqrt(nb))``.  One-sided p-value
``(1 + #{t* >= t}) / (B + 1)``.  The percentile form validated nowhere in PF-1/PF-2
(D-73); the studentized form carries its own variance and is the candidate here.

Holm step-down over the registered family ``K_C = 3`` (C0, C1, C2) at one-sided
``alpha_op = 0.020``; only the configuration selected on development data before
registration can be promoted (the strategy-selection adjustment).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from iap.validation import profiles as _pf

__all__ = [
    "ALPHA_OP",
    "FAMILY",
    "PROCEDURE",
    "SCREENS",
    "accrual_status",
    "dependence_length_many",
    "evaluate",
    "operating_domain",
    "registration_refusals",
    "scbb_pvalue",
    "scbb_pvalues_many",
    "screen_stats_many",
]

PROFILE_ID = "PF-3"
PROFILE_INDEX = 3
ALPHA_OP = _pf.ALPHA_OP
STATISTIC = "combined_book_net_session_return"
FAMILY = ("C0", "C1", "C2")  # K_C, fixed: the registered C0-C2 configurations
K_C = len(FAMILY)
PROCEDURE = "P-SCBB"
RESAMPLES = 999
MAX_LAG = 60
FLOOR = 5
CAP = 60
MULTIPLIER = 2
T_GRID = (250, 500)
WINDOW_CAP = 1.25
SEALED_PARTITION = "iap-sealed-c"
SCREENS = ("dep_length", "vol_cluster", "excess_kurtosis")
SCREEN_QUANTILE = 0.995  # a screen's bound: the largest in-domain cell quantile at this level
STUDY_FILE = Path("research") / "v2" / "confirmation_evidence" / "PF-3_STUDY.json"
_ROOT = Path(__file__).resolve().parents[4]


# ------------------------------------------------------------- the procedure --
def _studentize_setup(x: np.ndarray, L: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    nb = -(-x.shape[1] // L)
    cb = np.ascontiguousarray(np.transpose(_pf._circular_block_means(x, L), (0, 2, 1)))
    m = x.mean(axis=1)
    d = cb - m[:, :, None]
    g0 = (d * d).mean(axis=2)
    g1 = (d * np.roll(d, -L, axis=2)).mean(axis=2)  # adjacent (abutting) blocks
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(g0 > 0.0, np.maximum(1.0, 1.0 + 2.0 * g1 / g0), 1.0)
    s = np.sqrt(g0 * f / nb)
    return cb, m, s, nb


def scbb_pvalues_many(
    x: np.ndarray,
    L: int,
    resamples: int,
    rng: np.random.Generator,
    *,
    sub_chunk_elements: int = 8_000_000,
) -> np.ndarray:
    """Studentized circular block bootstrap p-values: ``x`` of shape ``(n, T, K)`` -> ``(n,
    K)``.  Block starts are ``rng.integers(0, T)`` of shape ``(datasets, K, B * nb)`` per
    sub-chunk, in dataset order then hypothesis order.  NaN where the series has no
    variance; a resample with zero spread contributes ``t* = -inf`` (never counted)."""
    n_, _T, K = x.shape
    L = int(L)
    cb, m, s, nb = _studentize_setup(x, L)
    with np.errstate(divide="ignore", invalid="ignore"):
        t0 = np.where(s > 0.0, m / s, np.nan)
    out = np.empty((n_, K))
    step = max(1, int(sub_chunk_elements // (K * resamples * nb)))
    T = x.shape[1]
    for i0 in range(0, n_, step):
        i1 = min(n_, i0 + step)
        idx = rng.integers(0, T, size=(i1 - i0, K, resamples * nb))
        b = np.take_along_axis(cb[i0:i1], idx, axis=2).reshape(i1 - i0, K, resamples, nb)
        mb = b.mean(axis=3)
        sb = b.std(axis=3, ddof=1) / math.sqrt(nb)
        with np.errstate(divide="ignore", invalid="ignore"):
            ts = np.where(sb > 0.0, (mb - m[i0:i1, :, None]) / sb, -np.inf)
        out[i0:i1] = (1.0 + np.count_nonzero(ts >= t0[i0:i1, :, None], axis=2)) / (resamples + 1.0)
    return np.where(np.isfinite(t0), out, np.nan)


def scbb_pvalue(
    x: Sequence[float] | np.ndarray, L: int, *, resamples: int = RESAMPLES, seed: int
) -> dict[str, float]:
    """One series: the statistic, its standard error and the p-value (rng ``default_rng(seed)``)."""
    a = np.asarray(x, dtype=float).reshape(1, -1, 1)
    _cb, m, s, nb = _studentize_setup(a, int(L))
    p = scbb_pvalues_many(a, int(L), resamples, np.random.default_rng(seed))
    return {
        "mean": float(m[0, 0]),
        "se": float(s[0, 0]),
        "t": float(m[0, 0] / s[0, 0]) if s[0, 0] > 0 else float("nan"),
        "blocks": int(nb),
        "p": float(p[0, 0]),
    }


# --------------------------------------------------------- domain screens ----
def _acf_many(x: np.ndarray, max_lag: int) -> np.ndarray:
    """Biased sample autocorrelations at lags 1..max_lag along axis 1: ``(n, max_lag, K)``."""
    d = x - x.mean(axis=1, keepdims=True)
    s0 = (d * d).sum(axis=1)
    out = np.empty((x.shape[0], max_lag, x.shape[2]))
    for k in range(1, max_lag + 1):
        out[:, k - 1, :] = (d[:, k:, :] * d[:, :-k, :]).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return out / s0[:, None, :]


def dependence_length_many(x: np.ndarray, max_lag: int = MAX_LAG) -> np.ndarray:
    """``L_dep`` of :func:`iap.validation.profiles.block_length_rule`, vectorised over
    ``(n, T, K)``: the smallest ``L`` whose Ljung-Box tail statistic over lags ``L+1 ..
    max_lag`` is at or below the 95 % chi-square quantile with ``max_lag - L`` df."""
    from scipy import stats

    T = x.shape[1]
    a = _acf_many(x, max_lag)
    k = np.arange(1, max_lag + 1)
    terms = T * (T + 2.0) * a * a / (T - k)[None, :, None]
    tail = np.cumsum(terms[:, ::-1, :], axis=1)[:, ::-1, :]  # tail[L] = sum_{k > L}
    crit = stats.chi2.ppf(1.0 - _pf.LJUNG_BOX_LEVEL, max_lag - np.arange(max_lag))
    ok = tail <= crit[None, :, None]
    first = np.where(ok.any(axis=1), ok.argmax(axis=1), max_lag)
    return first.astype(int)


def screen_stats_many(x: np.ndarray, max_lag: int = MAX_LAG) -> dict[str, np.ndarray]:
    """The three declared screens per series, each ``(n, K)``:

    * ``dep_length``: ``L_dep`` on the confirmation series (serial dependence and
      overlapping exposures);
    * ``vol_cluster``: lag-1 autocorrelation of squares minus the squared lag-1
      autocorrelation (volatility clustering beyond what linear dependence alone puts into
      the squares; zero for a Gaussian linear process at lag 1);
    * ``excess_kurtosis``: bias-corrected (non-normal tails)."""
    from scipy import stats

    a1 = _acf_many(x, 1)[:, 0, :]
    sq = _acf_many(x * x, 1)[:, 0, :]
    return {
        "dep_length": dependence_length_many(x, max_lag),
        "vol_cluster": sq - a1 * a1,
        "excess_kurtosis": stats.kurtosis(x, axis=1, fisher=True, bias=False),
    }


# ------------------------------------------------------------ accrual rule ---
def accrual_status(
    *, usable_sessions: int, attempted_sessions: int, T_min: int, cap: float = WINDOW_CAP
) -> str:
    """The forward window's state from COUNTS only (no returns are passed, so this
    function cannot peek): ``accruing`` until ``T_min`` usable sessions; ``complete`` at
    ``T_min`` (the single read is now due); ``pending`` once ``cap * T_min`` sessions were
    attempted without reaching ``T_min`` (nothing is examined)."""
    if usable_sessions < 0 or attempted_sessions < usable_sessions:
        raise ValueError("usable sessions must be between 0 and the attempted sessions")
    if usable_sessions >= T_min:
        return "complete"
    if attempted_sessions >= math.floor(cap * T_min):
        return "pending"
    return "accruing"


# ---------------------------------------------------------------- evidence ---
def load_study(root: Path | str | None = None) -> dict[str, Any] | None:
    f = Path(root or _ROOT) / STUDY_FILE
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def _cell_key(c: Mapping[str, Any]) -> str:
    p = c.get("params") or {}
    return c["generator"] + (json.dumps(p, sort_keys=True) if p else "")


def operating_domain(study: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Generators whose size cells pass at every T of the grid; screen bounds per T as the
    largest in-domain ``SCREEN_QUANTILE`` quantile; ``None`` without a full study or when
    the reference (iid normal) size or power cells do not all pass."""
    if not study or study.get("status") != "full":
        return None
    cells = study["cells"]
    gens: dict[str, list[dict[str, Any]]] = {}
    for c in cells:
        if c["kind"] == "size":
            gens.setdefault(_cell_key(c), []).append(c)
    ref_power = [c for c in cells if c["kind"] == "power" and c["generator"] == "iid_normal"]
    if not all(c["result"] == "pass" for c in gens.get("iid_normal", [])) or not ref_power:
        return None
    if not all(c["result"] == "pass" for c in ref_power):
        return None
    inside = sorted(g for g, cs in gens.items() if all(c["result"] == "pass" for c in cs))
    bounds: dict[str, dict[str, float]] = {}
    for T in study["T_grid"]:
        rows = [gens[g] for g in inside]
        sel = [c for cs in rows for c in cs if c["T"] == T]
        bounds[str(T)] = {s: max(float(c["screen_q"][s]) for c in sel) for s in SCREENS}
    max_h = max((int(c["params"].get("h_long", 0)) for g in inside for c in gens[g]), default=0)
    return {"generators": inside, "bounds": bounds, "max_component_horizon": max_h}


# -------------------------------------------------------- the registration ---
def registration_sha256(reg: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(reg, sort_keys=True).encode("utf-8")).hexdigest()


def registration_refusals(reg: Mapping[str, Any], domain: Mapping[str, Any] | None) -> list[str]:
    """Every reason the registration is outside the protocol (empty = admissible).  Checked
    before any series is read; ``evaluate`` refuses on the first."""
    r: list[str] = []
    if domain is None:
        return ["profile not validated (no full PF-3 study with passing reference cells)"]
    if reg.get("profile") != PROFILE_ID:
        r.append("registration names another profile")
    if reg.get("statistic") != STATISTIC:
        r.append("unregistered statistic")
    if tuple(reg.get("family") or ()) != FAMILY:
        r.append("family is not the fixed K_C = 3 configurations C0, C1, C2")
    if reg.get("selected") not in FAMILY:
        r.append("no configuration selected on development data before registration")
    if reg.get("procedure") != PROCEDURE:
        r.append("procedure is not P-SCBB")
    if reg.get("alpha_op") != ALPHA_OP:
        r.append("alpha_op is not 0.020")
    bl = reg.get("block_length") or {}
    diag = bl.get("diagnostic") or {}
    if diag.get("source") != "development" or not diag.get("development_sha256"):
        r.append("block length without a development-data diagnostic")
    elif len(diag.get("acf") or []) != MAX_LAG or int(diag.get("T_dev", 0)) < 2 * MAX_LAG + 2:
        r.append("development diagnostic short of the declared lags")
    else:
        rule = _pf._block_from_acf(diag, floor=FLOOR, cap=CAP, multiplier=MULTIPLIER)
        if int(bl.get("block_length", -1)) != rule["block_length"]:
            r.append("block length is not the declared rule applied to the development diagnostic")
    ss = reg.get("sample_size") or {}
    if int(ss.get("T_min", 0)) < min(T_GRID) or ss.get("cap") != WINDOW_CAP:
        r.append("sample-size rule is not T_min >= 250 with cap 1.25")
    hz = reg.get("component_horizons") or []
    if not hz or max(hz) > max(1, int(domain["max_component_horizon"])):
        r.append("a component horizon lies outside the validated overlapping-exposure domain")
    if list(reg.get("refusal_conditions") or []) != list(SCREENS):
        r.append("refusal conditions are not the declared screens")
    es = reg.get("economic_screen") or {}
    if not (float(es.get("capital_usd", 0)) > 0 and es.get("on") == "combined_capital"):
        r.append("economic screen missing or not on the combined registered capital")
    rg = reg.get("registry") or {}
    if rg.get("window_state") != "fresh" or int(rg.get("confirmation_dataset_looks", 1)) != 0:
        r.append("spent window or looks on the confirmation dataset")
    data = reg.get("data") or {}
    if data.get("origin") not in ("synthetic", "real"):
        r.append("data origin undeclared")
    elif data.get("origin") == "real" and data.get("partition") != SEALED_PARTITION:
        r.append("Track C confirmation must read iap-sealed-c (never Track B's sealed test)")
    if "bootstrap_seed" not in reg:
        r.append("bootstrap seed not registered")
    return r


def evaluate(
    reg: Mapping[str, Any],
    series: np.ndarray,
    *,
    study: Mapping[str, Any] | None = None,
    read: _pf.SeriesRead | None = None,
) -> dict[str, Any]:
    """Apply the protocol once.  ``series`` is ``(T, 3)`` in FAMILY order, net session
    returns on the registered combined capital (conservative column).  Real data must come
    with the runner's ``read`` record (``iap.validation.profiles.read_series``).  Returns
    ``{"outcome": "refused: ..."}`` or an inference record with the Holm rejections, the
    economic screen and ``promotable`` (the selected configuration only)."""
    reg_sha = registration_sha256(reg)
    domain = operating_domain(study if study is not None else load_study())

    def refuse(reason: str, **extra: Any) -> dict[str, Any]:
        return {"outcome": f"{_pf.REFUSED}: {reason}", "registration_sha256": reg_sha, **extra}

    reasons = registration_refusals(reg, domain)
    if reasons:
        return refuse(reasons[0], all_reasons=reasons)
    assert domain is not None
    if (reg.get("data") or {}).get("origin") == "real":
        if read is None:
            return refuse("real-data series not read by the registered runner")
        if read.identity != _pf.RUNNER_ACCOUNT:
            return refuse("real-data series read by an identity other than iap-runner")
        series = read.values  # the runner's read is the only real-data source
    x = np.asarray(series, dtype=float)
    if x.ndim != 2 or x.shape[1] != K_C:
        return refuse("series shape differs from the family")
    T = x.shape[0]
    if T < int(reg["sample_size"]["T_min"]):
        return refuse("fewer sessions than T_min")
    if not np.all(np.isfinite(x)):
        return refuse("non-finite values")
    if np.any(np.ptp(x, axis=0) == 0.0):
        return refuse("zero variance")
    Tb = str(max(t for t in T_GRID if t <= T))
    bounds = domain["bounds"][Tb]
    sc = screen_stats_many(x[None, :, :])
    stats_ = {s: [float(v) for v in sc[s][0]] for s in SCREENS}
    L = int(reg["block_length"]["block_length"])
    for j, cfg in enumerate(FAMILY):
        for s in SCREENS:
            if sc[s][0, j] > bounds[s]:
                return refuse(f"{s} outside the validated domain in {cfg}", screens=stats_)
    rng = np.random.default_rng(int(reg["bootstrap_seed"]))
    p = scbb_pvalues_many(x[None, :, :], L, RESAMPLES, rng)[0]
    rej = _pf.holm_many(p[None, :], ALPHA_OP)[0]
    es = reg["economic_screen"]
    hurdle = float(es.get("hurdle_bps", 0.0)) * (1.0 + float(es.get("margin", 0.0))) / 1e4
    means = x.mean(axis=0)
    sel = FAMILY.index(reg["selected"])
    econ = [bool(means[j] >= hurdle) for j in range(K_C)]
    return {
        "outcome": "inference (pre-freeze; not a verdict)",
        "registration_sha256": reg_sha,
        "T": T,
        "block_length": L,
        "p": [float(v) for v in p],
        "holm_reject": [bool(v) for v in rej],
        "mean_return": [float(v) for v in means],
        "economic_screen": econ,
        "screens": stats_,
        "screen_bounds": bounds,
        "selected": reg["selected"],
        "promotable": bool(rej[sel] and econ[sel]),
        "read": None if read is None else {"path": read.path, "sha256": read.sha256},
    }

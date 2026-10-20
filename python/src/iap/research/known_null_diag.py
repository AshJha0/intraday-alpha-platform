"""REL-06 known-null protocol v3 (revision 4): A0 centring invariant, residual-mean
diagnostic (descriptive), SE understatement ratio, interpretation gate and verdict.

Pure functions on arrays of per-trial statistics (no data access, no I/O).  The v3
runner (``tools/rel06_known_null_v3.py`` with ``iap.research.known_null_v3``) calls
exactly these functions for the centring invariant, the B blocker, the gate and the
verdict (``research/v118/NULL_PROTOCOL_v3.md`` sections 3.3, 3.4, 6).

Roles: the A0 centring invariant is a HARD construction check (failure = invalid A0
run, not a statistical finding).  The residual-mean flag is DESCRIPTIVE for A0 (never a
verdict).  For B it keeps its v3 role (a flag blocks PASS), a point put to the reviewer.
The understatement gate is interpretive and cannot override a FAIL.

``residual_mean_flag`` has two modes:

* ``independent`` (generator B): trials are independent realisations, SE = sd / sqrt(n).
* ``dependent`` (A0): the trials are the 1,854 derangements of one dataset; they are
  dependent, and ``n_eff`` (the number of sessions, 7) is a HEURISTIC effective sample
  size.  Its false-flag probability is not calibrated; see ``measure_*`` below.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

FLAG_SE = 4.0

#: A0 centring invariant: |group mean| <= CENTRING_TOL_REL * max(|label| in group).
#: Float64 rounding of a mean of centred values is about 1e-16 of the scale; 1e-9 leaves
#: seven orders of margin and is far below any economically meaningful label mean.
CENTRING_TOL_REL = 1e-9
N_HYPOTHESES = 16
#: option A calibration acceptance counts (protocol 6); frozen, not tunable
B_MAX_COUNT = 60  # of 2,000
A0_MAX_COUNT = 55  # of 1,854
PASS_LABEL_GATE = "PASS (B-supported only; A0 non-informative)"
PASS_LABEL_PLAIN = (
    "PASS (B on the repeated-simulation process; A0 as a conditional randomisation "
    "diagnostic on these seven sessions)"
)


CLAIM_SCOPE = (
    "A B pass means the observed FWER in the specified independent synthetic trials met "
    "the pre-specified calibration threshold. An A0 pass means the exact conditional "
    "rejection proportion over the specified transformations met its threshold. Neither, "
    "alone or combined, proves that the true FWER on real trading data is controlled at "
    "2.5 %."
)
VERDICT_PASS_B_ONLY = "PASS_B_ONLY"


class CentringInvariantError(RuntimeError):
    """Construction / implementation failure of the A0 centring (protocol 3.3)."""


def centre_labels(labels: np.ndarray, name: np.ndarray, session: np.ndarray) -> np.ndarray:
    """A0 construction for one horizon: subtract the per-(name, session) mean of the
    finite labels.  Rows with non-finite labels stay NaN and do not enter the mean."""
    x = np.asarray(labels, dtype=float)
    out = np.full(x.shape, np.nan)
    ok = np.isfinite(x)
    nm = np.asarray(name, dtype=np.int64)
    ss = np.asarray(session, dtype=np.int64)
    for n in np.unique(nm[ok]):
        for s in np.unique(ss[ok & (nm == n)]):
            sel = ok & (nm == n) & (ss == s)
            out[sel] = x[sel] - x[sel].mean()
    return out


def check_centring_invariant(
    centred_by_horizon: Mapping[str, np.ndarray],
    name: np.ndarray,
    session: np.ndarray,
    required_names: Sequence[int] | Mapping[str, Sequence[int]],
    required_sessions: Sequence[int],
    tol_rel: float = CENTRING_TOL_REL,
) -> dict[str, Any]:
    """HARD construction check (protocol 3.3) on the centred labels BEFORE the derangement.

    Groups: every (name, horizon, session) in the cross product of the required names
    (one list for all horizons, or a mapping horizon -> names), the horizons present and
    the required sessions.  A group with no finite label is a
    failure ("empty group"), never skipped; an infinite label is a failure; otherwise a
    group fails iff |mean of its finite labels| > tol_rel * max|label| (an all-zero group
    passes).  Returns ``ok`` and the list of failures."""
    nm = np.asarray(name, dtype=np.int64)
    ss = np.asarray(session, dtype=np.int64)
    failures: list[dict[str, Any]] = []
    worst = 0.0
    n_groups = 0
    for h, lab in sorted(centred_by_horizon.items()):
        x = np.asarray(lab, dtype=float)
        if x.shape != nm.shape or ss.shape != nm.shape:
            raise ValueError("labels, name and session must have equal shape")
        if np.any(np.isinf(x)):
            failures.append({"horizon": h, "reason": "infinite label"})
        names = required_names[h] if isinstance(required_names, Mapping) else required_names
        for n in names:
            for s in required_sessions:
                n_groups += 1
                v = x[(nm == n) & (ss == s)]
                v = v[np.isfinite(v)]
                if v.size == 0:
                    failures.append(
                        {"name": n, "horizon": h, "session": s, "reason": "empty group"}
                    )
                    continue
                mean = float(v.mean())
                worst = max(worst, abs(mean))
                if abs(mean) > tol_rel * float(np.max(np.abs(v))):
                    failures.append(
                        {
                            "name": n,
                            "horizon": h,
                            "session": s,
                            "reason": "mean not zero",
                            "mean": mean,
                        }
                    )
    return {"ok": not failures, "n_groups": n_groups, "max_abs_mean": worst, "failures": failures}


def verify_series_used(used: np.ndarray, centred: np.ndarray) -> bool:
    """The array the derangement reads must be the checked centred array: bit-identical
    (NaN equal NaN)."""
    a = np.asarray(used, dtype=float)
    b = np.asarray(centred, dtype=float)
    return bool(a.shape == b.shape and np.array_equal(a, b, equal_nan=True))


def require_centring_invariant(result: Mapping[str, Any]) -> None:
    """Abort the A0 run (construction failure) if the invariant check failed."""
    if not result["ok"]:
        raise CentringInvariantError(f"A0 centring invariant failed: {result['failures'][:5]}")


def residual_mean_flag(
    stats: np.ndarray,
    mode: str,
    n_eff: int | None = None,
    flag_se: float = FLAG_SE,
) -> dict[str, Any]:
    """Residual-mean diagnostic for one hypothesis' per-trial S2 statistic.

    DESCRIPTIVE for A0 (dependent mode): the result never enters a verdict.  For B
    (independent mode) a flag blocks PASS (see ``primary_verdict``)."""
    x = np.asarray(stats, dtype=float)
    n = int(x.size)
    if n < 2:
        raise ValueError("need at least two trials")
    if mode == "independent":
        n_used = n
    elif mode == "dependent":
        if n_eff is None or n_eff < 2:
            raise ValueError("dependent mode needs n_eff >= 2")
        n_used = int(n_eff)
    else:
        raise ValueError(f"unknown mode {mode!r}")
    mean = float(x.mean())
    sd = float(x.std(ddof=1))
    se = sd / math.sqrt(n_used)
    t = mean / se if se > 0 else (0.0 if mean == 0 else math.inf)
    return {
        "mean": mean,
        "sd": sd,
        "se": se,
        "n_used": n_used,
        "t": t,
        "flag": bool(abs(t) > flag_se),
    }


def understatement_ratio(means: np.ndarray, ses: np.ndarray) -> float:
    """U = sd over trials of the session-mean statistic / sqrt(mean of squared CR1 SE).

    ``means`` are the per-trial S2 session-mean statistics (centred on their own trial
    average, so a constant bias does not enter), ``ses`` the per-trial CR1 standard
    errors of the same statistic (one value per trial, one hypothesis).  U > 1: the
    reported SE understates the variability.

    Exceptional values (protocol 3.4, no silent omission): if any trial has a missing or
    non-finite ``m`` or ``se``, fewer than two trials, or a zero / non-finite denominator,
    U is NaN ("invalid"); trials are never dropped.  A shape mismatch raises."""
    m = np.asarray(means, dtype=float)
    s = np.asarray(ses, dtype=float)
    if m.shape != s.shape or m.ndim != 1:
        raise ValueError("means and ses must be equal-length 1-d arrays")
    if m.size < 2 or not (np.all(np.isfinite(m)) and np.all(np.isfinite(s))):
        return math.nan
    denom = math.sqrt(float(np.mean(s * s)))
    if not math.isfinite(denom) or denom <= 0.0:
        return math.nan
    return float(m.std(ddof=1) / denom)


def lost_difficulty(
    u_a0: Mapping[str, float], u_b: Mapping[str, float], expected_ids: Sequence[str]
) -> dict[str, Any]:
    """Interpretation gate (protocol 3.4), S2 only.  ``u_*`` map hypothesis id -> U;
    ``expected_ids`` is the frozen list of 16 (``variants.K16`` of the config).  A
    missing id or a non-finite U makes the gate UNDETERMINED (reported, never omitted
    from the median).  Otherwise TRIGGERED iff median U(A0) <= 1.0 and median U(B) > 1.0
    (a declared diagnostic rule, not a validated boundary)."""
    ids = list(expected_ids)
    if len(ids) != N_HYPOTHESES or len(set(ids)) != N_HYPOTHESES:
        raise ValueError(f"expected exactly {N_HYPOTHESES} distinct hypothesis ids")
    if (set(u_a0) | set(u_b)) - set(ids):
        raise ValueError("U contains ids outside the frozen list")
    va = [float(u_a0.get(i, math.nan)) for i in ids]
    vb = [float(u_b.get(i, math.nan)) for i in ids]
    bad = [
        i
        for i, a, b in zip(ids, va, vb, strict=True)
        if not (math.isfinite(a) and math.isfinite(b))
    ]
    per = {"u_a0": dict(zip(ids, va, strict=True)), "u_b": dict(zip(ids, vb, strict=True))}
    if bad:
        return {
            "status": "UNDETERMINED",
            "lost": None,
            "invalid_ids": bad,
            "median_u_a0": None,
            "median_u_b": None,
            "reason": (
                f"U missing, non-finite or not computable for {len(bad)} of {N_HYPOTHESES} "
                f"hypotheses ({', '.join(bad)}); treated as A0 non-informative"
            ),
            **per,
        }
    ma, mb = float(np.median(va)), float(np.median(vb))
    lost = bool(ma <= 1.0 and mb > 1.0)
    return {
        "status": "TRIGGERED" if lost else "NOT_TRIGGERED",
        "lost": lost,
        "invalid_ids": [],
        "median_u_a0": ma,
        "median_u_b": mb,
        "reason": (
            f"median U(A0) = {ma:.4f}, median U(B) = {mb:.4f}; triggered iff "
            "U(A0) <= 1.0 and U(B) > 1.0"
        ),
        **per,
    }


def _stats_array(stats_by_id: Mapping[str, Any], hid: str) -> np.ndarray | None:
    """The per-trial statistics of one hypothesis, or ``None`` if absent, shorter than two
    trials, or containing any missing / non-finite value (nothing is dropped)."""
    if hid not in stats_by_id:
        return None
    try:
        x = np.asarray([math.nan if v is None else v for v in stats_by_id[hid]], dtype=float)
    except (TypeError, ValueError):
        return None
    if x.ndim != 1 or x.size < 2 or not np.all(np.isfinite(x)):
        return None
    return x


def b_residual_blocker(
    stats_by_id: Mapping[str, Any], expected_ids: Sequence[str], flag_se: float = FLAG_SE
) -> dict[str, Any]:
    """Generator B residual-mean blocker at the verdict's aggregation level (protocol 3.3).

    One residual-mean check per hypothesis of the frozen list (``independent`` mode:
    SE = sd / sqrt(n trials), flag at |t| > 4); the blocker fires if ANY of the
    ``expected_ids`` is flagged: ``any_flag``.  A hypothesis with a missing, short or
    non-finite series cannot be checked: it is listed in ``invalid_ids`` and the blocker
    also fires (``blocks_pass``): an unchecked hypothesis is never read as "no flag".

    What it detects: a mean of the S2 session-mean statistic across the independent B
    trials that is more than 4 standard errors from zero, i.e. a per-trial-mean bias of
    roughly 0.09 sd or more is detected half of the time (``measure_b_blocker``).  What it
    does NOT show: a non-flag is not proof that B is unbiased, only that no bias of that
    size was detected; it says nothing on dependence, tails or inference validity."""
    ids = list(expected_ids)
    if len(ids) != N_HYPOTHESES or len(set(ids)) != N_HYPOTHESES:
        raise ValueError(f"expected exactly {N_HYPOTHESES} distinct hypothesis ids")
    per: dict[str, Any] = {}
    invalid: list[str] = []
    flagged: list[str] = []
    for hid in ids:
        x = _stats_array(stats_by_id, hid)
        if x is None:
            invalid.append(hid)
            per[hid] = None
            continue
        r = residual_mean_flag(x, "independent", flag_se=flag_se)
        per[hid] = r
        if r["flag"]:
            flagged.append(hid)
    return {
        "any_flag": bool(flagged),
        "flagged_ids": flagged,
        "invalid_ids": invalid,
        "blocks_pass": bool(flagged or invalid),
        "flag_se": flag_se,
        "n_hypotheses": len(ids),
        "per_hypothesis": per,
    }


def a0_residual_descriptive(
    stats_by_id: Mapping[str, Any], expected_ids: Sequence[str], n_eff: int = 7
) -> dict[str, Any]:
    """A0 residual-mean values: DESCRIPTIVE only (heuristic SE, uncalibrated false-flag
    probability, protocol 3.3).  Never an argument of ``primary_verdict``."""
    per: dict[str, Any] = {}
    flagged: list[str] = []
    invalid: list[str] = []
    for hid in expected_ids:
        x = _stats_array(stats_by_id, hid)
        if x is None:
            invalid.append(hid)
            per[hid] = None
            continue
        r = residual_mean_flag(x, "dependent", n_eff=n_eff)
        per[hid] = r
        if r["flag"]:
            flagged.append(hid)
    return {
        "role": "descriptive only: no verdict, no NULL BIASED, no invalidation",
        "any_flag": bool(flagged),
        "flagged_ids": flagged,
        "invalid_ids": invalid,
        "per_hypothesis": per,
    }


def primary_verdict(
    b_counts: Mapping[tuple[str, int], int],
    a0_counts: Mapping[tuple[str, int], int],
    centring_ok: bool,
    b_residual_flag: bool,
    gate: Mapping[str, Any],
    *,
    b_report: Mapping[str, Any] | None = None,
    a0_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mechanical verdict (protocol 6).  Cells are (statistic in S1/S2, K in 3/6/16).

    * centring invariant not exactly ``True`` -> INVALID (A0 construction failure);
    * any cell over its threshold (B > 60 of 2,000, A0 > 55 of 1,854), missing, or not an
      integer count -> FAIL, whatever the gate says;
    * B blocker not exactly ``False`` (flag, uncheckable, or not supplied) -> FAIL;
      A0's flag has no role and is not an argument;
    * otherwise PASS.  Only a gate whose status is exactly ``NOT_TRIGGERED`` gives the
      scoped label (verdict ``PASS``); every other gate content (TRIGGERED, UNDETERMINED,
      missing, malformed) gives "PASS (B-supported only; A0 non-informative)" with the
      verdict ``PASS_B_ONLY``, which is never equal to a plain ``PASS``.

    Every result carries ``claim_scope`` (no real-market FWER claim) and an ``a0`` block
    (gate status and reason, A0 counts and the descriptive A0 report) so the A0 result
    and the reason for a downgrade stay visible.  ``b_report`` / ``a0_report`` are passed
    through for the record only."""
    cells = [(s, k) for s in ("S1", "S2") for k in (3, 6, 16)]
    gate_status = gate.get("status") if isinstance(gate, Mapping) else None
    a0_block = {
        "gate_status": gate_status,
        "gate_reason": gate.get("reason") if isinstance(gate, Mapping) else None,
        "gate_invalid_ids": list(gate.get("invalid_ids", [])) if isinstance(gate, Mapping) else [],
        "median_u_a0": gate.get("median_u_a0") if isinstance(gate, Mapping) else None,
        "median_u_b": gate.get("median_u_b") if isinstance(gate, Mapping) else None,
        "a0_counts": {f"{s}/K{k}": a0_counts.get((s, k)) for s, k in cells},
        "a0_residual_mean": dict(a0_report) if a0_report is not None else None,
    }
    base = {"claim_scope": CLAIM_SCOPE, "a0": a0_block, "b_residual_mean": b_report}
    if centring_ok is not True:
        return {
            **base,
            "verdict": "INVALID",
            "label": "INVALID (A0 construction failure)",
            "reason": "centring",
        }

    def _ok(counts: Mapping[tuple[str, int], int], c: tuple[str, int], limit: int) -> bool:
        v = counts.get(c)
        return isinstance(v, (int, np.integer)) and not isinstance(v, bool) and 0 <= v <= limit

    fails = [("B", c) for c in cells if not _ok(b_counts, c, B_MAX_COUNT)]
    fails += [("A0", c) for c in cells if not _ok(a0_counts, c, A0_MAX_COUNT)]
    if fails:
        return {**base, "verdict": "FAIL", "label": "FAIL", "reason": "rate", "cells": fails}
    if b_residual_flag is not False:
        return {
            **base,
            "verdict": "FAIL",
            "label": "FAIL (generator B residual-mean blocker)",
            "reason": "b_flag",
        }
    if gate_status != "NOT_TRIGGERED":
        return {**base, "verdict": VERDICT_PASS_B_ONLY, "label": PASS_LABEL_GATE, "reason": "gate"}
    return {**base, "verdict": "PASS", "label": PASS_LABEL_PLAIN, "reason": "ok"}


# ------------------------------------------------------------ measurement helpers --


def derangement_index(g: int = 7) -> np.ndarray:
    """All derangements of g items as an (n, g) integer array (g = 7: 1,854 rows)."""
    rows = [p for p in itertools.permutations(range(g)) if all(p[i] != i for i in range(g))]
    return np.asarray(rows, dtype=np.intp)


def _derangement_stats(m: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Per-derangement session-mean statistic for payoff matrices m (r, g, g):
    row i = decision rows of session i, column j = label series of session j."""
    g = idx.shape[1]
    rows = np.arange(g)
    return m[:, rows[None, :], idx].mean(axis=-1)  # (r, n_derangements)


def measure_b_style(
    n_reps: int, bias_sd: float, n_trials: int = 2000, seed: int = 1, flag_se: float = FLAG_SE
) -> float:
    """Flag rate over ``n_reps`` independent B-style datasets: ``n_trials`` independent
    N(bias_sd, 1) trial statistics (bias in units of the trial sd)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n_reps, n_trials)) + bias_sd
    mean = x.mean(axis=1)
    se = x.std(axis=1, ddof=1) / math.sqrt(n_trials)
    return float(np.mean(np.abs(mean / se) > flag_se))


def measure_a0_style(
    n_reps: int,
    model: str,
    bias_sd: float = 0.0,
    seed: int = 1,
    g: int = 7,
    flag_se: float = FLAG_SE,
    chunk: int = 250,
) -> float:
    """Flag rate of the dependent-mode diagnostic over ``n_reps`` independent datasets,
    each enumerated over all derangements (dependent trials).

    Payoff matrix M[i, j] (decision session i, label session j):
    * ``iid``: M = e, e ~ N(0, 1) i.i.d. entries (dependence arises only from reuse of
      the g*g entries by the derangements);
    * ``effects``: M = r_i + c_j + e, with r, c ~ N(0, 1) session effects.  Every
      derangement sums every r_i and c_j once, so the trial statistic varies only
      through e while the dataset mean carries r and c: the SE is far too small.
    ``bias_sd`` adds a constant to every entry in units of the entry sd of e.
    """
    rng = np.random.default_rng(seed)
    idx = derangement_index(g)
    flags = 0
    done = 0
    while done < n_reps:
        r = min(chunk, n_reps - done)
        e = rng.standard_normal((r, g, g))
        if model == "iid":
            m = e
        elif model == "effects":
            m = e + rng.standard_normal((r, g, 1)) + rng.standard_normal((r, 1, g))
        else:
            raise ValueError(f"unknown model {model!r}")
        st = _derangement_stats(m + bias_sd, idx)
        mean = st.mean(axis=1)
        se = st.std(axis=1, ddof=1) / math.sqrt(g)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(se > 0, mean / se, np.inf)
        flags += int(np.sum(np.abs(t) > flag_se))
        done += r
    return flags / n_reps


def _synthetic_b_stats(
    rng: np.random.Generator, n_trials: int, n_hyp: int, model: str, rho: float
) -> np.ndarray:
    """(n_hyp, n_trials) mean-zero unit-variance per-trial statistics.  Trials are
    independent (B's trials are); hypotheses share a common factor with correlation ``rho``
    (the 16 hypotheses of a B trial are correlated through the shared labels)."""
    if model in ("iid", "factor"):
        e = rng.standard_normal((n_hyp, n_trials))
    elif model == "t3":  # heavy tails, finite variance, scaled to unit variance
        e = rng.standard_t(3, (n_hyp, n_trials)) / math.sqrt(3.0)
    elif model == "skew":  # centred exponential, skewness 2
        e = rng.exponential(1.0, (n_hyp, n_trials)) - 1.0
    else:
        raise ValueError(f"unknown model {model!r}")
    if rho:
        common = rng.standard_normal((1, n_trials))
        e = math.sqrt(1.0 - rho) * e + math.sqrt(rho) * common
    return e


def measure_b_blocker(
    n_reps: int,
    *,
    model: str = "iid",
    rho: float = 0.0,
    bias_sd: float = 0.0,
    biased_hypotheses: int = 1,
    n_trials: int = 2000,
    seed: int = 1,
) -> dict[str, Any]:
    """Rate at which the B blocker makes the VERDICT a ``b_flag`` FAIL, over ``n_reps``
    synthetic B datasets of 16 hypotheses x ``n_trials`` independent trials.

    This runs the runner's own path: ``b_residual_blocker`` (same statistic, SE,
    threshold, aggregation over the 16 hypotheses) and then ``primary_verdict`` with every
    rate cell passing and a NOT_TRIGGERED gate, so a FAIL with reason ``b_flag`` is
    exactly "at least one false flag across the 16 hypotheses".  ``bias_sd`` adds a
    constant (in units of the trial sd) to the first ``biased_hypotheses`` hypotheses
    (injected bias: detection rate)."""
    rng = np.random.default_rng(seed)
    ids = [f"h{i:02d}" for i in range(N_HYPOTHESES)]
    cells = [(s, k) for s in ("S1", "S2") for k in (3, 6, 16)]
    ok_b = dict.fromkeys(cells, 0)
    ok_a0 = dict.fromkeys(cells, 0)
    fails = 0
    for _ in range(n_reps):
        x = _synthetic_b_stats(rng, n_trials, N_HYPOTHESES, model, rho)
        if bias_sd:
            x[:biased_hypotheses] += bias_sd
        blk = b_residual_blocker(dict(zip(ids, x, strict=True)), ids)
        v = primary_verdict(
            ok_b, ok_a0, True, blk["blocks_pass"], {"status": "NOT_TRIGGERED"}, b_report=blk
        )
        fails += int(v["verdict"] == "FAIL" and v["reason"] == "b_flag")
    return {
        "model": model,
        "rho": rho,
        "bias_sd": bias_sd,
        "biased_hypotheses": biased_hypotheses if bias_sd else 0,
        "n_reps": n_reps,
        "n_trials": n_trials,
        "n_hypotheses": N_HYPOTHESES,
        "verdict_b_flag_count": fails,
        "verdict_b_flag_rate": fails / n_reps,
    }


def b_blocker_union_bound(n_trials: int = 2000, flag_se: float = FLAG_SE) -> dict[str, float]:
    """Analytic check for normal trial statistics: mean / (sd / sqrt(n)) is exactly
    Student t with n - 1 df, so one hypothesis false-flags with p = P(|t| > 4), and the
    probability of at least one false flag among 16 hypotheses, for ANY dependence between
    them, is at most the union bound 16 p (equal to it up to O(p^2) if independent)."""
    from scipy import stats

    p = float(2.0 * stats.t.sf(flag_se, n_trials - 1))
    return {
        "per_hypothesis": p,
        "union_bound_16": N_HYPOTHESES * p,
        "independent_16": 1.0 - (1.0 - p) ** N_HYPOTHESES,
    }

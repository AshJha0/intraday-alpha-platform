"""REL-06 known-null protocol v3: residual-mean diagnostic and SE understatement ratio.

Pure functions on arrays of per-trial statistics (no data access, no I/O).  The v3
runner does not exist yet; these functions are the pieces the protocol needs to have
tested before signing (``research/v118/NULL_PROTOCOL_v3.md`` sections 3.3, 3.4).

``residual_mean_flag`` has two modes:

* ``independent`` (generator B): trials are independent realisations, SE = sd / sqrt(n).
* ``dependent`` (A0): the trials are the 1,854 derangements of one dataset; they are
  dependent, and ``n_eff`` (the number of sessions, 7) is a HEURISTIC effective sample
  size.  Its false-flag probability is not calibrated; see ``measure_*`` below.
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

FLAG_SE = 4.0


def residual_mean_flag(
    stats: np.ndarray,
    mode: str,
    n_eff: int | None = None,
    flag_se: float = FLAG_SE,
) -> dict[str, Any]:
    """Residual-mean diagnostic for one hypothesis' per-trial S2 statistic."""
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
    errors of the same statistic.  U > 1: the reported SE understates the variability.
    """
    m = np.asarray(means, dtype=float)
    s = np.asarray(ses, dtype=float)
    if m.shape != s.shape or m.size < 2:
        raise ValueError("means and ses must be equal-length arrays of >= 2 trials")
    denom = math.sqrt(float(np.mean(s * s)))
    return float(m.std(ddof=1) / denom)


def lost_difficulty(u_a0: np.ndarray, u_b: np.ndarray) -> dict[str, Any]:
    """Interpretation gate (protocol 3.4): A0 has lost the difficulty iff the median
    over hypotheses of U under A0 is <= 1.0 while the median under B is > 1.0."""
    ma = float(np.median(np.asarray(u_a0, dtype=float)))
    mb = float(np.median(np.asarray(u_b, dtype=float)))
    return {"median_u_a0": ma, "median_u_b": mb, "lost": bool(ma <= 1.0 and mb > 1.0)}


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

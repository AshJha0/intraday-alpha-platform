"""The leakage canary suite (v2.0 U2; plan section 5 "leakage canary suite").

Three canaries, each with a positive control so a vacuous detector cannot pass:

``future_shift_canary``   a feature shifted by +1 period (the value of period
                          t-1 used at t) must score at chance against the label,
                          while the unshifted feature scores above chance.  It
                          calibrates the scoring and alignment path: a harness
                          that still finds signal in a stale feature is leaking
                          through its alignment.
``shuffled_label_canary`` a label permuted within the sample must score at
                          chance, while the true label scores above chance.
``planted_leak_canary``   a deliberately leaked feature must be caught by the
                          contract check (:func:`iap.data.contracts.check_causality`)
                          while the causal control passes it.

"At chance" is ``|IC| <= k / sqrt(n)`` with ``k = 3`` (the null IC of ``n``
independent pairs has standard deviation about ``1 / sqrt(n)``); "above
chance" is the same band exceeded.  IC is the Pearson correlation over finite
pairs.  Permutations use SplitMix64 only.  :func:`run_canaries` runs all three
and raises :class:`CanaryFailure` if any fails; it is what CI runs on the
synthetic fixture and what every development run must run on its own panel.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from iap.core.rng import SplitMix64
from iap.data.contracts import LeakageDetected, check_causality, contract_of

__all__ = [
    "CHANCE_K",
    "CanaryFailure",
    "chance_band",
    "future_shift_canary",
    "ic",
    "planted_leak_canary",
    "run_canaries",
    "shuffled_label_canary",
]

#: Width of the chance band in null standard deviations.
CHANCE_K = 3.0


class CanaryFailure(AssertionError):
    """A canary failed: the pipeline leaks, or its detector is vacuous."""


def ic(x: pd.Series | np.ndarray, y: pd.Series | np.ndarray) -> tuple[float, int]:
    """Pearson correlation over finite pairs and the number of pairs; (nan, n) if degenerate."""
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    if xa.shape != ya.shape:
        raise ValueError("ic needs equal-length inputs")
    ok = np.isfinite(xa) & np.isfinite(ya)
    n = int(ok.sum())
    if n < 3:
        return float("nan"), n
    xs, ys = xa[ok], ya[ok]
    sx, sy = xs.std(), ys.std()
    if sx == 0.0 or sy == 0.0:
        return float("nan"), n
    return float(np.mean((xs - xs.mean()) * (ys - ys.mean())) / (sx * sy)), n


def chance_band(n: int, k: float = CHANCE_K) -> float:
    """``k / sqrt(n)``: the half-width of the at-chance band for ``n`` pairs."""
    if n < 1:
        return float("inf")
    return k / math.sqrt(n)


def _at_chance(value: float, band: float) -> bool:
    return math.isfinite(value) and abs(value) <= band


def _above_chance(value: float, band: float) -> bool:
    return math.isfinite(value) and abs(value) > band


def future_shift_canary(
    feature: pd.Series, label: pd.Series, *, group: pd.Series | None = None, periods: int = 1
) -> dict[str, Any]:
    """The feature shifted by ``+periods`` (per group) must score at chance; unshifted above chance."""
    if periods < 1:
        raise ValueError("periods must be >= 1")
    shifted = (
        feature.groupby(group, sort=False).shift(periods)
        if group is not None
        else feature.shift(periods)
    )
    ic0, n0 = ic(feature, label)
    ic1, n1 = ic(shifted, label)
    band = chance_band(min(n0, n1))
    passed = _above_chance(ic0, band) and _at_chance(ic1, band)
    return {
        "canary": "LK-FUTURE-SHIFT",
        "ic_unshifted": ic0,
        "ic_shifted": ic1,
        "pairs": min(n0, n1),
        "band": band,
        "positive_control": _above_chance(ic0, band),
        "passed": passed,
    }


def _permutation(n: int, seed: int) -> np.ndarray:
    rng = SplitMix64(seed)
    perm = np.arange(n)
    for i in range(n - 1, 0, -1):  # Fisher-Yates from SplitMix64 only
        j = rng.below(i + 1)
        perm[i], perm[j] = perm[j], perm[i]
    return perm


def shuffled_label_canary(
    feature: pd.Series, label: pd.Series, *, seed: int = 20261024
) -> dict[str, Any]:
    """A permuted label must score at chance; the true label above chance."""
    ic0, n0 = ic(feature, label)
    perm = _permutation(len(label), seed)
    shuffled = pd.Series(np.asarray(label, dtype=float)[perm], index=label.index)
    ic1, n1 = ic(feature, shuffled)
    band = chance_band(min(n0, n1))
    passed = _above_chance(ic0, band) and _at_chance(ic1, band)
    return {
        "canary": "LK-SHUFFLED-LABEL",
        "ic_true": ic0,
        "ic_shuffled": ic1,
        "pairs": min(n0, n1),
        "band": band,
        "seed": seed,
        "positive_control": _above_chance(ic0, band),
        "passed": passed,
    }


def planted_leak_canary(
    leaked_fn: Callable[[pd.DataFrame], pd.Series],
    causal_fn: Callable[[pd.DataFrame], pd.Series],
    frame: pd.DataFrame,
    label: pd.Series | None = None,
) -> dict[str, Any]:
    """The contract check must catch ``leaked_fn`` and pass ``causal_fn``.

    When ``label`` is given the leaked feature's IC is reported too, so the
    report shows how inflated an undetected leak would have looked.
    """
    leaked_contract = contract_of(leaked_fn)
    caught = False
    detail = ""
    try:
        check_causality(leaked_fn, frame)
    except LeakageDetected as exc:
        caught = True
        detail = str(exc)
    control = check_causality(
        causal_fn, frame
    )  # raises LeakageDetected if the control is itself leaky
    out: dict[str, Any] = {
        "canary": "LK-PLANTED-LEAK",
        "leaked_feature": leaked_contract.feature_id,
        "caught": caught,
        "detail": detail,
        "causal_control": control["feature_id"],
        "control_probes": control["probes"],
        "passed": caught,
    }
    if label is not None:
        out["ic_leaked_if_undetected"], _ = ic(leaked_fn(frame), label)
    return out


def run_canaries(
    frame: pd.DataFrame,
    feature_fn: Callable[[pd.DataFrame], pd.Series],
    label_col: str,
    leaked_fn: Callable[[pd.DataFrame], pd.Series],
    *,
    group_col: str | None = "name",
    seed: int = 20261024,
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    """All three canaries on one panel; raises :class:`CanaryFailure` unless every one passes."""
    contract_of(feature_fn)  # a feature without a contract is refused here too
    if label_col in {
        c for f in (feature_fn, leaked_fn) for c in getattr(contract_of(f), "inputs", ())
    }:
        raise CanaryFailure(f"label column {label_col!r} is declared as a feature input")
    label = frame[label_col]
    feat = feature_fn(frame)
    group = frame[group_col] if group_col else None
    report = {
        "future_shift": future_shift_canary(feat, label, group=group),
        "shuffled_label": shuffled_label_canary(feat, label, seed=seed),
        "planted_leak": planted_leak_canary(leaked_fn, feature_fn, frame, label),
    }
    report["passed"] = all(r["passed"] for r in report.values() if isinstance(r, dict))
    if raise_on_failure and not report["passed"]:
        failed = [k for k, r in report.items() if isinstance(r, dict) and not r["passed"]]
        raise CanaryFailure(f"leakage canaries failed: {failed}; report: {report}")
    return report

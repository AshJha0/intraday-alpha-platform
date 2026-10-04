"""Distribution drift monitors: PSI, two-sample KS, rolling realized IC.

The answer to "markets shift" begins with *measuring* the shift.  This
module pins three monitors (mirrored bit-for-bit by the Java live-metric
agent — see /API_ADAPTIVE.md, the normative contract):

1. **PSI** (population stability index) of any scalar distribution
   (feature values, alpha signal values) against a serialized baseline.
   Pinned 10-quantile-bucket formula:

   - Baseline capture from finite values ``x`` (n >= MIN_BASELINE_N):
     interior edges ``e_k = quantile(x, k/10)`` for k = 1..9 using linear
     interpolation (numpy default); bucket of a value v =
     ``searchsorted(edges, v, side='left')`` — i.e. bucket i covers
     ``(e_i, e_{i+1}]`` with bucket 0 = ``(-inf, e_1]`` and bucket 9 =
     ``(e_9, +inf)``; values exactly on an edge fall in the LOWER bucket.
     ``expected_frac[i]`` = baseline count in bucket i / n (NOT assumed
     0.1 — ties can concentrate mass).
   - PSI of current finite values ``y``:
     ``a_i = max(count_y(i)/n_y, PSI_EPS)``,
     ``e_i = max(expected_frac[i], PSI_EPS)``,
     ``PSI = sum_i (a_i - e_i) * ln(a_i / e_i)``  with PSI_EPS = 1e-6.
     Clamped fractions are NOT renormalized (pinned).

2. **Two-sample KS**: ``D = sup_v |F_a(v) - F_b(v)|`` over the pooled
   sample values, F = right-continuous empirical CDF (proportion <= v).
   Asymptotic p-value via the Kolmogorov distribution
   ``Q(lam) = 2 * sum_{j=1..100} (-1)^{j-1} exp(-2 j^2 lam^2)`` with
   ``lam = (en + 0.12 + 0.11/en) * D``, ``en = sqrt(n_a n_b/(n_a+n_b))``
   (Numerical Recipes form, 100 terms pinned, clamped to [0, 1]).

3. **Rolling realized-vs-research IC** — the LEGACY z
   (:func:`rolling_ic_z`, ``adaptive.ic_z_method = "legacy"``; the default
   up to v1.4.0): the research window pins a baseline (mean, std over
   5-minute-bucket ICs, metrics.bucket_ics semantics); live evaluation
   computes bucket ICs over a rolling window of MATURED rows and reports
   the unweighted mean bucket IC and
   ``z = (mean(live) - ic_mean) / (ic_std / sqrt(n_live_buckets))``.
   Fewer than ``min_buckets`` live buckets, or ``ic_std <= 1e-12``,
   yields z = None (monitors never fabricate confidence).

Baseline serialization (research/baselines/<name>.json) — normative
schema, ``x-version`` 2 (each file names the ``feature_version`` it was
captured against; a loader rejects any other version):

```
{
  "x-version": 2,
  "feature_version": str,           # feature-registry hash
  "kind": "signal" | "feature",     # distribution baselines
  "name": str,                      # file stem, unique
  "alpha_id": str,                  # "" when not alpha-specific
  "source": str,                    # provenance, human-readable
  "n": int,                         # baseline sample size (finite values)
  "edges": [9 floats],              # interior decile edges, ascending
  "expected_frac": [10 floats],     # baseline bucket fractions, sum 1
  "mean": float, "std": float,      # baseline moments (std ddof=0)
  "min": float, "max": float,
  "psi_eps": 1e-6, "n_buckets": 10  # pinned constants, echoed
}
```

IC baselines use ``"kind": "ic"`` and replace edges/expected_frac with
``{"ic_mean", "ic_std", "n_buckets_baseline", "bucket_ns", "horizon"}``.

4. **Two-sample HAC z** — the DEFAULT since v1.5.0
   (:func:`rolling_ic_z_hac`, ``adaptive.ic_z_method = "hac"``).  The
   legacy z of monitor 3 treats the baseline mean as a known constant and
   the live bucket ICs as independent and equally informative.  None of
   the three holds: the baseline mean is itself an estimate from
   ``n_buckets_baseline`` buckets; bucket ICs of overlapping-label signals
   are autocorrelated; and fixed time buckets carry very different pair
   counts.  The corrected statistic is the two-sample form

       z = (mean_w(live) - ic_mean) / sqrt(var_live + var_base)

   where ``mean_w`` / ``var_live`` are the pair-count-weighted mean of the
   live bucket ICs and its Bartlett (Newey-West) variance
   (:func:`iap.validation.metrics.hac_mean_variance`, lag rule
   :func:`iap.validation.metrics.nw_lags`), and ``var_base`` is the variance
   of the baseline mean: HAC from the baseline's own bucket series when the
   caller has it, otherwise ``ic_std^2 / n_buckets_baseline`` (all the
   serialized baseline can support).  It is never larger in magnitude than
   the legacy z on independent equal buckets, and it does not fire on the
   baseline's own sampling error.

   The rolling IC it reports — the number the lifecycle rule reads — is the
   PAIR-COUNT-WEIGHTED mean of the live bucket ICs (``mean_w``); the legacy
   monitor reports the unweighted mean.  The Java live gauge
   (``com.iap.adaptive.RollingIc``) computes the weighted mean since v1.5.0
   and the golden ``rolling_ic`` vector pins it.  The z itself is computed
   by the Python reference only: no port evaluates a refit trigger.

Everything here is deterministic and wall-clock-free (conventions §3).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from iap.validation.metrics import (
    HORIZONS_NS,
    bucket_ics,
    bucket_ics_with_counts,
    hac_mean_variance,
    nw_lags,
)

PSI_EPS = 1e-6
PSI_BUCKETS = 10
MIN_BASELINE_N = 100
KS_PVALUE_TERMS = 100
_STD_EPS = 1e-12


def _finite(values) -> np.ndarray:
    v = np.asarray(values, dtype=float).ravel()
    return v[np.isfinite(v)]


# ---------------------------------------------------------------------------
# distribution baselines + PSI
# ---------------------------------------------------------------------------


#: Baseline schema version. 2 (round-3) adds the ``feature_version``
#: provenance field; see MIGRATIONS.md.
BASELINE_VERSION = 2


def _registry_hash() -> str:
    """Current feature-registry hash (empty if the registry is unavailable)."""
    try:
        from iap.features.registry import registry_hash

        return registry_hash()
    except Exception:  # pragma: no cover - registry always present in-repo
        return ""


def _check_feature_version(blob: dict, expected, what: str) -> str:
    """Pinned loader check (API_ADAPTIVE section 4).

    A baseline captured against a different feature registry describes a
    distribution of a feature whose SEMANTICS may have changed under the same
    name, so PSI/IC against it is meaningless.  ``expected`` defaults to the
    running engine's registry hash; pass ``None`` to skip (tooling that
    inspects a historic file).  A mismatch is an error, never a warning.
    """
    got = str(blob.get("feature_version", ""))
    if expected == "":
        expected = _registry_hash()
    if expected is not None and got != expected:
        raise ValueError(
            f"{what}: feature_version {got!r} does not match the engine's "
            f"registry hash {expected!r} — the baseline was captured against "
            "a different feature registry"
        )
    return got


@dataclass(frozen=True)
class DriftBaseline:
    """Pinned PSI baseline for one scalar distribution (schema above)."""

    kind: str  # "signal" | "feature"
    name: str
    alpha_id: str
    source: str
    n: int
    edges: tuple[float, ...]  # 9 interior decile edges
    expected_frac: tuple[float, ...]  # 10 baseline bucket fractions
    mean: float
    std: float
    min: float
    max: float
    #: feature-registry hash this baseline was captured against (provenance)
    feature_version: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("signal", "feature"):
            raise ValueError(f"baseline kind must be signal|feature, got {self.kind!r}")
        if len(self.edges) != PSI_BUCKETS - 1:
            raise ValueError(f"baseline needs {PSI_BUCKETS - 1} edges, got {len(self.edges)}")
        if len(self.expected_frac) != PSI_BUCKETS:
            raise ValueError(
                f"baseline needs {PSI_BUCKETS} expected fractions, got {len(self.expected_frac)}"
            )
        if any(b < a for a, b in zip(self.edges, self.edges[1:], strict=False)):
            raise ValueError("baseline edges must be non-decreasing")

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "x-version": BASELINE_VERSION,
            "kind": self.kind,
            "name": self.name,
            "alpha_id": self.alpha_id,
            "source": self.source,
            "feature_version": self.feature_version,
            "n": self.n,
            "edges": list(self.edges),
            "expected_frac": list(self.expected_frac),
            "mean": self.mean,
            "std": self.std,
            "min": self.min,
            "max": self.max,
            "psi_eps": PSI_EPS,
            "n_buckets": PSI_BUCKETS,
        }

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")

    @staticmethod
    def from_dict(blob: dict, expected_feature_version="") -> DriftBaseline:
        if int(blob.get("x-version", 0)) != BASELINE_VERSION:
            raise ValueError(
                f"unsupported baseline x-version {blob.get('x-version')!r} "
                f"(pinned {BASELINE_VERSION}); see MIGRATIONS.md"
            )
        fv = _check_feature_version(
            blob, expected_feature_version, f"baseline {blob.get('name')!r}"
        )
        if int(blob.get("n_buckets", 0)) != PSI_BUCKETS:
            raise ValueError("baseline n_buckets mismatch (pinned 10)")
        if float(blob.get("psi_eps", -1.0)) != PSI_EPS:
            raise ValueError("baseline psi_eps mismatch (pinned 1e-6)")
        return DriftBaseline(
            kind=str(blob["kind"]),
            name=str(blob["name"]),
            alpha_id=str(blob.get("alpha_id", "")),
            source=str(blob.get("source", "")),
            n=int(blob["n"]),
            edges=tuple(float(e) for e in blob["edges"]),
            expected_frac=tuple(float(f) for f in blob["expected_frac"]),
            mean=float(blob["mean"]),
            std=float(blob["std"]),
            min=float(blob["min"]),
            max=float(blob["max"]),
            feature_version=fv,
        )

    @staticmethod
    def load(path, expected_feature_version="") -> DriftBaseline:
        return DriftBaseline.from_dict(json.loads(Path(path).read_text()), expected_feature_version)


def bucket_counts(values: np.ndarray, edges) -> np.ndarray:
    """Pinned bucket assignment: ``searchsorted(edges, v, side='left')``
    (values equal to an edge fall in the LOWER bucket)."""
    v = _finite(values)
    idx = np.searchsorted(np.asarray(edges, dtype=float), v, side="left")
    return np.bincount(idx, minlength=PSI_BUCKETS).astype(np.int64)


def capture_baseline(
    values,
    kind: str,
    name: str,
    alpha_id: str = "",
    source: str = "",
    feature_version: str = "",
) -> DriftBaseline:
    """Capture a PSI baseline from a research-window sample (pinned recipe:
    module docstring).  Raises on < MIN_BASELINE_N finite values."""
    v = _finite(values)
    if v.size < MIN_BASELINE_N:
        raise ValueError(f"baseline {name!r}: need >= {MIN_BASELINE_N} finite values, got {v.size}")
    q = np.arange(1, PSI_BUCKETS) / PSI_BUCKETS
    edges = np.quantile(v, q)  # linear interpolation (numpy default), pinned
    counts = bucket_counts(v, edges)
    return DriftBaseline(
        kind=kind,
        name=name,
        alpha_id=alpha_id,
        source=source,
        n=int(v.size),
        edges=tuple(float(e) for e in edges),
        expected_frac=tuple(float(c) / float(v.size) for c in counts),
        mean=float(v.mean()),
        std=float(v.std()),
        min=float(v.min()),
        max=float(v.max()),
        feature_version=feature_version or _registry_hash(),
    )


def psi(baseline: DriftBaseline, values, min_samples: int = 1) -> float | None:
    """Population stability index of ``values`` against ``baseline``
    (pinned formula, module docstring).  None with < min_samples finite
    values — a monitor with no data must not report stability."""
    v = _finite(values)
    if v.size < max(min_samples, 1):
        return None
    counts = bucket_counts(v, baseline.edges)
    total = 0.0
    for i in range(PSI_BUCKETS):
        a = max(counts[i] / v.size, PSI_EPS)
        e = max(baseline.expected_frac[i], PSI_EPS)
        total += (a - e) * math.log(a / e)
    return float(total)


# ---------------------------------------------------------------------------
# two-sample Kolmogorov-Smirnov
# ---------------------------------------------------------------------------


def ks_statistic(a, b) -> float:
    """Two-sample KS statistic D = sup |F_a - F_b| (pinned: empirical CDFs
    evaluated at every pooled sample value)."""
    x = np.sort(_finite(a))
    y = np.sort(_finite(b))
    if x.size == 0 or y.size == 0:
        raise ValueError("ks_statistic: both samples need >= 1 finite value")
    pooled = np.concatenate([x, y])
    cdf_x = np.searchsorted(x, pooled, side="right") / x.size
    cdf_y = np.searchsorted(y, pooled, side="right") / y.size
    return float(np.max(np.abs(cdf_x - cdf_y)))


def ks_pvalue(d: float, n_a: int, n_b: int) -> float:
    """Asymptotic two-sample KS p-value (Numerical Recipes form, pinned:
    100 series terms, clamped to [0, 1])."""
    if not 0.0 <= d <= 1.0:
        raise ValueError("KS statistic must be in [0, 1]")
    if n_a < 1 or n_b < 1:
        raise ValueError("sample sizes must be >= 1")
    en = math.sqrt(n_a * n_b / float(n_a + n_b))
    lam = (en + 0.12 + 0.11 / en) * d
    if lam <= 0.0:
        return 1.0
    total = 0.0
    for j in range(1, KS_PVALUE_TERMS + 1):
        total += (-1.0) ** (j - 1) * math.exp(-2.0 * j * j * lam * lam)
    return float(min(1.0, max(0.0, 2.0 * total)))


def ks_test(a, b) -> tuple[float, float]:
    """(D, asymptotic p-value) for two samples."""
    x = _finite(a)
    y = _finite(b)
    d = ks_statistic(x, y)
    return d, ks_pvalue(d, int(x.size), int(y.size))


# ---------------------------------------------------------------------------
# rolling realized-vs-research IC monitor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ICBaseline:
    """Research-window IC baseline (bucket-IC mean/std, pinned semantics)."""

    name: str
    alpha_id: str
    source: str
    ic_mean: float
    ic_std: float  # population std (ddof=0) of bucket ICs
    n_buckets_baseline: int
    bucket_ns: int
    horizon: str
    #: "oos" (required) — the baseline rows were NOT used to fit the model
    baseline_kind: str = "oos"
    #: feature-registry hash this baseline was captured against (provenance)
    feature_version: str = ""

    def to_dict(self) -> dict:
        return {
            "x-version": BASELINE_VERSION,
            "kind": "ic",
            "name": self.name,
            "alpha_id": self.alpha_id,
            "source": self.source,
            "feature_version": self.feature_version,
            "ic_mean": self.ic_mean,
            "ic_std": self.ic_std,
            "n_buckets_baseline": self.n_buckets_baseline,
            "bucket_ns": self.bucket_ns,
            "horizon": self.horizon,
            "baseline_kind": self.baseline_kind,
        }

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")

    @staticmethod
    def from_dict(blob: dict, expected_feature_version="") -> ICBaseline:
        if int(blob.get("x-version", 0)) != BASELINE_VERSION or blob.get("kind") != "ic":
            raise ValueError(
                f"not a v{BASELINE_VERSION} IC baseline "
                f"(x-version {blob.get('x-version')!r}, kind "
                f"{blob.get('kind')!r}); see MIGRATIONS.md"
            )
        fv = _check_feature_version(
            blob, expected_feature_version, f"IC baseline {blob.get('name')!r}"
        )
        kind = str(blob.get("baseline_kind", "")).lower()
        if kind != "oos":
            # An IN-SAMPLE baseline (the warmup model scored on its own
            # training rows) is an optimistic prior: ic_z is biased negative
            # and drift-triggered refits fire on the IS/OOS gap, not on
            # drift. Rejected at load (pinned, API_ADAPTIVE section 4).
            raise ValueError(
                f"IC baseline {blob.get('name')!r}: baseline_kind must be "
                f"'oos' (got {blob.get('baseline_kind')!r}) — an in-sample "
                "IC baseline is rejected"
            )
        return ICBaseline(
            name=str(blob["name"]),
            alpha_id=str(blob.get("alpha_id", "")),
            source=str(blob.get("source", "")),
            ic_mean=float(blob["ic_mean"]),
            ic_std=float(blob["ic_std"]),
            n_buckets_baseline=int(blob["n_buckets_baseline"]),
            bucket_ns=int(blob["bucket_ns"]),
            horizon=str(blob["horizon"]),
            baseline_kind=kind,
            feature_version=fv,
        )

    @staticmethod
    def load(path, expected_feature_version="") -> ICBaseline:
        return ICBaseline.from_dict(json.loads(Path(path).read_text()), expected_feature_version)


def capture_ic_baseline(
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    name: str,
    alpha_id: str,
    horizon: str,
    bucket_ns: int = 300_000_000_000,
    source: str = "",
    min_buckets: int = 4,
    baseline_kind: str = "oos",
    feature_version: str = "",
) -> ICBaseline:
    """IC baseline from a research window (bucket ICs, metrics semantics).

    ``baseline_kind`` must be ``"oos"``: the rows passed in must NOT be rows
    the scoring model was fitted on (API_ADAPTIVE section 4).  The caller is
    responsible for the split; this constructor records the claim so a
    loader can reject an in-sample file.
    """
    if baseline_kind != "oos":
        raise ValueError("IC baselines must be out-of-sample (baseline_kind='oos')")
    bics = bucket_ics(ts, scores, labels, bucket_ns=bucket_ns)
    if bics.size < min_buckets:
        raise ValueError(f"ic baseline {name!r}: need >= {min_buckets} IC buckets, got {bics.size}")
    return ICBaseline(
        name=name,
        alpha_id=alpha_id,
        source=source,
        ic_mean=float(bics.mean()),
        ic_std=float(bics.std()),
        n_buckets_baseline=int(bics.size),
        bucket_ns=int(bucket_ns),
        horizon=horizon,
        baseline_kind=baseline_kind,
        feature_version=feature_version or _registry_hash(),
    )


@dataclass
class ICWindowResult:
    """One rolling-IC evaluation."""

    rolling_ic: float | None  # mean live bucket IC (None: too little data)
    z: float | None  # z vs baseline (None: unavailable/degenerate)
    n_buckets: int = 0
    bucket_ics: list[float] = field(default_factory=list)


def rolling_ic_z(
    baseline: ICBaseline,
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    min_buckets: int = 4,
) -> ICWindowResult:
    """Rolling realized IC vs the research baseline — the LEGACY z formula
    (module docstring, monitor 3).  Inputs are the MATURED rows of the live
    window — the caller is responsible for maturity/no-lookahead filtering."""
    bics = bucket_ics(ts, scores, labels, bucket_ns=baseline.bucket_ns)
    if bics.size < max(min_buckets, 1):
        return ICWindowResult(rolling_ic=None, z=None, n_buckets=int(bics.size))
    mean_live = float(bics.mean())
    if baseline.ic_std <= _STD_EPS:
        return ICWindowResult(
            rolling_ic=mean_live,
            z=None,
            n_buckets=int(bics.size),
            bucket_ics=[float(v) for v in bics],
        )
    z = (mean_live - baseline.ic_mean) / (baseline.ic_std / math.sqrt(bics.size))
    return ICWindowResult(
        rolling_ic=mean_live,
        z=float(z),
        n_buckets=int(bics.size),
        bucket_ics=[float(v) for v in bics],
    )


#: IC z methods (module docs, monitors 4 and 3): the default, then the
#: legacy rule.  Up to v1.4.0 the legacy method was spelled ``"pinned"``.
IC_Z_METHODS = ("hac", "legacy")
DEFAULT_IC_Z_METHOD = "hac"
LEGACY_IC_Z_METHOD = "legacy"


def two_sample_hac_z(
    live_mean: float,
    live_var: float,
    base_mean: float,
    base_var: float,
) -> float | None:
    """``(live_mean - base_mean) / sqrt(live_var + base_var)``; ``None`` when
    either variance is unavailable or their sum is not positive."""
    if not (
        math.isfinite(live_mean)
        and math.isfinite(base_mean)
        and math.isfinite(live_var)
        and math.isfinite(base_var)
    ):
        return None
    total = live_var + base_var
    if live_var < 0.0 or base_var < 0.0 or total <= _STD_EPS * _STD_EPS:
        return None
    return float((live_mean - base_mean) / math.sqrt(total))


def rolling_ic_z_hac(
    baseline: ICBaseline,
    ts: np.ndarray,
    scores: np.ndarray,
    labels: np.ndarray,
    min_buckets: int = 4,
    lags: int | None = None,
    baseline_bucket_ics: np.ndarray | None = None,
    baseline_bucket_counts: np.ndarray | None = None,
) -> ICWindowResult:
    """Rolling realized IC vs the baseline as a two-sample HAC z (monitor 4
    of the module docstring).

    ``lags`` defaults to :func:`nw_lags` of the baseline's horizon and
    bucket.  ``baseline_bucket_ics`` / ``baseline_bucket_counts`` are the
    research window's own bucket series when the caller still has it (the
    baseline mean and its HAC variance are then recomputed from it, pair
    weighted); without them the serialized ``ic_mean`` and
    ``ic_std^2 / n_buckets_baseline`` are used.  ``rolling_ic`` is the
    pair-count-weighted live mean — the quantity the z is about.
    """
    if lags is None:
        lags = nw_lags(HORIZONS_NS[baseline.horizon], baseline.bucket_ns)
    bics, counts = bucket_ics_with_counts(ts, scores, labels, bucket_ns=baseline.bucket_ns)
    n = int(bics.size)
    if n < max(min_buckets, 1):
        return ICWindowResult(rolling_ic=None, z=None, n_buckets=n)
    live_mean, live_var, _ = hac_mean_variance(bics, lags=lags, weights=counts)
    if baseline_bucket_ics is not None:
        base_mean, base_var, _ = hac_mean_variance(
            baseline_bucket_ics, lags=lags, weights=baseline_bucket_counts
        )
    else:
        base_mean = baseline.ic_mean
        base_var = (
            baseline.ic_std**2 / baseline.n_buckets_baseline
            if baseline.n_buckets_baseline > 0
            else float("nan")
        )
    return ICWindowResult(
        rolling_ic=float(live_mean),
        z=two_sample_hac_z(live_mean, live_var, base_mean, base_var),
        n_buckets=n,
        bucket_ics=[float(v) for v in bics],
    )
